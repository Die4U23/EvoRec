"""Ephemeral hash-approved R06 demo on loopback, never the business schema.

Requires EVOREC_DATABASE_URL and a prepared local package (not a checkpoint to
train). Each invocation owns a fresh schema, disabled admin access, and a fresh
output directory. Graceful exit removes its schema; hard kill/power loss cannot
guarantee cleanup. ready/stopped are lifecycle records, not acceptance reports.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from evorec.domain.errors import ManagementError
from evorec.infrastructure.residual_ranker import ControlledLoadError, _digest
from scripts.migrate_database import migrate


def validate(output, database_url, managed_root, bundle_id, digest, backend, port):
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    root = Path(managed_root).resolve(strict=True)
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("demo output must be a fresh artifacts subdirectory")
    if output.exists():
        raise FileExistsError("demo output already exists")
    if not root.is_dir() or type(bundle_id) is not UUID:
        raise ValueError("managed root and bundle UUID are required")
    _digest(digest)
    if backend not in {"numpy", "stdlib"} or type(port) is not int or not 0 <= port < 65536 or port == 8000:
        raise ValueError("unsupported backend or protected/invalid port")
    parameters = conninfo_to_dict(database_url)
    if parameters.get("host", "") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("isolated demo requires an explicit local PostgreSQL host")
    if parameters.get("hostaddr", "") not in {"", "127.0.0.1", "::1"}:
        raise ValueError("database host address must also be loopback")
    return output, root, parameters


def marker(directory, name, data):
    # Do not leave a partially readable ready marker or overwrite another writer.
    partial = directory / (name + ".partial")
    with partial.open("xb") as stream:
        stream.write(json.dumps(data, sort_keys=True, allow_nan=False).encode("utf-8"))
    os.link(partial, directory / (name + ".json"))
    partial.unlink()


async def serve(application, listener, output, metadata):
    import uvicorn
    from evorec.api.app import create_app

    class DemoServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                if not (await application.readiness.execute()).ready:
                    raise ValueError("isolated model is not ready after startup")
                marker(output, "ready", metadata)
                print(json.dumps(dict(status="ready", url=metadata["url"],
                                      ephemeral=True, output=str(output))), flush=True)

    server = DemoServer(uvicorn.Config(create_app(demo_application=application),
                                      log_level="error", access_log=False, lifespan="on"))

    async def stop_requested():
        while not (output / "stop").exists():
            await asyncio.sleep(.1)
        server.should_exit = True

    watcher = asyncio.create_task(stop_requested())
    try:
        await server.serve(sockets=[listener])
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await application.backend.aclose()


def run(output, database_url, managed_root, bundle_id, digest, *, backend="numpy", port=0, run_id=None):
    if os.getenv("EVOREC_ADMIN_TOKEN"):
        raise ValueError("use the CLI child with admin access disabled")
    output, root, parameters = validate(output, database_url, managed_root, bundle_id, digest, backend, port)
    schema = "test_evorec_" + uuid4().hex
    isolated = make_conninfo(**{**parameters, "options": "-c search_path=" + schema})
    created, application = False, None
    # Bind first: an occupied port cannot cause database writes, and a retained
    # socket prevents choose-free-port/rebind races. Port 8000 is always excluded.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        if listener.getsockname()[1] == 8000:
            raise ValueError("OS selected the protected port")
        output.mkdir(parents=True, exist_ok=False)
        metadata = dict(pid=os.getpid(), run_id=str(run_id or uuid4()), schema=schema,
                        url=f"http://127.0.0.1:{listener.getsockname()[1]}", bundle_id=str(bundle_id),
                        manifest_sha256=digest, backend=backend, ephemeral=True,
                        api_deadline_seconds=2.0, admin_enabled=False)
        try:
            with psycopg.connect(database_url) as connection:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            created = True
            marker(output, "owned", metadata)
            migrate(isolated)
            from evorec.bootstrap import build_demo_application
            from evorec.infrastructure.postgres import PostgresDemoBackend
            application = build_demo_application(PostgresDemoBackend(
                isolated, r06_enabled=True, r06_content_backend=backend, r06_ranker_backend=backend,
            ), managed_root=root)
            prepared = application.manager.prepare_r06_bundle(bundle_id, digest)
            application.manager.publish(uuid4(), bundle_id, None)
            metadata.update(item_count=prepared["item_count"],
                            model_version=application.backend.runtime.bundle.model_version)
            asyncio.run(serve(application, listener, output, metadata))
        finally:
            if application is not None:
                # Also covers setup/startup failure before the ASGI lifespan.
                asyncio.run(application.backend.aclose())
            if created:
                with psycopg.connect(database_url) as connection:
                    connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                marker(output, "stopped", dict(pid=os.getpid(), run_id=metadata["run_id"],
                    schema=schema, owned_schema_removed=True,
                    cpu_jobs_drained=application is None or application.backend.r06_queue.outstanding == 0))


class DemoProcess:
    """Owned child controller for TCP tests, not an ASGI mock or test fault mode."""

    def __init__(self, output, database_url, managed_root, bundle_id, digest, *, backend="numpy", startup_timeout=180):
        self.output = Path(output).resolve()
        self.args = (database_url, managed_root, bundle_id, digest, backend)
        validate(output, database_url, managed_root, bundle_id, digest, backend, 0)
        self.timeout = startup_timeout
        self.run_id = uuid4()
        self.process = self.ready = self.stopped = None

    def __enter__(self):
        from time import monotonic, sleep
        url, root, identity, digest, backend = self.args
        environment = {k: v for k, v in os.environ.items() if not k.startswith("EVOREC_")}
        environment["EVOREC_DATABASE_URL"] = url
        command = [sys.executable, "-m", "scripts.run_r06_demo", str(self.output), str(root), str(identity),
                   "--expected-manifest-sha256", digest, "--backend", backend, "--run-id", str(self.run_id)]
        try:
            self.process = subprocess.Popen(command, env=environment, cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = monotonic() + self.timeout
            while not (self.output / "ready.json").exists():
                if self.process.poll() is not None:
                    raise RuntimeError("owned demo exited before readiness")
                if monotonic() >= deadline:
                    raise TimeoutError("owned demo startup deadline exceeded")
                sleep(.05)
            self.ready = json.loads((self.output / "ready.json").read_bytes())
            address = urlsplit(self.ready["url"])
            # Windows venv python.exe may be a redirector with a different PID.
            # Bind markers to a parent-generated invocation ID, not that stub PID.
            if (self.ready["run_id"] != str(self.run_id) or type(self.ready["pid"]) is not int
                    or self.ready["pid"] <= 0 or self.ready["bundle_id"] != str(identity)
                    or self.ready["manifest_sha256"] != digest or self.ready["backend"] != backend
                    or self.ready["admin_enabled"] is not False or self.ready["ephemeral"] is not True
                    or address.scheme != "http" or address.hostname != "127.0.0.1"
                    or address.port in {None, 0, 8000} or address.username is not None
                    or address.path or address.query or address.fragment):
                raise ValueError("owned demo readiness identity differs")
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                # Never make a missing/rival output directory on behalf of the child.
                if self.output.is_dir():
                    (self.output / "stop").touch(exist_ok=True)
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
                    raise TimeoutError("owned demo needed force kill; schema cleanup is not proven")
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()
        if (self.output / "stopped.json").exists():
            self.stopped = json.loads((self.output / "stopped.json").read_bytes())
        if self.process.returncode != 0:
            raise RuntimeError("owned demo failed; inspect its sanitized lifecycle status")
        if self.ready is not None and (self.stopped is None
                or self.stopped["pid"] != self.ready["pid"] or self.stopped["run_id"] != str(self.run_id)
                or not self.stopped["owned_schema_removed"] or not self.stopped["cpu_jobs_drained"]):
            raise RuntimeError("owned demo cleanup identity or CPU/schema state differs")

    def __exit__(self, *_):
        self.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--backend", choices=("numpy", "stdlib"), default="numpy")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--run-id", type=UUID, help="invocation identity for an owned child controller")
    args = parser.parse_args(argv)
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if not database_url:
        print(json.dumps(dict(status="failed", code="database_not_configured")))
        return 1
    # Management is deliberately unavailable in this child, regardless of the
    # invoking shell's business-service environment. Never expose an admin key.
    os.environ.pop("EVOREC_ADMIN_TOKEN", None)
    try:
        run(args.output, database_url, args.managed_root, args.bundle_id,
            args.expected_manifest_sha256, backend=args.backend, port=args.port, run_id=args.run_id)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, psycopg.Error, ManagementError, ControlledLoadError) as error:
        print(json.dumps(dict(status="failed", code=getattr(error, "code", "demo_failed"),
                              error_type=type(error).__name__)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
