const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'inline application script exists');

function page(fileReplies, catalogFetch = null, catalogItems = [], fileStatusFetch = null) {
  const elements = new Map();
  const requests = [];
  let interval = null;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', value: '', files: [], children: [],
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); },
    });
    return elements.get(id);
  };
  let nextId = 1;
  const context = {
    document: {getElementById: element, createElement: () => element(Symbol())},
    crypto: {randomUUID: () => `batch-${nextId++}`},
    setInterval: callback => { interval = callback; return 1; },
    clearInterval: () => { interval = null; },
    fetch: async (url, options) => {
      requests.push({url, options});
      if (url === '/api/v1/system') return {ok: true, json: async () => ({
        version: 'test', capabilities: {persistence: true, catalog_publication: true},
      })};
      if (url.startsWith('/api/v1/items?')) {
        const query = new URL(url, 'http://test').searchParams;
        const offset = Number(query.get('offset'));
        const limit = Number(query.get('limit'));
        return {ok: true, json: async () => catalogItems.slice(offset, offset + limit)};
      }
      if (url.startsWith('/api/v1/admin/catalog/file-import-jobs/') && !options?.method)
        return fileStatusFetch ? fileStatusFetch(url) :
          {ok: false, status: 404, json: async () => ({error: {code: 'file_job_not_found'}})};
      if (catalogFetch && url.startsWith('/api/v1/admin/catalog/') &&
          url !== '/api/v1/admin/catalog/file-import-jobs') return catalogFetch(url, options);
      if (catalogFetch && url === '/api/v1/admin/publication') return catalogFetch(url, options);
      assert.equal(url, '/api/v1/admin/catalog/file-import-jobs');
      const reply = fileReplies.shift();
      if (reply instanceof Error) throw reply;
      return reply;
    },
  };
  vm.runInNewContext(script, context, {filename: 'web/index.html'});
  return {element, requests, tick: async () => { if (interval) await interval(); }};
}

test('catalog workbench paginates beyond the first hundred items', async () => {
  const items = Array.from({length: 103}, (_, index) => ({
    item_id: `item-${String(index).padStart(3, '0')}`,
    title: `Item ${index}`, category: 'test', is_active: true,
  }));
  const {element, requests} = page([], null, items);
  await element('refresh-items').onclick();
  assert.match(element('items-page').textContent, /第 1–20 件/);
  assert.equal(element('items').children.length, 20);
  for (let index = 0; index < 5; index++) await element('items-next').onclick();
  assert.match(element('items-page').textContent, /第 101–103 件/);
  assert.equal(element('items').children.length, 3);
  assert.equal(element('items-next').disabled, true);
  assert.equal(requests.filter(request => request.url.startsWith('/api/v1/items?')).length, 6);
  await element('items-previous').onclick();
  assert.match(element('items-page').textContent, /第 81–100 件/);
});

test('file upload uses the file body and reports imported, not published', async () => {
  const response = {ok: true, json: async () => ({batch_id: 'batch-1', status: 'imported', item_count: 2,
    attempts: 1, row_errors: [], replayed: false})};
  const {element, requests} = page([response]);
  const file = {name: 'catalog.csv', size: 60};
  element('admin-token').value = 'test-admin';
  element('catalog-file').files = [file];
  await element('import-file').onclick();
  const upload = requests.find(request => request.url.endsWith('/file-import-jobs'));
  assert.equal(upload.options.body, file);
  assert.equal(upload.options.headers['Content-Type'], 'text/csv');
  assert.equal(upload.options.headers['X-Admin-Token'], 'test-admin');
  assert.equal(upload.options.headers['X-Batch-Id'], 'batch-1');
  assert.match(element('import-status').textContent, /已导入 2 件；尚未生成内容索引或发布/);
  assert.equal(element('catalog-file').value, '');
});

test('queued file validation is polled and remains separate from publication', async () => {
  let reads = 0;
  const {element, tick} = page([
    {ok: true, json: async () => ({batch_id: 'batch-1', status: 'queued', attempts: 0,
      row_errors: []})},
  ], null, [], () => {
    reads += 1;
    return {ok: true, json: async () => ({batch_id: 'batch-1',
      status: reads === 1 ? 'validating' : 'imported', item_count: reads === 1 ? 0 : 2,
      attempts: 1, row_errors: []})};
  });
  element('catalog-file').files = [{name: 'catalog.csv', size: 60}];
  element('publish-catalog').disabled = true;
  await element('import-file').onclick();
  assert.match(element('import-status').textContent, /待校验/);
  assert.equal(element('publish-catalog').disabled, true);
  await tick();
  assert.match(element('import-status').textContent, /校验中/);
  await tick();
  assert.match(element('import-status').textContent, /已导入 2 件；尚未生成内容索引或发布/);
  assert.equal(element('catalog-batch').value, 'batch-1');
  assert.equal(element('publish-catalog').disabled, true);
});

test('row errors remain visible and a corrected file receives a new batch ID', async () => {
  const failure = {ok: true, status: 200, json: async () => ({
    batch_id: 'batch-1', status: 'failed', attempts: 1, error_code: 'invalid_catalog_items',
    row_errors: [{row: 2, field: 'title', reason: 'required'},
           {row: 3, field: 'item_id', reason: 'duplicate'}],
  })};
  const success = {ok: true, json: async () => ({batch_id: 'batch-2', status: 'imported',
    item_count: 2, attempts: 1, row_errors: [], replayed: false})};
  const {element, requests} = page([failure, success]);
  element('catalog-file').files = [{name: 'catalog.JSON', size: 200}];
  await element('import-file').onclick();
  assert.equal(element('import-errors').children.length, 2);
  assert.match(element('import-errors').children[0].textContent, /第 2 行/);
  assert.match(element('import-status').textContent, /校验失败/);
  element('catalog-file').files = [{name: 'corrected.JSON', size: 210}];
  element('catalog-file').onchange();
  await element('import-file').onclick();
  const uploads = requests.filter(request => request.url.endsWith('/file-import-jobs'));
  assert.equal(uploads.length, 2);
  assert.notEqual(uploads[0].options.headers['X-Batch-Id'], uploads[1].options.headers['X-Batch-Id']);
  assert.equal(uploads[0].options.headers['Content-Type'], 'application/json');
  assert.equal(element('import-errors').children.length, 0);
});

test('network retry keeps the same batch ID when the file is unchanged', async () => {
  const {element, requests} = page([
    new Error('network interrupted'),
    {ok: true, json: async () => ({batch_id: 'batch-1', status: 'imported',
      item_count: 1, attempts: 1, row_errors: [], replayed: true})},
  ]);
  element('catalog-file').files = [{name: 'catalog.csv', size: 60}];
  await element('import-file').onclick();
  assert.match(element('message').textContent, /network interrupted/);
  await element('import-file').onclick();
  const uploads = requests.filter(request => request.url.endsWith('/file-import-jobs'));
  assert.equal(uploads.length, 2);
  assert.equal(uploads[0].options.headers['X-Batch-Id'], uploads[1].options.headers['X-Batch-Id']);
  assert.match(element('import-status').textContent, /已导入 1 件/);
});

test('oversized file never sends a request', async () => {
  const {element, requests} = page([]);
  element('catalog-file').files = [{name: 'too-large.csv', size: 20_000_001}];
  await element('import-file').onclick();
  assert.equal(requests.filter(request => request.url.endsWith('/file-import-jobs')).length, 0);
  assert.match(element('message').textContent, /20 MB/);
});

test('an imported batch is processed, previewed, then explicitly published', async () => {
  let published = false;
  const {element, requests} = page([
    {ok: true, json: async () => ({batch_id: 'batch-1', status: 'imported',
      item_count: 1, attempts: 1, row_errors: [], replayed: false})},
  ], async (url, options) => {
    if (url === '/api/v1/admin/catalog/imports/batch-1/build-jobs') return {
      ok: true, json: async () => ({build_id: 'batch-2', status: 'ready'}),
    };
    if (url === '/api/v1/admin/catalog/builds/batch-2') return {
      ok: true, json: async () => ({
        build_id: 'batch-2', batch_id: 'batch-1', status: 'ready', total_count: 1,
        processed_count: 1, failed_count: 0, attempts: 1, error_code: null,
        publication_status: published ? 'active' : 'ready',
      }),
    };
    if (url.startsWith('/api/v1/admin/catalog/builds/batch-2/items?')) return {
      ok: true, json: async () => ({build_id: 'batch-2', total_count: 1, offset: 0,
        items: [{item_id: 'cold', title: 'Cold item', category: 'test', is_active: true,
          currently_recommendable: published, ready_for_publication: true}]}),
    };
    if (url === '/api/v1/admin/catalog/builds/batch-2/publish') {
      published = true;
      return {ok: true, json: async () => ({status: 'completed'})};
    }
    if (url === '/api/v1/admin/publication') return {
      ok: true, json: async () => ({active_bundle_id: 'new', admission_open: true, exclusion_version: 0}),
    };
    throw new Error(`unexpected request: ${url}`);
  });
  element('catalog-file').files = [{name: 'new.csv', size: 30}];
  await element('import-file').onclick();
  assert.equal(element('catalog-batch').value, 'batch-1');
  assert.equal(element('publish-catalog').disabled, true);
  await element('process-catalog').onclick();
  assert.equal(element('catalog-build').value, 'batch-2');
  assert.equal(element('build-preview').children.length, 2);
  assert.equal(element('publish-catalog').disabled, false);
  assert.equal(requests.filter(request => request.url.endsWith('/publish')).length, 0);
  await element('publish-catalog').onclick();
  assert.equal(requests.filter(request => request.url.endsWith('/publish')).length, 1);
  assert.equal(element('publish-catalog').disabled, true);
  assert.match(element('build-status').textContent, /当前已发布/);
});

test('queued build does not publish until the worker finishes and admin confirms', async () => {
  let finished = false;
  const {element, tick, requests} = page([], async url => {
    if (url === '/api/v1/admin/catalog/imports/batch-1/build-jobs') return {
      ok: true, json: async () => ({build_id: 'batch-1', status: 'queued'}),
    };
    if (url === '/api/v1/admin/catalog/builds/batch-1') return {
      ok: true, json: async () => ({
        build_id: 'batch-1', batch_id: 'batch-1', status: finished ? 'ready' : 'queued',
        total_count: 1, processed_count: finished ? 1 : 0, failed_count: 0,
        attempts: 1, error_code: null, publication_status: finished ? 'ready' : null,
      }),
    };
    if (url.startsWith('/api/v1/admin/catalog/builds/batch-1/items?')) return {
      ok: true, json: async () => ({build_id: 'batch-1', total_count: 1, offset: 0,
        items: [{item_id: 'cold', title: 'Cold', category: 'test', is_active: true,
          currently_recommendable: false, ready_for_publication: true}]}),
    };
    throw new Error(`unexpected request: ${url}`);
  });
  element('catalog-batch').value = 'batch-1';
  await element('process-catalog').onclick();
  assert.match(element('build-status').textContent, /排队中/);
  assert.equal(element('publish-catalog').disabled, true);
  assert.equal(requests.filter(request => request.url.endsWith('/items?offset=0&limit=20')).length, 0);
  finished = true;
  await tick();
  assert.match(element('build-status').textContent, /待确认发布/);
  assert.equal(element('publish-catalog').disabled, false);
  assert.equal(requests.filter(request => request.url.endsWith('/publish')).length, 0);
});

test('saved import ID restores its latest build after reopening the page', async () => {
  const {element} = page([], async url => {
    if (url === '/api/v1/admin/catalog/imports/batch-1') return {
      ok: true, json: async () => ({batch_id: 'batch-1', item_count: 2,
        snapshot_available: true, latest_build: {build_id: 'build-1', status: 'ready'}}),
    };
    if (url === '/api/v1/admin/catalog/builds/build-1') return {
      ok: true, json: async () => ({build_id: 'build-1', batch_id: 'batch-1', status: 'ready',
        total_count: 2, processed_count: 2, failed_count: 0, attempts: 1,
        error_code: null, publication_status: 'ready'}),
    };
    if (url.startsWith('/api/v1/admin/catalog/builds/build-1/items?')) return {
      ok: true, json: async () => ({build_id: 'build-1', total_count: 2, offset: 0, items: []}),
    };
    throw new Error(`unexpected request: ${url}`);
  });
  element('admin-token').value = 'test-admin';
  element('catalog-batch').value = 'batch-1';
  await element('lookup-import').onclick();
  assert.match(element('import-status').textContent, /已导入 2 件/);
  assert.equal(element('catalog-build').value, 'build-1');
  assert.match(element('build-status').textContent, /待确认发布/);
  assert.equal(element('publish-catalog').disabled, false);
});

test('restored active import is labelled published, not pending confirmation', async () => {
  const {element} = page([], async url => {
    if (url === '/api/v1/admin/catalog/imports/batch-1') return {
      ok: true, json: async () => ({batch_id: 'batch-1', item_count: 1,
        snapshot_available: true,
        latest_build: {build_id: 'build-1', status: 'ready', publication_status: 'active'}}),
    };
    if (url === '/api/v1/admin/catalog/builds/build-1') return {
      ok: true, json: async () => ({build_id: 'build-1', batch_id: 'batch-1', status: 'ready',
        total_count: 1, processed_count: 1, failed_count: 0, attempts: 1,
        error_code: null, publication_status: 'active'}),
    };
    if (url.startsWith('/api/v1/admin/catalog/builds/build-1/items?')) return {
      ok: true, json: async () => ({build_id: 'build-1', total_count: 1, offset: 0, items: []}),
    };
    throw new Error(`unexpected request: ${url}`);
  });
  element('catalog-batch').value = 'batch-1';
  await element('lookup-import').onclick();
  assert.match(element('import-status').textContent, /最近构建已发布/);
  assert.match(element('build-status').textContent, /当前已发布/);
  assert.match(element('build-preview').children[0].textContent, /当前活动版本/);
  assert.equal(element('publish-catalog').disabled, true);
});

test('restored retired import is labelled superseded, not awaiting publication', async () => {
  const {element} = page([], async url => {
    if (url === '/api/v1/admin/catalog/imports/batch-1') return {
      ok: true, json: async () => ({batch_id: 'batch-1', item_count: 1,
        snapshot_available: true,
        latest_build: {build_id: 'build-1', status: 'ready', publication_status: 'retired'}}),
    };
    if (url === '/api/v1/admin/catalog/builds/build-1') return {
      ok: true, json: async () => ({build_id: 'build-1', batch_id: 'batch-1', status: 'ready',
        total_count: 1, processed_count: 1, failed_count: 0, attempts: 1,
        error_code: null, publication_status: 'retired'}),
    };
    if (url.startsWith('/api/v1/admin/catalog/builds/build-1/items?')) return {
      ok: true, json: async () => ({build_id: 'build-1', total_count: 1, offset: 0, items: []}),
    };
    throw new Error(`unexpected request: ${url}`);
  });
  element('catalog-batch').value = 'batch-1';
  await element('lookup-import').onclick();
  assert.match(element('import-status').textContent, /已被新版本替代/);
  assert.match(element('build-status').textContent, /已被新版本替代/);
  assert.match(element('build-preview').children[0].textContent, /此版本已被替代/);
  assert.equal(element('publish-catalog').disabled, true);
});

test('a failed build retains its identifier for a retry', async () => {
  let attempts = 0;
  const {element, requests} = page([], async (url) => {
    if (url === '/api/v1/admin/catalog/imports/batch-1/build-jobs') {
      attempts += 1;
      return attempts === 1
        ? {ok: false, status: 422, json: async () => ({error: {code: 'catalog_build_failed', message: 'build failed'}})}
        : {ok: true, json: async () => ({status: 'ready'})};
    }
    if (url === '/api/v1/admin/catalog/builds/batch-1') return {
      ok: true, json: async () => ({
        build_id: 'batch-1', batch_id: 'batch-1', status: attempts === 1 ? 'failed' : 'ready',
        total_count: 1, processed_count: attempts === 1 ? 0 : 1,
        failed_count: attempts === 1 ? 1 : 0, attempts, error_code: attempts === 1 ? 'catalog_build_failed' : null,
        publication_status: attempts === 1 ? null : 'ready',
      }),
    };
    if (url.startsWith('/api/v1/admin/catalog/builds/batch-1/items?')) return {
      ok: true, json: async () => ({build_id: 'batch-1', total_count: 1, offset: 0, items: []}),
    };
    throw new Error(`unexpected request: ${url}`);
  });
  element('catalog-batch').value = 'batch-1';
  await element('process-catalog').onclick();
  assert.match(element('build-status').textContent, /处理失败/);
  assert.equal(element('catalog-build').value, 'batch-1');
  await element('process-catalog').onclick();
  assert.equal(requests.filter(request => request.url.endsWith('/batch-1/build-jobs')).length, 2);
  assert.equal(element('catalog-build').value, 'batch-1');
  assert.equal(element('publish-catalog').disabled, false);
});

test('lost publish response is reconciled from durable status', async () => {
  let active = false;
  const {element} = page([], async (url) => {
    if (url === '/api/v1/admin/catalog/builds/build-1') return {
      ok: true, json: async () => ({
        build_id: 'build-1', batch_id: 'batch-1', status: 'ready', total_count: 1,
        processed_count: 1, failed_count: 0, attempts: 1, error_code: null,
        publication_status: active ? 'active' : 'ready',
      }),
    };
    if (url.startsWith('/api/v1/admin/catalog/builds/build-1/items?')) return {
      ok: true, json: async () => ({build_id: 'build-1', total_count: 1, offset: 0, items: []}),
    };
    if (url === '/api/v1/admin/catalog/builds/build-1/publish') {
      active = true;
      throw new Error('network interrupted');
    }
    if (url === '/api/v1/admin/publication') return {
      ok: true, json: async () => ({active_bundle_id: 'new', admission_open: true, exclusion_version: 0}),
    };
    throw new Error(`unexpected request: ${url}`);
  });
  element('catalog-build').value = 'build-1';
  await element('refresh-build').onclick();
  assert.equal(element('publish-catalog').disabled, false);
  await element('publish-catalog').onclick();
  assert.match(element('message').textContent, /状态已核实/);
  assert.equal(element('publish-catalog').disabled, true);
});
