"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const capacity = require("../shared/email-limit-guard");
const { FakeTableService } = require("./helpers/fake-table");

const entries = (count, units = 1) => Array.from({ length: count }, (_, i) =>
  ({ key: `m${i + 1}`, units }));

test("Eastern calendar capacity resets at local midnight, not after 24 hours", () => {
  assert.equal(capacity.easternDay(Date.parse("2026-09-01T03:59:00Z")), "2026-08-31");
  assert.equal(capacity.easternDay(Date.parse("2026-09-01T04:01:00Z")), "2026-09-01");
});

test("9 AM Eastern conversion follows both DST offsets", () => {
  assert.equal(capacity.easternInstant("2026-03-09", 9), "2026-03-09T13:00:00.000Z");
  assert.equal(capacity.easternInstant("2026-11-02", 9), "2026-11-02T14:00:00.000Z");
});

test("Friday overflow skips the weekend and keeps deterministic order", () => {
  const plan = capacity.previewPlan(entries(5), {
    limit: 2, nowMs: Date.parse("2026-09-04T12:00:00Z"),
    startUtc: "2026-09-04T13:00:00Z", mailboxIntervalSeconds: 10,
  });
  assert.equal(plan.fit, true);
  assert.deepEqual(plan.days.map((day) => [day.day, day.messageCount]), [
    ["2026-09-04", 2], ["2026-09-07", 2], ["2026-09-08", 1],
  ]);
  assert.deepEqual(plan.assignments.map((row) => row.key), entries(5).map((row) => row.key));
});

test("the seven-day completion horizon reports exact excess", () => {
  const plan = capacity.previewPlan(entries(200), {
    limit: 25, nowMs: Date.parse("2026-08-31T13:00:00Z"),
    startUtc: "2026-08-31T13:00:00Z", mailboxIntervalSeconds: 10,
  });
  assert.equal(plan.fit, false);
  assert.equal(plan.scheduledCount, 150);
  assert.equal(plan.excessCount, 50);
  assert.equal(plan.days.at(-1).day, "2026-09-07");
});

test("existing daily commitments move overflow without changing message order", () => {
  const plan = capacity.previewPlan(entries(4), {
    limit: 3, nowMs: Date.parse("2026-08-31T13:00:00Z"),
    startUtc: "2026-08-31T13:00:00Z", mailboxIntervalSeconds: 10,
    usage: { "2026-08-31": 2 },
  });
  assert.deepEqual(plan.days.map((day) => [day.day, day.messageCount, day.units]), [
    ["2026-08-31", 1, 1], ["2026-09-01", 3, 3],
  ]);
  assert.deepEqual(plan.assignments.map((row) => row.key), ["m1", "m2", "m3", "m4"]);
});

test("one email with multiple external recipients is never split across days", () => {
  const plan = capacity.previewPlan([
    { key: "one", units: 2 }, { key: "two", units: 1 },
  ], { limit: 2, nowMs: Date.parse("2026-08-31T13:00:00Z"),
    startUtc: "2026-08-31T13:00:00Z", usage: { "2026-08-31": 1 } });
  assert.equal(plan.assignments[0].key, "one");
  assert.equal(plan.assignments[0].day, "2026-09-01");
  assert.equal(plan.assignments[1].day, "2026-09-02");
});

test("plan hashes bind allocation days and scheduled start intent", () => {
  const base = { limit: 25, nowMs: Date.parse("2026-08-31T13:00:00Z"),
    startUtc: "2026-09-01T13:00:00Z", dailyStartTime: "09:00", bindStart: true };
  const first = capacity.previewPlan(entries(2), base);
  const same = capacity.previewPlan(entries(2), base);
  const moved = capacity.previewPlan(entries(2), { ...base, startUtc: "2026-09-02T13:00:00Z" });
  const laterDailyTime = capacity.previewPlan(entries(2), { ...base, dailyStartTime: "10:30" });
  assert.equal(first.planHash, same.planHash);
  assert.notEqual(first.planHash, moved.planHash);
  assert.notEqual(first.planHash, laterDailyTime.planHash);
});

test("immediate plan review tolerates elapsed seconds but still binds later-day time", () => {
  const base = { limit: 2, nowMs: Date.parse("2026-08-31T13:00:00Z"),
    startUtc: "2026-08-31T13:00:20Z", dailyStartTime: "10:30" };
  const reviewed = capacity.previewPlan(entries(3), base);
  const approved = capacity.previewPlan(entries(3), {
    ...base, nowMs: Date.parse("2026-08-31T13:00:05Z"),
    startUtc: "2026-08-31T13:00:25Z",
  });
  assert.equal(reviewed.planHash, approved.planHash);
  assert.equal(reviewed.days[1].startUtc, "2026-09-01T14:30:00.000Z");
});

test("the selected Eastern time is reused on every later delivery day", () => {
  const plan = capacity.previewPlan(entries(5), {
    limit: 2, nowMs: Date.parse("2026-08-31T13:00:00Z"),
    startUtc: "2026-08-31T13:00:00Z", dailyStartTime: "10:30",
    mailboxIntervalSeconds: 10,
  });
  assert.equal(plan.dailyStartTime, "10:30");
  assert.deepEqual(plan.days.map((day) => [day.day, day.startUtc]), [
    ["2026-08-31", "2026-08-31T13:00:00.000Z"],
    ["2026-09-01", "2026-09-01T14:30:00.000Z"],
    ["2026-09-02", "2026-09-02T14:30:00.000Z"],
  ]);
});

test("invalid or after-hours daily delivery times are refused", () => {
  assert.throws(() => capacity.previewPlan(entries(1), {
    limit: 25, nowMs: Date.parse("2026-08-31T13:00:00Z"),
    startUtc: "2026-08-31T13:00:00Z", dailyStartTime: "17:00",
  }), (error) => error.code === "capacity_daily_time_invalid");
});

test("an after-hours automatic start moves to the next business morning", () => {
  const plan = capacity.previewPlan(entries(1), {
    limit: 25, nowMs: Date.parse("2026-09-04T22:30:00Z"),
    startUtc: "2026-09-04T22:30:00Z",
  });
  assert.equal(plan.days[0].day, "2026-09-07");
  assert.equal(plan.days[0].startUtc, "2026-09-07T13:00:00.000Z");
});

test("the first tranche preserves seconds so cancellation time is never rounded away", () => {
  const plan = capacity.previewPlan(entries(1), {
    limit: 25, nowMs: Date.parse("2026-08-31T13:00:50.000Z"),
    startUtc: "2026-08-31T13:01:10.000Z", mailboxIntervalSeconds: 10,
  });
  assert.equal(plan.firstSendUtc, "2026-08-31T13:01:10.000Z");
});

test("reservation locking, replay, release, and legacy usage are durable", async () => {
  const service = new FakeTableService();
  const policy = service.table("EmailPolicy"), ledger = service.table("EmailSendLedger");
  const oldConnection = process.env.AZURE_STORAGE_CONNECTION_STRING;
  process.env.AZURE_STORAGE_CONNECTION_STRING =
    "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net";
  capacity.__setClientsForTest(policy, ledger);
  const nowMs = Date.parse("2026-08-31T13:00:00Z");
  const options = { limit: 2, horizonDays: 0, nowMs,
    startUtc: "2026-08-31T13:00:00Z", mailboxIntervalSeconds: 10 };
  try {
    const results = await Promise.allSettled([
      capacity.reservePlan("u1", "one", entries(2), options),
      capacity.reservePlan("u1", "two", entries(2), options),
    ]);
    assert.equal(results.filter((result) => result.status === "fulfilled").length, 1,
      "the lock must not let two approvals spend the same remaining capacity");
    const failed = results.find((result) => result.status === "rejected");
    assert.equal(failed.reason.code, "capacity_horizon_exceeded");

    const winnerId = results[0].status === "fulfilled" ? "one" : "two";
    const winner = results.find((result) => result.status === "fulfilled").value;
    const replay = await capacity.reservePlan("u1", winnerId, entries(2),
      { ...options, expectedPlanHash: winner.planHash });
    assert.equal(replay.alreadyReserved, true);
    await assert.rejects(() => capacity.reservePlan("u1", winnerId,
      [{ key: "different", units: 2 }], options),
    (error) => error.code === "idempotency_conflict");

    let snapshot = await capacity.capacitySnapshot("u1", { limit: 2, horizonDays: 0, nowMs });
    assert.equal(snapshot.committedToday, 2);
    await capacity.releaseAllocations("u1", winnerId, ["m1"]);
    snapshot = await capacity.capacitySnapshot("u1", { limit: 2, horizonDays: 0, nowMs });
    assert.equal(snapshot.committedToday, 1);

    await ledger.createEntity({ partitionKey: "u1", rowKey: "legacy",
      externalCount: 1, reservedUtc: "2026-08-31T20:00:00Z" });
    snapshot = await capacity.capacitySnapshot("u1", { limit: 3, horizonDays: 0, nowMs });
    assert.equal(snapshot.committedToday, 2,
      "pre-release rolling rows remain conservatively counted on their Eastern day");
  } finally {
    capacity.__setClientsForTest(null, null);
    if (oldConnection === undefined) delete process.env.AZURE_STORAGE_CONNECTION_STRING;
    else process.env.AZURE_STORAGE_CONNECTION_STRING = oldConnection;
  }
});
