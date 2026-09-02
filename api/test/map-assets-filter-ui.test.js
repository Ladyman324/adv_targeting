'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const webapp = path.join(__dirname, '..', '..', 'webapp');
const desktop = fs.readFileSync(path.join(webapp, 'app.js'), 'utf8');
const field = fs.readFileSync(path.join(webapp, 'field.js'), 'utf8');
const index = fs.readFileSync(path.join(webapp, 'index.html'), 'utf8');

function declaration(source, name, nextName) {
  const start = source.indexOf('function ' + name + '(');
  const end = source.indexOf('function ' + nextName + '(', start);
  assert.notEqual(start, -1, name + ' must exist');
  assert.notEqual(end, -1, nextName + ' must follow ' + name);
  return source.slice(start, end);
}

function compileFunction(source, name, nextName, context) {
  vm.createContext(context);
  vm.runInContext(declaration(source, name, nextName)
    + '\nthis.subject = ' + name + ';', context);
  return context.subject;
}

test('desktop EIC-assets filter trusts the canonical CRD book, not contact tm/ia', () => {
  const books = { roster: { t: 'high', n: 1, ix: [0] } };
  const context = {
    assetsOnly: true,
    SUPPORT: { act: 'ready' },
    bookFor: (crd) => books[String(crd)] || null,
  };
  const passes = compileFunction(desktop, 'passesHasAssets', 'passesContinental', context);

  assert.equal(passes({ id: 'roster' }), true,
    'a roster-selected contact with a canonical ACT book must pass');
  assert.equal(passes({ id: 'legacy-only', tm: 'old-team', ia: 1000000 }), false,
    'contact-side team or individual amounts cannot create a book');
  context.SUPPORT.act = 'loading';
  assert.equal(passes({ id: 'legacy-only', tm: 'old-team', ia: 1000000 }), false,
    'an active filter must not silently broaden while canonical data is unavailable');
});

test('Field App EIC-assets filter uses the same CRD-keyed book and fails closed while active', () => {
  const books = { roster: { t: 'high', n: 1, ix: [0] } };
  const context = {
    BOOK_STATE: 'ready',
    COL: { crd: 0 },
    bookFor: (crd) => books[String(crd)] || null,
  };
  const passes = compileFunction(field, 'hasAssets', 'passes', context);

  assert.equal(passes(['roster', 'ignored-old-row-assets']), true);
  assert.equal(passes(['legacy-only', 999999999]), false);
  context.BOOK_STATE = 'failed';
  assert.equal(passes(['legacy-only', 999999999]), false,
    'an active Field filter must not silently broaden on lookup failure');
});

test('asset cards and badges no longer fall back to legacy contact or tile amounts', () => {
  const contactStart = desktop.indexOf('function contactBlock(');
  const contactEnd = desktop.indexOf('// ---- contact activity', contactStart);
  const contactCard = desktop.slice(contactStart, contactEnd);
  const marks = declaration(desktop, 'contactMarks', 'contactDots');
  const fieldRender = declaration(field, 'render', 'sameBuilding');
  const fieldSheet = declaration(field, 'withEic', 'territoryRow');

  assert.doesNotMatch(contactCard, /team\.a|c\.ia/);
  assert.match(contactCard, /const book = bookFor\(p\.id\)/);
  assert.match(marks, /assets: SUPPORT\.act === "ready" && !!bookFor\(advisorId\)/);
  assert.doesNotMatch(marks, /c\.tm|c\.ia/);
  assert.match(fieldRender, /const assetBook = bookFor\(r\[COL\.crd\]\)/);
  assert.doesNotMatch(fieldRender, /r\[COL\.assets\] > 0/);
  assert.doesNotMatch(fieldSheet, /COL\.assets/);
});

test('asset filter readiness is visible and saved audiences cannot broaden', () => {
  const loader = declaration(desktop, 'loadActAssets', 'bookFor');
  const apply = declaration(desktop, 'applyAudienceDefinition', 'currentAudiencePreview');
  const prepare = declaration(desktop, 'prepareAudiencePreviewData', 'audienceTerritoryPolicy');
  const fieldLoaderStart = field.indexOf('async function loadExtras(');
  const fieldLoaderEnd = field.indexOf('const bookFor =', fieldLoaderStart);
  assert.notEqual(fieldLoaderStart, -1);
  assert.notEqual(fieldLoaderEnd, -1);
  const fieldLoader = field.slice(fieldLoaderStart, fieldLoaderEnd);
  const fieldWiring = field.slice(field.indexOf('const chip = e.target.closest("[data-chip]")'),
    field.indexOf('const open = e.target.closest("[data-open]")'));

  assert.match(loader, /if \(assetsOnly\)[\s\S]*assetsOnly = false[\s\S]*showNotice/);
  assert.match(apply, /f\.assetsOnly && SUPPORT\.act !== "ready"[\s\S]*throw new Error/);
  assert.match(prepare, /f\.ownerOnly \|\| f\.rankedOnly \|\| f\.assetsOnly/);
  assert.match(prepare, /f\.assetsOnly && SUPPORT\.act !== "ready"/);
  assert.match(fieldLoader, /BOOK_STATE === "failed" && CHIPS\.assets[\s\S]*CHIPS\.assets = false/);
  assert.match(fieldLoader, /Has assets was turned off/);
  assert.match(fieldWiring, /k === "assets" && BOOK_STATE !== "ready"/);
});

test('national EIC help separates mapped headline totals from source reconciliation', () => {
  const national = declaration(desktop, 'renderEicNational', 'renderEicAssets');

  assert.match(national, /ACT_ASSETS\.source_totals/);
  assert.match(national, /source\.unmapped_accounts/);
  assert.match(national, /source\.approved_off_map/);
  assert.match(national, /source\.unapproved_or_unresolved/);
  assert.match(national, /unapproved or unresolved ACT account/);
  assert.match(national, /absent from the current deployed map/);
  assert.match(national, /excluded from both this headline and the EIC-assets filter/);
  assert.doesNotMatch(national, /n\(source\.unmapped_accounts\).*unresolved/);
  assert.doesNotMatch(national, /t\.accounts_on_map/);
  assert.doesNotMatch(national, /they are still counted here/);
});

test('Mid-Cap and inferred-economic-link transparency reach every asset surface', () => {
  const team = declaration(desktop, 'teamBook', 'territoryFor');
  const contactStart = desktop.indexOf('function contactBlock(');
  const contactEnd = desktop.indexOf('// ---- contact activity', contactStart);
  const contactCard = desktop.slice(contactStart, contactEnd);
  const national = declaration(desktop, 'renderEicNational', 'renderEicAssets');
  const regional = declaration(desktop, 'renderEicAssets', 'renderEstimatedAum');
  const fieldRender = declaration(field, 'render', 'sameBuilding');
  const fieldSheet = declaration(field, 'withEic', 'territoryRow');

  assert.match(index, /id="eicMidcap"/);
  assert.match(team, /midcap \+= a\[3\] \|\| 0/);
  assert.match(contactCard, /line\("Mid-Cap", book\.midcap \|\| 0/);
  assert.doesNotMatch(contactCard, /value not classified/);
  assert.match(contactCard, /book\.t === "high"[\s\S]*inferred ACT economic link/);
  assert.match(national, /t\.midcap/);
  assert.match(national, /t\.advisors_economic_only/);
  assert.match(national, /do not approve a name, email address, phone number/);
  assert.match(regional, /midcap \+= a\[3\] \|\| 0/);
  assert.match(regional, /b\.t === "high"/);
  assert.match(fieldRender, /assetBook\.midcap/);
  assert.match(fieldRender, /assetBook\.t === "high"/);
  assert.match(fieldSheet, /part\("Mid-Cap", b\.midcap \|\| 0\)/);
  assert.match(fieldSheet, /inferred ACT economic link/);
});

test('mutual-fund ownership routes commentary as All-Cap on desktop and Field', () => {
  for (const source of [desktop, field]) {
    assert.match(source, /function materialStrategiesFor|const materialStrategiesFor/);
    assert.match(source, /Number\(book\.acv \|\| 0\) > 0 \|\| Number\(book\.mf \|\| 0\) > 0/);
    assert.match(source, /out\.push\("acv"\)/);
    assert.match(source, /materialStrategies: materialStrategiesFor/);
  }
});
