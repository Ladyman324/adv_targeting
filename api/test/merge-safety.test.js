"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../shared/email-core");
const { identityPresentationRefresh } = require("../shared/email-service");

// The rule validateMessage applies, lifted out so it can be exercised without
// storage. Kept in step with the real one by the last test in this file.
function tokenProblems(text, knownImageIds) {
  const tokens = [...String(text).matchAll(/\{\{\s*([^}]+)\s*\}\}/g)].map((m) => String(m[1]).trim());
  const unresolved = [], badImages = [];
  for (const token of tokens) {
    if (!/^image:/i.test(token)) { unresolved.push(token); continue; }
    const id = token.slice(6).trim().toLowerCase();
    if (knownImageIds && !knownImageIds.has(id)) badImages.push(id);
  }
  return { unresolved, badImages };
}

test("a MISTYPED chart id is caught instead of being mailed as literal text", () => {
  // The bug. {{image:performace}} rendered as the literal string
  // "{{image:performace}}" in the sent email, and every check passed, because
  // the unresolved-field test exempted anything starting with "image:".
  const known = new Set(["performance"]);
  const { badImages } = tokenProblems("Hi,\n\n{{image:performace}}\n\nRegards", known);
  assert.deepEqual(badImages, ["performace"]);

  // And it really would have shipped as visible text.
  const images = [{ id: "performance", name: "ACV performance", cid: "performance@eicadvisormap" }];
  const html = core.plainTextToSafeHtml("{{image:performace}}", images);
  assert.match(html, /\{\{image:performace\}\}/, "renders as text, which is why it must be blocked");
});

test("a correctly spelled chart is still exempt and still renders", () => {
  const known = new Set(["performance"]);
  assert.deepEqual(tokenProblems("{{image:performance}}", known).badImages, []);
  assert.deepEqual(tokenProblems("{{image:performance}}", known).unresolved, []);
  const images = [{ id: "performance", name: "ACV performance", cid: "performance@eicadvisormap" }];
  assert.match(core.plainTextToSafeHtml("{{image:performance}}", images), /<img[^>]+cid:performance@/);
});

test("case and spacing do not defeat the check", () => {
  const known = new Set(["performance"]);
  assert.deepEqual(tokenProblems("{{ IMAGE:Performance }}", known).badImages, []);
  assert.deepEqual(tokenProblems("{{image: nope }}", known).badImages, ["nope"]);
});

test("an unloadable template exempts every chart rather than blocking the batch", () => {
  // knownImageIds null means the template lookup failed. Blocking a whole batch
  // over that would be a worse failure than the one being prevented.
  assert.deepEqual(tokenProblems("{{image:anything}}", null).badImages, []);
});

test("a single brace is caught by the lint reps now run through", () => {
  // Not a token, so it renders as literal text and the unresolved-field check
  // never sees it: "Hi {first_name}," goes out. Only lintTemplate catches it,
  // and lintTemplate used to run for administrators only.
  const lint = core.lintTemplate({ subject: "Hello", bodyText: "Hi {first_name}," });
  assert.ok(lint.errors.length, "a single brace must be an error");
  assert.match(JSON.stringify(lint.errors), /brace/i);
});

test("an unknown merge field is an error, and the approved ones are not", () => {
  assert.ok(core.lintTemplate({ subject: "", bodyText: "Hi {{frist_name}}," }).errors.length);
  assert.equal(core.lintTemplate({ subject: "Hi {{first_name}}",
    bodyText: "{{first_name}} {{last_name}} {{company_name}}" }).errors.length, 0);
});

test("the extracted rule matches the one email-service.js actually applies", () => {
  // This file reimplements the check to test it without storage. If the real one
  // drifts, these tests would keep passing while the product broke.
  const src = require("fs").readFileSync(require.resolve("../shared/email-service.js"), "utf8");
  const fn = src.split("async function validateMessage")[1].split("async function validateBatch")[0];
  assert.match(fn, /knownImageIds && !knownImageIds\.has\(id\)/);
  assert.match(fn, /badImages\.push\(id\)/);
  assert.match(fn, /code: "unknown_image"/);
});

test("an identity correction rerenders unedited content for fresh review", () => {
  const refresh = identityPresentationRefresh({
    recipientName: "Wrong Person", greetingName: "Wrong",
    recipientLastName: "Person", companyName: "Old Firm",
    subjectOverridden: false, bodyOverridden: false,
  }, {
    name: "Christopher Tolman", greetingName: "Chris",
    lastName: "Tolman", firm: "UBS",
  }, {
    commonSubject: "Hello {{first_name}}",
    commonBodyText: "Hi {{first_name}},",
  }, null, []);
  assert.equal(refresh.changed, true);
  assert.equal(refresh.blocked, false);
  assert.equal(refresh.patch.subject, "Hello Chris");
  assert.equal(refresh.patch.bodyText, "Hi Chris,");
  assert.doesNotMatch(refresh.patch.bodyHtml, /Wrong/);
});

test("an identity correction blocks individually edited stale wording", () => {
  const refresh = identityPresentationRefresh({
    recipientName: "Wrong Person", greetingName: "Wrong",
    recipientLastName: "Person", companyName: "Old Firm",
    subjectOverridden: false, bodyOverridden: true,
  }, {
    name: "Christopher Tolman", greetingName: "Chris",
    lastName: "Tolman", firm: "UBS",
  }, { commonSubject: "", commonBodyText: "" }, null, []);
  assert.equal(refresh.changed, true);
  assert.equal(refresh.blocked, true);
  assert.deepEqual(refresh.patch, {});
});
