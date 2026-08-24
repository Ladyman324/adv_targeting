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

test("a bounce dismissal acknowledges only bounces observed before it", () => {
  const dismissedAt = ago(2);
  const previous = { bounceDismissed: true, bounceDismissedAt: dismissedAt };
  const old = engagement.fold([
    ev({ classification: "bounce", occurredAt: ago(3) }),
  ], previous);
  assert.equal(old.bounceDismissed, true,
    "refolding the bounce already acknowledged must not reopen it");

  const newer = engagement.fold([
    ev({ classification: "bounce", occurredAt: ago(1) }),
    ev({ classification: "bounce", occurredAt: ago(3) }),
  ], previous);
  assert.equal(newer.bounceDismissed, false,
    "a later delivery failure is new address work and must reopen the queue");
  assert.equal(engagement.reason(newer), "bounced");
});

test("legacy dismissed rows use their persisted updated time as the boundary", () => {
  const previous = { bounceDismissed: true, updatedUtc: ago(2) };
  const carried = engagement.fold([
    ev({ classification: "bounce", occurredAt: ago(3) }),
  ], previous);
  assert.equal(carried.bounceDismissed, true);
  assert.equal(carried.bounceDismissedAt, previous.updatedUtc,
    "the compatibility boundary becomes stable on the first new-code fold");
  assert.equal(engagement.fold([
    ev({ classification: "bounce", occurredAt: ago(1) }),
  ], previous).bounceDismissed, false);
});

test("dismissing a bounce cannot make an unread reply look reviewed", async () => {
  let dismissal = null;
  await engagement.dismissBounce("u1", "111", { store: {
    putEngagement: async (_u, _c, value) => { dismissal = value; return value; },
  } });
  assert.ok(dismissal.bounceDismissedAt);
  assert.ok(!("actedAt" in dismissal), "bounce work and reply work need separate clocks");

  const beforeDismissal = new Date(new Date(dismissal.bounceDismissedAt).getTime() - 1000).toISOString();
  const state = engagement.fold([
    ev({ classification: "reply", occurredAt: beforeDismissal }),
    ev({ classification: "bounce", occurredAt: beforeDismissal }),
  ], { replyState: "none", actedAt: "", ...dismissal });
  assert.equal(state.replyState, "new", "the reply is still unread work");
  assert.equal(state.actedAt, "");
  assert.equal(state.bounceDismissed, true);
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

test("queue actions match the reason and never offer an ineffective verb", async () => {
  assert.deepEqual(engagement.ACTIONS_BY_REASON, {
    reply_new: ["mark_reviewed", "snooze"],
    reply_followup: ["follow_up", "done", "snooze"],
    due: ["follow_up", "snooze"],
    bounced: ["dismiss_bounce", "snooze"],
    quiet_warm: ["follow_up", "snooze"],
  });
  const q = await engagement.queue("u1", queueStore([
    { advisorCrd: "due", nextActionAt: ago(1), lastActivityAt: ago(2) },
    { advisorCrd: "bounce", hasBounce: true, lastActivityAt: ago(1) },
  ]));
  const due = q.entries.find((row) => row.advisorCrd === "due");
  const bounce = q.entries.find((row) => row.advisorCrd === "bounce");
  assert.deepEqual(due.actions, ["follow_up", "snooze"]);
  assert.deepEqual(bounce.actions, ["dismiss_bounce", "snooze"]);
  assert.ok(!bounce.actions.includes("follow_up"));
  assert.ok(!due.actions.includes("done"));
});

test("a snoozed bounce returns as bounced, never as a follow-up due", async () => {
  let snooze = null;
  await engagement.snooze("u1", "111", 7, { store: {
    putEngagement: async (_u, _c, value) => { snooze = value; return value; },
  } });
  const row = { ...snooze, hasBounce: true, bounceDismissed: false };
  assert.equal(engagement.reason(row), null, "the active snooze still hides it");
  assert.equal(engagement.reason(row, Date.now() + 8 * 86400000), "bounced",
    "expiry cannot turn a known bad address into an invitation to follow up");
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

test("rep decisions are partial writes and cannot restore stale derived fields", async () => {
  const writes = [];
  const deps = { store: {
    getEngagement: async () => assert.fail("manual decisions must not read/rewrite the projection"),
    putEngagement: async (_u, _c, value) => { writes.push(value); return value; },
  } };
  await engagement.setReplyState("u1", "111", "reviewed", deps);
  await engagement.snooze("u1", "111", 30, deps);
  await engagement.dismissBounce("u1", "111", deps);
  assert.deepEqual(Object.keys(writes[0]).sort(), ["actedAt", "replyState"]);
  assert.deepEqual(Object.keys(writes[1]).sort(),
    ["actedAt", "nextActionAt", "nextActionType", "snoozedUntilUtc"]);
  assert.equal(writes[2].bounceDismissed, true);
  assert.ok(writes[2].bounceDismissedAt,
    "the dismissal needs a persisted boundary for later bounces");
  assert.deepEqual(Object.keys(writes[2]).sort(), ["bounceDismissed", "bounceDismissedAt"]);
});

test("a completed outbound action clears an already-due schedule", async () => {
  let saved = null;
  const deps = { store: { putEngagement: async (_u, _c, value) => { saved = value; return value; } } };
  await engagement.completeOutbound("u1", "111", deps);
  assert.equal(saved.replyState, "done");
  assert.ok(saved.actedAt);
  assert.equal(saved.nextActionAt, "");
  assert.equal(saved.nextActionType, "");
  assert.equal(saved.snoozedUntilUtc, "");
});

test("an unknown reply state is refused", async () => {
  await assert.rejects(
    () => engagement.setReplyState("u1", "111", "banana",
      { store: { getEngagement: async () => ({}), putEngagement: async (a) => a } }),
    (err) => err.statusCode === 400);
});

/* ---- projection concurrency -------------------------------------------- */

function storageConflict(statusCode) {
  return Object.assign(new Error(`storage conflict ${statusCode}`), { statusCode });
}

test("a stale refresh retries around every kind of rep decision", async (t) => {
  const cases = [
    {
      name: "mark reviewed",
      activity: [ev({ occurredAt: ago(3) })],
      initial: { replyState: "new", actedAt: "" },
      act: (deps) => engagement.setReplyState("u1", "111", "reviewed", deps),
      check: (row) => {
        assert.equal(row.replyState, "reviewed");
        assert.ok(row.actedAt);
      },
    },
    {
      name: "snooze",
      activity: [ev({ occurredAt: ago(3) })],
      initial: { replyState: "new", actedAt: "" },
      act: (deps) => engagement.snooze("u1", "111", 14, deps),
      check: (row) => {
        assert.ok(row.nextActionAt);
        assert.equal(row.nextActionType, "follow_up");
        assert.ok(row.snoozedUntilUtc);
      },
    },
    {
      name: "dismiss bounce",
      activity: [ev({ classification: "bounce", occurredAt: ago(3) })],
      initial: { replyState: "none", bounceDismissed: false },
      act: (deps) => engagement.dismissBounce("u1", "111", deps),
      check: (row) => {
        assert.equal(row.bounceDismissed, true);
        assert.ok(row.bounceDismissedAt);
      },
    },
    {
      name: "complete outbound",
      activity: [ev({ occurredAt: ago(3) })],
      initial: { replyState: "follow_up", nextActionAt: ago(1),
                 nextActionType: "follow_up", snoozedUntilUtc: ago(1) },
      act: (deps) => engagement.completeOutbound("u1", "111", deps),
      check: (row) => {
        assert.equal(row.replyState, "done");
        assert.ok(row.actedAt);
        assert.equal(row.nextActionAt, "");
        assert.equal(row.nextActionType, "");
        assert.equal(row.snoozedUntilUtc, "");
      },
    },
  ];

  for (const scenario of cases) await t.test(scenario.name, async () => {
    let version = 1;
    let row = { ...scenario.initial, etag: `v${version}` };
    let projectionAttempts = 0;
    const deps = { store: {
      listActivity: async () => scenario.activity.map((item) => ({ ...item })),
      getEngagement: async () => ({ ...row }),
      putEngagement: async (_u, _c, patch) => {
        row = { ...row, ...patch, etag: `v${++version}` };
        return { ...row };
      },
      putEngagementProjection: async (_u, _c, folded, etag) => {
        projectionAttempts++;
        if (projectionAttempts === 1) {
          assert.equal(etag, "v1");
          await scenario.act(deps);
          throw storageConflict(412);
        }
        assert.equal(etag, row.etag, "the retry must use the decision's new ETag");
        row = { ...row, ...folded, etag: `v${++version}` };
        return { ...row };
      },
    } };

    const saved = await engagement.refresh("u1", "111", deps);
    assert.equal(projectionAttempts, 2);
    scenario.check(saved);
  });
});

test("out-of-order refreshes cannot let an older fold replace a newer one", async () => {
  const oldReply = ev({ occurredAt: ago(5), advisorEmail: "old@ml.com" });
  const newReply = ev({ occurredAt: ago(1), advisorEmail: "new@ml.com" });
  let activity = [oldReply];
  let row = { replyState: "none", etag: "v1" };
  let releaseOld;
  let oldReachedWrite;
  const oldWaiting = new Promise((resolve) => { oldReachedWrite = resolve; });
  const release = new Promise((resolve) => { releaseOld = resolve; });
  let heldOld = false;
  let version = 1;
  const deps = { store: {
    listActivity: async () => activity.map((item) => ({ ...item })),
    getEngagement: async () => ({ ...row }),
    putEngagementProjection: async (_u, _c, folded, etag) => {
      if (folded.lastReplyAt === oldReply.occurredAt && !heldOld) {
        heldOld = true;
        oldReachedWrite();
        await release;
      }
      if (etag !== row.etag) throw storageConflict(412);
      row = { ...row, ...folded, etag: `v${++version}` };
      return { ...row };
    },
  } };

  const olderRefresh = engagement.refresh("u1", "111", deps);
  await oldWaiting;
  activity = [newReply, oldReply];
  const newerRefresh = engagement.refresh("u1", "111", deps);
  await newerRefresh;
  releaseOld();
  await olderRefresh;

  assert.equal(row.lastReplyAt, newReply.occurredAt);
  assert.equal(row.advisorEmail, "new@ml.com");
  assert.equal(row.etag, "v3", "the stale refresh must conflict and then refold");
});

test("a create conflict is reread and refolded instead of overwriting the winner", async () => {
  let row = null;
  let attempts = 0;
  const deps = { store: {
    listActivity: async () => [ev({ occurredAt: ago(3) })],
    getEngagement: async () => row && { ...row },
    putEngagementProjection: async (_u, _c, folded, etag) => {
      attempts++;
      if (attempts === 1) {
        assert.equal(etag, undefined);
        row = { replyState: "reviewed", actedAt: ago(1), etag: "v1" };
        throw storageConflict(409);
      }
      assert.equal(etag, "v1");
      row = { ...row, ...folded, etag: "v2" };
      return { ...row };
    },
  } };
  const saved = await engagement.refresh("u1", "111", deps);
  assert.equal(attempts, 2);
  assert.equal(saved.replyState, "reviewed");
  assert.equal(saved.actedAt, row.actedAt);
});

test("a historical-import retry keeps a newer manual action", async () => {
  let version = 1;
  let row = { replyState: "none", actedAt: "", etag: `v${version}` };
  let attempts = 0;
  let manualAt = "";
  const deps = { store: {
    listActivity: async () => [ev({ occurredAt: ago(30), historicalImport: true,
      seedBeforeUtc: ago(1) })],
    getEngagement: async () => ({ ...row }),
    putEngagement: async (_u, _c, patch) => {
      manualAt = new Date().toISOString();
      row = { ...row, ...patch, actedAt: manualAt, etag: `v${++version}` };
      return { ...row };
    },
    putEngagementProjection: async (_u, _c, folded, etag) => {
      attempts++;
      if (attempts === 1) {
        assert.equal(folded.replyState, "reviewed");
        assert.equal(folded.actedAt, "", "importing history must not fabricate a rep action");
        await engagement.setReplyState("u1", "111", "reviewed", deps);
        throw storageConflict(412);
      }
      assert.equal(etag, row.etag);
      row = { ...row, ...folded, etag: `v${++version}` };
      return { ...row };
    },
  } };

  const saved = await engagement.refresh("u1", "111", deps);
  assert.equal(attempts, 2);
  assert.equal(saved.replyState, "reviewed");
  assert.equal(saved.actedAt, manualAt,
    "event provenance is not permission to erase a later rep decision");
});

test("refresh stops after five projection conflicts and reports the last one", async () => {
  let reads = 0;
  let writes = 0;
  const last = storageConflict(412);
  const deps = { store: {
    listActivity: async () => { reads++; return [ev()]; },
    getEngagement: async () => ({ replyState: "none", etag: `v${reads}` }),
    putEngagementProjection: async () => {
      writes++;
      throw writes === 5 ? last : storageConflict(writes % 2 ? 409 : 412);
    },
  } };
  await assert.rejects(() => engagement.refresh("u1", "111", deps), (err) => err === last);
  assert.equal(reads, 5, "every retry must reread activity as well as engagement");
  assert.equal(writes, 5, "a hot row cannot spin forever inside one request");
});

test("the projection is rebuildable: fold is a pure function of the log", () => {
  const log = [ev({ occurredAt: ago(5) }), ev({ direction: "outbound", classification: "sent", occurredAt: ago(6) })];
  const a = engagement.fold(log);
  const b = engagement.fold(log);
  assert.deepEqual(a, b, "a cache that cannot be regenerated is a second source of truth");
});
