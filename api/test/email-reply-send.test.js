"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const send = require("../shared/email-reply-send");
const engagement = require("../shared/email-engagement");
const realLimitGuard = require("../shared/email-limit-guard");

const BO = { id: "user-bo", name: "Bo" };
const b64 = (s) => Buffer.from(s).toString("base64");

function directCfg(over = {}) {
  return { maxAttachmentBytes: 10 * 1024 * 1024,
    directSendEnvironmentEnabled: true, testAllowlist: new Set(),
    internalDomains: new Set(["eicatlanta.com"]), rollingExternalLimit: 5000,
    mailboxIntervalSeconds: 5, ...over };
}

function deps(over = {}) {
  const calls = { created: [], drafts: [], patched: [], sent: [], bodies: [],
                  activity: [], docs: [], files: [], reservations: [], pacing: [], waits: [],
                  policy: 0 };
  return {
    calls,
    store: {
      activityOwner: async () => "user-bo",
      recordActivity: async (e) => { calls.activity.push(e); return e; },
      policy: async () => { calls.policy++; return { killed: false, reason: "" }; },
      getDocuments: async (ids) => ids.map((id) => ({ id, name: `Doc ${id}`, size: 1000,
                                                      blobName: `b/${id}` })),
      listActivity: async () => ([{ userId: "user-bo", advisorEmail: "advisor@ml.com" }]),
      ...(over.store || {}),
    },
    auth: { tokenFor: async () => ({ accessToken: "t", mailbox: "bo@eicatlanta.com",
                                      profile: { displayName: "Bo", mail: "bo@eicatlanta.com" } }),
            ...(over.auth || {}) },
    advisors: {
      isInternalCrd: async () => false,
      emailForCrd: async () => "advisor@ml.com",
      ...(over.advisors || {}),
    },
    suppress: { blockedAmong: async () => new Map(), ...(over.suppress || {}) },
    core: {
      config: () => directCfg(),
      isExternal: (email, cfg) => !cfg.internalDomains.has(String(email).split("@").pop()),
      corporateSignature: () => "<div>Bo — EIC</div>",
      // The real rule: material to an external recipient is blind-copied.
      complianceBcc: (m) => (m.attachments && m.attachments.length
        ? ["compliance@eicatlanta.com"] : []),
      ...(over.core || {}),
    },
    graph: {
      getMessageContent: async () => ({ id: "g1", subject: "RE: EIC All-Cap Value",
        conversationId: "conv-1", from: { emailAddress: { address: "advisor@ml.com" } } }),
      createReply: async (_t, id, all) => { calls.created.push({ id, all }); return { id: "draft-1" }; },
      createDraft: async (_t, m) => { calls.drafts.push(m); return { id: "draft-2", conversationId: "conv-new" }; },
      findByAppId: async () => null,
      APP_PROPERTY_ID: "app-property",
      updateDraftBody: async (_t, id, html) => { calls.bodies.push({ id, html }); return {}; },
      attachDocuments: async (_t, id, d) => { calls.docs.push({ id, d }); },
      attachFiles: async (_t, id, f) => { calls.files.push({ id, f }); },
      sendDraft: async (_t, id) => { calls.sent.push(id); },
      request: async (_t, m, p, b) => { calls.patched.push({ m, p, b }); return { data: {} }; },
      ...(over.graph || {}),
    },
    limitGuard: {
      reserve: async (...args) => { calls.reservations.push(args); return { alreadyReserved: false }; },
      ...(over.limitGuard || {}),
    },
    mailboxGate: {
      acquire: async (...args) => { calls.pacing.push(args); return 0; },
      ...(over.mailboxGate || {}),
    },
    wait: async (ms) => { calls.waits.push(ms); },
  };
}

const OP_REPLY = "11111111-1111-4111-8111-111111111111";
const OP_FOLLOW = "22222222-2222-4222-8222-222222222222";
const REPLY = { crd: "111", id: "g1", text: "Sending the presentation now.", operationId: OP_REPLY };
const FOLLOW = { crd: "111", subject: "Following up", text: "It has been a while.", operationId: OP_FOLLOW };

/* ---- reply --------------------------------------------------------------- */

test("a reply uses Graph createReply, keeping the real conversation", async () => {
  const d = deps();
  const r = await send.reply(BO, REPLY, d);
  assert.equal(d.calls.created[0].id, "g1");
  assert.equal(d.calls.sent[0], "draft-1");
  assert.equal(r.conversationId, "conv-1",
    "a fabricated RE: would start a new thread and cost us thread matching");
});

test("the signature comes through on a reply", async () => {
  const d = deps();
  await send.reply(BO, REPLY, d);
  assert.match(d.calls.bodies[0].html, /Bo — EIC/);
});

test("what a rep types can never become markup in the advisor's client", () => {
  const html = send.textToHtml('<script>alert(1)</script> & "quotes"');
  assert.ok(!/<script>/.test(html));
  assert.match(html, /&lt;script&gt;/);
});

/* SUPPRESSION IS NOT THE SAME QUESTION FOR A REPLY AS FOR A CAMPAIGN.
 *
 * A suppression means "do not send this address marketing". Answering a message
 * somebody sent US is not marketing -- it is correspondence, and refusing to
 * answer an advisor because they once unsubscribed is unhelpful and slightly
 * rude. Starting a NEW conversation with them is a different act entirely. */

test("a reply to a suppressed address is ALLOWED, and says so", async () => {
  const d = deps({ suppress: { blockedAmong: async () => new Map([["advisor@ml.com", true]]) } });
  const r = await send.reply(BO, REPLY, d);
  assert.equal(d.calls.sent.length, 1, "they wrote to us; we may answer");
  assert.equal(r.suppressed, true, "the rep is told, they are just not stopped");
});

test("a FOLLOW-UP to a suppressed address is refused", async () => {
  const d = deps({ suppress: { blockedAmong: async () => new Map([["advisor@ml.com", true]]) } });
  await assert.rejects(() => send.followUp(BO, FOLLOW, d),
    (err) => err.statusCode === 409 && err.code === "suppressed");
  assert.equal(d.calls.drafts.length, 0,
    "this is us initiating contact with somebody who asked us not to");
});

test("another rep's message cannot be replied to", async () => {
  const d = deps({ store: { activityOwner: async () => "user-kate" } });
  await assert.rejects(() => send.reply(BO, REPLY, d),
    (err) => err.statusCode === 403 && err.code === "not_your_mailbox");
  assert.equal(d.calls.created.length, 0);
});

/* ---- fail-closed direct-send foundation --------------------------------- */

test("both paths require a valid operation UUID before any Graph mutation", async () => {
  for (const [name, invoke] of [
    ["reply", (d, operationId) => send.reply(BO, { ...REPLY, operationId }, d)],
    ["follow-up", (d, operationId) => send.followUp(BO, { ...FOLLOW, operationId }, d)],
  ]) {
    for (const value of ["", "not-a-uuid"]) {
      const d = deps();
      await assert.rejects(() => invoke(d, value),
        (err) => err.statusCode === 400 && err.code === "operation_id_required", `${name}: ${value}`);
      assert.equal(d.calls.created.length + d.calls.drafts.length, 0);
      assert.equal(d.calls.sent.length, 0);
    }
  }
});

test("advisor identity lookup failure stops both paths before a draft", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps({ advisors: { isInternalCrd: async () => { throw new Error("lookup down"); } } });
    await assert.rejects(() => invoke(d),
      (err) => err.statusCode === 503 && err.code === "advisor_lookup_unavailable", name);
    assert.equal(d.calls.created.length + d.calls.drafts.length, 0);
    assert.equal(d.calls.sent.length, 0);
  }
});

test("an unavailable Graph retry lookup fails closed before a draft", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps({ graph: { findByAppId: async () => { throw new Error("Graph unavailable"); } } });
    await assert.rejects(() => invoke(d),
      (err) => err.statusCode === 503 && err.code === "send_status_unavailable", name);
    assert.equal(d.calls.created.length + d.calls.drafts.length, 0);
    assert.equal(d.calls.sent.length, 0);
  }
});

test("an already-sent operation is replayed without another draft or send", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps({ graph: { findByAppId: async () => ({ id: "sent-1", isDraft: false }) } });
    const result = await invoke(d);
    assert.equal(result.alreadySent, true, name);
    assert.equal(d.calls.created.length + d.calls.drafts.length, 0);
    assert.equal(d.calls.sent.length, 0);
  }
});

test("environment and administrator kill switches stop both paths before a draft", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const environment = deps({ core: { config: () => directCfg({ directSendEnvironmentEnabled: false }) } });
    await assert.rejects(() => invoke(environment),
      (err) => err.statusCode === 403 && err.code === "direct_send_disabled", `${name} environment`);
    assert.equal(environment.calls.created.length + environment.calls.drafts.length, 0);

    const admin = deps({ store: { policy: async () => ({ killed: true, reason: "Paused by admin" }) } });
    await assert.rejects(() => invoke(admin),
      (err) => err.statusCode === 403 && err.code === "direct_send_disabled", `${name} admin`);
    assert.equal(admin.calls.created.length + admin.calls.drafts.length, 0);
  }
});

test("the production allowlist covers external Reply All participants", async () => {
  const d = deps({
    core: { config: () => directCfg({ testAllowlist: new Set(["advisor@ml.com"]) }) },
    graph: { getMessageContent: async () => ({ id: "g1", subject: "Thread", conversationId: "c1",
      from: { emailAddress: { address: "advisor@ml.com" } },
      toRecipients: [{ emailAddress: { address: "bo@eicatlanta.com" } }],
      ccRecipients: [{ emailAddress: { address: "assistant@morganstanley.com" } }],
    }) },
  });
  await assert.rejects(() => send.reply(BO, { ...REPLY, replyAll: true }, d),
    (err) => err.statusCode === 403 && err.code === "recipient_not_allowlisted");
  assert.equal(d.calls.created.length, 0);
  assert.equal(d.calls.sent.length, 0);
});

test("the caller mailbox is excluded from the effective Reply All audience", async () => {
  const d = deps({
    auth: { tokenFor: async () => ({ accessToken: "t", mailbox: "bo@outside.example",
      profile: { mail: "bo@outside.example" } }) },
    core: { config: () => directCfg({ testAllowlist:
      new Set(["advisor@ml.com", "assistant@morganstanley.com"]) }) },
    graph: { getMessageContent: async () => ({ id: "g1", subject: "Thread", conversationId: "c1",
      from: { emailAddress: { address: "advisor@ml.com" } },
      toRecipients: [{ emailAddress: { address: "bo@outside.example" } }],
      ccRecipients: [{ emailAddress: { address: "assistant@morganstanley.com" } }],
    }) },
  });
  const result = await send.reply(BO, { ...REPLY, replyAll: true }, d);
  assert.equal(result.ok, true);
  assert.equal(d.calls.sent.length, 1);
});

test("internal compliance BCC does not have to duplicate an external canary allowlist", async () => {
  const d = deps({ core: { config: () => directCfg({
    testAllowlist: new Set(["advisor@ml.com"]) }) } });
  const result = await send.reply(BO, { ...REPLY, documentIds: ["doc-1"] }, d);
  assert.equal(result.complianceCopied, true);
  assert.equal(d.calls.sent.length, 1);
});

test("rolling external limit refusal stops both paths before a draft", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps({ limitGuard: { reserve: async () => {
      const err = new Error("rolling limit"); err.statusCode = 429; throw err;
    } } });
    await assert.rejects(() => invoke(d), (err) => err.statusCode === 429, name);
    assert.equal(d.calls.created.length + d.calls.drafts.length, 0);
    assert.equal(d.calls.sent.length, 0);
  }
});

test("a rolling-limit reservation replay is bound to its external count", () => {
  assert.deepEqual(realLimitGuard.replayReservation({ externalCount: 2 }, 2),
    { alreadyReserved: true, externalCount: 2 });
  assert.throws(() => realLimitGuard.replayReservation({ externalCount: 1 }, 2),
    (err) => err.statusCode === 409 && err.code === "idempotency_conflict");
});

test("the same operation cannot expand from Reply to Reply All after a pre-send failure", async () => {
  const reservations = new Map();
  const d = deps({
    limitGuard: { reserve: async (_userId, id, count) => {
      if (reservations.has(id)) return realLimitGuard.replayReservation(reservations.get(id), count);
      reservations.set(id, { externalCount: count });
      return { alreadyReserved: false, externalCount: count };
    } },
    graph: { getMessageContent: async () => ({ id: "g1", subject: "Thread", conversationId: "c1",
      from: { emailAddress: { address: "advisor@ml.com" } },
      toRecipients: [{ emailAddress: { address: "bo@eicatlanta.com" } }],
      ccRecipients: [{ emailAddress: { address: "assistant@morganstanley.com" } }],
    }) },
  });
  let policyReads = 0;
  d.store.policy = async () => (++policyReads === 2
    ? { killed: true, reason: "Stopped before submission" }
    : { killed: false, reason: "" });

  await assert.rejects(() => send.reply(BO, REPLY, d),
    (err) => err.statusCode === 403 && err.code === "direct_send_disabled");
  assert.equal(d.calls.created.length, 1, "the first attempt prepared one unsent draft");
  assert.equal(d.calls.sent.length, 0);

  await assert.rejects(() => send.reply(BO, { ...REPLY, replyAll: true }, d),
    (err) => err.statusCode === 409 && err.code === "idempotency_conflict");
  assert.equal(d.calls.created.length, 1, "the conflicting replay is stopped before another draft");
  assert.equal(d.calls.sent.length, 0);
});

test("Reply All reserves every external participant but not self or internal compliance", async () => {
  const d = deps({ graph: { getMessageContent: async () => ({ id: "g1", subject: "Thread",
    conversationId: "c1", from: { emailAddress: { address: "advisor@ml.com" } },
    toRecipients: [{ emailAddress: { address: "bo@eicatlanta.com" } }],
    ccRecipients: [{ emailAddress: { address: "assistant@morganstanley.com" } }],
  }) } });
  await send.reply(BO, { ...REPLY, replyAll: true, documentIds: ["doc-1"] }, d);
  assert.equal(d.calls.reservations.length, 2, "initial and immediate pre-send policy reads");
  assert.deepEqual(d.calls.reservations.map((args) => args[2]), [2, 2]);
});

test("mailbox pacing waits and reacquires after preparation but before send", async () => {
  for (const [name, invoke, installDraft] of [
    ["reply", (d) => send.reply(BO, REPLY, d),
      (d, order) => { d.graph.createReply = async () => { order.push("draft"); return { id: "d1" }; }; }],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d),
      (d, order) => { d.graph.createDraft = async () => { order.push("draft"); return { id: "d2" }; }; }],
  ]) {
    const d = deps();
    const order = [], slots = [2, 0];
    d.mailboxGate.acquire = async () => { order.push("pace"); return slots.shift(); };
    d.wait = async (ms) => { order.push(`wait:${ms}`); };
    installDraft(d, order);
    d.graph.sendDraft = async (_token, id) => { order.push("send"); d.calls.sent.push(id); };
    await invoke(d);
    assert.deepEqual(order, ["draft", "pace", "wait:2000", "pace", "send"], name);
    assert.equal(d.calls.sent.length, 1);
  }
});

test("a mailbox that stays busy leaves the prepared draft unsent after 30 seconds", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps({ mailboxGate: { acquire: async () => 10 } });
    await assert.rejects(() => invoke(d),
      (err) => err.statusCode === 429 && err.code === "mailbox_busy", name);
    assert.deepEqual(d.calls.waits, [10000, 10000, 10000]);
    assert.equal(d.calls.created.length + d.calls.drafts.length, 1);
    assert.equal(d.calls.sent.length, 0);
  }
});

test("policy is rechecked immediately before send", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps();
    let reads = 0;
    d.store.policy = async () => (++reads === 1
      ? { killed: false, reason: "" } : { killed: true, reason: "Stopped during preparation" });
    await assert.rejects(() => invoke(d),
      (err) => err.statusCode === 403 && err.code === "direct_send_disabled", name);
    assert.equal(reads, 2);
    assert.equal(d.calls.created.length + d.calls.drafts.length, 1, "the second read is after preparation");
    assert.equal(d.calls.sent.length, 0);
  }
});

test("operation stamping is required on both paths before send", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps({ graph: { request: async () => { throw new Error("stamp failed"); } } });
    await assert.rejects(() => invoke(d), /stamp failed/, name);
    assert.equal(d.calls.created.length + d.calls.drafts.length, 1);
    assert.equal(d.calls.sent.length, 0);
  }
});

/* ---- attachments and the compliance copy --------------------------------- */

test("NO attachment means no compliance blind copy", async () => {
  const d = deps();
  const r = await send.reply(BO, REPLY, d);
  assert.equal(r.complianceCopied, false);
  assert.equal(d.calls.patched.filter((p) => p.b && p.b.bccRecipients).length, 0);
});

test("an approved document is attached AND blind-copied to compliance", async () => {
  const d = deps();
  const r = await send.reply(BO, { ...REPLY, documentIds: ["doc-1"] }, d);
  assert.equal(d.calls.docs[0].d[0].id, "doc-1");
  assert.equal(r.complianceCopied, true);
  const patch = d.calls.patched.find((p) => p.b && p.b.bccRecipients);
  assert.equal(patch.b.bccRecipients[0].emailAddress.address, "compliance@eicatlanta.com");
});

test("a file off the rep's device is attached AND blind-copied", async () => {
  const d = deps();
  const r = await send.reply(BO, { ...REPLY,
    files: [{ name: "forecast.xlsx", contentType: "application/vnd.ms-excel", data: b64("hello") }] }, d);
  assert.equal(d.calls.files[0].f[0].name, "forecast.xlsx");
  assert.equal(d.calls.files[0].f[0].bytes.toString(), "hello");
  assert.equal(r.complianceCopied, true,
    "an uploaded file is material reaching an advisor, exactly like an approved one");
});

test("the compliance copy is applied BEFORE the send, not after", async () => {
  const d = deps();
  const order = [];
  d.graph.request = async (_t, m, p, b) => { if (b && b.bccRecipients) order.push("bcc"); return { data: {} }; };
  d.graph.sendDraft = async () => { order.push("send"); };
  await send.reply(BO, { ...REPLY, documentIds: ["doc-1"] }, d);
  assert.deepEqual(order, ["bcc", "send"], "a copy added after the send copies nobody");
});

test("too many attachments is refused before anything is created", async () => {
  const d = deps();
  await assert.rejects(() => send.reply(BO, { ...REPLY,
    documentIds: ["a", "b", "c", "d", "e", "f"] }, d),
    (err) => err.statusCode === 400 && err.code === "too_many_files");
  assert.equal(d.calls.created.length, 0);
});

test("the size limit is enforced on the bytes received, not on a claimed size", async () => {
  const d = deps({ core: { config: () => directCfg({ maxAttachmentBytes: 10 }),
                           corporateSignature: () => "", complianceBcc: () => [] } });
  await assert.rejects(() => send.reply(BO, { ...REPLY,
    files: [{ name: "big.bin", data: b64("x".repeat(50)) }] }, d),
    (err) => err.statusCode === 400 && err.code === "too_large");
});

test("an unknown document id is refused rather than silently dropped", async () => {
  const d = deps({ store: { getDocuments: async () => [] } });
  await assert.rejects(() => send.reply(BO, { ...REPLY, documentIds: ["gone"] }, d),
    (err) => err.code === "no_such_document");
});

/* ---- follow-up ----------------------------------------------------------- */

test("a follow-up starts a NEW conversation, not the old thread", async () => {
  const d = deps();
  const r = await send.followUp(BO, FOLLOW, d);
  assert.equal(d.calls.created.length, 0, "createReply would revive a finished conversation");
  assert.equal(d.calls.drafts[0].subject, "Following up");
  assert.equal(r.conversationId, "conv-new");
  assert.equal(d.calls.sent[0], "draft-2");
});

test("a follow-up carries the signature and no template", async () => {
  const d = deps();
  await send.followUp(BO, FOLLOW, d);
  const draft = d.calls.drafts[0];
  assert.match(draft.signatureHtml, /Bo — EIC/);
  assert.match(draft.bodyHtml, /It has been a while\./);
  assert.ok(!/template/i.test(JSON.stringify(draft)), "a blank sheet, by design");
});

test("the follow-up recipient comes from the activity log, never from the request", async () => {
  const d = deps();
  await send.followUp(BO, { ...FOLLOW, to: "attacker@example.com",
                            recipientEmail: "attacker@example.com" }, d);
  assert.equal(d.calls.drafts[0].recipientEmail, "advisor@ml.com",
    "a client-named address would let this endpoint mail anybody from a rep's mailbox");
});

test("a follow-up to an advisor with no observed address is refused", async () => {
  const d = deps({ advisors: { emailForCrd: async () => "" },
                   store: { listActivity: async () => [] } });
  await assert.rejects(() => send.followUp(BO, FOLLOW, d),
    (err) => err.statusCode === 409 && err.code === "no_known_address");
});

test("a follow-up with an attachment is blind-copied to compliance", async () => {
  const d = deps();
  const r = await send.followUp(BO, { ...FOLLOW, documentIds: ["doc-1"] }, d);
  assert.equal(r.complianceCopied, true);
});

test("a follow-up needs a subject and a body", async () => {
  await assert.rejects(() => send.followUp(BO, { ...FOLLOW, subject: "" }, deps()),
    (err) => err.statusCode === 400);
  await assert.rejects(() => send.followUp(BO, { ...FOLLOW, text: "  " }, deps()),
    (err) => err.statusCode === 400);
});

test("both paths record the send immediately and store no body", async () => {
  const d = deps();
  await send.reply(BO, REPLY, d);
  await send.followUp(BO, FOLLOW, d);
  assert.equal(d.calls.activity.length, 2);
  assert.deepEqual(d.calls.activity.map((a) => a.source), ["app_reply", "app_followup"]);
  for (const row of d.calls.activity)
    for (const key of ["body", "bodyPreview", "uniqueBody", "text", "files"])
      assert.ok(!(key in row), `${key} must never reach storage`);
});

test("both paths refresh then complete outbound work, clearing a due schedule", async () => {
  const d = deps();
  const calls = [];
  const completed = [];
  d.store.putEngagement = async (_userId, crd, value) => {
    calls.push(`complete:${crd}`);
    completed.push(value);
    return value;
  };
  d.engagement = {
    ...engagement,
    refresh: async (_userId, crd) => { calls.push(`refresh:${crd}`); return {}; },
  };

  await send.reply(BO, REPLY, d);
  await send.followUp(BO, FOLLOW, d);

  assert.deepEqual(calls, ["refresh:111", "complete:111", "refresh:111", "complete:111"]);
  assert.equal(completed.length, 2);
  for (const state of completed) {
    assert.equal(state.replyState, "done");
    assert.ok(state.actedAt);
    assert.equal(state.nextActionAt, "");
    assert.equal(state.nextActionType, "");
    assert.equal(state.snoozedUntilUtc, "");
  }
});

test("both direct paths acknowledge the atomic dirty marker inline", async () => {
  const d = deps();
  const repaired = [];
  const completed = [];
  d.store.recordActivity = async (row) => {
    d.calls.activity.push(row);
    return { ...row, dirtyMarker: {
      partitionKey: row.advisorCrd, rowKey: "zD|user-bo",
      userId: row.userId, advisorCrd: row.advisorCrd, etag: `v${d.calls.activity.length}`,
    } };
  };
  d.engagement = {
    refresh: async () => { throw new Error("marker path must be preferred"); },
    refreshDirty: async (marker) => { repaired.push(marker.etag); return { acknowledged: true }; },
    completeOutbound: async (_userId, crd) => { completed.push(crd); return {}; },
  };

  await send.reply(BO, REPLY, d);
  await send.followUp(BO, FOLLOW, d);

  assert.deepEqual(repaired, ["v1", "v2"]);
  assert.deepEqual(completed, ["111", "111"]);
});

test("projection completion failure remains nonfatal after either email is sent", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps();
    d.engagement = {
      refresh: async () => ({}),
      completeOutbound: async () => { throw new Error("projection unavailable"); },
    };

    const result = await invoke(d);
    assert.equal(result.ok, true, `${name} was already sent and must remain successful`);
    assert.equal(d.calls.sent.length, 1);
  }
});

test("projection refresh failure cannot skip completing work already sent", async () => {
  for (const [name, invoke] of [
    ["reply", (d) => send.reply(BO, REPLY, d)],
    ["follow-up", (d) => send.followUp(BO, FOLLOW, d)],
  ]) {
    const d = deps();
    const calls = [];
    d.engagement = {
      refresh: async () => { calls.push("refresh"); throw new Error("projection stale"); },
      completeOutbound: async (_userId, crd) => { calls.push(`complete:${crd}`); return {}; },
    };

    const result = await invoke(d);
    assert.equal(result.ok, true, `${name} was accepted by Graph and remains successful`);
    assert.deepEqual(calls, ["refresh", "complete:111"],
      `${name} completion is an independent best-effort decision write`);
    assert.equal(d.calls.sent.length, 1);
  }
});
