"""Worker for durable file validation and local catalog build jobs."""

import argparse
import time

import psycopg

from evorec.api.catalog_file import parse_catalog_file
from evorec.bootstrap import build_demo_application
from evorec.domain.errors import ManagementError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="attempt at most one queued job")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    application = build_demo_application()
    manager = application.manager
    if manager is None:
        parser.error("EVOREC_DATABASE_URL is required")
    try:
        recovered = False
        while True:
            try:
                if not recovered:
                    manager.file_jobs.recover_interrupted()
                    manager.builds.recover_interrupted()
                    recovered = True
                worked = manager.file_jobs.run_next(parse_catalog_file)
                if not worked and manager.managed_root is not None:
                    worked = manager.builds.run_next()
            except ManagementError as exc:
                print(f"catalog build did not complete: {exc.code}", flush=True)
                if args.once:
                    return 1
                worked = False
            except psycopg.Error:
                print("catalog database unavailable; retrying", flush=True)
                recovered = False
                if args.once:
                    return 1
                worked = False
            if args.once:
                return 0
            if not worked:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
