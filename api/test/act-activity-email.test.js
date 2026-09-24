'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const artifact = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', 'shared', 'act_contacts.json'), 'utf8'));
const pair = Object.entries(artifact.activity_contacts || {}).find(
  ([crd]) => !artifact.contacts[crd]);
assert.ok(pair, 'test needs an exact-email route without an approved CRD route');
const [crd, route] = pair;

test('new ACT email and exact-email call routes are dark unless explicitly enabled', async () => {
  const keys = ['ACT_SYNC', 'ACT_USER', 'ACT_PASSWORD', 'ACT_DB',
    'ACT_ACTIVITY_EMAIL_ROUTE', 'ACT_EMAIL_HISTORY_SYNC', 'ACT_ONLY_CRD'];
  const prior = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  Object.assign(process.env, { ACT_SYNC: '1', ACT_USER: 'integration',
    ACT_PASSWORD: 'test-only', ACT_DB: 'test', ACT_ONLY_CRD: '' });
  delete process.env.ACT_ACTIVITY_EMAIL_ROUTE;
  delete process.env.ACT_EMAIL_HISTORY_SYNC;
  delete require.cache[require.resolve('../shared/act')];
  const act = require('../shared/act');
  try {
    assert.equal(await act.logEmail('rep@eicatlanta.com',
      { crd, email: route.email, messageId: 'sent-1' }), 'off');
    assert.equal(await act.logCall({ name: 'rep@eicatlanta.com' },
      { crd, email: route.email, kind: 'outcome',
        disposition: 'attempted' }), 'no-contact');
  } finally {
    for (const [key, value] of Object.entries(prior)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    delete require.cache[require.resolve('../shared/act')];
  }
});

test('exact unique email mirrors confirmed email and call without approving CRD identity', async () => {
  const prior = {};
  for (const key of ['ACT_SYNC', 'ACT_USER', 'ACT_PASSWORD', 'ACT_DB',
                     'ACT_BASE', 'ACT_ONLY_CRD', 'ACT_ACTIVITY_EMAIL_ROUTE',
                     'ACT_ACTIVITY_ONLY_CRD', 'ACT_EMAIL_HISTORY_SYNC',
                     'ACT_EMAIL_HISTORY_ONLY_CRD']) prior[key] = process.env[key];
  const oldFetch = global.fetch;
  const repSettings = require('../shared/store');
  const oldGetSettings = repSettings.getSettings;
  repSettings.getSettings = async () => ({ actEmailWrite: '1' });
  Object.assign(process.env, { ACT_SYNC: '1', ACT_USER: 'integration',
    ACT_PASSWORD: 'test-only', ACT_DB: 'test', ACT_BASE: 'https://act.test',
    ACT_ONLY_CRD: '', ACT_ACTIVITY_EMAIL_ROUTE: '1',
    ACT_ACTIVITY_ONLY_CRD: crd, ACT_EMAIL_HISTORY_SYNC: '1',
    ACT_EMAIL_HISTORY_ONLY_CRD: crd });
  delete require.cache[require.resolve('../shared/act')];
  const act = require('../shared/act');
  const history = [];
  let posts = 0;
  let lastTask;
  global.fetch = async (url, options = {}) => {
    const endpoint = String(url).replace('https://act.test/', '');
    const response = (value) => new Response(JSON.stringify(value), { status: 200 });
    if (endpoint === 'authorize')
      return new Response('a-very-long-test-token-1234567890', { status: 200 });
    if (endpoint === 'api/users')
      return response([{ id: 'rep-id', email: 'rep@eicatlanta.com', displayName: 'Rep' }]);
    if (endpoint === 'api/contacts/' + route.id)
      return response({ id: route.id, emailAddress: route.email });
    if (endpoint === 'api/contacts/' + route.id + '/history')
      return response(history);
    if (endpoint === 'api/organizers/rep-id/tasks' && options.method === 'POST') {
      posts++;
      lastTask = JSON.parse(options.body);
      return response({ id: 'task-' + posts });
    }
    if (endpoint.startsWith('api/tasks/') && endpoint.endsWith('/clear')) {
      const body = JSON.parse(options.body);
      history.push({ id: 'history-' + posts, subject: lastTask.subject,
        details: lastTask.details, recordManager: 'Rep', created: new Date().toISOString(),
        historyType: { id: body.result.id } });
      return response({});
    }
    throw new Error('Unexpected mocked ACT request: ' + options.method + ' ' + endpoint);
  };
  try {
    const event = { userId: 'rep-user-id', crd, email: route.email, messageId: 'graph-sent-1',
      sentAt: '2026-09-23T12:00:00Z', subject: 'Materials' };
    assert.equal(await act.logEmail('rep@eicatlanta.com',
      { ...event, email: 'coworker@eicatlanta.com' }), 'internal-recipient');
    repSettings.getSettings = async () => ({});
    assert.equal(await act.logEmail('rep@eicatlanta.com', event), 'user-opted-out');
    repSettings.getSettings = async () => ({ actEmailWrite: '1' });
    assert.equal(await act.logEmail('rep@eicatlanta.com', event), 'written');
    assert.equal(history[0].historyType.id, 16);
    assert.equal(await act.logEmail('rep@eicatlanta.com', event), 'written');
    assert.equal(posts, 1, 'retry must find the marker, not create a duplicate');
    assert.equal(await act.logEmail('rep@eicatlanta.com',
      { ...event, email: 'wrong@example.com' }), 'no-contact');
    assert.equal(posts, 1);
    assert.equal(await act.logCall({ name: 'rep@eicatlanta.com' },
      { crd, email: route.email, kind: 'outcome', disposition: 'attempted',
        name: 'Selected contact' }), 'written');
    assert.equal(posts, 2);
    assert.equal(history[1].historyType.id, 0);
  } finally {
    global.fetch = oldFetch;
    repSettings.getSettings = oldGetSettings;
    for (const [key, value] of Object.entries(prior)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    delete require.cache[require.resolve('../shared/act')];
  }
});
