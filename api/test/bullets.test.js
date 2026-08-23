"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../shared/email-core");

const NL = String.fromCharCode(10);
const TAB = String.fromCharCode(9);
const DOT = String.fromCharCode(8226);          // Word's first-level bullet
const render = (t) => core.sanitizeEmailHtml(core.plainTextToSafeHtml(t, []));

test("Word bullets survive as real list items", () => {
  // What a paste from the Word template library actually looks like. Before
  // this, the bullet reached the advisor as a literal character followed by a
  // tab -- and HTML collapses tabs, so the indent was simply gone.
  const html = render(["Three points:", DOT + TAB + "Avoid significant losses.",
                       DOT + TAB + "A narrower range of outcomes."].join(NL));
  assert.match(html, /<ul style="[^"]*padding-left/, "a real list with an indent");
  assert.match(html, /<li[^>]*>Avoid significant losses\.<\/li>/);
  assert.match(html, /<li[^>]*>A narrower range of outcomes\.<\/li>/);
  assert.doesNotMatch(html, new RegExp(DOT), "the bullet character itself is gone");
});

test("dashes, stars and Word sub-bullets all work", () => {
  for (const marker of ["-", "*", DOT, String.fromCharCode(183), "o"]) {
    const html = render(`${marker} One${NL}${marker} Two`);
    assert.match(html, /<ul/, `${marker} should start a list`);
    assert.equal((html.match(/<li/g) || []).length, 2, `${marker} should give two items`);
  }
});

test("numbered lists become ordered lists", () => {
  const html = render(`1. First${NL}2. Second${NL}3) Third`);
  assert.match(html, /<ol/);
  assert.equal((html.match(/<li/g) || []).length, 3);
  assert.doesNotMatch(html, /<ul/);
});

test("ordinary prose is untouched", () => {
  // The risk of a bullet detector is that it eats real sentences.
  const html = render(`October was busy.${NL}o'clock is not a bullet.${NL}3.5% is a number.`);
  assert.doesNotMatch(html, /<ul|<ol|<li/);
  assert.match(html, /October was busy/);
});

test("a list ends where the prose resumes", () => {
  const html = render(["Intro:", "- One", "- Two", "Back to prose."].join(NL));
  assert.match(html, /<\/ul><p[^>]*>Back to prose\.<\/p>/,
    "the list closes before the paragraph rather than swallowing it");
});

test("switching marker type starts a new list", () => {
  const html = render(`- Bullet${NL}1. Number`);
  assert.match(html, /<ul/);
  assert.match(html, /<ol/);
});

test("markup in a bullet is still escaped", () => {
  // Escaping happens before the list is built, so a list item is no more
  // trusted than a paragraph.
  const html = render(`- <script>alert(1)</script>${NL}- <b>bold</b>`);
  assert.doesNotMatch(html, /<script|<b>/);
  assert.match(html, /&lt;script&gt;/);
});

test("the bullet warning is gone but real Word artifacts still flag", () => {
  const bullets = core.lintTemplate({ subject: "Hi", bodyText: `Points:${NL}${DOT}${TAB}One` });
  assert.equal(bullets.warnings.filter((w) => /bullet/i.test(w.message)).length, 0,
    "warning about something the renderer now handles is noise");
  const nbsp = core.lintTemplate({ subject: "Hi", bodyText: `a${String.fromCharCode(160)}b` });
  assert.ok(nbsp.warnings.some((w) => /non-breaking/i.test(w.message)));
});

test("the editor preview recognises the same shapes as the renderer", () => {
  // If the preview and the sender disagree, an administrator formats a list,
  // sees it unformatted, and "fixes" it into something worse.
  const web = require("fs").readFileSync(require.resolve("../../webapp/email.js"), "utf8");
  const P_BULLET = eval(web.match(/const P_BULLET = (\/.*\/);/)[1]);
  const P_NUMBER = eval(web.match(/const P_NUMBER = (\/.*\/);/)[1]);
  const src = require("fs").readFileSync(require.resolve("../shared/email-core.js"), "utf8");
  const BULLET = eval(src.match(/const BULLET = (\/.*\/);/)[1]);
  const NUMBERED = eval(src.match(/const NUMBERED = (\/.*\/);/)[1]);
  for (const line of [DOT + TAB + "x", "- x", "* x", "o x", "October was busy", "1. x", "plain"]) {
    assert.equal(!!line.match(P_BULLET), !!line.match(BULLET), `bullet disagreement on: ${line}`);
    assert.equal(!!line.match(P_NUMBER), !!line.match(NUMBERED), `number disagreement on: ${line}`);
  }
});
