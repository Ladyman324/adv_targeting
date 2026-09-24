'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const store = require('../shared/store');
const handler = require('../settings');

const principal = Buffer.from(JSON.stringify({
  userId: 'rep-1', userDetails: 'rep@eicatlanta.com', userRoles: ['authenticated'],
})).toString('base64');
const request = (method, body) => ({ method, body,
  headers: { 'x-ms-client-principal': principal } });
const context = () => ({ log: { error() {} } });

test('ACT email setting is hidden and cannot be enabled while admin gate is off', async () => {
  const prior = process.env.ACT_EMAIL_HISTORY_SYNC;
  const oldGet = store.getSettings, oldPut = store.putSettings;
  let writes = 0;
  delete process.env.ACT_EMAIL_HISTORY_SYNC;
  store.getSettings = async () => ({});
  store.putSettings = async () => { writes++; return { actEmailWrite: '1' }; };
  try {
    const read = context();
    await handler(read, request('GET'));
    assert.equal(JSON.parse(read.res.body).features.actEmailWrite, false);
    const write = context();
    await handler(write, request('PUT', { actEmailWrite: '1' }));
    assert.equal(write.res.status, 403);
    assert.equal(writes, 0);
  } finally {
    store.getSettings = oldGet; store.putSettings = oldPut;
    if (prior === undefined) delete process.env.ACT_EMAIL_HISTORY_SYNC;
    else process.env.ACT_EMAIL_HISTORY_SYNC = prior;
  }
});

test('admin gate permits only explicit on/off values for signed-in sender', async () => {
  const prior = process.env.ACT_EMAIL_HISTORY_SYNC;
  const oldGet = store.getSettings, oldPut = store.putSettings;
  process.env.ACT_EMAIL_HISTORY_SYNC = '1';
  let saved;
  store.getSettings = async () => ({});
  store.putSettings = async (who, patch) => {
    saved = { who, patch }; return patch;
  };
  try {
    const read = context();
    await handler(read, request('GET'));
    assert.equal(JSON.parse(read.res.body).features.actEmailWrite, true);
    const invalid = context();
    await handler(invalid, request('PUT', { actEmailWrite: true }));
    assert.equal(invalid.res.status, 400);
    assert.equal(saved, undefined);
    const write = context();
    await handler(write, request('PUT', { actEmailWrite: '1' }));
    assert.equal(write.res.status, 200);
    assert.equal(saved.who.id, 'rep-1');
    assert.equal(saved.patch.actEmailWrite, '1');
  } finally {
    store.getSettings = oldGet; store.putSettings = oldPut;
    if (prior === undefined) delete process.env.ACT_EMAIL_HISTORY_SYNC;
    else process.env.ACT_EMAIL_HISTORY_SYNC = prior;
  }
});

test('both views show the control only from the server feature flag', () => {
  for (const name of ['app.js', 'field.js']) {
    const source = fs.readFileSync(path.join(__dirname, '..', '..', 'webapp', name), 'utf8');
    assert.match(source, /settingsFeatures\.actEmailWrite/);
    assert.match(source, /setActEmailWrite/);
  }
});
