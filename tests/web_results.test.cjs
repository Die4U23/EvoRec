const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const {webcrypto} = require('node:crypto');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'web/results.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const raw = Object.fromEntries(['results.json', 'uncertainty.json'].map(name =>
  [name, fs.readFileSync(path.join(root, 'docs/experiments/r06-multi-interest', name))]));
const result = JSON.parse(raw['results.json']);
const uncertainty = JSON.parse(raw['uncertainty.json']);
const clone = value => JSON.parse(JSON.stringify(value));
const response = bytes => ({ok:true, arrayBuffer:async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)});

function page(reply = null, cryptoAvailable = true) {
  const elements = new Map(), requests = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {textContent:'', hidden:id === 'evidence', children:[],
      replaceChildren(...values) { this.children = values; },
      append(...values) { this.children.push(...values); }});
    return elements.get(id);
  };
  const context = vm.createContext({document:{getElementById:element, createElement:() => element(Symbol())},
    crypto:cryptoAvailable ? webcrypto : {}, Uint8Array, TextDecoder, AbortSignal,
    fetch:async (url, options) => {
      requests.push({url, options});
      assert.ok(url.startsWith('/app/evidence/r06/'));
      const name = url.split('/').at(-1);
      assert.ok(raw[name], 'only the two explicit evidence files are requested');
      return reply ? reply(name, options) : response(raw[name]);
    }});
  vm.runInContext(script, context);
  return {element, requests, ready:vm.runInContext('startup',context),
    restoreCrypto:() => { context.crypto=webcrypto; },
    parse:(r,u) => { context.__r=r; context.__u=u; return vm.runInContext('evidenceView(__r,__u)',context); }};
}
const rows = (p,id) => p.element(id).children.map(row => row.children.map(cell => cell.textContent));

test('archived metrics are read and rendered; no training, session or recommendation writes', async () => {
  const p = page(); await p.ready;
  assert.equal(p.element('evidence').hidden,false);
  assert.equal(p.requests.length,2);
  for (const request of p.requests) assert.equal(request.options.method,undefined);
  assert.deepEqual(rows(p,'methods').map(row=>row[0]),['CF-blend','A-RRF','B-RRF','A-frozen-s17']);
  for (const row of rows(p,'methods')) {
    const source = result.series.test_results.find(value=>value.name === row[0]).metrics.cohorts;
    assert.equal(row[1], source.all_positive_events['ndcg@10'].toFixed(6));
    assert.equal(row[2], source.model_cold_available['recall@20'].toFixed(6));
  }
  assert.match(p.element('counts').textContent,/12524.*6978/);
  assert.match(p.element('provenance').textContent,/dirty：true.*clean：true.*LICENSE/);
  assert.match(p.element('identity').textContent,/e11840f33a2fec8d.*bf73ce7b/);
});

test('three-seed display agrees with independent raw trial mean and sample standard deviation', async () => {
  const p=page(); await p.ready;
  for (const row of rows(p,'families')) {
    const trials=result.series.test_results.filter(value=>value.name.startsWith(row[0]+'-s'));
    assert.equal(trials.length,3);
    for (const [column,cohort,key] of [[1,'all_positive_events','ndcg@10'],[2,'model_cold_available','recall@20']]) {
      const values=trials.map(value=>value.metrics.cohorts[cohort][key]);
      const mean=values.reduce((a,b)=>a+b,0)/3;
      const std=Math.sqrt(values.reduce((sum,v)=>sum+(v-mean)**2,0)/2);
      assert.equal(row[column],`${mean.toFixed(6)} ± ${std.toFixed(6)}`);
    }
  }
  assert.ok(html.includes('不是置信区间，也不是集成模型'));
});

test('coverage uses candidate inclusion, not ranking hits; no-history failure is visible', async () => {
  const p=page(); await p.ready;
  assert.deepEqual(rows(p,'coverage')[1],['冷目标（商品）','115 / 6978','122 / 6978']);
  assert.deepEqual(rows(p,'coverage')[3],['无历史＋冷目标','0 / 3928','0 / 3928']);
  assert.ok(html.includes('不是 Top20 命中数'));
  assert.match(p.element('conclusion').textContent,/不支持.*跨零.*不根据测试/);
});

test('intervals retain signs, tiny positive endpoint and user/request denominators', async () => {
  const p=page(); await p.ready;
  const intervals=rows(p,'intervals'); assert.equal(intervals.length,5);
  const matched=intervals.find(row=>row[0]==='C-adapted − D-adapted' && row[1]==='整体 NDCG@10');
  assert.equal(matched[3],'[-0.000686012, +0.000000421]');
  assert.equal(matched[4],'12524 / 9373');
  assert.ok(html.includes('不包含训练随机性，未做多重比较校正'));
});

for (const failure of ['http','network','tampered','unavailable-crypto']) {
  test(`${failure} hides all metrics without fabricated defaults and allows retry`, async () => {
    let broken=true;
    const p=page(name=> {
      if (!broken) return response(raw[name]);
      if (failure==='http') return {ok:false,status:404};
      if (failure==='network') throw new Error('network unavailable');
      return response(failure==='tampered' ? Buffer.from(raw[name].toString().replace('completed','fabricated')) : raw[name]);
    }, failure!=='unavailable-crypto');
    await p.ready;
    assert.equal(p.element('evidence').hidden,true);
    for (const id of ['methods','families','coverage','intervals']) assert.equal(rows(p,id).length,0);
    assert.match(p.element('status').textContent,/不可用.*未显示数值/);
    assert.equal(p.element('reload').disabled,false);
    broken=false; p.restoreCrypto(); await p.element('reload').onclick();
    assert.equal(p.element('evidence').hidden,false);
  });
}

for (const mutation of ['protocol','analysis','selected','chronology','nonfinite','missing-interval','wrong-interval-denominator']) {
  test(`schema ${mutation} mismatch is rejected before rendering`, async () => {
    const p=page(); await p.ready;
    const r=clone(result), u=clone(uncertainty);
    if (mutation==='protocol') r.series.protocol_id='other';
    if (mutation==='analysis') u.series_sha256='other';
    if (mutation==='selected') r.series.selected_method='C-adapted-s17';
    if (mutation==='chronology') r.series.selection_finished_at='2099-01-01';
    if (mutation==='nonfinite') r.series.test_results[0].metrics.cohorts.all_positive_events['ndcg@10']=NaN;
    if (mutation==='missing-interval') {r.analysis.intervals.shift(); u.intervals.shift();}
    if (mutation==='wrong-interval-denominator') {r.analysis.intervals[0].requests=1;u.intervals[0].requests=1;}
    assert.throws(()=>p.parse(r,u));
  });
}

test('reload clears a formerly successful table if the next pinned source is unavailable', async () => {
  let broken=false;
  const p=page(name=>broken ? {ok:false,status:503} : response(raw[name]));
  await p.ready; assert.equal(rows(p,'methods').length,4);
  broken=true; await p.element('reload').onclick();
  assert.equal(p.element('evidence').hidden,true);
  assert.equal(rows(p,'methods').length,0);
});

test('a second reload while reading does not create overlapping fetches', async () => {
  const waiting=[];
  const p=page(name=>new Promise(resolve=>waiting.push(()=>resolve(response(raw[name])))));
  assert.equal(p.element('reload').disabled,true);
  await p.element('reload').onclick();
  assert.equal(p.requests.length,2);
  for (const finish of waiting) finish();
  await p.ready;
  assert.equal(p.element('evidence').hidden,false);
  for (const request of p.requests) assert.ok(request.options.signal instanceof AbortSignal);
});
