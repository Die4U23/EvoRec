const {test} = require('node:test');
const assert = require('node:assert/strict');
const {evaluationPayload,metricText,evaluationRows,pendingOperation,validPending,comparisonView} = require('../web/evaluations.js');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const script=fs.readFileSync(path.join(__dirname,'../web/evaluations.js'),'utf8');
const session={session_id:'11111111-1111-4111-8111-111111111111',access_token:'fixture-token',history_version:0};
const original='22222222-2222-4222-8222-222222222222';
const ok=value => ({ok:true,status:200,json:async () => value});
function page(fetchReply,storage=new Map()) {
  const elements=new Map(),requests=[];let timeout=null,counter=0,timerId=0;
  class Element {
    constructor(){this.value='';this.textContent='';this.children=[];this.disabled=false;}
    replaceChildren(...children){this.children=children;}
    append(...children){this.children.push(...children);}
    click(){if(this.onclick)return this.onclick();}
  }
  const element=id => {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  if(!storage.has('evorec.comparison'))storage.set('evorec.comparison',JSON.stringify({session}));
  const context={document:{getElementById:element,createElement:() => new Element(),querySelectorAll:() => [...elements.values()]},
    Node:Element,window:{addEventListener(){}},crypto:{randomUUID:() => `33333333-3333-4333-8333-${String(++counter).padStart(12,'0')}`},
    sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    setTimeout:callback=>{timeout=callback;return ++timerId;},clearTimeout:()=>{timeout=null;},
    confirm:()=>true,fetch:async (url,options)=>{requests.push({url,options});return fetchReply(url,options);}};
  vm.runInNewContext(script,context,{filename:'web/evaluations.js'});
  element('dataset-name').value='synthetic';element('label-origin').value='synthetic';
  element('source-description').value='synthetic only';element('evaluation-k').value='20';
  element('cases').value=JSON.stringify([{comparison_id:original,target_item_id:'independent',target_at:'2030-01-01T00:00:00Z'}]);
  return {element,requests,hasTimer:()=>timeout!==null,tick:async()=>{
    const callback=timeout;timeout=null;if(callback)await callback();
  }};
}

const jobState=(id=original,status='running')=>({job_id:id,session_id:session.session_id,
  status,dataset_name:'fixture',label_origin:'synthetic',completed_cases:0,total_cases:1,attempts:1});
const selectedStorage=()=>new Map([['evorec.evaluation',JSON.stringify({session,selected:original})]]);
const listButtons=p=>p.element('job-list').children[0].children.slice(1).map(row=>row.children[0].children[0]);

function writeBlockedStorage() {
  const storage=new Map([['evorec.comparison',JSON.stringify({session})]]);
  const set=storage.set.bind(storage);
  storage.set=(key,value)=>{
    if(key==='evorec.evaluation')throw Error('storage quota exceeded');
    return set(key,value);
  };
  return storage;
}

test('storage write failure does not block reading the selected task',async()=>{
  const p=page(url=>ok(url.includes('&offset=')?{items:[jobState()],has_more:false}:jobState()),writeBlockedStorage());
  await p.element('jobs').onclick();await listButtons(p)[0].onclick();
  assert.match(p.element('job-status').textContent,/running/);
  assert.match(p.element('storage-warning').textContent,/内存/);
  assert.equal(p.requests.length,2);
});

test('successful session creation remains visible when tab storage cannot be written',async()=>{
  const next='55555555-5555-4555-8555-555555555555';
  const p=page(()=>ok({...session,session_id:next}),writeBlockedStorage());
  await p.element('create-session').onclick();
  assert.match(p.element('session').textContent,new RegExp(next));
  assert.match(p.element('message').textContent,/创建成功/);
  assert.match(p.element('storage-warning').textContent,/请勿刷新/);
  assert.equal(p.requests.length,1);
});

test('submission keeps its in-memory key and input after storage failure and lost response',async()=>{
  const posts=[];let job;
  const p=page((url,options)=>{
    if(options.method==='POST'){
      posts.push(options);job=jobState(options.headers['Idempotency-Key'],'completed');
      if(posts.length===1)throw Error('lost submission response');return ok(job);
    }
    return ok(url.includes('&offset=')?{items:[job],has_more:false}:job);
  },writeBlockedStorage());
  await p.element('submit').onclick();p.element('cases').value='invalid edited JSON';
  await p.element('submit').onclick();
  assert.equal(posts.length,2);
  assert.equal(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);
  assert.equal(posts[0].body,posts[1].body);
  assert.match(p.element('job-status').textContent,/completed/);
  assert.match(p.element('storage-warning').textContent,/内存/);
});

test('capture remains retryable in memory when storage is blocked',async()=>{
  const posts=[];let sessionReads=0;
  const p=page((_url,options)=>{
    if(options.method!=='POST'){sessionReads++;return ok(session);}
    posts.push(options);if(posts.length===1)throw Error('lost capture response');
    return ok({comparison_id:options.headers['Idempotency-Key'],input_snapshot:{eligible_items:[],history:[]},strategies:[]});
  },writeBlockedStorage());
  await p.element('capture').onclick();await p.element('capture').onclick();
  assert.equal(posts.length,2);
  assert.equal(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);
  assert.equal(posts[0].body,posts[1].body);
  assert.equal(sessionReads,1);
  assert.match(p.element('message').textContent,/快照已保存/);
});

test('malformed evaluation state does not hide a valid recommendation session',()=>{
  const storage=new Map([['evorec.evaluation','{broken'],['evorec.comparison',JSON.stringify({session})]]);
  const p=page(()=>{throw Error('must not auto request');},storage);
  assert.match(p.element('session').textContent,new RegExp(session.session_id));
  assert.match(p.element('storage-warning').textContent,/恢复记录/);
  assert.equal(p.requests.length,0);
});

test('valid saved evaluation is restored even if unrelated recommendation state is malformed',()=>{
  const storage=selectedStorage();storage.set('evorec.comparison','{broken');
  const p=page(()=>{throw Error('must not auto request');},storage);
  assert.match(p.element('session').textContent,new RegExp(session.session_id));
  assert.match(p.element('selected-job').textContent,new RegExp(original));
  assert.equal(p.requests.length,0);
});

test('unreadable tab storage still allows an explicit new session with a persistent recovery warning',async()=>{
  const storage=writeBlockedStorage();storage.get=()=>{throw Error('storage access denied');};
  const p=page(()=>ok(session),storage);
  assert.equal(p.requests.length,0);
  assert.match(p.element('storage-warning').textContent,/恢复记录/);
  await p.element('create-session').onclick();
  assert.match(p.element('session').textContent,new RegExp(session.session_id));
  assert.match(p.element('message').textContent,/创建成功/);
  assert.match(p.element('storage-warning').textContent,/恢复记录.*内存/);
  assert.equal(p.element('storage-warning').hidden,false);
});

test('a later successful write clears the memory-only warning and persists current state',async()=>{
  const storage=writeBlockedStorage();
  const p=page(url=>ok(url.includes('&offset=')?{items:[jobState()],has_more:false}:jobState()),storage);
  await p.element('jobs').onclick();await listButtons(p)[0].onclick();
  assert.equal(p.element('storage-warning').hidden,false);
  storage.set=Map.prototype.set;
  await listButtons(p)[0].onclick();
  assert.equal(p.element('storage-warning').textContent,'');
  assert.equal(p.element('storage-warning').hidden,true);
  assert.equal(JSON.parse(storage.get('evorec.evaluation')).selected,original);
});

test('successful writes do not erase a warning about lost recovery records',async()=>{
  const storage=new Map([['evorec.evaluation','{broken']]);
  const p=page(()=>ok(session),storage);
  await p.element('create-session').onclick();
  assert.match(p.element('message').textContent,/创建成功/);
  assert.match(p.element('storage-warning').textContent,/恢复记录/);
  assert.doesNotMatch(p.element('storage-warning').textContent,/内存/);
  assert.equal(p.element('storage-warning').hidden,false);
});

test('selecting another task clears the previous status before a slow or failed query',async()=>{
  const next='44444444-4444-4444-8444-444444444444';
  const storage=selectedStorage();let rejectQuery;
  const p=page(url=>{
    if(url.includes('&offset='))return ok({items:[jobState(original,'completed'),jobState(next)],has_more:false});
    if(url.includes(next))return new Promise((_resolve,reject)=>{rejectQuery=reject;});
    return ok(jobState(original,'completed'));
  },storage);
  await p.element('poll').onclick();await p.element('jobs').onclick();
  const loading=listButtons(p)[1].onclick();
  const during=p.element('job-status').textContent;
  rejectQuery(Error('next task unavailable'));await loading;
  assert.equal(JSON.parse(storage.get('evorec.evaluation')).selected,next);
  assert.doesNotMatch(during,new RegExp(original),'old status must disappear as soon as selection changes');
  assert.doesNotMatch(p.element('job-status').textContent,new RegExp(original));
  assert.match(p.element('selected-job').textContent,new RegExp(next));
  assert.match(p.element('job-status').textContent,/尚未读取/);
  assert.equal(p.element('metrics').children.length,0);
});

for(const kind of ['submit','replay']) {
  test(`acknowledged ${kind} shows the new task when the subsequent progress read fails`,async()=>{
    let created;
    const p=page((url,options)=>{
      if(options.method==='POST'){
        created=jobState(options.headers['Idempotency-Key'],'queued');return ok(created);
      }
      if(created && url.includes(created.job_id))throw Error('progress unavailable');
      return ok(jobState(original,'completed'));
    },selectedStorage());
    await p.element('poll').onclick();await p.element(kind).onclick();
    assert.match(p.element('selected-job').textContent,new RegExp(created.job_id));
    const displayed=JSON.parse(p.element('job-status').textContent);
    assert.equal(displayed.job_id,created.job_id);
    assert.equal(displayed.status,'queued');
    assert.equal(p.hasTimer(),true);
  });
}

test('restored selected task is visible without automatically requesting or replaying it',()=>{
  const p=page(()=>{throw Error('no request expected');},selectedStorage());
  assert.match(p.element('selected-job').textContent,new RegExp(original));
  assert.match(p.element('job-status').textContent,/尚未读取/);
  assert.equal(p.requests.length,0);
  assert.equal(p.hasTimer(),false);
});

for(const direction of ['next','previous']) {
  test(`failed ${direction} page retains displayed jobs and retries the same offset`,async()=>{
    const offsets=[];let fail=false;
    const p=page(url=>{
      if(!url.includes('&offset='))return ok(jobState());
      const offset=Number(new URL(url,'http://test').searchParams.get('offset'));offsets.push(offset);
      if(fail){fail=false;throw Error('temporary list failure');}
      return ok({items:[jobState(`page-${offset}`)],has_more:true});
    });
    await p.element('jobs').onclick();
    if(direction==='previous')await p.element('next').onclick();
    const before=listButtons(p)[0],previousDisabled=p.element('previous').disabled;
    fail=true;await p.element(direction).onclick();
    assert.equal(listButtons(p)[0],before,'failed navigation keeps usable records');
    assert.equal(p.element('previous').disabled,previousDisabled);
    await before.onclick();assert.match(p.element('job-status').textContent,/running/);
    await p.element(direction).onclick();
    assert.deepEqual(offsets,direction==='next'?[0,20,20]:[0,20,0,0]);
  });
}

test('new session clears previous pagination eligibility until its own list is loaded',async()=>{
  const p=page((url,options)=>options.method==='POST'?ok({...session,session_id:'new-session'}):
    ok({items:[jobState()],has_more:true}));
  await p.element('jobs').onclick();assert.equal(p.element('next').disabled,false);
  await p.element('create-session').onclick();
  assert.equal(p.element('previous').disabled,true);
  assert.equal(p.element('next').disabled,true);
  const count=p.requests.length;await p.element('next').onclick();assert.equal(p.requests.length,count);
});

test('progress polling survives its timer firing while a list request is busy',async()=>{
  let resolveList,reads=0;
  const p=page(url=>url.includes('&offset=')?new Promise(resolve=>{resolveList=resolve;}):
    (reads++,ok(jobState())),selectedStorage());
  await p.element('poll').onclick();
  const loading=p.element('jobs').onclick();await p.tick();
  assert.equal(reads,1,'busy page does not overlap requests');
  resolveList(ok({items:[],has_more:false}));await loading;
  assert.equal(p.hasTimer(),true,'skipped polling must retain a timer');
  await p.tick();assert.equal(reads,2);
});

for(const failure of ['network','server','rate-limit']) {
  test(`progress retries a transient ${failure} failure without losing the last good status`,async()=>{
    let reads=0;
    const p=page(()=>{
      if(++reads===2){
        if(failure==='network')throw Error('network disconnected');
        return {ok:false,status:failure==='rate-limit'?429:503,json:async()=>({error:{code:'unavailable'}})};
      }
      return ok(jobState(original,reads===1?'running':'completed'));
    },selectedStorage());
    await p.element('poll').onclick();await p.tick();
    assert.match(p.element('job-status').textContent,/running/);
    assert.match(p.element('job-progress-note').textContent,/自动重试/);
    assert.equal(p.hasTimer(),true);
    await p.tick();assert.match(p.element('job-status').textContent,/completed/);
    assert.equal(p.element('job-progress-note').textContent,'');
    assert.equal(p.hasTimer(),false,'terminal task stops polling');
  });
}

test('progress stops automatic requests after permission rejection and permits manual recovery',async()=>{
  let reads=0;
  const p=page(()=>++reads===2
    ? {ok:false,status:403,json:async()=>({error:{code:'forbidden'}})}
    : ok(jobState(original,reads===1?'running':'completed')),selectedStorage());
  await p.element('poll').onclick();await p.tick();
  assert.equal(p.hasTimer(),false);
  assert.match(p.element('job-progress-note').textContent,/已停止/);
  assert.match(p.element('message').textContent,/forbidden/);
  await p.element('poll').onclick();
  assert.match(p.element('job-status').textContent,/completed/);
  assert.equal(p.element('job-progress-note').textContent,'');
});

test('failed session creation preserves polling for the existing selected task',async()=>{
  let reads=0;
  const p=page((_url,options)=>{
    if(options.method==='POST')throw Error('session unavailable');
    return ok(jobState(original,++reads===1?'running':'completed'));
  },selectedStorage());
  await p.element('poll').onclick();await p.element('create-session').onclick();
  assert.equal(p.hasTimer(),true);
  await p.tick();assert.match(p.element('job-status').textContent,/completed/);
});

test('actual page preserves uncertain submission across reload and field changes',async()=>{
  const storage=new Map(),posts=[];let job;
  const reply=(url,options)=>{
    if(options.method==='POST'){
      posts.push(options);job={job_id:options.headers['Idempotency-Key'],session_id:session.session_id,status:'queued'};
      if(posts.length===1)throw Error('lost response');return ok(job);
    }
    return ok(url.includes('&offset=')?{items:[job],has_more:false}:job);
  };
  const first=page(reply,storage);await first.element('submit').onclick();
  assert.match(first.element('message').textContent,/lost response/);
  const restored=page(reply,storage);assert.equal(restored.requests.length,0,'refresh sends no automatic request');
  restored.element('cases').value='invalid edited JSON';await restored.element('submit').onclick();
  assert.equal(posts.length,2);assert.equal(posts[1].body,posts[0].body);
  assert.equal(posts[1].headers['Idempotency-Key'],posts[0].headers['Idempotency-Key']);
  assert.match(restored.element('job-status').textContent,/queued/);
});

test('known 422 rejection allows corrected explicit submission with a fresh key',async()=>{
  const posts=[];let job;
  const p=page((url,options)=>{
    if(options.method==='POST'){
      posts.push(options);
      if(posts.length===1)return {ok:false,status:422,json:async()=>({error:{code:'evaluation_label_leakage',message:'target in history'}})};
      job={job_id:options.headers['Idempotency-Key'],session_id:session.session_id,status:'completed'};return ok(job);
    }
    return ok(url.includes('&offset=')?{items:[job],has_more:false}:job);
  });
  await p.element('submit').onclick();assert.match(p.element('message').textContent,/leakage/);
  p.element('cases').value=JSON.stringify([{comparison_id:original,target_item_id:'corrected',target_at:'2030-01-01T00:00:00Z'}]);
  await p.element('submit').onclick();assert.equal(posts.length,2);
  assert.notEqual(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);
  assert.equal(JSON.parse(posts[1].body).cases[0].target_item_id,'corrected');
});

test('actual replay retains original target across a lost response and reload',async()=>{
  const storage=new Map([['evorec.evaluation',JSON.stringify({session,selected:original})]]),posts=[];let job;
  const reply=(url,options)=>{
    if(options.method==='POST'){
      assert.match(url,new RegExp(`/evaluation-jobs/${original}/replay`));posts.push(options);
      job={job_id:options.headers['Idempotency-Key'],session_id:session.session_id,status:'completed'};
      if(posts.length===1)throw Error('lost replay response');return ok(job);
    }
    return ok(url.includes('&offset=')?{items:[job],has_more:false}:job);
  };
  const first=page(reply,storage);await first.element('replay').onclick();
  const restored=page(reply,storage);await restored.element('replay').onclick();
  assert.equal(posts.length,2);assert.equal(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);
});

test('mismatched response does not clear pending identity',async()=>{
  const storage=new Map(),posts=[];
  const p=page((url,options)=>{posts.push(options);return ok({job_id:original,session_id:session.session_id});},storage);
  await p.element('submit').onclick();await p.element('submit').onclick();
  assert.match(p.element('message').textContent,/身份不一致/);
  assert.equal(posts.length,2);assert.equal(posts[0].body,posts[1].body);
  assert.equal(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);
  assert(JSON.parse(storage.get('evorec.evaluation')).pending);
});

test('double click coalesces an in-flight submission and cancellation waits for acknowledgement',async()=>{
  let resolve,job,posts=0;
  const p=page((url,options)=>{
    if(options.method==='POST' && !url.includes('/cancel')){
      posts++;job={job_id:options.headers['Idempotency-Key'],session_id:session.session_id,status:'running'};
      return new Promise(done=>{resolve=()=>done(ok(job));});
    }
    if(url.includes('/cancel'))job={...job,status:'cancelling'};
    return ok(url.includes('&offset=')?{items:[job],has_more:false}:job);
  });
  const active=p.element('submit').onclick();await p.element('submit').onclick();assert.equal(posts,1);
  resolve();await active;await p.element('cancel').onclick();
  assert.match(p.element('job-status').textContent,/cancelling/);
  job={...job,status:'cancelled'};await p.tick();assert.match(p.element('job-status').textContent,/cancelled/);
});

test('unknown and nonfinite metrics are not rendered as zero', () => {
  for(const value of [null,undefined,NaN,Infinity,'0']) assert.equal(metricText(value),'未知（未评估）');
  assert.equal(metricText(0),'0.000000');
});
test('failed samples and actual strategies remain visible', () => {
  const rows=evaluationRows({groups:{all_cases:{adaptive:{cases:2,evaluated_cases:1,failed_cases:1,
    fallback_cases:0,actual_strategy_counts:{dense:1},metrics:{'ndcg@10':null,'recall@10':0}}}}});
  assert.deepEqual(rows[0].slice(0,7),['all_cases','adaptive',2,1,1,0,'{"dense":1}']);
  assert.match(rows[0][7],/未知/);
});
test('uncertain submit or replay preserves original key and exact input', () => {
  const first=pendingOperation(null,'submit',{cases:['old']},() => 'original');
  assert.equal(pendingOperation(first,'submit',{cases:['new']},() => {throw Error('must not generate')}),first);
  const replay=pendingOperation(null,'replay',{original:'job-1'},() => 'replay-key');
  assert.equal(pendingOperation(replay,'replay',{original:'job-2'},() => 'changed'),replay);
});
test('invalid JSON, empty dataset, oversize or disconnected session rejected', () => {
  for(const cases of ['invalid','[]',JSON.stringify(Array.from({length:101},() => ({})))])
    assert.throws(() => evaluationPayload({session_id:'s',access_token:'t'},{cases}));
  assert.throws(() => evaluationPayload(null,{cases:'[{}]'}));
});
test('label source survives payload without interpretation as real research', () => {
  const value=evaluationPayload({session_id:'s',access_token:'private'},{cases:'[{"target_item_id":"x"}]',
    name:'synthetic',origin:'synthetic',source:'not quality evidence'});
  assert.equal(value.label_origin,'synthetic'); assert.equal(value.source_description,'not quality evidence');
  assert(!JSON.stringify(value).includes('private'));
});
test('malformed stored operations never restore replay paths or foreign inputs', () => {
  const uuid='11111111-1111-4111-8111-111111111111';
  const session={session_id:uuid};
  assert(validPending({kind:'replay',key:uuid,body:{original:uuid}},session));
  assert(!validPending({kind:'replay',key:uuid,body:{original:'../../admin/recover'}},session));
  assert(!validPending({kind:'submit',key:uuid,body:{session_id:'other',cases:[{}]}},session));
  assert(!validPending({kind:'submit',key:uuid,body:{session_id:uuid,cases:[]}},session));
});
test('comparison display keeps identity and ranking without dumping the full catalog', () => {
  const value=comparisonView({comparison_id:original,bundle_id:'bundle',requested_k:10,
    input_snapshot:{eligible_items:['catalog-1','catalog-2'],history:['seed']},strategies:[]});
  assert.equal(value.eligible_item_count,2);assert.equal(value.comparison_id,original);
  assert(!JSON.stringify(value).includes('catalog-1'));
});
