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

function chip(key) {
  return { dataset: { assetProduct: key }, disabled: false, pressed: '', on: false,
    setAttribute(name, value) { if (name === 'aria-pressed') this.pressed = value; },
    classList: { toggle() {} } };
}
function setup() {
  const chips = ['any', 'acv', 'lcv', 'midcap', 'mf'].map(chip);
  const nodes = { assetProductsHelp: {}, status: { textContent: '' } };
  const books = { acv: { acv: 5 }, lcv: { lcv: 4 }, midcap: { midcap: 3 },
    mf: { mf: 2 }, zero: { acv: 0, mf: 0 }, bad: { acv: -1, mf: 'not a number' } };
  const context = { CHIPS: { direct: false, crm: false, assets: false, ranked: false },
    assetProducts: new Set(), BOOK_STATE: 'ready', COL: { crd: 0 },
    bookFor: id => books[id] || null, AssetProductFilters,
    document: { querySelectorAll: () => chips }, $: id => nodes[id],
    isDirect: () => true, isCRM: () => true, PAGE: 200, limit: 20,
    ROWS: [], render: () => {} };
  vm.createContext(context);
  vm.runInContext(between('function syncAssetChip(){', 'async function loadExtras(){')
    + between('function hasAssets(r){', '/* ---------- rendering')
    + 'this.clickProduct = function(e) {'
    + between('  const product = e.target.closest("[data-asset-product]");',
      '  const chip = e.target.closest("[data-chip]");') + '};', context);
  return { context, chips, nodes,
    click(key) { context.clickProduct({ target: { closest: () => chips.find(c => c.dataset.assetProduct === key) } }); } };
}

test('Field toggles Any EIC exclusively, ORs products, and last deselection includes prospects', () => {
  const { context: c, click } = setup();
  assert.equal(c.passes(['prospect']), true);
  click('any');
  assert.equal(c.passes(['zero']), true, 'Any EIC retains approved zero-value books');
  assert.equal(c.passes(['prospect']), false);
  click('acv');
  assert.deepEqual([...c.assetProducts], ['acv']);
  assert.equal(c.passes(['zero']), false);
  assert.equal(c.passes(['bad']), false);
  click('mf');
  assert.equal(c.passes(['acv']), true);
  assert.equal(c.passes(['mf']), true);
  assert.equal(c.passes(['lcv']), false);
  click('any');
  assert.equal(c.assetProducts.size, 0);
  assert.equal(c.CHIPS.assets, true);
  click('any');
  assert.equal(c.CHIPS.assets, false);
  click('midcap');
  assert.equal(c.passes(['midcap']), true);
  assert.equal(c.passes(['acv']), false);
  click('midcap');
  assert.equal(c.CHIPS.assets, false);
  assert.equal(c.passes(['prospect']), true);
});

test('Field pressed state and loading/failure guards stay honest', () => {
  const { context: c, click, chips, nodes } = setup();
  click('lcv');
  assert.equal(chips.find(x => x.dataset.assetProduct === 'lcv').pressed, 'true');
  assert.equal(chips[0].pressed, 'false');
  c.BOOK_STATE = 'loading';
  c.syncAssetChip();
  assert.ok(chips.every(x => x.disabled && x.pressed === 'false'));
  assert.equal(c.passes(['lcv']), false, 'active filter fails closed until canonical data is ready');
  click('acv');
  assert.deepEqual([...c.assetProducts], ['lcv']);
  assert.match(nodes.status.textContent, /still loading/);
  c.BOOK_STATE = 'failed';
  click('acv');
  assert.match(nodes.assetProductsHelp.textContent, /unavailable/);
});

test('Field failed extras clear both asset selection states and retain visible disclosure', async () => {
  const { context: c, click, nodes } = setup();
  click('acv');
  Object.assign(c, { dataUrl: x => x, fetch: async () => ({ ok: false }), BOOK: null, TERR: null });
  vm.runInContext(between('async function loadExtras(){', 'const bookFor ='), c);
  await c.loadExtras();
  assert.equal(c.CHIPS.assets, false);
  assert.equal(c.assetProducts.size, 0);
  assert.equal(c.BOOK_STATE, 'failed');
  assert.match(nodes.status.textContent, /product filters were turned off/);
});

test('Field markup preserves other filters, explains MCV, and loads helper before Field', () => {
  const html = web('field.html'), css = web('field.css');
  assert.ok(html.indexOf('asset-product-filters.js') < html.indexOf('field.js'));
  assert.match(html, /Mid-Cap \/ unallocated in ACT!/);
  for (const key of ['any', 'acv', 'lcv', 'midcap', 'mf'])
    assert.match(html, new RegExp('data-asset-product="' + key + '"'));
  for (const key of ['direct', 'crm', 'ranked']) assert.match(html, new RegExp('data-chip="' + key + '"'));
  assert.doesNotMatch(html, /data-chip="assets"/);
  assert.match(css, /#assetProducts\{[^}]*flex-wrap:wrap/);
  assert.match(css, /#assetProducts \.chip\{min-height:var\(--tap\)/);
  assert.match(source, /AssetProductFilters\.matches\(bookFor\(r\[COL\.crd\]\),/);
});
