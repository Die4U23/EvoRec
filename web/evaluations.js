'use strict';

// Pure presentation helpers are shared with negative Node tests.
function evaluationPayload(session, fields) {
  const cases = JSON.parse(fields.cases);
  if (!Array.isArray(cases) || !cases.length || cases.length > 100) throw new Error('样本必须是 1–100 项的 JSON 数组');
  if (!session?.session_id || !session.access_token) throw new Error('请先连接会话');
  const k=fields.k===undefined ? 20 : Number(fields.k);
  if (!Number.isInteger(k) || k<1 || k>50) throw new Error('评估 K 必须是 1–50 的整数');
  return {session_id: session.session_id, k, dataset_name: fields.name, label_origin: fields.origin,
    source_description: fields.source, cases};
}
function metricText(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(6) : '未知（未评估）';
}
function evaluationRows(report) {
  const rows = [];
  for (const [group, methods] of Object.entries(report.groups)) {
    for (const [method, entry] of Object.entries(methods)) {
      rows.push([group, method, entry.cases, entry.evaluated_cases, entry.failed_cases,
        entry.fallback_cases, JSON.stringify(entry.actual_strategy_counts),
        ...Object.entries(entry.metrics).map(([key,value]) => `${key}: ${metricText(value)}`)]);
    }
  }
  return rows;
}
function comparisonView(saved) {
  return {comparison_id:saved.comparison_id,snapshot_at:saved.snapshot_at,bundle_id:saved.bundle_id,
    model_version:saved.model_version,history_version:saved.history_version,requested_k:saved.requested_k,
    eligible_item_count:saved.input_snapshot.eligible_items.length,
    history:saved.input_snapshot.history,strategies:saved.strategies};
}
function pendingOperation(prior, kind, body, uuid) {
  if (prior) return prior; // Uncertain responses never change the key, payload or action.
  return {kind, key:uuid(), body};
}
function validPending(value, session) {
  const uuid=value => typeof value==='string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
  return !!value && uuid(value.key) && (value.kind==='replay' ? uuid(value.body?.original)
    : value.kind==='submit' && value.body?.session_id===session?.session_id && uuid(session?.session_id)
      && Array.isArray(value.body.cases) && value.body.cases.length>=1 && value.body.cases.length<=100);
}

if (typeof module !== 'undefined') module.exports = {evaluationPayload, metricText, evaluationRows, pendingOperation, validPending, comparisonView};
if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  let session = null, pending = null, selected = null, offset = 0, hasMore = false, timer = null, busy = false;
  let reportValue = null, pendingCapture = null;
  const say = text => { el('message').textContent = text; };
  const terminal = status => ['completed','cancelled','failed'].includes(status);
  function remember() {
    sessionStorage.setItem('evorec.evaluation',JSON.stringify({session,pending,selected,pendingCapture}));
  }
  try {
    const saved = JSON.parse(sessionStorage.getItem('evorec.evaluation'));
    const previous = JSON.parse(sessionStorage.getItem('evorec.comparison'));
    session = saved?.session || previous?.session || null;
    pending = validPending(saved?.pending,session) ? saved.pending : null;
    selected = typeof saved?.selected==='string' && /^[0-9a-f-]{36}$/i.test(saved.selected) ? saved.selected : null;
    pendingCapture = saved?.pendingCapture?.body?.session_id===session?.session_id
      && /^[0-9a-f-]{36}$/i.test(saved?.pendingCapture?.key || '') ? saved.pendingCapture : null;
  } catch (_) { say('标签页存储不可用，请勿刷新未确认操作。'); }
  function showSession() { el('session').textContent = session ? `会话 ${session.session_id}；历史版本 ${session.history_version}` : '未连接会话'; }
  function clearReport() { reportValue=null; el('metrics').replaceChildren(); el('report-view').textContent='暂无当前任务报告'; }
  showSession();
  async function api(path, options = {}) {
    const response = await fetch(path,{...options,headers:{'Content-Type':'application/json',
      'X-Session-Token':session?.access_token || '',...options.headers},cache:'no-store'});
    const body = await response.json();
    if (!response.ok) { const error = new Error(`${body.error?.code || response.status}: ${body.error?.message || '请求失败'}`); error.status = response.status; throw error; }
    return body;
  }
  function table(container, headers, rows) {
    const target = el(container); target.replaceChildren();
    const table = document.createElement('table');
    const head = document.createElement('tr');
    for (const value of headers) { const cell=document.createElement('th'); cell.textContent=value; head.append(cell); }
    table.append(head);
    for (const values of rows) {
      const row=document.createElement('tr');
      for (const value of values) { const cell=document.createElement('td'); if (value instanceof Node) cell.append(value); else cell.textContent=String(value); row.append(cell); }
      table.append(row);
    }
    target.append(table);
  }
  async function taskList() {
    if (!session) throw new Error('请先连接会话');
    const page = await api(`/api/v1/evaluation-jobs?session_id=${session.session_id}&offset=${offset}&limit=20`);
    hasMore=page.has_more; el('previous').disabled=offset===0; el('next').disabled=!hasMore;
    table('job-list',['任务','数据集／来源','状态','进度','尝试'],page.items.map(job => {
      const button=document.createElement('button'); button.textContent=job.job_id;
      button.onclick=() => guarded(async () => { selected=job.job_id; clearReport(); remember(); await progress(); });
      return [button,`${job.dataset_name} / ${job.label_origin}`,job.status,`${job.completed_cases}/${job.total_cases}`,job.attempts];
    }));
  }
  function queryPath(suffix='') {
    if (!session || !selected) throw new Error('请先选择任务');
    return `/api/v1/evaluation-jobs/${selected}${suffix}?session_id=${session.session_id}`;
  }
  async function progress() {
    clearTimeout(timer);
    const job=await api(queryPath()); el('job-status').textContent=JSON.stringify(job,null,2);
    if (!terminal(job.status)) timer=setTimeout(() => guarded(progress),1500);
    return job;
  }
  async function submitOperation(kind, body) {
    if (pending && pending.kind !== kind) throw new Error('另一笔操作尚未确认，先重试原操作');
    pending=pendingOperation(pending,kind,body,() => crypto.randomUUID()); remember();
    const operation=pending;
    const path=operation.kind==='submit' ? '/api/v1/evaluation-jobs' : `/api/v1/evaluation-jobs/${operation.body.original}/replay?session_id=${session.session_id}`;
    let job;
    try {
      job=await api(path,{method:'POST',headers:{'Idempotency-Key':operation.key},
        ...(operation.kind==='submit' ? {body:JSON.stringify(operation.body)} : {})});
    } catch(error) {
      if(error.status===422) { pending=null; remember(); } // Known rejection: edit, then explicitly submit again.
      throw error;
    }
    if (job.job_id!==operation.key || job.session_id!==session.session_id) throw new Error('返回任务身份不一致，保留原操作');
    selected=job.job_id; pending=null; clearReport(); remember();
    say('任务已持久提交。'); await progress(); await taskList();
  }
  async function report() {
    reportValue=await api(queryPath('/report'));
    el('report-view').textContent=JSON.stringify(reportValue,null,2);
    const metricNames=Object.keys(Object.values(reportValue.groups.all_cases)[0].metrics);
    table('metrics',['分组','请求策略','样本','已评估','失败','回退','实际策略计数',...metricNames],evaluationRows(reportValue));
    say('指标分母为成功样本；失败另列。标注真实性未自动核验，排序耗时不是 HTTP 时延。');
  }
  async function guarded(action) {
    if (busy) return;
    busy=true;
    for (const button of document.querySelectorAll('button')) button.disabled=true;
    try { await action(); } catch (error) { say(error.message); }
    finally { busy=false; for (const button of document.querySelectorAll('button')) button.disabled=false;
      el('previous').disabled=offset===0; el('next').disabled=!hasMore; }
  }
  const actions = {
    'create-session': async () => {
      if (pending || pendingCapture) throw new Error('先确认或放弃本地未确认操作，再切换会话');
      clearTimeout(timer); session=await api('/api/v1/sessions',{method:'POST',body:JSON.stringify({profile_id:el('profile').value})});
      selected=null; offset=0; clearReport(); el('job-list').replaceChildren(); el('job-status').textContent='尚未选择';
      el('comparison-list').textContent='尚未查询'; remember(); showSession(); say('创建成功，尚未保存比较快照。');
    },
    capture: async () => {
      if (!session) throw new Error('请先连接会话');
      if (!pendingCapture) {
        const fresh=await api(`/api/v1/sessions/${session.session_id}`); session={...session,...fresh}; showSession();
        pendingCapture={key:crypto.randomUUID(),body:{session_id:session.session_id,expected_history_version:session.history_version,
          strategies:['popular','dense'],k:10}}; remember();
      }
      const saved=await api('/api/v1/strategy-comparisons',{method:'POST',headers:{'Idempotency-Key':pendingCapture.key},body:JSON.stringify(pendingCapture.body)});
      if (saved.comparison_id!==pendingCapture.key) throw new Error('比较身份不一致，保留原键');
      pendingCapture=null; remember(); el('comparison-list').textContent=JSON.stringify(comparisonView(saved),null,2);
      say('比较快照已保存。目标必须从独立标注获得；不要用这里的结果生成真实测试答案。');
    },
    comparisons: async () => { if(!session) throw new Error('请先连接会话'); el('comparison-list').textContent=JSON.stringify(await api(`/api/v1/strategy-comparisons?session_id=${session.session_id}&limit=50`),null,2); },
    submit: async () => submitOperation('submit',pending?.body || evaluationPayload(session,{cases:el('cases').value,
      name:el('dataset-name').value,k:el('evaluation-k').value,origin:el('label-origin').value,source:el('source-description').value})),
    abandon: async () => { if(!confirm('只放弃本地未确认记录，不会取消服务端任务。确定继续？')) return; pending=null; pendingCapture=null; remember(); say('本地记录已放弃；可查询任务列表核对服务端。'); },
    jobs:taskList,
    previous:async () => {offset=Math.max(0,offset-20);await taskList();},
    next:async () => {if(hasMore){offset+=20;await taskList();}},
    poll:progress,
    cancel:async () => {await api(queryPath('/cancel'),{method:'POST'});await progress();},
    replay:async () => { if(!selected) throw new Error('请先选择原任务'); await submitOperation('replay',{original:selected}); },
    report,
    export:async () => {await report();const blob=new Blob([JSON.stringify(reportValue,null,2)],{type:'application/json'});
      const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`evaluation-${selected}.json`;
      link.click();URL.revokeObjectURL(url);}
  };
  for (const [id,action] of Object.entries(actions)) el(id).onclick=() => guarded(action);
  window.addEventListener('beforeunload',() => clearTimeout(timer));
  // Never submit, cancel or replay automatically after a refresh.
}
