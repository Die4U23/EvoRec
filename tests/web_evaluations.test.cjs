const {test} = require('node:test');
const assert = require('node:assert/strict');
const {evaluationPayload,metricText,evaluationRows,pendingOperation,validPending} = require('../web/evaluations.js');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const script=fs.readFileSync(path.join(__dirname,'../web/evaluations.js'),'utf8');
const session={session_id:'11111111-1111-4111-8111-111111111111',access_token:'fixture-token',history_version:0};
const original='22222222-2222-4222-8222-222222222222';
const ok=value => ({ok:true,status:200,json:async () => value});
function page(fetchReply,storage=new Map()) {
  const elements=new Map(),requests=[];let timeout=null,counter=0;
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
    setTimeout:callback=>{timeout=callback;return 1;},clearTimeout:()=>{timeout=null;},
    confirm:()=>true,fetch:async (url,options)=>{requests.push({url,options});return fetchReply(url,options);}};
  vm.runInNewContext(script,context,{filename:'web/evaluations.js'});
  element('dataset-name').value='synthetic';element('label-origin').value='synthetic';
  element('source-description').value='synthetic only';element('evaluation-k').value='20';
  element('cases').value=JSON.stringify([{comparison_id:original,target_item_id:'independent',target_at:'2030-01-01T00:00:00Z'}]);
  return {element,requests,tick:async()=>{if(timeout)await timeout();}};
}

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
