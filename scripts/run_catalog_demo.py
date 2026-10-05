"""Ephemeral loopback content baseline with an owned catalog worker.

Synthetic UI fixtures, not R06. Only the generated schema/root/token are used;
normal exit drains the worker, removes the token and drops the owned schema.
Hard kill/power loss cannot guarantee cleanup. Markers are not acceptance.
"""
import argparse
import asyncio
import csv
import os
from pathlib import Path
import secrets
import socket
from threading import Event
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from evorec.api.catalog_file import parse_catalog_file
from evorec.bootstrap import build_demo_application
from evorec.contracts import CatalogImportInput
from evorec.domain.errors import ManagementError
from evorec.infrastructure.demo_catalog import DEMO_ITEMS
from evorec.infrastructure.postgres import PostgresDemoBackend
from scripts.migrate_database import migrate
from scripts.run_r06_demo import marker, serve, validate_isolation


def fixtures(output):
    # These deliberately synthetic items have no user/research data or images.
    with (output/'new-items.csv').open('x',encoding='utf-8',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['item_id','title','category','description'])
        writer.writerow(['demo-new-coop','Co-op Demo','合作','合成新品；同标题/分类用于内容基线演示，不是效果实验。'])
        writer.writerow(['demo-new-racing','New Racing Demo','竞速','合成新品。'])
    with (output/'invalid-items.csv').open('x',encoding='utf-8',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['item_id','title','category'])
        writer.writerow(['demo-rejected-valid','合成合法行','解谜'])
        writer.writerow(['demo-rejected-invalid','','解谜'])


async def serve_catalog(application, listener, output, metadata):
    stop_worker=Event()
    def work():
        try:
            while not stop_worker.is_set():
                worked=application.manager.file_jobs.run_next(parse_catalog_file)
                if not worked:
                    worked=application.manager.builds.run_next()
                if not worked:
                    stop_worker.wait(.1)
        except BaseException:
            # Fail closed: stop the owned listener instead of silently losing jobs.
            (output/'stop').touch()
            raise
    worker=asyncio.create_task(asyncio.to_thread(work))
    try:
        await serve(application,listener,output,metadata)
    finally:
        stop_worker.set()
        # Do not cancel a to_thread future and then drop a still-used schema.
        await worker


def run(output, database_url, *, port=0):
    if os.getenv('EVOREC_ADMIN_TOKEN'):
        raise ValueError('CLI child must replace the business administrator token')
    output, parameters=validate_isolation(output,database_url,port)
    schema='test_evorec_'+uuid4().hex
    isolated=make_conninfo(**{**parameters,'options':'-c search_path='+schema})
    application=None
    created=False
    drained=False
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',port))
        if listener.getsockname()[1]==8000:
            raise ValueError('OS selected protected port')
        output.mkdir(parents=True,exist_ok=False)
        metadata=dict(pid=os.getpid(),run_id=str(uuid4()),schema=schema,
            url=f'http://127.0.0.1:{listener.getsockname()[1]}',ephemeral=True,
            model_kind='content-baseline',admin_enabled=True,worker_enabled=True,
            api_deadline_seconds=2.0)
        token_path=output/'admin-token.txt'
        try:
            with psycopg.connect(database_url) as connection:
                connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            created=True
            marker(output,'owned',metadata)
            migrate(isolated)
            root=output/'bundles'
            root.mkdir()
            application=build_demo_application(PostgresDemoBackend(isolated,r06_enabled=False,
                r06_content_backend='stdlib',r06_ranker_backend='stdlib'),managed_root=root)
            batch=uuid4()
            application.manager.import_items(CatalogImportInput(batch_id=batch,items=[
                dict(item_id=i,title=t,category=c,description=d) for i,t,c,d in DEMO_ITEMS]))
            build=application.manager.builds.process(batch,uuid4())
            application.manager.builds.publish(build['build_id'],uuid4())
            token=secrets.token_urlsafe(32)
            with token_path.open('x',encoding='utf-8') as stream:
                stream.write(token)
            fixtures(output)
            metadata.update(bundle_id=str(build['bundle_id']),item_count=len(DEMO_ITEMS),
                model_id=application.backend.runtime.model_id,
                content_encoder_id=application.backend.runtime.content_encoder_id)
            os.environ['EVOREC_ADMIN_TOKEN']=token
            asyncio.run(serve_catalog(application,listener,output,metadata))
            drained=True
        finally:
            os.environ.pop('EVOREC_ADMIN_TOKEN',None)
            if application is not None:
                asyncio.run(application.backend.aclose())
            # Own exact temporary credential only. It is never printed or archived.
            token_path.unlink(missing_ok=True)
            if created:
                with psycopg.connect(database_url) as connection:
                    connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                marker(output,'stopped',dict(pid=os.getpid(),run_id=metadata['run_id'],schema=schema,
                    owned_schema_removed=True,worker_drained=drained))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args(argv)
    database_url=os.getenv('EVOREC_DATABASE_URL')
    import json
    if not database_url:
        print(json.dumps(dict(status='failed',code='database_not_configured')))
        return 1
    for name in list(os.environ):
        if name.startswith('EVOREC_') and name!='EVOREC_DATABASE_URL':
            os.environ.pop(name)
    try:
        run(args.output,database_url,port=args.port)
    except KeyboardInterrupt:
        return 0
    except (ValueError,OSError,psycopg.Error,ManagementError) as error:
        print(json.dumps(dict(status='failed',code=getattr(error,'code','catalog_demo_failed'),
            error_type=type(error).__name__)))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
