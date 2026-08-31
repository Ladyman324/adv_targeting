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
  const result = await service.capacityPlan({ id: "u1" }, { batchId: "b1" }, {
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
