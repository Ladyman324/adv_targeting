"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const engagement = require("../shared/email-engagement");

const DAY = 24 * 3600 * 1000;
const ago = (days) => new Date(Date.now() - days * DAY).toISOString();

function ev(over = {}) {
  return { userId: "u1", direction: "inbound", classification: "reply",
           route: "thread_match", occurredAt: ago(1),
           advisorEmail: "advisor@ml.com", advisorCrd: "111", ...over };
}

/* ---- fold: what the log adds up to --------------------------------------- */

test("a reply sets the state to new and records when it arrived", () => {
  const s = engagement.fold([ev()]);
  assert.equal(s.replyState, "new");
  assert.equal(s.everReplied, true);
  assert.ok(s.lastReplyAt);
});

test("an out-of-office is inbound mail but is NOT a reply", () => {
  const s = engagement.fold([ev({ classification: "auto_reply" })]);
  assert.equal(s.everReplied, false, "an away message must never open the morning queue");
  assert.equal(s.replyState, "none");
  assert.ok(s.lastInboundAt, "it is still inbound activity");
});

test("a bounce is flagged and is not a reply", () => {
  const s = engagement.fold([ev({ classification: "bounce" })]);
  assert.equal(s.hasBounce, true);
  assert.equal(s.everReplied, false);
});

test("a reviewed conversation does not fall back to new on a later sweep", () => {
  const previous = { replyState: "reviewed", actedAt: ago(1) };
  const s = engagement.fold([ev({ occurredAt: ago(3) })], previous);
  assert.equal(s.replyState, "reviewed",
    "re-seeing an already-handled reply must not re-open it");
});

test("but a reply NEWER than the rep's last action does re-open it", () => {
  const previous = { replyState: "reviewed", actedAt: ago(3) };
  const s = engagement.fold([ev({ occurredAt: ago(1) })], previous);
  assert.equal(s.replyState, "new", "they have answered again since we last acted");
});

test("30-day counts separate the directions", () => {
  const s = engagement.fold([
    ev({ direction: "outbound", classification: "sent", occurredAt: ago(2) }),
    ev({ direction: "outbound", classification: "sent", occurredAt: ago(60) }),
    ev({ occurredAt: ago(3) }),
  ]);
  assert.equal(s.outbound30d, 1, "the 60-day-old send is outside the window");
  assert.equal(s.inbound30d, 1);
});

/* ---- reason: why somebody is in the queue -------------------------------- */

test("a new reply outranks everything else", () => {
  const s = engagement.fold([ev()]);
  assert.equal(engagement.reason(s), "reply_new");
});

test("a due follow-up surfaces once its date passes", () => {
  const base = { replyState: "reviewed", nextActionAt: ago(1), lastActivityAt: ago(2) };
  assert.equal(engagement.reason(base), "due");
  assert.equal(engagement.reason({ ...base, nextActionAt: new Date(Date.now() + DAY).toISOString() }),
    null, "a follow-up scheduled for tomorrow is not work for today");
});

test("someone who never replied going quiet is NOT queue-worthy", () => {
  const s = engagement.fold([
    ev({ direction: "outbound", classification: "sent", occurredAt: ago(90) })]);
  assert.equal(engagement.reason(s), null,
    "cold outreach going unanswered is the ordinary case, not a lapse");
});

test("a WARM contact going quiet is queue-worthy", () => {
  const s = engagement.fold([ev({ occurredAt: ago(90) })], { replyState: "done", actedAt: ago(89) });
  assert.equal(engagement.reason(s), "quiet_warm");
});

test("a finished conversation with recent activity is not in the queue", () => {
  const s = engagement.fold([ev({ occurredAt: ago(2) })], { replyState: "done", actedAt: ago(1) });
  assert.equal(engagement.reason(s), null);
});

test("only ONE reason is returned, the most urgent", () => {
  const s = { ...engagement.fold([ev()]), hasBounce: true, nextActionAt: ago(5) };
  assert.equal(engagement.reason(s), "reply_new",
    "a person listed three times is a queue a rep stops reading");
});

/* ---- queue: ordering and shape ------------------------------------------- */

function queueStore(rows) {
  return { store: { listEngagement: async () => rows } };
}

test("replies sort above quiet contacts however old the quiet one is", async () => {
  const q = await engagement.queue("u1", queueStore([
    { advisorCrd: "quiet", everReplied: true, lastActivityAt: ago(300), replyState: "reviewed" },
    { advisorCrd: "replied", replyState: "new", lastReplyAt: ago(1), lastActivityAt: ago(1) },
  ]));
  assert.equal(q.entries[0].advisorCrd, "replied");
  assert.equal(q.entries[1].advisorCrd, "quiet");
});

test("within one reason the longest-waiting comes first", async () => {
  const q = await engagement.queue("u1", queueStore([
    { advisorCrd: "recent", replyState: "new", lastReplyAt: ago(1), lastActivityAt: ago(1) },
    { advisorCrd: "older", replyState: "new", lastReplyAt: ago(9), lastActivityAt: ago(9) },
  ]));
  assert.equal(q.entries[0].advisorCrd, "older",
    "the thing waiting longest is the thing most likely to be forgotten");
});

test("the queue counts by reason and excludes anyone with no reason", async () => {
  const q = await engagement.queue("u1", queueStore([
    { advisorCrd: "a", replyState: "new", lastReplyAt: ago(1), lastActivityAt: ago(1) },
    { advisorCrd: "b", hasBounce: true, lastActivityAt: ago(1) },
    { advisorCrd: "c", replyState: "none", lastActivityAt: ago(1) },
  ]));
  assert.equal(q.count, 2);
  assert.equal(q.counts.reply_new, 1);
  assert.equal(q.counts.bounced, 1);
});

test("the queue reports no volume metric of any kind", async () => {
  const q = await engagement.queue("u1", queueStore([
    { advisorCrd: "a", replyState: "new", lastReplyAt: ago(1), lastActivityAt: ago(1) }]));
  const keys = Object.keys(q).concat(Object.keys(q.entries[0]));
  for (const banned of ["sent", "sentCount", "emailsSent", "volume", "outbound30d"])
    assert.ok(!keys.includes(banned),
      `${banned} would make the dashboard reward sending, which the 25/day limits exist to restrain`);
});

/* ---- rep decisions ------------------------------------------------------- */

test("marking a reply reviewed stamps when, so it cannot re-open by itself", async () => {
  let saved = null;
  const deps = { store: { getEngagement: async () => ({ replyState: "new" }),
                          putEngagement: async (_u, _c, v) => { saved = v; return v; } } };
  await engagement.setReplyState("u1", "111", "reviewed", deps);
  assert.equal(saved.replyState, "reviewed");
  assert.ok(saved.actedAt, "without this the next sweep would call it new again");
});

test("an unknown reply state is refused", async () => {
  await assert.rejects(
    () => engagement.setReplyState("u1", "111", "banana",
      { store: { getEngagement: async () => ({}), putEngagement: async (a) => a } }),
    (err) => err.statusCode === 400);
});

test("the projection is rebuildable: fold is a pure function of the log", () => {
  const log = [ev({ occurredAt: ago(5) }), ev({ direction: "outbound", classification: "sent", occurredAt: ago(6) })];
  const a = engagement.fold(log);
  const b = engagement.fold(log);
  assert.deepEqual(a, b, "a cache that cannot be regenerated is a second source of truth");
});
