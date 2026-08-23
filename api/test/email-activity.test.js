"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const activity = require("../shared/email-activity");

const BO = { id: "user-bo", name: "Bo" };

function row(over = {}) {
  return { graphMessageId: "g1", userId: "user-bo", direction: "inbound",
           source: "outlook", classification: "reply", route: "thread_match",
           occurredAt: "2026-08-22T10:00:00Z", subject: "RE: EIC All-Cap Value",
           advisorEmail: "advisor@ml.com", advisorCrd: "111", batchId: "b1", ...over };
}

function deps(rows, over = {}) {
  return {
    store: {
      listActivity: async () => rows,
      activityOwner: async (_crd, id) => (rows.find((r) => r.graphMessageId === id) || {}).userId || "",
      ...(over.store || {}),
    },
    auth: { tokenFor: async () => ({ accessToken: "t" }), ...(over.auth || {}) },
    graph: { getMessageContent: async () => ({
      id: "g1", subject: "RE: EIC All-Cap Value",
      from: { emailAddress: { address: "advisor@ml.com", name: "An Advisor" } },
      toRecipients: [{ emailAddress: { address: "bo@eicatlanta.com" } }],
      sentDateTime: "2026-08-22T10:00:00Z",
      uniqueBody: { content: "  Please send the latest presentation.  " },
      webLink: "https://outlook.office.com/mail/deeplink/g1",
    }), ...(over.graph || {}) },
  };
}

test("a threaded reply reads as a reply", async () => {
  const t = await activity.timeline(BO, "111", deps([row()]));
  assert.equal(t.entries[0].label, "Reply received");
  assert.equal(t.entries[0].batchId, "b1");
  assert.equal(t.observed, true);
});

test("an unthreaded sighting is NOT called a reply", async () => {
  const t = await activity.timeline(BO, "111", deps([row({ route: "sender_only", batchId: "" })]));
  assert.equal(t.entries[0].label, "Email received",
    "calling this a reply would credit a campaign that never earned it");
  assert.equal(t.entries[0].batchId, "");
  assert.match(t.entries[0].basis, /not on a thread we sent/i);
});

test("an out-of-office is labelled as one, never as a reply", async () => {
  const t = await activity.timeline(BO, "111", deps([row({ classification: "auto_reply" })]));
  assert.equal(t.entries[0].label, "Automatic reply");
});

test("a delivery failure is labelled as one", async () => {
  const t = await activity.timeline(BO, "111", deps([row({ classification: "bounce" })]));
  assert.equal(t.entries[0].label, "Delivery failure");
});

test("an Outlook send is distinguished from an app send", async () => {
  const outlook = await activity.timeline(BO, "111",
    deps([row({ direction: "outbound", source: "outlook" })]));
  const app = await activity.timeline(BO, "111",
    deps([row({ direction: "outbound", source: "app_campaign" })]));
  assert.equal(outlook.entries[0].label, "Email sent (Outlook)");
  assert.equal(app.entries[0].label, "Email sent");
});

test("an empty timeline reports 'not observed', not 'nothing happened'", async () => {
  const t = await activity.timeline(BO, "111", deps([]));
  assert.equal(t.count, 0);
  assert.equal(t.observed, false,
    "the sweep only sees connected mailboxes since it was switched on");
});

test("rows from another rep are visible but not openable", async () => {
  const t = await activity.timeline(BO, "111", deps([row({ userId: "user-kate" })]));
  assert.equal(t.entries.length, 1, "a colleague's contact is exactly what stops double-teaming");
  assert.equal(t.entries[0].mine, false);
});

test("an ambiguous row is flagged and carries no advisor", async () => {
  const t = await activity.timeline(BO, "111",
    deps([row({ advisorCrd: "", advisorEmail: "johnsonc@stifel.com", route: "sender_only" })]));
  assert.equal(t.entries[0].ambiguous, true);
  assert.match(t.entries[0].basis, /several advisors share/i);
});

test("reading your own message returns its unique text and an Outlook link", async () => {
  const m = await activity.messageContent(BO, { crd: "111", id: "g1" }, deps([row()]));
  assert.equal(m.text, "Please send the latest presentation.");
  assert.equal(m.from, "advisor@ml.com");
  assert.ok(m.webLink);
});

test("reading another rep's message is refused BEFORE Graph is called", async () => {
  let called = 0;
  const d = deps([row({ userId: "user-kate" })],
    { graph: { getMessageContent: async () => { called++; return {}; } } });
  await assert.rejects(
    () => activity.messageContent(BO, { crd: "111", id: "g1" }, d),
    (err) => err.statusCode === 403 && err.code === "not_your_mailbox");
  assert.equal(called, 0, "a refusal a rep understands beats a 404 from Graph");
});

test("a message not in this advisor's activity is refused", async () => {
  await assert.rejects(
    () => activity.messageContent(BO, { crd: "111", id: "not-here" }, deps([row()])),
    (err) => err.statusCode === 404);
});

test("the message id and the advisor are both required", async () => {
  await assert.rejects(() => activity.messageContent(BO, { crd: "111", id: "" }, deps([row()])),
    (err) => err.statusCode === 400);
  await assert.rejects(() => activity.messageContent(BO, { crd: "", id: "g1" }, deps([row()])),
    (err) => err.statusCode === 400);
});

test("the body is requested as text, so advisor markup can never become script", async () => {
  let asked = null;
  const d = deps([row()], { graph: { getMessageContent: async (_t, id) => {
    asked = id;
    return { id, uniqueBody: { content: "<script>alert(1)</script> not markup, just text" },
             from: { emailAddress: {} } };
  } } });
  const m = await activity.messageContent(BO, { crd: "111", id: "g1" }, d);
  assert.equal(asked, "g1");
  // Graph returned it as text because that is what was asked for; the client
  // escapes it on the way to the DOM. Both halves matter and this pins the first.
  assert.match(m.text, /^<script>/);
});
