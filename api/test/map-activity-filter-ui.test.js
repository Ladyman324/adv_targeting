'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', '..', 'webapp', 'app.js'), 'utf8');

function declaration(name, nextName) {
  const start = source.indexOf('function ' + name + '(');
  const end = source.indexOf('function ' + nextName + '(', start);
  assert.notEqual(start, -1, name + ' must exist');
  assert.notEqual(end, -1, nextName + ' must follow ' + name);
  return source.slice(start, end);
}

function summaryFunction() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(
    'const FILTER_DAY_MS = 86400000;\n'
      + declaration('activityAgeBand', 'summarizeActivityOptions')
      + declaration('summarizeActivityOptions', 'passesActivityAge')
      + '\nthis.summarize = summarizeActivityOptions;', context);
  return context.summarize;
}

test('activity option counts are distinct, scoped, and honor other filters', () => {
  const now = Date.now();
  const feature = (id, keep = true) => ({ properties: { id, keep } });
  const features = [
    feature('recent'), feature('recent'),
    feature('excluded', false), feature('unobserved'), feature('old'),
  ];
  const rows = new Map([
    ['recent', now - 5 * 86400000],
    ['excluded', now - 8 * 86400000],
    ['old', now - 220 * 86400000],
    ['elsewhere', now - 50 * 86400000],
  ]);
  const stats = summaryFunction()(features, rows, p => p.keep);

  assert.deepEqual({ ...stats.counts }, {
    any: 3, d30: 1, d90: 0, d180: 0, older: 1, none: 1,
  });
  assert.equal(stats.observedInScope, 3);
  assert.equal(stats.excludedByOtherFilters, 1);
  assert.equal(stats.outsideScopeOrUnmapped, 1);
});

test('activity controls expose zero counts without allowing a blank-map choice', () => {
  const paint = declaration('paintActivityOptions', 'clearUnreachableActivitySelections');
  const clear = declaration('clearUnreachableActivitySelections', 'syncActivityFilterUI');
  const wireStart = source.indexOf('function wireActivitySelect(');
  const wire = source.slice(wireStart,
    source.indexOf('wireActivitySelect(lastEmailedSelect', wireStart));

  assert.match(paint, /option\.textContent[\s\S]*count\.toLocaleString/);
  assert.match(paint, /option\.disabled = value !== '' && count === 0/);
  assert.match(clear, /if \(stats\.counts\[value\]\) continue/);
  assert.match(clear, /setValue\(''\)/);
  assert.match(clear, /activityChoiceExplanation\(label, value, stats, true\)/);
  assert.ok(wire.indexOf('if (value && !stats.counts[value])')
    < wire.indexOf('setValue(value)'), 'a zero choice must be rejected before mutation');
  assert.match(wire, /The map was left unchanged|activityChoiceExplanation/);
});

test('each option is counted with only its own activity predicate omitted', () => {
  const stats = declaration('activityOptionStats', 'activityChoiceExplanation');
  const baseStart = source.indexOf('function passesBase(');
  const baseEnd = source.indexOf('function passesFilters(', baseStart);
  const filtersEnd = source.indexOf('function colorFor(', baseEnd);
  const predicates = source.slice(baseStart, filtersEnd);

  assert.match(stats, /passesFilters\(p, kind\)/);
  assert.match(predicates, /skipActivity === 'email' \|\| passesLastEmailed\(p\)/);
  assert.match(predicates, /skipActivity === 'call' \|\| passesLastCalled\(p\)/);
  assert.match(predicates, /selectedFirms\.includes\(p\.fc\)/);
});
