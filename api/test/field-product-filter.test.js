'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const AssetProductFilters = require('../../webapp/asset-product-filters');
const web = name => fs.readFileSync(path.join(__dirname, '../../webapp', name), 'utf8');
const source = web('field.js');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

function setup() {
  const chip = { dataset: { chip: 'assets' }, disabled: false, pressed: '',
    setAttribute(name, value) { if (name === 'aria-pressed') this.pressed = value; },
    classList: { toggle() {} } };
  const nodes = { assetChip: chip, status: { textContent: '' } };
  const books = { acv: { acv: 5 }, lcv: { lcv: 4 }, midcap: { midcap: 3 },
    mf: { mf: 2 }, zero: { acv: 0, mf: 0 } };
  const context = { CHIPS: { direct: false, crm: false, assets: false, ranked: false },
    BOOK_STATE: 'ready', COL: { crd: 0 },
    bookFor: id => books[id] || null, AssetProductFilters, $: id => nodes[id],
    isDirect: () => true, isCRM: () => true, PAGE: 200, limit: 20,
    ROWS: [], render: () => {} };
  vm.createContext(context);
  vm.runInContext(between('function syncAssetChip(){', 'async function loadExtras(){')
    + between('function hasAssets(r){', '/* ---------- rendering')
    + 'this.clickChip = function(e) {'
    + between('  const chip = e.target.closest("[data-chip]");',
      '  const open = e.target.closest("[data-open]");') + '};', context);
  return { context, chip, nodes,
    click() { context.clickChip({ target: { closest: () => chip } }); } };
}

test('Field EIC Assets uses the approved book across products and shows prospects when off', () => {
  const { context: c, chip, click } = setup();
  assert.equal(c.passes(['prospect']), true);
  click();
  assert.equal(chip.pressed, 'true');
  for (const id of ['acv', 'lcv', 'midcap', 'mf', 'zero'])
    assert.equal(c.passes([id]), true, id);
  assert.equal(c.passes(['prospect']), false);
  click();
  assert.equal(chip.pressed, 'false');
  assert.equal(c.passes(['prospect']), true);
});

test('Field disables EIC Assets while book data is loading or unavailable', () => {
  const { context: c, chip, click } = setup();
  c.BOOK_STATE = 'loading';
  c.syncAssetChip();
  assert.equal(chip.disabled, true);
  assert.equal(chip.pressed, 'false');
  assert.match(chip.title, /Loading/);
  click();
  assert.equal(c.CHIPS.assets, false);
  assert.equal(c.passes(['prospect']), true);
  c.BOOK_STATE = 'failed';
  c.syncAssetChip();
  assert.equal(chip.disabled, true);
  assert.match(chip.title, /unavailable/);
  c.BOOK_STATE = 'ready';
  c.syncAssetChip();
  assert.equal(chip.disabled, false);
});

test('Field failed extras clear an active EIC Assets filter', async () => {
  const { context: c, chip, click, nodes } = setup();
  click();
  let assetCardRefreshes = 0;
  Object.assign(c, { dataUrl: x => x, fetch: async () => ({ ok: false }), BOOK: null, TERR: null,
    ActAddresses: { refreshAssetDisplay: () => { assetCardRefreshes++; } } });
  vm.runInContext(between('async function loadExtras(){', 'const bookFor ='), c);
  await c.loadExtras();
  assert.equal(assetCardRefreshes, 1);
  assert.equal(c.CHIPS.assets, false);
  assert.equal(c.BOOK_STATE, 'failed');
  assert.equal(chip.pressed, 'false');
  assert.equal(chip.disabled, true);
  assert.match(nodes.status.textContent, /EIC Assets filter was turned off/);
});

test('Field shows the four compact filters in the same group', () => {
  const html = web('field.html');
  const group = html.match(/<div id="chips"[^>]*>([\s\S]*?)<\/div>/)[1];
  assert.deepEqual([...group.matchAll(/data-chip="([^"]+)"[^>]*>([^<]+)<\/button>/g)]
    .map(([, key, label]) => [key, label]), [
    ['direct', 'Direct line'], ['crm', 'In ACT!'], ['assets', 'EIC Assets'], ['ranked', 'Ranked'],
  ]);
  assert.doesNotMatch(html, /data-asset-product|assetProductsHelp/);
});
