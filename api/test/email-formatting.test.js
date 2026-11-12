"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");
const core = require("../shared/email-core");
const { FakeTableService } = require("./helpers/fake-table");
const recipient = { name: "Dana Whitfield", firstName: "Dana", lastName: "Whitfield", firm: "UBS", nameFallback: false };
const sender = { displayName: "Example Sender", jobTitle: "Director" };
const html = '<p>Hi <b>{{first_name}}</b>,</p><p><span style="color:#006699">Selected words</span> and <i>italic</i> <u>underline</u>.</p>';

test("selected inline styles survive personalization and keep surrounding wording plain", () => {
  const result = core.renderBody("ignored client text", html, recipient, sender);
  assert.match(result.bodyHtml, /<b>Dana<\/b>/);
  assert.match(result.bodyHtml, /color:#006699/);
  assert.match(result.bodyHtml, /<i>italic<\/i> <u>underline<\/u>/);
  assert.equal(result.bodyText, "Hi Dana,\n\nSelected words and italic underline.");
  assert.doesNotMatch(result.bodyHtml, /<b>Selected/);
});
test("span styles emitted by browser commands survive the final email sanitizer", () => {
  const h = '<p><span style="font-weight:bold;font-style:italic;text-decoration:underline;color:rgb(4, 5, 6)">Only this</span> remains plain</p>';
  const result = core.renderBody("", h, recipient, sender);
  for (const value of ["font-weight:bold", "font-style:italic", "text-decoration:underline", "color:rgb(4, 5, 6)"]) assert.ok(result.bodyHtml.includes(value));
  assert.match(result.bodyHtml, /<\/span> remains plain/);
});
test("legacy plain-text messages keep the existing rendering exactly", () => {
  const text = "Hi {{first_name}},\n\n- One\n- Two\n\nLiteral <b>text</b>";
  const result = core.renderBody(text, "", recipient, sender);
  assert.equal(result.bodyHtml, core.plainTextToSafeHtml(core.renderTemplate(text, recipient, sender).rendered));
  assert.equal(result.bodyRichHtml, "");
});
test("unsafe pasted HTML, remote images, CSS and event handlers are discarded", () => {
  const result = core.normalizeRichBody('<p onclick="bad()">Hello<script>bad()</script><img src="https://track.test/x"><span style="color:#123456;background:url(https://track.test/x);position:fixed">safe</span><a href="javascript:bad()">link</a></p>');
  assert.equal(result.bodyText, "Hellosafelink");
  assert.doesNotMatch(result.bodyRichHtml, /script|onclick|<img|https:\/\/track|javascript:|position:|background:/i);
});
test("font color pasted from older editors normalizes to a safe span", () => {
  const result = core.normalizeRichBody('<p><font color="#123456" face="Comic Sans">Color</font></p>');
  assert.match(result.bodyRichHtml, /span style="color:#123456"/);
  assert.doesNotMatch(result.bodyRichHtml, /font|Comic/);
});
test("merge values are HTML-escaped, including quote and markup characters", () => {
  const result = core.renderBody("", '<p>{{first_name}} <b>{{sender_name}}</b></p>', { ...recipient, firstName: '<img src=x onerror="bad">' }, { displayName: "<script>sender</script>" });
  assert.doesNotMatch(result.bodyHtml, /<img|<script/);
  assert.match(result.bodyHtml, /&lt;img/);
  assert.match(result.bodyHtml, /&lt;script&gt;/);
  assert.match(result.bodyText, /<img src=x/);
});
test("empty or unknown merge values remain visible for the existing validation gate", () => {
  const result = core.renderBody("", '<p>{{first_name}} {{unknown}} {{SENDER_TITLE}}</p>', { nameFallback: false }, {});
  assert.equal(result.bodyText, "{{first_name}} {{unknown}} {{SENDER_TITLE}}");
});
test("chart placeholders still resolve only approved images and stay editable as tokens", () => {
  const result = core.renderBody("", '<p><b>Results</b></p><p>{{image:performance}}</p><p>{{image:unknown}}</p>', recipient, sender,
    [{ id: "performance", cid: "approved@map", name: "Performance" }]);
  assert.match(result.bodyHtml, /src="cid:approved@map"/);
  assert.match(result.bodyRichHtml, /\{\{image:performance\}\}/);
  assert.match(result.bodyText, /\{\{image:unknown\}\}/);
});
test("formatting that splits merge or chart tokens is refused", () => {
  for (const h of ['<p>{{first<b>_name</b>}}</p>', '<p>{<b>{first_name}</b>}</p>', '<p>{{image:<span>performance</span>}}</p>'])
    assert.throws(() => core.normalizeRichBody(h), /Keep merge fields/);
});
test("formatted bodies reject oversize markup instead of truncating tags in storage", () => {
  assert.throws(() => core.normalizeRichBody('<p>' + 'x'.repeat(30000) + '</p>'), /30,000/);
});

function loadStore() {
  const tables = new FakeTableService();
  const storePath = require.resolve("../shared/email-store");
  const previous = process.env.AZURE_STORAGE_CONNECTION_STRING;
  process.env.AZURE_STORAGE_CONNECTION_STRING = "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net";
  delete require.cache[storePath];
  const store = require(storePath);
  const sdk = require("@azure/data-tables");
  const old = sdk.TableClient.fromConnectionString;
  sdk.TableClient.fromConnectionString = (_connection, name) => tables.table(name);
  return { store, tables, restore() { sdk.TableClient.fromConnectionString = old; delete require.cache[storePath];
    if (previous === undefined) delete process.env.AZURE_STORAGE_CONNECTION_STRING; else process.env.AZURE_STORAGE_CONNECTION_STRING = previous; } };
}
test("rich template, common body, and individual body survive real storage whitelists and patches", async () => {
  const f = loadStore(), who = { id: "u1", name: "Rep" };
  try {
    await f.store.putTemplate(who, { id: "formatting", name: "Formatting", subject: "Example", bodyText: "forged", bodyRichHtml: html });
    const saved = await f.store.getTemplate("formatting");
    assert.equal(saved.bodyRichHtml, html);
    assert.equal(saved.bodyText, "Hi {{first_name}},\n\nSelected words and italic underline.");
    await f.store.createBatch(who, { id: "batch", commonBodyText: saved.bodyText, commonBodyRichHtml: html });
    const batch = await f.store.getBatch(who.id, "batch");
    assert.equal(batch.commonBodyRichHtml, html);
    await f.store.patchBatch(who.id, "batch", { commonBodyRichHtml: '<p><u>Changed</u></p>' }, batch.etag);
    assert.equal((await f.store.getBatch(who.id, "batch")).commonBodyRichHtml, '<p><u>Changed</u></p>');
    await f.store.createMessage(who.id, "batch", { id: "m1", ...core.renderBody("", html, recipient, sender) });
    const message = await f.store.getMessage(who.id, "batch", "m1");
    assert.match(message.bodyRichHtml, /<b>Dana<\/b>/);
    await f.store.patchMessage(who.id, "batch", "m1", { bodyRichHtml: '<p><i>Updated</i></p>' }, message.etag);
    assert.equal((await f.store.getMessage(who.id, "batch", "m1")).bodyRichHtml, '<p><i>Updated</i></p>');
  } finally { f.restore(); }
});

async function serviceFixture(run) {
  const f = loadStore(), who = { id: "u1", name: "Rep" };
  const modulePath = require.resolve("../shared/email-service");
  const oldLoad = Module._load;
  try {
    await f.store.putTemplate(who, { id: "formatting", name: "Formatting", subject: "Example", bodyText: "Hi {{first_name}}", bodyRichHtml: html });
    await f.store.createBatch(who, { id: "batch", templateId: "formatting", graphMailbox: "rep@example.test", commonSubject: "Example", commonBodyText: "Hi {{first_name}}", commonBodyRichHtml: html, attachmentIds: [] });
    for (const id of ["m1", "m2"]) await f.store.createMessage(who.id, "batch", { id, recipientEmail: "rep@example.test", recipientName: "Dana Whitfield", greetingName: "Dana", companyName: "UBS", subject: "Example", bodyText: "Original", bodyHtml: "<p>Original</p>", bodyOverridden: id === "m2", attachments: [] });
    Module._load = function(name, parent) {
      if (parent && parent.filename === modulePath) {
        if (name === "./email-store") return f.store;
        if (name === "./email-auth") return { status: async () => ({ connected: true, profile: sender }) };
        if (name === "./recipient-registry") return { load: async () => ({}) };
      }
      return oldLoad.apply(this, arguments);
    };
    delete require.cache[modulePath];
    const service = require(modulePath); Module._load = oldLoad;
    await run(service, f.store, who);
  } finally { Module._load = oldLoad; delete require.cache[modulePath]; f.restore(); }
}
test("common formatting reaches unmodified messages and preserves individual wording", () => serviceFixture(async (service, store, who) => {
  await service.updateCommon(who, { batchId: "batch", bodyText: "forged", bodyRichHtml: '<p><span style="color:#123456">Hi {{first_name}}</span></p>' });
  const m1 = await store.getMessage(who.id, "batch", "m1"), m2 = await store.getMessage(who.id, "batch", "m2");
  assert.match(m1.bodyHtml, /color:#123456/); assert.equal(m1.bodyText, "Hi Dana");
  assert.equal(m2.bodyText, "Original");
}));
test("individual formatting persists and reset restores the formatted common body", () => serviceFixture(async (service, store, who) => {
  await service.updateMessage(who, { batchId: "batch", messageId: "m1", bodyText: "ignored", bodyRichHtml: '<p><u>Personal wording</u></p>', reviewed: true });
  let m = await store.getMessage(who.id, "batch", "m1");
  assert.match(m.bodyHtml, /<u>Personal wording<\/u>/); assert.equal(m.bodyOverridden, true);
  await service.updateMessage(who, { batchId: "batch", messageId: "m1", resetBody: true });
  m = await store.getMessage(who.id, "batch", "m1");
  assert.match(m.bodyHtml, /<b>Dana<\/b>/); assert.match(m.bodyHtml, /color:#006699/); assert.equal(m.bodyOverridden, false);
}));
test("an approved identity presentation refresh retains template formatting", () => {
  const service = require("../shared/email-service");
  const refreshed = service.identityPresentationRefresh({ recipientName: "Old name", greetingName: "Old", companyName: "UBS" },
    { name: "Dana Whitfield", greetingName: "Dana", lastName: "Whitfield", firm: "UBS" },
    { commonSubject: "Hello", commonBodyText: "Hi {{first_name}}", commonBodyRichHtml: html }, sender, []);
  assert.equal(refreshed.changed, true); assert.match(refreshed.patch.bodyHtml, /<b>Dana<\/b>/);
});
