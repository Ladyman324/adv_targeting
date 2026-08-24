"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { FakeTableService } = require("./helpers/fake-table");
const engagement = require("../shared/email-engagement");
const repair = require("../email-engagement-repair/index");

function loadStore() {
  const service = new FakeTableService();
  const path = require.resolve("../shared/email-store");
  delete require.cache[path];
  process.env.AZURE_STORAGE_CONNECTION_STRING =
    "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net";
  const store = require(path);
  const dataTables = require.resolve("@azure/data-tables");
  const real = require.cache[dataTables].exports.TableClient.fromConnectionString;
  require.cache[dataTables].exports.TableClient.fromConnectionString = (_conn, name) => service.table(name);
  return { store, service, restore: () => {
    require.cache[dataTables].exports.TableClient.fromConnectionString = real;
    delete require.cache[path];
  } };
}

function event(overrides = {}) {
  return { userId: "u1", advisorCrd: "111", advisorEmail: "a@example.test",
    direction: "inbound", classification: "reply", occurredAt: "2026-01-01T00:00:00Z",
    ...overrides };
}

test("historical replies and bounces establish history without opening work", () => {
  const boundary = "2026-08-24T12:00:00Z";
  const state = engagement.fold([
    event({ historicalImport: true, seedBeforeUtc: boundary }),
    event({ classification: "bounce", occurredAt: "2026-02-01T00:00:00Z",
      historicalImport: true, seedBeforeUtc: boundary }),
  ]);
  assert.equal(state.everReplied, true);
  assert.equal(state.replyState, "reviewed");
  assert.equal(state.hasBounce, false);
  assert.equal(state.actedAt, "", "import must not fabricate a rep action");
  assert.equal(state.historySeedBeforeUtc, boundary);
  assert.equal(engagement.reason(state, new Date("2026-08-25T00:00:00Z").getTime()), null,
    "old imported relationships are not immediately quiet work");
});

test("current mail after the fixed import boundary still opens reply and bounce work", () => {
  const boundary = "2026-08-24T12:00:00Z";
  const imported = event({ historicalImport: true, seedBeforeUtc: boundary });
  const replyState = engagement.fold([imported,
    event({ occurredAt: "2026-08-24T12:01:00Z", historicalImport: false, seedBeforeUtc: boundary })]);
  assert.equal(replyState.replyState, "new");
  assert.equal(engagement.reason(replyState), "reply_new");
  const bounceState = engagement.fold([imported,
    event({ classification: "bounce", occurredAt: "2026-08-24T12:02:00Z",
      historicalImport: false, seedBeforeUtc: boundary })]);
  assert.equal(bounceState.hasBounce, true);
});

test("mail exactly at the backfill cutoff is current, not imported history", () => {
  const boundary = "2026-08-24T12:00:00Z";
  const state = engagement.fold([event({ occurredAt: boundary,
    historicalImport: true, seedBeforeUtc: boundary })]);
  assert.equal(state.replyState, "new",
    "the historical side is strictly before the captured cutoff");
  assert.equal(engagement.reason(state), "reply_new");
});

test("a later historical fold cannot erase already-current new work", () => {
  const current = event({ occurredAt: "2026-08-20T00:00:00Z", historicalImport: false });
  const before = engagement.fold([current]);
  const after = engagement.fold([current,
    event({ occurredAt: "2025-01-01T00:00:00Z", historicalImport: true,
      seedBeforeUtc: "2026-08-24T12:00:00Z" })], before);
  assert.equal(after.replyState, "new");
  assert.equal(after.lastReplyAt, current.occurredAt);
});

test("refreshDirty cannot acknowledge a marker changed after its activity snapshot", async () => {
  const { store, restore } = loadStore();
  try {
    const first = await store.recordActivity({ ...event(), graphMessageId: "first" });
    const realAck = store.ackEngagementDirty;
    store.ackEngagementDirty = async (marker) => {
      await store.recordActivity({ ...event({ occurredAt: "2026-08-24T13:00:00Z" }),
        graphMessageId: "arrived-during-refresh" });
      return realAck(marker);
    };
    const result = await engagement.refreshDirty(first.dirtyMarker, { store });
    assert.equal(result.acknowledged, false);
    assert.ok(await store.getEngagementDirty("111", "u1"));
  } finally { restore(); }
});

test("the timer is fail-closed and performs no reads while disabled", async () => {
  const saved = process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
  delete process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
  let reads = 0;
  try {
    const result = await repair.run({ log() {} }, { store: new Proxy({}, {
      get() { reads++; throw new Error("storage must not be touched"); },
    }) });
    assert.equal(result.disabled, true);
    assert.equal(reads, 0);
  } finally {
    if (saved === undefined) delete process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
    else process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED = saved;
  }
});

test("an unmatched canary claims no markers", async () => {
  const savedEnabled = process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
  const savedUsers = process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS;
  process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED = "1";
  process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS = "allowed";
  let claims = 0;
  try {
    const result = await repair.run({ log() {} }, { store: {
      getEngagementRepairCursor: async () => null,
      listEngagementDirtyPage: async () => ({ rows: [{ userId: "someone-else" }], continuationToken: "" }),
      claimEngagementDirty: async () => { claims++; },
      putEngagementRepairCursor: async () => {},
    } });
    assert.equal(claims, 0);
    assert.equal(result.unmatched, 1);
  } finally {
    if (savedEnabled === undefined) delete process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
    else process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED = savedEnabled;
    if (savedUsers === undefined) delete process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS;
    else process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS = savedUsers;
  }
});

test("cursor scope resets and poison rows cannot starve a later pending page", async () => {
  const savedEnabled = process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
  const savedUsers = process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS;
  process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED = "1";
  delete process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS;
  const tokens = [];
  let savedCursor = null;
  const poison = { status: "poison", userId: "u1", dirtyAtUtc: "2026-08-20T00:00:00Z" };
  const deferred = { status: "retry", userId: "u1", dirtyAtUtc: "2026-08-21T00:00:00Z",
    retryAfterUtc: "2026-08-25T00:00:00Z" };
  const leased = { status: "processing", userId: "u1", dirtyAtUtc: "2026-08-22T00:00:00Z",
    leaseUntilUtc: "2026-08-25T00:00:00Z" };
  const pending = { status: "pending", userId: "u1", advisorCrd: "111", etag: "v1",
    dirtyAtUtc: "2026-08-23T00:00:00Z" };
  try {
    const result = await repair.run({ log() {} }, { store: {
      getEngagementRepairCursor: async () => ({ scope: "stale-scope", continuationToken: "stale-token" }),
      listEngagementDirtyPage: async (token) => {
        tokens.push(token);
        return token ? { rows: [pending], continuationToken: "" }
          : { rows: [poison, deferred, leased], continuationToken: "next" };
      },
      claimEngagementDirty: async (marker) => marker.status === "pending" ? marker : null,
      putEngagementRepairCursor: async (scope, token) => { savedCursor = { scope, token }; },
    }, engagement: { refreshDirty: async () => ({ acknowledged: true }) },
      now: () => new Date("2026-08-24T12:00:00Z") });
    assert.deepEqual(tokens, ["", "next"], "a changed allowlist scope starts at the beginning");
    assert.equal(result.repaired, 1);
    assert.equal(result.poison, 1);
    assert.equal(result.deferred, 1);
    assert.equal(result.leased, 1);
    assert.equal(result.oldestDirtyAtUtc, "2026-08-20T00:00:00Z");
    assert.equal(savedCursor.token, "", "the completed scan wraps for the next invocation");
  } finally {
    if (savedEnabled === undefined) delete process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
    else process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED = savedEnabled;
    if (savedUsers === undefined) delete process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS;
    else process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS = savedUsers;
  }
});

test("a repair failure releases the lease with bounded retry metadata and a sanitized code", async () => {
  const savedEnabled = process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
  const savedUsers = process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS;
  process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED = "1";
  process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS = "u1";
  const { store, restore } = loadStore();
  try {
    await store.recordActivity({ ...event(), graphMessageId: "fail" });
    const result = await repair.run({ log() {}, warn() {} }, { store,
      engagement: { refreshDirty: async () => { throw new Error("private subject and address"); } },
      now: () => new Date("2026-08-24T12:00:00Z") });
    assert.equal(result.failed, 1);
    const marker = await store.getEngagementDirty("111", "u1");
    assert.equal(marker.status, "retry");
    assert.equal(marker.attemptCount, 1);
    assert.equal(marker.lastErrorCode, "engagement_refresh_failed");
    assert.equal(marker.retryAfterUtc, "2026-08-24T12:15:00.000Z");
    assert.equal(marker.leaseUntilUtc, "");
    assert.ok(!JSON.stringify(marker).includes("private subject"));

    await store.recordActivity({ ...event({ occurredAt: "2026-08-24T13:00:00Z" }),
      graphMessageId: "new-after-failure" });
    const reopened = await store.getEngagementDirty("111", "u1");
    assert.equal(reopened.status, "pending");
    assert.equal(reopened.attemptCount, 0);
    assert.equal(reopened.retryAfterUtc, "");
    assert.equal(reopened.lastErrorCode, "");
  } finally {
    restore();
    if (savedEnabled === undefined) delete process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED;
    else process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED = savedEnabled;
    if (savedUsers === undefined) delete process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS;
    else process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS = savedUsers;
  }
});
