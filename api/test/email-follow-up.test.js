"use strict";

/* The bulk follow-up: who is left after a campaign, and who must not be.
 *
 * The rep's rule is "everyone who did not reply". These pin the three
 * exclusions that rule does not cover, each of which is a different failure:
 *
 *   an OUT-OF-OFFICE is not a reply     following up is exactly right
 *   a BOUNCE is not a candidate         a second send buys a second bounce
 *   an OPT-OUT since the send           compliance, not preference
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("node:fs");
const Module = require("module");

function load(stubs) {
  const target = require.resolve("../shared/email-service.js");
  delete require.cache[target];
  const realLoad = Module._load;
  Module._load = function (request, parent) {
    if (parent && parent.filename === target) {
      const key = path.basename(String(request));
      if (stubs[key]) return stubs[key];
    }
    return realLoad.apply(this, arguments);
  };
  let mod;
  try { mod = require(target); } finally { Module._load = realLoad; }
  return mod;
}

const WHO = { id: "u-1", name: "bo@eicatlanta.com" };

function editingFixture(followUp = true) {
  const parent = { id: "P", status: "completed" };
  let batch = { id: "F", parentBatchId: followUp ? "P" : "", status: "editing",
    commonSubject: followUp ? "" : "Intro", commonBodyText: "Checking in.",
    commonRevision: 1, attachmentIds: [], graphMailbox: "rep@eicatlanta.com" };
  let message = { id: "M", state: "editing", contactId: "", recipientEmail: "rep@eicatlanta.com",
    subject: "RE: Sent subject", bodyText: "Checking in.", bodyHtml: "Checking in.",
    followUpOfGraphId: "G", attachments: [], baseRevision: 1, etag: "m" };
  const original = { id: "O", state: "sent", graphMessageId: "G", subject: "Sent subject",
    bodyText: "Original saved wording", recipientEmail: "rep@eicatlanta.com", attachments: [] };
  const st = {
    getBatch: async (u, id) => { assert.equal(u, WHO.id); return id === "P" ? parent : batch; },
    getMessage: async () => message,
    listMessages: async (u, id) => { assert.equal(u, WHO.id); return id === "P" ? [original] : [message]; },
    patchMessage: async (_u, _b, _m, p) => (message = { ...message, ...p }),
    patchBatch: async (_u, _b, p) => (batch = { ...batch, ...p }),
    getDocuments: async () => [], getTemplate: async () => null,
    getSuppression: async () => null, audit: async () => {},
  };
  const svc = load({ "email-store": st, "recipient-registry": { load: async () => {} },
    "email-auth": { status: async () => ({ profile: { mail: "rep@eicatlanta.com" } }) } });
  return { svc, original, parent, get batch() { return batch; }, get message() { return message; } };
}

test("common follow-up body edits preserve the original subject despite a blank shared subject", async () => {
  const f = editingFixture();
  const r = await f.svc.updateCommon(WHO, { batchId: "F", subject: "", bodyText: "Another note." });
  assert.equal(r.messages[0].subject, "RE: Sent subject");
  assert.equal(r.messages[0].bodyText, "Another note.");
  assert.equal(r.batch.commonSubject, "");
  assert.equal(r.originals[0].bodyText, "Original saved wording");
  assert.equal(r.valid, true);
});

test("follow-up detail preserves the original sent HTML and signature for the thread preview", async () => {
  const f = editingFixture();
  f.parent.senderMail = "rep@eicatlanta.com";
  f.parent.templateId = "intro";
  f.original.bodyHtml = "<p>Saved original wording</p>";
  f.original.signatureHtml = "<p>Original signature</p>";
  f.original.inlineImages = [{ id: "chart", cid: "original-chart" }];
  const r = await f.svc.validateBatch(WHO, "F");
  assert.equal(r.originals[0].bodyHtml, f.original.bodyHtml);
  assert.equal(r.originals[0].signatureHtml, f.original.signatureHtml);
  assert.equal(r.originals[0].senderMail, f.parent.senderMail);
  assert.equal(r.originals[0].templateId, "intro");
  assert.deepEqual(r.originals[0].inlineImages, f.original.inlineImages);
});

test("individual edits and resets cannot replace or erase a follow-up's inherited subject", async () => {
  const f = editingFixture();
  await f.svc.updateMessage(WHO, { batchId: "F", messageId: "M", subject: "Wrong subject", bodyText: "Personal note." });
  assert.equal(f.message.subject, "RE: Sent subject");
  await f.svc.updateMessage(WHO, { batchId: "F", messageId: "M", resetSubject: true, resetBody: true });
  assert.equal(f.message.subject, "RE: Sent subject");
  assert.equal(f.message.bodyText, "Checking in.");
  assert.equal(f.message.bodyOverridden, false);
});

test("a reply prefix alone is not accepted as an original subject", async () => {
  const f = editingFixture(); f.original.subject = "RE: ";
  await assert.rejects(() => f.svc.updateMessage(WHO, { batchId: "F", messageId: "M", bodyText: "Note" }),
    (e) => e.code === "original_subject_unavailable");
});

test("validation repairs an older follow-up's blank subject from its saved original", async () => {
  const f = editingFixture();
  f.message.subject = "";
  const r = await f.svc.validateBatch(WHO, "F");
  assert.equal(r.messages[0].subject, "RE: Sent subject");
  assert.equal(r.valid, true);
});

test("older follow-up drafts missing original copies cannot validate silently", async () => {
  const f = editingFixture();
  f.parent.ccColleague = "holly@eicatlanta.com";
  const r = await f.svc.validateBatch(WHO, "F");
  assert.equal(r.valid, false);
  assert.match(r.copyRetentionWarning, /missing original copied recipients/);
});

test("copy-aware follow-up drafts do not override deliberate copy edits", async () => {
  const f = editingFixture();
  f.parent.ccColleague = "holly@eicatlanta.com";
  f.batch.followUpCopiesVersion = 1;
  const r = await f.svc.validateBatch(WHO, "F");
  assert.equal(r.valid, true);
  assert.equal(r.copyRetentionWarning, "");
});

test("ordinary batch edits still require a subject", async () => {
  const f = editingFixture(false);
  await assert.rejects(() => f.svc.updateCommon(WHO, { batchId: "F", subject: "", bodyText: "Note" }),
    (e) => e.code === "common_text_invalid");
});

test("follow-up preparation is a summary; drafting uses the shared three-step editor", () => {
  const ui = fs.readFileSync(path.resolve(__dirname, "../../webapp/email.js"), "utf8");
  const view = ui.split("function followUpView()")[1].split("const FOLLOW_UP_DEFAULT")[0];
  assert.doesNotMatch(view, /scheduleHtml|textarea/);
  assert.match(view, /batchSummaryHtml/);
  assert.match(view, /follow-up-discard/);
  assert.match(view, /Prepare follow-up/);
  assert.doesNotMatch(ui, /function followUpDraftView|function followUpDeliveryView/);
  const editor = ui.split("function composerView()")[1].split("let swipeFrom")[0];
  assert.match(editor, /Step 1/); assert.match(editor, /Step 2/); assert.match(editor, /Step 3/);
  assert.equal((editor.match(/parentBatchId \? "readonly"/g) || []).length, 2);
  assert.ok(editor.indexOf("originalEmailHtml(") > editor.indexOf("Step 3"));
});

test("batch summaries keep source names, prefer a single person, and collapse their disclosures", () => {
  const vm = require("node:vm");
  const ui = fs.readFileSync(path.resolve(__dirname, "../../webapp/email.js"), "utf8");
  const start = ui.indexOf("  function batchAudience(");
  const end = ui.indexOf('  document.addEventListener("toggle"', start);
  const ctx = vm.createContext({ esc: (s) => String(s || "").replace(/</g, "&lt;").replace(/"/g, "&quot;") });
  vm.runInContext(ui.slice(start, end), ctx);
  const b = { id: "B", recipientCount: 40, sourceListName: "Deleted list - SH", commonSubject: "<unsafe>" };
  ctx.b = b;
  const html = vm.runInContext("batchSummaryHtml(b)", ctx);
  assert.match(html, /Deleted list - SH/);
  assert.match(html, /&lt;unsafe>/);
  assert.doesNotMatch(html, /<details[^>]* open/);
  assert.match(html, /Show original email/); assert.match(html, /Show list emails/);
  ctx.b = { ...b, recipientCount: 1, sourceRecipientName: "Bo Ladyman" };
  assert.equal(vm.runInContext("batchAudience(b)", ctx), "Bo Ladyman");
  const row = ui.split("function historyRow(")[1].split("function historyView")[0];
  assert.ok(row.indexOf('email-history-chip') < row.indexOf('<b>'));
});

test("Step 3 presents one continuous reply with an Outlook-style quoted original", () => {
  const vm = require("node:vm");
  const ui = fs.readFileSync(path.resolve(__dirname, "../../webapp/email.js"), "utf8");
  const start = ui.indexOf("  function originalEmailHtml(");
  const end = ui.indexOf("  async function openFollowUp(", start);
  const ctx = vm.createContext({
    esc: (s) => String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    previewWithImages: (html) => html,
    original: { senderMail: "rep@example.com", email: "advisor@example.com",
      subject: "<Original subject>", bodyHtml: "<p>Saved message</p>",
      signatureHtml: "<p>Saved signature</p>", sentUtc: "2026-09-25T14:00:00Z" },
  });
  vm.runInContext(ui.slice(start, end), ctx);
  const html = vm.runInContext("originalEmailHtml(original)", ctx);
  assert.match(html, /blockquote class="email-thread-quote"/);
  for (const header of ["From:", "Sent:", "To:", "Subject:"]) assert.ok(html.includes(header));
  assert.match(html, /&lt;Original subject>/);
  assert.match(html, /<p>Saved message<\/p><p>Saved signature<\/p>/);
  assert.doesNotMatch(html, /<section|<h3>|Original attachments:/);
  ctx.original = { bodyText: "<script>old plain text</script>", sentUtc: "invalid" };
  const legacy = vm.runInContext("originalEmailHtml(original)", ctx);
  assert.match(legacy, /&lt;script>/);
  assert.doesNotMatch(legacy, /Invalid Date|<script>/);
  const preview = ui.slice(ui.indexOf('<section class="email-preview">'), ui.indexOf('<aside id="emailDeliveryPlan"'));
  assert.match(preview, /originalEmailHtml\([\s\S]*?<\/div><\/section>/);
  assert.doesNotMatch(preview, /signatureHtml \|\| ""\}<\/div>/);
});

test("desktop and Field expose email history immediately between Lists and Settings", () => {
  for (const file of ["index.html", "field.html"]) {
    const html = fs.readFileSync(path.resolve(__dirname, "../../webapp", file), "utf8");
    const list = html.indexOf('id="listsBtn"'), history = html.indexOf('data-email="history"', list);
    assert.ok(list >= 0 && history > list && history < html.indexOf('id="settingsBtn"', list));
    assert.equal((html.match(/id="settingsBtn"/g) || []).length, 1);
  }
});

test("history separates sent batches and editing drafts, with a linked follow-up and discard action", () => {
  const ui = fs.readFileSync(path.resolve(__dirname, "../../webapp/email.js"), "utf8");
  const view = ui.split("function historyView()")[1].split("async function openHistory()")[0];
  assert.match(view, /Sent batches/);
  assert.match(view, /Drafts in progress/);
  assert.match(ui, /function historyFollowUp\(/);
  assert.match(ui, /b\.status === "completed" && sent\) return \["SENT", "sent"\]/);
  assert.match(ui, /b\.status === "drafts_ready"\) return \["OUTLOOK DRAFT", "draft"\]/);
  assert.match(ui, /Follow up with non-responders/);
  assert.match(ui, /data-email="history-discard"/);
  assert.match(ui, /data-email="history-toggle-discarded"/);
  assert.match(ui, /await api\("discard_draft", \{ batchId: batch\.id \}\)/);
});

function build({ messages, activity = {}, blocked = [], batch = {} }) {
  const store = {
    getBatch: async () => ({ id: "B1", name: "Intro", status: "completed", mode: "send",
                             parentBatchId: "", followUpSentUtc: "",
                             attachmentIds: [], etag: "e", ...batch }),
    listMessages: async () => messages,
    listActivity: async (crd) => activity[crd] || [],
    id: () => "new-id",
  };
  const suppress = {
    blockedAmong: async (rs) => new Set(rs.map(r => r.email).filter(e => blocked.includes(e))),
    manageUrl: () => "https://x/manage",
  };
  return load({ "email-store": store, "email-suppress": suppress });
}

const msg = (over) => ({ id: "m", state: "sent", contactId: "1", recipientEmail: "a@x.com",
  recipientName: "A", graphConversationId: "conv", graphMessageId: "g", subject: "Intro",
  bounceKind: "", ...over });

test("somebody who replied comes off the list", async () => {
  const svc = build({
    messages: [msg({ id: "m1", contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "inbound", classification: "reply", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.replied, 1);
  assert.equal(r.counts.remaining, 0);
});

test("an unavailable activity table does not turn an unknown reply into no reply", async () => {
  const svc = build({ messages: [msg({})] });
  await assert.rejects(() => svc.followUpCandidates(WHO, "B1", {
    store: { getBatch: async () => ({ id: "B1", status: "completed", mode: "send" }),
      listMessages: async () => [msg({})],
      listActivity: async () => { throw new Error("activity table unavailable"); } },
  }), /activity table unavailable/);
});

test("an OUT-OF-OFFICE is not a reply, so they stay on the list", async () => {
  const svc = build({
    messages: [msg({ id: "m1", contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "inbound", classification: "auto_reply", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.replied, 0);
  assert.equal(r.counts.remaining, 1, "an auto-responder says nothing about whether they read it");
});

test("a hard bounce is excluded rather than mailed again", async () => {
  const svc = build({ messages: [msg({ bounceKind: "hard" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.bounced, 1);
  assert.equal(r.counts.remaining, 0);
});

test("somebody who opted out after the send is excluded", async () => {
  const svc = build({
    messages: [msg({ recipientEmail: "gone@x.com" })],
    blocked: ["gone@x.com"],
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.suppressed, 1);
  assert.equal(r.counts.remaining, 0);
});

test("a message that never sent is not a first touch", async () => {
  const svc = build({ messages: [msg({ state: "failed" }), msg({ id: "m2", state: "sent",
    contactId: "2", recipientEmail: "two@x.com" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.notSent, 1);
  assert.equal(r.counts.remaining, 1);
});

test("a reply on a DIFFERENT conversation does not silence this one", async () => {
  const svc = build({
    messages: [msg({ contactId: "1", recipientEmail: "one@x.com", graphConversationId: "conv-A" })],
    activity: { "1": [{ direction: "inbound", classification: "reply",
                        conversationId: "conv-B", batchId: "OTHER" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.remaining, 1);
});

test("outbound activity is not mistaken for a reply", async () => {
  const svc = build({
    messages: [msg({ contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "outbound", classification: "sent", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.remaining, 1);
});

test("a message without its Outlook original is excluded rather than creating a broken follow-up", async () => {
  const svc = build({ messages: [msg({ graphMessageId: "" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.unthreadable, 1);
  assert.equal(r.counts.remaining, 0);
});

test("an unfinished or draft-only campaign cannot be followed up", async () => {
  const svc = build({ messages: [msg({})], batch: { status: "sending" } });
  await assert.rejects(() => svc.followUpCandidates(WHO, "B1"),
    (err) => err.code === "batch_not_completed");
});

function creationFixture({ conflict = false, failMessage = false } = {}) {
  const calls = [], batches = new Map(), messages = new Map();
  batches.set("B1", { id: "B1", name: "Intro", status: "completed", mode: "send",
    parentBatchId: "", followUpSentUtc: "", followUpBatchId: "", attachmentIds: [], etag: "e1" });
  messages.set("B1", [msg({ id: "m1", submittedUtc: "2026-08-20T12:00:00Z" })]);
  let ids = 0;
  const store = {
    id: () => (++ids === 1 ? "F1" : `M${ids}`),
    getBatch: async (_u, id) => batches.get(id) || null,
    listMessages: async (_u, id) => messages.get(id) || [],
    listActivity: async () => [], getDocuments: async () => [],
    createBatch: async (_who, row) => {
      calls.push(["create_batch", row.status]);
      const saved = { ...row, userId: WHO.id, mode: "", etag: "c1" };
      batches.set(row.id, saved); messages.set(row.id, []); return saved;
    },
    createMessage: async (_u, id, row) => {
      calls.push(["create_message", id]);
      if (failMessage) throw Object.assign(new Error("write failed"), { statusCode: 503 });
      messages.get(id).push({ ...row, state: "editing", etag: "m1" });
    },
    patchBatch: async (_u, id, patch, etag) => {
      calls.push(["patch_batch", id, { ...patch }, etag]);
      if (conflict && id === "B1" && patch.followUpBatchId)
        throw Object.assign(new Error("etag conflict"), { statusCode: 412 });
      const current = batches.get(id);
      const saved = { ...current, ...patch, etag: id === "B1" ? "e2" : "c2" };
      batches.set(id, saved); return saved;
    },
    audit: async () => {},
  };
  const registry = {
    load: async () => {},
    verify: async (crd, email) => ({ crd, email, name: "Advisor One", firm: "Firm",
      greetingName: "Advisor", lastName: "One", tier: "approved", source: "roster",
      matchScore: 100, matchGap: 100, registryHash: "rh", routingHash: "route" }),
    allowedTeammates: async () => [], verifyTeammates: async (_crd, rows) => rows,
    policy: () => ({ version: "v1" }),
  };
  const svc = load({
    "email-store": store,
    "email-suppress": { blockedAmong: async () => new Set(), manageUrl: () => "https://x/manage" },
    "recipient-registry": registry,
    "email-auth": { status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
    "email-materials": { currentDocument: () => true },
    "email-core": { config: () => ({ maxBodyChars: 50000, internalRecipients: [
      { address: "holly@eicatlanta.com" }, { address: "hannah@eicatlanta.com" }] }),
      splitName: () => ({ first: "Rep", last: "Test" }),
      plainTextToSafeHtml: (value) => value, corporateSignature: () => "<sig>",
      extraRecipients: () => ({ cc: [], bcc: [] }) },
  });
  return { svc, store, calls, batches, messages };
}

test("only selected still-eligible recipients are created, with their personal wording", async () => {
  const f = creationFixture();
  const original = f.messages.get("B1")[0];
  f.messages.get("B1").push({ ...original, id: "other", contactId: "2", recipientEmail: "other@x.com", graphMessageId: "other-g" });
  const r = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Shared note.",
    messageIds: [original.id], personalized: { [original.id]: "Personal note." } });
  assert.equal(r.messages.length, 1);
  assert.equal(r.messages[0].bodyText, "Personal note.");
  assert.equal(r.messages[0].bodyOverridden, true);
  assert.equal(r.batch.mode, "", "preparation does not approve sending");
});

test("reattachment preserves each recipient's original document variant", async () => {
  const f = creationFixture();
  const first = f.messages.get("B1")[0];
  const a = { id: "a", name: "UBS version", version: 1, sha256: "aaa" };
  const b = { id: "b", name: "RJ version", version: 1, sha256: "bbb" };
  first.attachments = [a];
  f.messages.get("B1").push({ ...first, id: "second", graphMessageId: "second-g",
    contactId: "2", recipientEmail: "two@x.com", attachments: [b] });
  f.store.getDocuments = async () => [a, b];
  const r = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Note.", includeAttachments: true });
  assert.deepEqual(r.messages.map((m) => m.attachments.map((d) => d.id)), [["a"], ["b"]]);
});

test("changed original documents block reattachment before claiming a follow-up", async () => {
  const f = creationFixture();
  f.messages.get("B1")[0].attachments = [{ id: "a", version: 1, sha256: "old" }];
  f.store.getDocuments = async () => [{ id: "a", version: 2, sha256: "new" }];
  await assert.rejects(() => f.svc.createFollowUp(WHO, { batchId: "B1", text: "Note.", includeAttachments: true }),
    (e) => e.code === "attachment_unavailable");
  assert.equal(f.calls.length, 0);
});

test("follow-up creation claims the parent before exposing an editable child", async () => {
  const f = creationFixture();
  const result = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store,
    recipientRegistry: {
      load: async () => {}, verify: async (crd, email) => ({ crd, email, name: "Advisor One",
        firm: "Firm", greetingName: "Advisor", lastName: "One", tier: "approved",
        source: "roster", matchScore: 100, matchGap: 100, registryHash: "rh", routingHash: "route" }),
      allowedTeammates: async () => [], policy: () => ({ version: "v1" }),
    },
    auth: { status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  });
  assert.equal(f.calls[0][0], "patch_batch");
  assert.equal(f.calls[0][1], "B1", "the parent claim must win before a child can be approved");
  assert.equal(result.batch.status, "editing");
  assert.equal(result.batch.parentBatchId, "B1");
  assert.equal(f.batches.get("B1").followUpBatchId, "F1");
});

test("follow-ups inherit the source list and all original configured colleague and teammate copies", async () => {
  const f = creationFixture();
  Object.assign(f.batches.get("B1"), { sourceListId: "deleted-list", sourceListName: "Leaders-SH",
    ccColleague: "holly@eicatlanta.com", copySelf: "bcc", copyInternal: "cc",
    copyInternalTo: "hannah@eicatlanta.com" });
  Object.assign(f.messages.get("B1")[0], { teammateCc: ["mate@example.com"], teammateCcCrds: ["123"],
    attachments: [{ id: "doc", name: "Original material" }] });
  const result = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Following up." });
  assert.equal(result.batch.sourceListName, "Leaders-SH");
  assert.equal(result.batch.ccColleague, "holly@eicatlanta.com");
  assert.equal(result.batch.copySelf, "bcc");
  assert.equal(result.batch.copyInternalTo, "hannah@eicatlanta.com");
  assert.deepEqual(JSON.parse(result.messages[0].teammateCcJson), ["mate@example.com"]);
  assert.deepEqual(JSON.parse(result.messages[0].teammateCcCrdsJson), ["123"]);
  assert.equal(result.messages[0].originalAttachmentCount, 1);
});

test("discarding an unapproved follow-up preserves the original and allows a replacement", async () => {
  const f = creationFixture();
  const first = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Draft." });
  await f.svc.control(WHO, { batchId: first.batch.id, action: "discard_draft" });
  assert.equal(f.batches.get(first.batch.id).status, "canceled");
  assert.equal(f.batches.get("B1").status, "completed");
  assert.equal(f.batches.get("B1").followUpBatchId, "");
  const next = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Replacement." });
  assert.notEqual(next.batch.id, first.batch.id);
  assert.equal(next.batch.status, "editing");
});

test("draft discard refuses approved or Outlook-created follow-ups", async () => {
  const f = creationFixture();
  const first = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Draft." });
  f.batches.get(first.batch.id).approvedUtc = "2026-09-26T12:00:00Z";
  await assert.rejects(() => f.svc.control(WHO, { batchId: first.batch.id, action: "discard_draft" }),
    (e) => e.code === "not_unapproved_draft");
  f.batches.get(first.batch.id).approvedUtc = "";
  f.messages.get(first.batch.id)[0].graphMessageId = "outlook-draft";
  await assert.rejects(() => f.svc.control(WHO, { batchId: first.batch.id, action: "discard_draft" }),
    (e) => e.code === "not_unapproved_draft");
  assert.equal(f.batches.get("B1").followUpBatchId, first.batch.id);
});

test("connected-mailbox self-test follow-up needs no fabricated advisor CRD", async () => {
  const f = creationFixture();
  Object.assign(f.messages.get("B1")[0], { contactId: "", recipientEmail: "rep@eicatlanta.com" });
  let verified = 0;
  const result = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store,
    recipientRegistry: { load: async () => {}, verify: async () => { verified++; throw new Error("not an advisor"); },
      allowedTeammates: async () => [], policy: () => ({ version: "v1" }) },
    auth: { status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox", mail: "rep@eicatlanta.com", displayName: "Rep Test" } }) },
  });
  assert.equal(verified, 0);
  assert.equal(result.messages[0].contactId, "");
  assert.equal(result.messages[0].recipientEmail, "rep@eicatlanta.com");
  assert.equal(result.batch.parentBatchId, "B1");
});

test("a finished batch cannot be relabeled canceled when tidying history", async () => {
  const f = creationFixture();
  await assert.rejects(() => f.svc.control(WHO, { batchId: "B1", action: "cancel" }),
    (error) => error.code === "batch_already_finished");
  assert.equal(f.batches.get("B1").status, "completed");
});

test("an ETag race refuses a duplicate follow-up before creating a child", async () => {
  const f = creationFixture({ conflict: true });
  await assert.rejects(() => f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store,
    recipientRegistry: { load: async () => {}, verify: async () => ({}), allowedTeammates: async () => [],
      policy: () => ({ version: "v1" }) },
    auth: { status: async () => ({ connected: true, profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  }), (err) => err.code === "follow_up_exists");
  assert.equal(f.calls.some((call) => call[0] === "create_batch"), false);
});

test("a partial follow-up build is canceled and releases its parent claim", async () => {
  const f = creationFixture({ failMessage: true });
  await assert.rejects(() => f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store,
    recipientRegistry: { load: async () => {}, verify: async (crd, email) => ({ crd, email,
      name: "Advisor", firm: "Firm", greetingName: "Advisor", lastName: "One",
      registryHash: "rh", routingHash: "route" }), allowedTeammates: async () => [],
      policy: () => ({ version: "v1" }) },
    auth: { status: async () => ({ connected: true, profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  }), /write failed/);
  assert.equal(f.batches.get("F1").status, "canceled");
  assert.equal(f.batches.get("B1").followUpBatchId, "");
  assert.equal(f.batches.get("B1").followUpSentUtc, "");
});

test("a stale interrupted build is retired so the rep can prepare the follow-up again", async () => {
  const f = creationFixture();
  f.batches.set("stale-child", { id: "stale-child", status: "building", etag: "stale-etag" });
  f.batches.set("B1", { ...f.batches.get("B1"),
    followUpSentUtc: "2026-08-01T12:00:00Z", followUpBatchId: "stale-child" });
  const registry = { load: async () => {}, verify: async (crd, email) => ({ crd, email,
    name: "Advisor", firm: "Firm", greetingName: "Advisor", lastName: "One",
    registryHash: "rh", routingHash: "route" }), allowedTeammates: async () => [],
    policy: () => ({ version: "v1" }) };
  const result = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store, recipientRegistry: registry,
    auth: { status: async () => ({ connected: true, profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  });
  assert.equal(f.batches.get("stale-child").status, "canceled");
  assert.equal(result.batch.id, "F1");
  assert.equal(result.batch.status, "editing");
  assert.equal(f.batches.get("B1").followUpBatchId, "F1");
});
