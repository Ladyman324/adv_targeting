"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const repair = require("../email-campaign-repair/index");

const NOW = Date.parse("2026-08-28T12:00:00Z");
function message(state, extra = {}) {
  return { id: "m1", state, ordinal: 0, sendPosition: 0,
    updatedUtc: "2026-08-28T11:00:00Z", leaseUntilUtc: "", retryAfterUtc: "", ...extra };
}
function batch(extra = {}) {
  return { id: "b1", status: "sending", mode: "send",
    approvedUtc: "2026-08-28T10:00:00Z", sendNotBeforeUtc: "2026-08-28T10:01:00Z", ...extra };
}

test("work mapping repairs every safe nonterminal phase", () => {
  assert.equal(repair.workFor(message("draft_pending"), batch(), NOW, 5), "draft");
  assert.equal(repair.workFor(message("draft_creating"), batch(), NOW, 5), "draft");
  assert.equal(repair.workFor(message("send_scheduled"), batch(), NOW, 5), "send");
  assert.equal(repair.workFor(message("sending"), batch(), NOW, 5), "send");
  assert.equal(repair.workFor(message("submitted"), batch(), NOW, 5), "reconcile");
  assert.equal(repair.workFor(message("send_ambiguous"), batch(), NOW, 5), "reconcile");
});

test("repair respects leases, retry times, pacing, and terminal states", () => {
  assert.equal(repair.workFor(message("sending", { leaseUntilUtc: "2026-08-28T12:01:00Z" }), batch(), NOW, 5), null);
  assert.equal(repair.workFor(message("send_ambiguous", { retryAfterUtc: "2026-08-28T12:01:00Z" }), batch(), NOW, 5), null);
  assert.equal(repair.workFor(message("sent"), batch(), NOW, 5), null);
  assert.equal(repair.workFor(message("send_scheduled", { sendPosition: 20 }),
    batch({ sendNotBeforeUtc: "2026-08-28T11:59:00Z" }), NOW, 5), null);
  assert.equal(repair.workFor(message("draft_pending"), batch({ approvedUtc: "" }), NOW, 5), null);
  assert.equal(repair.workFor(message("send_scheduled"), batch({ sendNotBeforeUtc: "" }), NOW, 5), null);
});

test("timer re-enqueues identifiers without a browser or Graph call", async () => {
  const savedEnabled = process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  const savedUsers = process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS;
  process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = "1";
  process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS = "u1";
  const enqueued = [];
  try {
    const result = await repair.run({ log() {} }, {
      now: () => NOW,
      core: { config: () => ({ mailboxIntervalSeconds: 5 }) },
      store: {
        listConnections: async () => [{ userId: "u1" }, { userId: "u2" }],
        listBatches: async () => [batch()],
        listMessages: async () => [message("draft_pending"), message("submitted", { id: "m2" })],
      },
      enqueue: async (work, delay) => enqueued.push({ work, delay }),
    });
    assert.equal(result.enqueued, 2);
    assert.deepEqual(enqueued.map((item) => item.work.kind), ["draft", "reconcile"]);
    assert.deepEqual(enqueued.map((item) => item.delay), [0, 5]);
    assert.ok(enqueued.every((item) => item.work.userId === "u1" && item.work.batchId === "b1"));
  } finally {
    if (savedEnabled === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
    else process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = savedEnabled;
    if (savedUsers === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS;
    else process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS = savedUsers;
  }
});

test("an approved batch promotes stale editing rows before paced dispatch", async () => {
  const savedEnabled = process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = "1";
  const row = message("editing", { etag: "v1" });
  const enqueued = [];
  try {
    const result = await repair.run({ log() {} }, {
      now: () => NOW,
      core: { config: () => ({ mailboxIntervalSeconds: 7 }) },
      store: {
        listConnections: async () => [{ userId: "u1" }],
        listBatches: async () => [batch({ status: "drafting" })],
        listMessages: async () => [row],
        patchMessage: async (_u, _b, _m, patch, etag) => {
          assert.equal(etag, "v1");
          return { ...row, ...patch, updatedUtc: "2026-08-28T11:00:00Z", etag: "v2" };
        },
      },
      enqueue: async (work, delay) => enqueued.push({ work, delay }),
    });
    assert.equal(result.promoted, 1);
    assert.equal(result.enqueued, 1);
    assert.equal(enqueued[0].work.kind, "draft");
    assert.equal(enqueued[0].delay, 0);
  } finally {
    if (savedEnabled === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
    else process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = savedEnabled;
  }
});

test("one user's backlog is capped so another user gets a turn", async () => {
  const savedEnabled = process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  const savedUsers = process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS;
  delete process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS;
  process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = "1";
  const seen = [];
  try {
    const result = await repair.run({ log() {} }, {
      now: () => NOW,
      core: { config: () => ({ mailboxIntervalSeconds: 5 }) },
      store: {
        listConnections: async () => [{ userId: "u1" }, { userId: "u2" }],
        listBatches: async (userId) => [batch({ id: "b-" + userId })],
        listMessages: async (userId) => Array.from({ length: userId === "u1" ? 20 : 1 },
          (_, index) => message("submitted", { id: userId + "-" + index })),
      },
      enqueue: async (work) => seen.push(work.userId),
    });
    assert.equal(seen.filter((user) => user === "u1").length, 2);
    assert.equal(seen.filter((user) => user === "u2").length, 1);
    assert.equal(result.enqueued, 3);
  } finally {
    if (savedEnabled === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
    else process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = savedEnabled;
    if (savedUsers === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS;
    else process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS = savedUsers;
  }
});

test("timer is inert unless explicitly enabled", async () => {
  const saved = process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  let reads = 0;
  try {
    const result = await repair.run({ log() {} }, { store: new Proxy({}, {
      get() { reads++; throw new Error("must not read storage"); },
    }) });
    assert.equal(result.enabled, false);
    assert.equal(reads, 0);
  } finally {
    if (saved === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
    else process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = saved;
  }
});
