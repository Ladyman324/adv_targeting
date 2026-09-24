"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const service = require("../shared/email-service");
const capacity = require("../shared/email-limit-guard");

test("delivery plans count the final To and advisor Cc envelope, not stale batch totals", async () => {
  const batch = { id: "b1", status: "editing", externalCount: 99 };
  const messages = [
    { id: "m1", recipientEmail: "advisor@ubs.com",
      teammateCc: ["partner@ubs.com", "rep@eicatlanta.com"] },
    { id: "m2", recipientEmail: "colleague@eicatlanta.com", teammateCc: [] },
  ];
  const cfg = { calendarCapacityEnabled: true, dailyExternalLimit: 25,
    cancellationSeconds: 20, mailboxIntervalSeconds: 10,
    internalDomains: new Set(["eicatlanta.com"]) };
  const result = await service.capacityPlan({ id: "u1" }, {
    batchId: "b1", dailyStartTime: "10:30",
  }, {
    store: { getBatch: async () => batch, listMessages: async () => messages },
    core: { config: () => cfg },
    limitGuard: {
      capacitySnapshot: async () => ({ days: [] }),
      previewPlan: capacity.previewPlan,
    },
  });
  assert.equal(result.deliveryPlan.recipientCount, 2);
  assert.equal(result.deliveryPlan.externalUnits, 2,
    "one external To plus one external advisor teammate Cc consumes two units");
  assert.equal(result.deliveryPlan.days[0].units, 2);
  assert.equal(result.deliveryPlan.dailyStartTime, "10:30");
});

test("a new plan finishes deferred release from a prior schedule review", async () => {
  let batch = { id: "b1", status: "editing", etag: "v1",
    capacityReservationId: "b1-p1", capacityPlanHash: "old-hash" };
  const released = [];
  const cfg = { calendarCapacityEnabled: true, dailyExternalLimit: 25,
    cancellationSeconds: 20, mailboxIntervalSeconds: 10,
    internalDomains: new Set(["eicatlanta.com"]) };
  const result = await service.capacityPlan({ id: "u1" }, { batchId: "b1" }, {
    store: {
      getBatch: async () => batch,
      patchBatch: async (_user, _batch, patch, etag) => {
        assert.equal(etag, "v1"); batch = { ...batch, ...patch, etag: "v2" }; return batch;
      },
      listMessages: async () => [{ id: "m1", recipientEmail: "advisor@ubs.com", teammateCc: [] }],
    },
    core: { config: () => cfg },
    limitGuard: {
      assertReservation: async () => ({ assignments: [{ key: "m1" }] }),
      releaseAllocations: async (_user, reservationId, keys) => released.push({ reservationId, keys }),
      capacitySnapshot: async () => ({ days: [] }),
      previewPlan: capacity.previewPlan,
    },
  });
  assert.deepEqual(released, [{ reservationId: "b1-p1", keys: ["m1"] }]);
  assert.equal(batch.capacityReservationId, "");
  assert.equal(result.deliveryPlan.fit, true);
});

test("an individual override changes the daily plan without changing the default", async () => {
  const cfg = { calendarCapacityEnabled: true, dailyExternalLimit: 25,
    cancellationSeconds: 20, mailboxIntervalSeconds: 10,
    internalDomains: new Set(["eicatlanta.com"]) };
  const seen = [];
  const result = await service.capacityPlan({ id: "u1" }, { batchId: "b1" }, {
    store: {
      getDailyCap: async (userId) => userId === "u1" ? 75 : null,
      getBatch: async () => ({ id: "b1", status: "editing" }),
      listMessages: async () => [{ id: "m1", recipientEmail: "advisor@ubs.com" }],
    },
    core: { config: () => ({ ...cfg }) },
    limitGuard: {
      capacitySnapshot: async (_user, options) => {
        seen.push(options.limit); return { days: [] };
      },
      previewPlan: capacity.previewPlan,
    },
  });
  assert.deepEqual(seen, [75]);
  assert.equal(result.deliveryPlan.dailyLimit, 75);
  assert.equal(cfg.dailyExternalLimit, 25);
});

test("an individual mailbox interval spaces the plan and does not change the default", async () => {
  const cfg = { calendarCapacityEnabled: true, dailyExternalLimit: 25,
    cancellationSeconds: 20, mailboxIntervalSeconds: 20,
    internalDomains: new Set(["eicatlanta.com"]) };
  const messages = [
    { id: "m1", recipientEmail: "a@ubs.com" },
    { id: "m2", recipientEmail: "b@ubs.com" },
  ];
  async function plan(userId) {
    return service.capacityPlan({ id: userId }, { batchId: "b1" }, {
      store: {
        getMailboxInterval: async (id) => id === "u1" ? 60 : null,
        getBatch: async () => ({ id: "b1", status: "editing" }),
        listMessages: async () => messages,
      },
      core: { config: () => ({ ...cfg }) },
      limitGuard: { capacitySnapshot: async () => ({ days: [] }),
        previewPlan: capacity.previewPlan },
    });
  }
  const slower = (await plan("u1")).deliveryPlan;
  const normal = (await plan("u2")).deliveryPlan;
  assert.equal(slower.mailboxIntervalSeconds, 60);
  assert.equal(normal.mailboxIntervalSeconds, 20);
  assert.notEqual(slower.hash, normal.hash);
  assert.equal(cfg.mailboxIntervalSeconds, 20);
});

test("a 200-recipient override does not stop a campaign at the 25-person default", async () => {
  const cfg = { calendarCapacityEnabled: true, dailyExternalLimit: 25,
    cancellationSeconds: 20, mailboxIntervalSeconds: 20,
    internalDomains: new Set(["eicatlanta.com"]) };
  const messages = Array.from({ length: 201 }, (_, i) => ({
    id: `m${i + 1}`, recipientEmail: `advisor${i + 1}@example.com`, teammateCc: [],
  }));
  const st = {
    getDailyCap: async (userId) => userId === "high-cap" ? 200 : null,
    getBatch: async () => ({ id: "b1", status: "editing" }),
    listMessages: async () => messages,
  };
  const guard = {
    capacitySnapshot: async () => ({ days: [] }),
    previewPlan: (ordered, options) => capacity.previewPlan(ordered, {
      ...options, nowMs: Date.parse("2026-09-24T12:00:00Z"),
      startUtc: "2026-09-24T12:01:00Z",
    }),
  };
  const high = await service.capacityPlan({ id: "high-cap" },
    { batchId: "b1" }, { store: st, core: { config: () => ({ ...cfg }) }, limitGuard: guard });
  assert.equal(high.deliveryPlan.dailyLimit, 200);
  assert.deepEqual(high.deliveryPlan.days.map((day) => day.messageCount), [200, 1]);
  const ordinary = await service.capacityPlan({ id: "ordinary" },
    { batchId: "b1" }, { store: st, core: { config: () => ({ ...cfg }) }, limitGuard: guard });
  assert.equal(ordinary.deliveryPlan.dailyLimit, 25);
  assert.equal(ordinary.deliveryPlan.days[0].messageCount, 25);
});

test("Settings returns configured colleagues to a signed-in user without loading the email catalog", async () => {
  const handler = require("../email/index");
  const saved = process.env.EMAIL_INTERNAL_RECIPIENTS;
  process.env.EMAIL_INTERNAL_RECIPIENTS = "Teammate <teammate@eicatlanta.com>";
  const principal = Buffer.from(JSON.stringify({
    userId: "settings-test-user", userDetails: "Rep", userRoles: ["authenticated"],
  })).toString("base64");
  try {
    const context = { log: { error: () => {} } };
    await handler(context, { method: "GET", query: { op: "settings" },
      headers: { "x-ms-client-principal": principal } });
    assert.equal(context.res.status, 200);
    const body = JSON.parse(context.res.body);
    assert.deepEqual(body.internalRecipients,
      [{ address: "teammate@eicatlanta.com", name: "Teammate" }]);
    assert.equal(body.isAdmin, false);
    const anonymous = { log: { error: () => {} } };
    await handler(anonymous, { method: "GET", query: { op: "settings" }, headers: {} });
    assert.equal(anonymous.res.status, 401);
  } finally {
    if (saved === undefined) delete process.env.EMAIL_INTERNAL_RECIPIENTS;
    else process.env.EMAIL_INTERNAL_RECIPIENTS = saved;
  }
});
