"""Owned full R06 API + evaluation worker; temporary schema, never business configuration."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from time import sleep
from uuid import UUID

from scripts.r06_service_lab import R06ServiceLab
from scripts.run_r06_demo import marker


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('managed_root',type=Path)
    parser.add_argument('bundle_id',type=UUID)
    parser.add_argument('--expected-manifest-sha256',required=True)
    args=parser.parse_args()
    lab=R06ServiceLab(args.output,os.environ['EVOREC_DATABASE_URL'],args.managed_root,
                      args.bundle_id,args.expected_manifest_sha256)
    with lab:
        environment={k:v for k,v in os.environ.items() if not k.startswith('EVOREC_')}
        environment.update(EVOREC_DATABASE_URL=lab.isolated_url,EVOREC_BUNDLE_ROOT=str(lab.root),
            EVOREC_R06_SERVING_ENABLED='1',EVOREC_R06_CONTENT_BACKEND='numpy',EVOREC_R06_RANKER_BACKEND='numpy')
        worker=subprocess.Popen([sys.executable,'-m','scripts.evaluation_worker','--stop-file',
            str(lab.output/'stop-worker')],env=environment,cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        lab.dependents.append(worker)
        try:
            ready={**lab.ready,'evaluation_worker_enabled':True}
            marker(lab.output,'ready',ready)
            print(json.dumps(dict(status='ready',url=ready['url']+'/app/evaluations',output=str(lab.output),ephemeral=True)),flush=True)
            try:
                while not (lab.output/'stop').exists():
                    if worker.poll() is not None:
                        raise RuntimeError('owned evaluation worker exited')
                    sleep(.2)
            except KeyboardInterrupt:
                pass
        finally:
            (lab.output/'stop-worker').touch()
            # Do not drop a schema underneath live CPU work or force-kill it as success.
            worker.wait(timeout=600)
            if worker.returncode!=0:
                raise RuntimeError('owned worker did not exit normally')
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps(dict(status='failed',error_type=type(error).__name__)),flush=True)
        raise SystemExit(1)
