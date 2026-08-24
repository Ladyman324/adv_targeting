"use strict";

/* The tests that would have caught the deployment blockers.
 *
 * Every one of these exercises a REAL storage behaviour that the hand-written
 * mocks could not express -- Replace deleting absent properties, ascending key
 * order, and the characters Azure refuses in a key. 186 tests passed while all
 * of this was broken, because the mocks recorded writes and modelled nothing
 * about what a write destroys.
 *
 * These drive the store's own logic against the faithful double rather than
 * re-testing the double.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");
const { FakeTableService } = require("./helpers/fake-table");

/* The store reads its connection string at require time and builds clients
 * lazily, so it is loaded fresh with the table factory swapped out. */
function loadStore() {
  const service = new FakeTableService();
  const path = require.resolve("../shared/email-store");
  delete require.cache[path];
  process.env.AZURE_STORAGE_CONNECTION_STRING =
    "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net";
  const store = require(path);
  const dataTables = require.resolve("@azure/data-tables");
  const real = require.cache[dataTables].exports.TableClient.fromConnectionString;
  require.cache[dataTables].exports.TableClient.fromConnectionString =
    (_conn, name) => service.table(name);
  return { store, service, restore: () => {
    require.cache[dataTables].exports.TableClient.fromConnectionString = real;
    delete require.cache[path];
  } };
}

function connection(overrides = {}) {
  return {
    partitionKey: "u1", rowKey: "graph", userName: "Bo",
    homeAccountId: "home-1", mailboxId: "u1", mailbox: "bo@example.test",
    profileJson: JSON.stringify({ id: "u1", profileVersion: 2 }),
    tokenCache: "cache-one", connectedUtc: "2026-08-24T12:00:00Z",
    needsReconnect: false, etag: 'W/"1"',
    ...overrides,
  };
}

/* Load email-auth with deterministic MSAL, crypto and store seams. The module
 * itself remains unchanged apart from its real retry logic; only its external
 * systems are replaced. */
function loadAuthHarness({ current: initial, acquire, put }) {
  const store = require("../shared/email-store");
  const crypt = require("../shared/email-crypto");
  const msal = require("@azure/msal-node");
  const authPath = require.resolve("../shared/email-auth");
  const settingKeys = ["GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_TENANT_ID", "GRAPH_REDIRECT_URI"];
  const saved = {
    getConnection: store.getConnection, putConnection: store.putConnection,
    encrypt: crypt.encrypt, decrypt: crypt.decrypt,
    cca: msal.ConfidentialClientApplication,
    interaction: msal.InteractionRequiredAuthError,
    env: Object.fromEntries(settingKeys.map((key) => [key, process.env[key]])),
  };
  let current = { ...initial };
  const caches = [];
  class InteractionRequiredAuthError extends Error {}
  class FakeCca {
    constructor() {
      this.cache = "";
      this.tokenCache = {
        deserialize: (value) => { this.cache = value; },
        getAllAccounts: async () => [{ homeAccountId: "home-1" }],
        serialize: () => `${this.cache}-rotated`,
      };
    }
    getTokenCache() { return this.tokenCache; }
    async acquireTokenSilent() {
      caches.push(this.cache);
      return acquire(this.cache, InteractionRequiredAuthError);
    }
  }
  store.getConnection = async () => current && { ...current };
  store.putConnection = async (userId, value, etag) => {
    const result = await put({ userId, value: { ...value }, etag, current: { ...current },
      setCurrent: (next) => { current = { ...next }; } });
    if (result) current = { ...result };
    return current && { ...current };
  };
  crypt.decrypt = (value) => value;
  crypt.encrypt = (value) => value;
  msal.ConfidentialClientApplication = FakeCca;
  msal.InteractionRequiredAuthError = InteractionRequiredAuthError;
  for (const [key, value] of Object.entries({ GRAPH_CLIENT_ID: "client", GRAPH_CLIENT_SECRET: "secret",
    GRAPH_TENANT_ID: "tenant", GRAPH_REDIRECT_URI: "https://example.test/callback" }))
    process.env[key] = value;
  delete require.cache[authPath];
  const auth = require(authPath);
  return { auth, caches, current: () => ({ ...current }), restore: () => {
    delete require.cache[authPath];
    store.getConnection = saved.getConnection;
    store.putConnection = saved.putConnection;
    crypt.encrypt = saved.encrypt;
    crypt.decrypt = saved.decrypt;
    msal.ConfidentialClientApplication = saved.cca;
    msal.InteractionRequiredAuthError = saved.interaction;
    for (const key of settingKeys) {
      if (saved.env[key] === undefined) delete process.env[key];
      else process.env[key] = saved.env[key];
    }
  } };
}

function conflict() {
  const err = new Error("stale etag");
  err.statusCode = 412;
  return err;
}

/* ---- optimistic concurrency foundations -------------------------------- */

test("connection replacement is conditional and returns the new etag", async () => {
  const { store, restore } = loadStore();
  try {
    const first = await store.putConnection("u1", connection({ etag: undefined }));
    assert.ok(first.etag);
    const second = await store.putConnection("u1", { ...first, tokenCache: "cache-two" }, first.etag);
    assert.notEqual(second.etag, first.etag);
    await assert.rejects(
      () => store.putConnection("u1", { ...first, tokenCache: "stale-cache" }, first.etag),
      (err) => err.statusCode === 412);
    assert.equal((await store.getConnection("u1")).tokenCache, "cache-two",
      "a stale refresh must never replace the cache that won");
  } finally { restore(); }
});

test("projection creation has one winner and stale folds cannot overwrite manual work", async () => {
  const { store, restore } = loadStore();
  try {
    const created = await store.putEngagementProjection("u1", "111", {
      lastActivityAt: "2026-08-24T12:00:00Z", replyState: "new",
    });
    assert.ok(created.etag);
    await store.putEngagement("u1", "111", {
      replyState: "done", actedAt: "2026-08-24T13:00:00Z",
    });
    await assert.rejects(
      () => store.putEngagementProjection("u1", "111", {
        lastActivityAt: "2026-08-24T14:00:00Z", replyState: "new",
      }, created.etag),
      (err) => err.statusCode === 412);
    const after = await store.getEngagement("u1", "111");
    assert.equal(after.replyState, "done");
    assert.equal(after.actedAt, "2026-08-24T13:00:00Z");
    await assert.rejects(
      () => store.putEngagementProjection("u1", "111", { lastActivityAt: "later" }),
      (err) => err.statusCode === 409,
      "two first writers must not silently become an upsert");
  } finally { restore(); }
});

test("the faithful double makes a transaction atomic on an etag conflict", async () => {
  const service = new FakeTableService();
  const table = service.table("ConcurrencyTest");
  const one = await table.createEntity({ partitionKey: "p", rowKey: "one", value: 1 });
  await table.createEntity({ partitionKey: "p", rowKey: "two", value: 2 });
  await assert.rejects(() => table.submitTransaction([
    ["update", { partitionKey: "p", rowKey: "one", value: 10 }, "Merge", { etag: one.etag }],
    ["delete", { partitionKey: "p", rowKey: "two" }, { etag: 'W/"stale"' }],
  ]), (err) => err.statusCode === 412);
  assert.equal((await table.getEntity("p", "one")).value, 1,
    "the successful first action must roll back when the second conflicts");
  assert.equal((await table.getEntity("p", "two")).value, 2);
});

/* ---- token cache concurrency -------------------------------------------- */

test("tokenFor reloads the winning cache and repeats silent acquisition after 412", async () => {
  let writes = 0;
  const h = loadAuthHarness({
    current: connection(),
    acquire: async (cache) => ({ accessToken: `token:${cache}` }),
    put: async ({ value, etag, setCurrent }) => {
      writes++;
      if (writes === 1) {
        assert.equal(etag, 'W/"1"');
        setCurrent(connection({ tokenCache: "cache-two", etag: 'W/"2"' }));
        throw conflict();
      }
      assert.equal(etag, 'W/"2"');
      return { ...value, etag: 'W/"3"' };
    },
  });
  try {
    const result = await h.auth.tokenFor("u1");
    assert.equal(result.accessToken, "token:cache-two");
    assert.deepEqual(h.caches, ["cache-one", "cache-two"],
      "a 412 retries acquisition from the new encrypted cache, not the stale write");
    assert.equal(h.current().tokenCache, "cache-two-rotated");
  } finally { h.restore(); }
});

test("a stale interaction-required result cannot mark a reconnected mailbox", async () => {
  let writes = 0;
  const h = loadAuthHarness({
    current: connection(),
    acquire: async (cache, Interaction) => {
      if (cache === "cache-one") throw new Interaction("reconnect");
      return { accessToken: `token:${cache}` };
    },
    put: async ({ value, etag, setCurrent }) => {
      writes++;
      if (writes === 1) {
        assert.equal(value.needsReconnect, true);
        setCurrent(connection({ tokenCache: "oauth-cache", etag: 'W/"9"', needsReconnect: false }));
        throw conflict();
      }
      assert.equal(etag, 'W/"9"');
      return { ...value, etag: 'W/"10"' };
    },
  });
  try {
    const result = await h.auth.tokenFor("u1");
    assert.equal(result.accessToken, "token:oauth-cache");
    assert.equal(h.current().needsReconnect, false,
      "the OAuth callback that won must remain authoritative");
    assert.deepEqual(h.caches, ["cache-one", "oauth-cache"]);
  } finally { h.restore(); }
});

test("tokenFor stops after a bounded number of connection conflicts", async () => {
  let version = 1;
  const h = loadAuthHarness({
    current: connection(),
    acquire: async (cache) => ({ accessToken: `token:${cache}` }),
    put: async ({ setCurrent }) => {
      version++;
      setCurrent(connection({ tokenCache: `cache-${version}`, etag: `W/"${version}"` }));
      throw conflict();
    },
  });
  try {
    await assert.rejects(() => h.auth.tokenFor("u1"),
      (err) => err.statusCode === 409 && err.code === "graph_connection_changed");
    assert.equal(h.caches.length, 3, "conflict retries must be bounded");
  } finally { h.restore(); }
});

/* ---- the watermark ------------------------------------------------------- */

test("a failed sweep does not erase the watermark it had", async () => {
  const { store, restore } = loadStore();
  try {
    await store.putSweepState("u1", "reply", {
      watermarkUtc: "2026-08-22T10:00:00Z", lastOkUtc: "2026-08-22T10:00:05Z",
      lookupHash: "hash-1",
    });
    // The error path writes health fields and nothing else. Under Replace this
    // deleted the watermark and sent the rep back to a 48-hour window.
    await store.putSweepState("u1", "reply", { lastError: "graph_reconnect_required" });
    const after = await store.getSweepState("u1", "reply");
    assert.equal(after.watermarkUtc, "2026-08-22T10:00:00Z",
      "one failed run must not undo days of progress");
    assert.equal(after.lastError, "graph_reconnect_required");
    assert.equal(after.lookupHash, "hash-1");
  } finally { restore(); }
});

test("the watermark can still be moved forward", async () => {
  const { store, restore } = loadStore();
  try {
    await store.putSweepState("u1", "reply", { watermarkUtc: "2026-08-22T10:00:00Z" });
    await store.putSweepState("u1", "reply", { watermarkUtc: "2026-08-22T11:00:00Z", lastError: "" });
    const after = await store.getSweepState("u1", "reply");
    assert.equal(after.watermarkUtc, "2026-08-22T11:00:00Z");
  } finally { restore(); }
});

/* ---- activity ordering --------------------------------------------------- */

test("the timeline returns the NEWEST rows, not the oldest", async () => {
  const { store, restore } = loadStore();
  try {
    // 40 rows over 40 days, written oldest-first so insertion order cannot be
    // what makes this pass.
    for (let i = 0; i < 40; i++) {
      await store.recordActivity({
        userId: "u1", advisorCrd: "111", direction: "inbound", classification: "reply",
        occurredAt: new Date(Date.UTC(2026, 0, i + 1)).toISOString(),
        graphMessageId: `g${i}`, advisorEmail: "advisor@ml.com",
      });
    }
    const rows = await store.listActivity("111", 5);
    assert.equal(rows.length, 5);
    const got = rows.map((r) => r.occurredAt);
    const expected = [39, 38, 37, 36, 35]
      .map((i) => new Date(Date.UTC(2026, 0, i + 1)).toISOString());
    assert.deepEqual(got, expected,
      "a cut taken in key order must keep the most recent activity");
  } finally { restore(); }
});

test("a Graph id containing a slash does not blow up a row key", async () => {
  const { store, restore } = loadStore();
  try {
    // Graph immutable ids are base64-flavoured; "/" is legal in one and illegal
    // in an Azure Table key. clean() only truncated, so this used to throw.
    const nasty = "AAkALgAAAAAAHYQDEapmEc2byACqAC/EWg==";
    await store.recordActivity({
      userId: "u1", advisorCrd: "111", direction: "inbound",
      occurredAt: "2026-08-22T10:00:00Z", graphMessageId: nasty,
    });
    await store.markReplySeen("u1", nasty, "reply");
    assert.equal(await store.replyAlreadySeen("u1", nasty), true);
    const rows = await store.listActivity("111", 5);
    assert.equal(rows[0].graphMessageId, nasty, "the id is preserved as a property");
  } finally { restore(); }
});

/* ---- the engagement projection ------------------------------------------- */

test("bounce dismissal time is serialized separately from reply actedAt", async () => {
  const { store, restore } = loadStore();
  try {
    const dismissedAt = "2026-08-24T15:00:00.000Z";
    await store.putEngagement("u1", "111", {
      bounceDismissed: true, bounceDismissedAt: dismissedAt,
    });
    const after = await store.getEngagement("u1", "111");
    assert.equal(after.bounceDismissed, true);
    assert.equal(after.bounceDismissedAt, dismissedAt,
      "the bounce identity must survive the storage whitelist");
    assert.equal(after.actedAt, undefined,
      "dismissing address work must not fabricate a handled-reply timestamp");
  } finally { restore(); }
});

test("a reviewed reply stays reviewed across TWO persisted refreshes", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    const when = new Date(Date.now() - 3 * 86400000).toISOString();
    await store.recordActivity({
      userId: "u1", advisorCrd: "111", direction: "inbound", classification: "reply",
      occurredAt: when, graphMessageId: "g1", advisorEmail: "advisor@ml.com",
    });

    await engagement.refresh("u1", "111", { store });
    assert.equal((await store.getEngagement("u1", "111")).replyState, "new");

    await engagement.setReplyState("u1", "111", "reviewed", { store });
    assert.equal((await store.getEngagement("u1", "111")).replyState, "reviewed");

    /* THE BUG: fold() read previous.actedAt and did not return it, so the next
     * refresh replaced the row without it -- and with no record that anybody
     * had acted, an old reply came back to the top of the queue as new. */
    await engagement.refresh("u1", "111", { store });
    const after = await store.getEngagement("u1", "111");
    assert.equal(after.replyState, "reviewed", "a handled reply must not re-open itself");
    assert.ok(after.actedAt, "actedAt must survive a refresh");
  } finally { restore(); }
});

test("a genuinely newer reply still re-opens a reviewed conversation", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() - 5 * 86400000).toISOString(),
      graphMessageId: "g1", advisorEmail: "a@ml.com" });
    await engagement.refresh("u1", "111", { store });
    await engagement.setReplyState("u1", "111", "reviewed", { store });

    // Clearly after the review, not milliseconds after -- a test that depends
    // on clock resolution tells you nothing when it fails.
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() + 60000).toISOString(),
      graphMessageId: "g2", advisorEmail: "a@ml.com" });
    await engagement.refresh("u1", "111", { store });
    assert.equal((await store.getEngagement("u1", "111")).replyState, "new",
      "they have answered again since we last acted");
  } finally { restore(); }
});

test("a snooze silences the row and then gives it back as due", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() - 86400000).toISOString(),
      graphMessageId: "g1", advisorEmail: "a@ml.com" });
    await engagement.refresh("u1", "111", { store });
    assert.equal((await engagement.queue("u1", { store })).count, 1);

    await engagement.snooze("u1", "111", 7, { store });
    assert.equal((await engagement.queue("u1", { store })).count, 0, "not now");

    const later = Date.now() + 8 * 86400000;
    const back = await engagement.queue("u1", { store, now: later });
    assert.equal(back.count, 1, "not now is not never");
    // Back as reply_new, NOT as a generic "follow-up due": the reply still has
    // not been dealt with, and saying so is more useful than saying a date
    // passed. `due` is for the case where there is no stronger reason left.
    assert.equal(back.entries[0].reason, "reply_new");
  } finally { restore(); }
});

test("a snoozed quiet contact comes back as a follow-up due", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    // Replied once, long ago, and already dealt with -- so the only reason they
    // are in the queue at all is that they have gone quiet.
    await store.recordActivity({ userId: "u1", advisorCrd: "222", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() - 200 * 86400000).toISOString(),
      graphMessageId: "old", advisorEmail: "a@ubs.com" });
    await engagement.refresh("u1", "222", { store });
    await engagement.setReplyState("u1", "222", "done", { store });
    assert.equal((await engagement.queue("u1", { store })).entries[0].reason, "quiet_warm");

    await engagement.snooze("u1", "222", 14, { store });
    assert.equal((await engagement.queue("u1", { store })).count, 0);

    const back = await engagement.queue("u1", { store, now: Date.now() + 15 * 86400000 });
    assert.equal(back.entries[0].reason, "due",
      "the snooze itself is what is now due -- this is the path that reason exists for");
  } finally { restore(); }
});

test("the projection survives being thrown away and rebuilt", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() - 86400000).toISOString(),
      graphMessageId: "g1", advisorEmail: "a@ml.com" });
    await engagement.refresh("u1", "111", { store });
    await engagement.setReplyState("u1", "111", "reviewed", { store });

    const report = await engagement.rebuild("u1", { store });
    assert.equal(report.advisors, 1);
    assert.deepEqual(report.changed, [], "a cache that matches its log has no drift");
    assert.equal((await store.getEngagement("u1", "111")).replyState, "reviewed",
      "a rebuild recomputes what the log knows and keeps what only the rep knows");
  } finally { restore(); }
});

test("the follow-up address is the newest on file, not the oldest", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: "2024-01-01T00:00:00Z",
      graphMessageId: "old", advisorEmail: "old.address@ml.com" });
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: "2026-08-01T00:00:00Z",
      graphMessageId: "new", advisorEmail: "current.address@ml.com" });
    await engagement.refresh("u1", "111", { store });
    assert.equal((await store.getEngagement("u1", "111")).advisorEmail, "current.address@ml.com",
      "a follow-up to the old one would reach nobody, silently");
  } finally { restore(); }
});

/* ---- internal contacts, and backfill ------------------------------------- */

test("a backfill records history without queueing it as work", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    // Eight months old: a real reply, long since dealt with or forgotten.
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() - 240 * 86400000).toISOString(),
      graphMessageId: "old-reply", advisorEmail: "a@ml.com" });

    await engagement.refresh("u1", "111", { store, seed: true });
    const q = await engagement.queue("u1", { store });
    assert.equal(q.counts.reply_new, 0,
      "four hundred of these would destroy the queue on the first morning");
    // Still on the timeline, in full.
    assert.equal((await store.listActivity("111", 10)).length, 1);
  } finally { restore(); }
});

test("a reply arriving AFTER the backfill still surfaces", async () => {
  const { store, restore } = loadStore();
  try {
    const engagement = require("../shared/email-engagement");
    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() - 240 * 86400000).toISOString(),
      graphMessageId: "old-reply", advisorEmail: "a@ml.com" });
    await engagement.refresh("u1", "111", { store, seed: true });

    await store.recordActivity({ userId: "u1", advisorCrd: "111", direction: "inbound",
      classification: "reply", occurredAt: new Date(Date.now() + 60000).toISOString(),
      graphMessageId: "new-reply", advisorEmail: "a@ml.com" });
    await engagement.refresh("u1", "111", { store });
    assert.equal((await engagement.queue("u1", { store })).counts.reply_new, 1,
      "seeding history must not deafen the queue to what happens next");
  } finally { restore(); }
});
