"""Independent single-process worker for bounded saved-snapshot batch evaluations."""

import argparse
import asyncio
from math import isfinite
from pathlib import Path

import psycopg

from evorec.bootstrap import build_demo_application


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--poll-seconds',type=float,default=1)
    parser.add_argument('--stop-file',type=Path,help='owned controller marker; checked between drained jobs')
    args = parser.parse_args()
    if not isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        parser.error('poll seconds must be finite and positive')
    application = build_demo_application()
    if application.evaluation_jobs is None:
        parser.error('EVOREC_DATABASE_URL is required')
    async def run():
        try:
            while True:
                if args.stop_file and args.stop_file.exists():
                    return 0
                try:
                    worked = await application.evaluation_jobs.run_next()
                except psycopg.Error:
                    print('evaluation database unavailable; retrying',flush=True)
                    if args.once:
                        return 1
                    worked = False
                if args.once:
                    return 0
                if not worked:
                    await asyncio.sleep(args.poll_seconds)
        finally:
            await application.backend.aclose()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
