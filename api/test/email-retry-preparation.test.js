"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { prepare, stableId, assertOriginalUnsent } = require("../shared/email-retry-preparation");
const { prepareEligibility } = require("../shared/email-review");
const now = Date.parse("2026-09-19T15:00:00Z");
const batch = () => ({ id: "parent", userId: "u", status: "partial_failure", mode: "send",
  approvedUtc: "2026-09-18T13:00:00Z", graphMailboxId: "mailbox", templateId: "t",
  capacityReservationId: "old-reservation", commonSubject: "Common", copySelf: "bcc",
  graphMailbox: "rep@example.test", senderMail: "rep@example.test" });
const message = (id = "m1") => ({ id, state: "failed", sendAttempts: 0,
  failureCode: "draft_permanent_failure", capacityDay: "2026-09-18",
  outlookCheckStatus: "not_found", outlookCheckedUtc: new Date(now).toISOString(),
  subject: "Personal subject", subjectOverridden: true, bodyText: "Personal wording",
  bodyHtml: "<p>Personal wording</p>", bodyOverridden: true, graphMessageId: "old-draft",
  recipientEmail: "advisor@example.test", contactId: "123", attachments: [{ id: "doc", version: 1 }],
  teammateCc: ["mate@example.test"], teammateCcCrds: ["456"], inlineImages: [{ id: "image" }],
  reviewed: true, draftAttempts: 6, etag: "v1" });
function fixture() {
  const batches = new Map([["parent", batch()]]), rows = new Map([["parent", [message(), message("m2")]]]);
  const audits = [], releases = [];
  const store = {
    getBatch: async (u, b) => u === "u" ? structuredClone(batches.get(b) || null) : null,
    listMessages: async (_u, b) => structuredClone(rows.get(b) || []),
    getMessage: async (_u, b, id) => structuredClone((rows.get(b) || []).find(m => m.id === id) || null),
    getTemplate: async () => ({ id: "t", published: true, version: 2 }),
    getDocuments: async () => [{ id: "doc", name: "Current.pdf", version: 2 }],
    createBatch: async (_who, b) => {
      if (batches.has(b.id)) throw Object.assign(new Error("exists"), { statusCode: 409 });
      batches.set(b.id, { ...b, mode: "", approvedUtc: "", etag: "b1" }); rows.set(b.id, []);
    },
    reserveRetryMessages: async (_u, b, selected, child) => {
      if (selected.some(m => rows.get(b).find(x => x.id === m.id)?.etag !== m.etag))
        throw Object.assign(new Error("conflict"), { statusCode: 412 });
      for (const m of selected) Object.assign(rows.get(b).find(x => x.id === m.id), {
        retryBatchId: child, retryOriginalState: m.state, state: "canceled", etag: "reserved",
      });
    },
    createMessage: async (_u, b, m) => {
      if (rows.get(b).some(x => x.id === m.id)) throw Object.assign(new Error("exists"), { statusCode: 409 });
      rows.get(b).push({ ...m, sendAttempts: 0, reviewed: false });
    },
    patchBatch: async (_u, b, patch) => Object.assign(batches.get(b), patch),
    audit: async (...args) => audits.push(args),
  };
  const d = { store, now: () => now, auth: { status: async () => ({ connected: true, profile: { id: "mailbox" } }) },
    core: { config: () => ({}), isExternal: () => true, corporateSignature: () => "signature" },
    suppress: { blockedAmong: async () => new Map(), manageUrl: () => "" },
    materials: { currentDocument: () => true },
    capacity: { releaseAllocations: async (...args) => releases.push(args) } };
  return { d, batches, rows, audits, releases, input: { batchId: "parent", messageIds: ["m2", "m1"], confirmNotSentElsewhere: true } };
}

test("old-day pre-send failure can be prepared only with fresh successful Outlook check", () => {
  assert.equal(prepareEligibility(batch(), message(), now).ready, true);
  for (const patch of [
    { outlookCheckStatus: "unavailable" }, { outlookCheckStatus: "sent" },
    { outlookCheckStatus: "inconclusive" }, { outlookCheckedUtc: new Date(now - 600001).toISOString() },
    { outlookCheckedUtc: new Date(now + 1).toISOString() }, { sendAttempts: undefined },
    { sendAttempts: 1 }, { sendStartedUtc: "start" }, { sendAttemptId: "attempt" },
    { submittedUtc: "submitted" }, { state: "sent" }, { state: "draft_pending" },
    { failureCode: "send_outcome_unknown" }, { failureCode: "reconciliation_failed" },
    { sendOutcome: "accepted" }, { sendOutcome: "started" }, { bounceKind: "hard" },
    { handledManuallyUtc: "manual" }, { leaseUntilUtc: new Date(now + 1000).toISOString() },
  ]) assert.equal(prepareEligibility(batch(), { ...message(), ...patch }, now).ready, undefined, JSON.stringify(patch));
});

test("definitely rejected send requires a positive original draft, never just missing", () => {
  const m = { ...message(), failureCode: "send_retryable_exhausted", sendAttempts: 6, sendOutcome: "rejected" };
  assert.equal(prepareEligibility(batch(), m, now).ready, undefined);
  assert.equal(prepareEligibility(batch(), { ...m, outlookCheckStatus: "draft" }, now).ready, true);
});

test("preparation preserves content/copies, retires sources and creates no approval or old Graph work", async () => {
  const f = fixture(), result = await prepare({ id: "u" }, f.input, f.d);
  const b = f.batches.get(result.batchId), ms = f.rows.get(result.batchId);
  assert.equal(b.status, "editing"); assert.equal(b.mode, ""); assert.equal(b.approvedUtc, "");
  assert.equal(b.capacityReservationId, undefined); assert.equal(b.copySelf, "bcc");
  assert.deepEqual(b.retrySourceMessageIds, ["m1", "m2"]); assert.equal(ms.length, 2);
  for (const m of ms) {
    assert.equal(m.state, "editing"); assert.equal(m.subject, "Personal subject");
    assert.equal(m.bodyText, "Personal wording"); assert.equal(m.bodyOverridden, true);
    assert.equal(m.subjectOverridden, true); assert.equal(m.reviewed, false);
    assert.equal(m.graphMessageId, undefined); assert.equal(m.sendAttempts, 0);
    assert.equal(m.capacityDay, undefined); assert.equal(m.retryOfBatchId, "parent");
    assert.equal(m.attachments[0].version, 2); assert.equal(m.teammateCcJson, '["mate@example.test"]');
  }
  assert.ok(f.rows.get("parent").every(m => m.state === "canceled" && m.retryBatchId === b.id));
  assert.equal(f.releases.length, 1);
  assert.ok(f.audits.some(a => a[2] === "retry_review_prepared"));
});

test("repeated selection reopens same review without resetting edits or approval", async () => {
  const f = fixture(), first = await prepare({ id: "u" }, f.input, f.d);
  f.batches.get(first.batchId).status = "sending"; f.batches.get(first.batchId).approvedUtc = "approved";
  f.rows.get(first.batchId)[0].subject = "Edited later";
  const again = await prepare({ id: "u" }, { ...f.input, messageIds: ["m1", "m2", "m1"] }, f.d);
  assert.equal(again.batchId, first.batchId); assert.equal(again.existing, true);
  assert.equal(f.rows.get(first.batchId)[0].subject, "Edited later");
  assert.equal(f.batches.get(first.batchId).approvedUtc, "approved"); assert.equal(f.batches.size, 2);
});

test("overlapping selections cannot transfer an original twice", async () => {
  const f = fixture(); await prepare({ id: "u" }, f.input, f.d);
  await assert.rejects(prepare({ id: "u" }, { ...f.input, messageIds: ["m1"] }, f.d), /already belongs/);
  assert.equal(f.batches.size, 2);
});

test("partial child creation resumes exactly missing rows after a crash", async () => {
  const f = fixture(), real = f.d.store.createMessage; let count = 0;
  f.d.store.createMessage = async (...args) => { if (++count === 2) throw new Error("lost process"); return real(...args); };
  await assert.rejects(prepare({ id: "u" }, f.input, f.d), /lost process/);
  const id = stableId("retry-review-v1", "u", "parent", ["m1", "m2"]);
  assert.equal(f.batches.get(id).status, "building"); assert.equal(f.rows.get(id).length, 1);
  f.d.store.createMessage = real;
  assert.equal((await prepare({ id: "u" }, f.input, f.d)).batchId, id);
  assert.equal(f.rows.get(id).length, 2); assert.equal(f.batches.get(id).status, "editing");
});

test("lost original transaction response is reconciled without another transfer", async () => {
  const f = fixture(), real = f.d.store.reserveRetryMessages;
  f.d.store.reserveRetryMessages = async (...args) => { await real(...args); throw new Error("lost response"); };
  const result = await prepare({ id: "u" }, f.input, f.d);
  assert.equal(f.batches.get(result.batchId).status, "editing");
});

test("preparation fails closed before retirement on missing authority/evidence/materials", async () => {
  const cases = [
    f => { f.input.confirmNotSentElsewhere = false; },
    f => { f.input.messageIds = ["not-owned"]; },
    f => { f.rows.get("parent")[0].failureCode = "send_outcome_unknown"; },
    f => { f.rows.get("parent")[0].state = "sending"; },
    f => { f.d.auth.status = async () => ({ connected: false }); },
    f => { f.d.auth.status = async () => ({ connected: true, profile: { id: "other" } }); },
    f => { f.d.suppress.blockedAmong = async () => new Map([["advisor", "opted out"]]); },
    f => { f.d.store.getTemplate = async () => null; },
    f => { f.d.store.getDocuments = async () => []; },
    f => { f.d.materials.currentDocument = () => false; },
  ];
  for (const change of cases) {
    const f = fixture(); change(f); await assert.rejects(prepare({ id: "u" }, f.input, f.d));
    assert.equal(f.batches.size, 1); assert.ok(f.rows.get("parent").every(m => !m.retryBatchId));
  }
  const f = fixture(); await assert.rejects(prepare({ id: "other" }, f.input, f.d), /not found/);
});

test("source change during retirement leaves a non-sendable child and no messages", async () => {
  const f = fixture();
  f.d.store.reserveRetryMessages = async () => { throw Object.assign(new Error("conflict"), { statusCode: 412 }); };
  await assert.rejects(prepare({ id: "u" }, f.input, f.d), /conflict/);
  const child = [...f.batches.values()].find(b => b.id !== "parent");
  assert.equal(child.status, "building"); assert.equal(f.rows.get(child.id).length, 0);
});

test("source guard checks drafts and app IDs, blocks sent, manual, unknown and missing send-stage originals", async () => {
  const source = { ...message(), state: "canceled", retryBatchId: "child" };
  const m = { retryOfBatchId: "parent", retryOfMessageId: "m1" };
  const deps = { store: { getMessage: async () => source }, graph: {
    getMessage: async () => ({ isDraft: true }), findByAppId: async () => null,
  } };
  await assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps);
  for (const patch of [{ handledManuallyUtc: "yes" }, { state: "sent" }, { bounceKind: "hard" },
    { retryBatchId: "other" }, { submittedUtc: "yes" }, { sendOutcome: "started" }]) {
    deps.store.getMessage = async () => ({ ...source, ...patch });
    await assert.rejects(assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps));
  }
  deps.store.getMessage = async () => source;
  deps.graph.findByAppId = async () => ({ isDraft: false });
  await assert.rejects(assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps), /sent or inconclusive/);
  deps.graph.getMessage = deps.graph.findByAppId = async () => null;
  await assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps);
  source.sendAttempts = 1;
  await assert.rejects(assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps), /send-stage history/);
  deps.graph.findByAppId = async () => { throw new Error("network unavailable"); };
  await assert.rejects(assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps), /network unavailable/);
});

test("nested retry checks every ancestor and detects cycles", async () => {
  const m = { retryOfBatchId: "parent", retryOfMessageId: "m1" };
  const sources = { parent: { ...message(), state: "canceled", retryBatchId: "child",
    retryOfBatchId: "grandparent", retryOfMessageId: "m0" },
    grandparent: { ...message(), state: "canceled", retryBatchId: "parent", handledManuallyUtc: "manual" } };
  const deps = { store: { getMessage: async (_u, b) => sources[b] }, graph: {
    getMessage: async () => ({ isDraft: true }), findByAppId: async () => null,
  } };
  await assert.rejects(assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps), /handled manually/);
  delete sources.grandparent.handledManuallyUtc;
  await assertOriginalUnsent(m, { id: "child", userId: "u" }, "token", deps);
  sources.parent.retryBatchId = "parent"; sources.parent.retryOfBatchId = "parent";
  await assert.rejects(assertOriginalUnsent(m, { id: "parent", userId: "u" }, "token", deps), /history is incomplete/);
});
