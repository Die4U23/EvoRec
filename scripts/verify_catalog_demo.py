"""Reusable real-TCP assertions for the synthetic content demo, not research."""
from time import monotonic, sleep
from uuid import uuid4


def exercise(client, ready, token, output):
    admin={'X-Admin-Token':token}
    def ok(method,path,expected=200,**kwargs):
        response=client.request(method,path,**kwargs)
        # Never put session/admin tokens or response bodies in diagnostic output.
        assert response.status_code==expected,(method,path,response.status_code)
        return response.json()
    def wait(path,terminal):
        deadline=monotonic()+15
        while True:
            value=ok('GET',path,headers=admin)
            if value['status'] in terminal:
                return value
            assert monotonic()<deadline,('job deadline',path,value['status'])
            sleep(.05)
    capabilities=ok('GET','/api/v1/system')['capabilities']
    assert capabilities['catalog_publication'] and not capabilities['r06_serving']
    assert client.get('/app').status_code==200
    assert client.get('/app/results').status_code==200
    assert client.get('/api/v1/admin/publication').status_code==403
    state=ok('GET','/api/v1/admin/publication',headers=admin)
    assert state['active_bundle_id']==ready['bundle_id']
    session=ok('POST','/api/v1/sessions',201,json={'profile_id':'sample'})
    assert session['history']==['demo-coop']
    auth={'X-Session-Token':session['access_token']}
    request=dict(session_id=session['session_id'],expected_history_version=0,strategy='dense',k=10)
    def recommend(key=None):
        headers={**auth,'Idempotency-Key':key or str(uuid4())}
        return ok('POST','/api/v1/recommendations',headers=headers,json=request)
    before=recommend()
    assert before['actual_strategy']=='dense' and before['bundle_id']==ready['bundle_id']
    def upload(name,batch,expected=202):
        return ok('POST','/api/v1/admin/catalog/file-import-jobs',expected,
            headers={**admin,'X-Batch-Id':batch,'Content-Type':'text/csv'},
            content=(output/name).read_bytes())
    invalid=str(uuid4())
    upload('invalid-items.csv',invalid)
    failed=wait('/api/v1/admin/catalog/file-import-jobs/'+invalid,{'failed','imported'})
    assert failed['status']=='failed' and failed['row_errors']
    assert any(row['row']==3 and row['field']=='title' for row in failed['row_errors'])
    for name in ['demo-rejected-valid','demo-rejected-invalid']:
        assert client.get('/api/v1/items/'+name).status_code==404
    batch=str(uuid4())
    upload('new-items.csv',batch)
    imported=wait('/api/v1/admin/catalog/file-import-jobs/'+batch,{'failed','imported'})
    assert imported['status']=='imported' and imported['item_count']==2
    assert upload('new-items.csv',batch,200)['replayed']
    conflict=client.post('/api/v1/admin/catalog/file-import-jobs',
        headers={**admin,'X-Batch-Id':batch,'Content-Type':'text/csv'},content=b'changed')
    assert conflict.status_code==409
    assert 'demo-new-coop' not in {i['item_id'] for i in recommend()['items']}
    build=str(uuid4())
    route='/api/v1/admin/catalog/imports/'+batch+'/build-jobs'
    ok('POST',route,202,headers=admin,json={'build_id':build})
    built=wait('/api/v1/admin/catalog/builds/'+build,{'ready','failed'})
    assert built['status']=='ready' and built['total_count']==26
    preview=ok('GET','/api/v1/admin/catalog/builds/'+build+'/items?limit=50',headers=admin)
    cold=next(i for i in preview['items'] if i['item_id']=='demo-new-coop')
    assert cold['ready_for_publication'] and not cold['currently_recommendable']
    assert ok('GET','/api/v1/admin/publication',headers=admin)['active_bundle_id']==ready['bundle_id']
    saved_key=str(uuid4())
    saved=recommend(saved_key)
    assert 'demo-new-coop' not in {i['item_id'] for i in saved['items']}
    publish='/api/v1/admin/catalog/builds/'+build+'/publish'
    operation={'operation_id':str(uuid4())}
    published=ok('POST',publish,headers=admin,json=operation)
    assert published['active_bundle_id']==built['bundle_id']
    assert ok('POST',publish,headers=admin,json=operation)['replayed']
    after=recommend()
    assert after['bundle_id']==built['bundle_id'] and after['items'][0]['item_id']=='demo-new-coop'
    assert after['items'][0]['score']>0
    ok('POST','/api/v1/admin/items/demo-new-coop/deactivate',headers=admin)
    assert 'demo-new-coop' not in {i['item_id'] for i in recommend()['items']}
    rollback={'operation_id':str(uuid4()),'expected_active_bundle_id':built['bundle_id']}
    roll='/api/v1/admin/bundles/'+ready['bundle_id']+'/rollback'
    rolled=ok('POST',roll,headers=admin,json=rollback)
    assert rolled['active_bundle_id']==ready['bundle_id']
    assert ok('POST',roll,headers=admin,json=rollback)['replayed']
    assert not ok('GET','/api/v1/items/demo-new-coop')['is_active']
    assert recommend()['bundle_id']==ready['bundle_id']
    assert recommend(saved_key)==saved
    return dict(actual_tcp_http=True,invalid_csv_atomic_and_row_errors=True,
        cold_item_recommendable_only_after_publish=True,deactivate_and_rollback_do_not_reactivate=True,
        original_recommendation_replays_after_rollback=True,publication_and_file_idempotency=True)
