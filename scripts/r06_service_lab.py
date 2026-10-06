"""Owned persistent-schema R06 process lab. Only the parent deletes its fresh schema."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from evorec.bootstrap import build_demo_application
from evorec.infrastructure.postgres import PostgresDemoBackend
from scripts.migrate_database import migrate
from scripts.run_r06_demo import marker, serve, validate


class R06ServiceLab:
    """Restart actual API processes without replacing the owned database or bundle."""

    def __init__(self, output, database_url, root, bundle_id, digest, backend="numpy"):
        self.output, self.root, parameters = validate(
            output, database_url, root, bundle_id, digest, backend, 0)
        self.database_url, self.bundle_id, self.digest, self.backend = database_url, bundle_id, digest, backend
        self.schema, self.run_id = "test_evorec_" + uuid4().hex, str(uuid4())
        self.isolated_url = make_conninfo(**{**parameters, "options": "-c search_path=" + self.schema})
        self.process, self.ready, self.created, self.attempt = None, None, False, 0
        self.stopped = []
        self.dependents = []

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=False)
        try:
            with psycopg.connect(self.database_url) as connection:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            self.created = True
            marker(self.output, "owned", dict(schema=self.schema, run_id=self.run_id, ephemeral=True))
            migrate(self.isolated_url)
            application = build_demo_application(PostgresDemoBackend(
                self.isolated_url, r06_enabled=True, r06_content_backend=self.backend,
                r06_ranker_backend=self.backend), managed_root=self.root)
            try:
                application.manager.prepare_r06_bundle(self.bundle_id, self.digest)
                application.manager.publish(uuid4(), self.bundle_id, None)
            finally:
                asyncio.run(application.backend.aclose())
            self.start()
            return self
        except BaseException:
            self.close()
            raise

    def start(self):
        if self.process is not None:
            raise ValueError("stop the owned process before restarting")
        self.attempt += 1
        self.child_output = self.output / f"process-{self.attempt}"
        environment = {k: v for k, v in os.environ.items() if not k.startswith("EVOREC_")}
        environment.update(EVOREC_DATABASE_URL=self.isolated_url, EVOREC_BUNDLE_ROOT=str(self.root),
                           EVOREC_R06_SERVING_ENABLED="1", EVOREC_R06_CONTENT_BACKEND=self.backend,
                           EVOREC_R06_RANKER_BACKEND=self.backend)
        self.process = subprocess.Popen([sys.executable, "-m", "scripts.r06_service_lab",
            str(self.child_output), self.run_id], env=environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[1],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = monotonic() + 180
        while not (self.child_output / "ready.json").exists():
            if self.process.poll() is not None:
                raise RuntimeError("owned API exited before readiness")
            if monotonic() > deadline:
                raise TimeoutError("owned API startup deadline exceeded")
            sleep(.05)
        self.ready = json.loads((self.child_output / "ready.json").read_bytes())
        if (self.ready["run_id"] != self.run_id or self.ready["schema"] != self.schema
                or self.ready["bundle_id"] != str(self.bundle_id)
                or self.ready["manifest_sha256"] != self.digest or self.ready["admin_enabled"] is not False):
            raise ValueError("owned API identity changed")

    def stop(self, *, crash=False):
        if self.process is None:
            return
        if self.process.poll() is None:
            if crash:
                if self.ready is None or self.ready["run_id"] != self.run_id:
                    raise ValueError("cannot kill an unverified process")
                # Windows venv's redirector PID may differ; kill only the verified actual server.
                os.kill(self.ready["pid"], signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
            else:
                (self.child_output / "stop").touch()
            self.process.wait(timeout=30)
        if not crash:
            stopped = json.loads((self.child_output / "stopped.json").read_bytes())
            if self.process.returncode != 0 or not stopped["cpu_jobs_drained"] or stopped["run_id"] != self.run_id:
                raise ValueError("owned API did not drain normally")
        self.stopped.append(dict(attempt=self.attempt, hard_killed=crash, normal_cpu_drain=not crash))
        self.process, self.ready = None, None

    def close(self):
        # Refuse schema deletion if a process has not exited; no broad cleanup fallback.
        if any(process.poll() is None for process in self.dependents):
            raise RuntimeError('owned worker still live; refuse schema deletion')
        self.stop()
        if self.created:
            with psycopg.connect(self.database_url) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
            self.created = False
            with psycopg.connect(self.database_url) as connection:
                if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (self.schema,)).fetchone():
                    raise ValueError("owned schema still exists")
            marker(self.output, "stopped", dict(run_id=self.run_id, owned_schema_removed=True,
                                                processes=self.stopped))

    def __exit__(self, *_):
        self.close()


def child(output, run_id):
    output = output.resolve()
    project = Path(__file__).resolve().parents[1]
    if not output.is_relative_to(project / "artifacts"):
        raise ValueError("child output is outside artifacts")
    owned = json.loads((output.parent / "owned.json").read_bytes())
    if owned["run_id"] != run_id or not owned["ephemeral"]:
        raise ValueError("child invocation is not owned")
    application = build_demo_application()
    try:
        with application.backend._connect() as connection:
            if connection.execute("SELECT current_schema() AS schema").fetchone()["schema"] != owned["schema"]:
                raise ValueError("child database does not match its owner")
        # Recover is the same production path as application lifespan, not re-publication.
        application.manager.recover()
        runtime = application.backend.runtime
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            if listener.getsockname()[1] == 8000:
                raise ValueError("protected port")
            output.mkdir(exist_ok=False)
            metadata = dict(pid=os.getpid(), run_id=run_id, schema=owned["schema"],
                url=f"http://127.0.0.1:{listener.getsockname()[1]}", bundle_id=str(runtime.bundle.bundle_id),
                manifest_sha256=runtime.manifest_sha256, model_version=runtime.bundle.model_version,
                item_count=len(runtime.item_ids),
                admin_enabled=False, ephemeral=True, api_deadline_seconds=2.0)
            asyncio.run(serve(application, listener, output, metadata))
            marker(output, "stopped", dict(run_id=run_id,
                cpu_jobs_drained=application.backend.r06_queue.outstanding == 0))
    finally:
        asyncio.run(application.backend.aclose())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("run_id")
    args = parser.parse_args()
    try:
        child(args.output, args.run_id)
    except Exception as error:
        print(json.dumps(dict(status="failed", error_type=type(error).__name__)), flush=True)
        raise SystemExit(1)
