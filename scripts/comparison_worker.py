"""Independent worker for frozen single-session strategy comparison jobs."""

import argparse
import asyncio
from math import isfinite

import psycopg

from evorec.bootstrap import build_demo_application


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='attempt at most one queued job')
    parser.add_argument('--poll-seconds', type=float, default=1.0)
    args = parser.parse_args()
    if not isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        parser.error('--poll-seconds must be finite and positive')
    application = build_demo_application()
    if application.comparison_jobs is None:
        parser.error('EVOREC_DATABASE_URL is required')

    async def run():
        try:
            while True:
                try:
                    worked = await application.comparison_jobs.run_next()
                except psycopg.Error:
                    print('comparison database unavailable; retrying', flush=True)
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
