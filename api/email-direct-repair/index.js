"use strict";

/* Dispatcher only: no Graph calls and no message content. The durable q| row
 * is the outbox, so a process crash can at worst enqueue a harmless duplicate.
 */

const crypto = require("crypto");
const store = require("../shared/email-direct-store");
const queue = require("../shared/email-direct-queue");

function scope() {
  return [...new Set(String(process.env.EMAIL_DIRECT_REPAIR_USER_IDS || "")
    .split(/[,;\s]+/).map((value) => value.trim()).filter(Boolean))].sort();
}

function scopeHash(users) {
  return crypto.createHash("sha256").update(users.join("\n")).digest("hex");
}

function due(marker, nowMs) {
  return (!marker.dueUtc || Date.parse(marker.dueUtc) <= nowMs)
    && (!marker.leaseUntilUtc || Date.parse(marker.leaseUntilUtc) <= nowMs);
}

function workKind(operation) {
  if (!operation) return "";
  if (operation.state === "prepared") return "direct_send";
  if (["submitted", "ambiguous"].includes(operation.state)) return "direct_reconcile";
  if (operation.state === "reconciled") return "direct_finalize";
  if (operation.state === "preparing") return "direct_recover";
  return "";
}

async function run(context = {}, overrides = {}) {
  const deps = { store, queue, now: () => Date.now(), ...overrides };
  if (process.env.EMAIL_DIRECT_REPAIR_ENABLED !== "1") {
    if (context.log) context.log("Direct-send repair disabled.");
    return { enabled: false, scanned: 0, enqueued: 0 };
  }
  const users = scope();
  const hash = scopeHash(users);
  const cursor = await deps.store.getRepairCursor();
  let continuation = cursor && cursor.scopeHash === hash ? cursor.continuationToken : "";
  let scanned = 0, enqueued = 0, conflicts = 0, wrapped = false;
  const started = deps.now();
  while (scanned < 500 && enqueued < 50 && deps.now() - started < 240000) {
    const page = await deps.store.listWorkPage(continuation, 50);
    if (!page.markers.length && !page.continuationToken) {
      continuation = ""; wrapped = true; break;
    }
    for (const marker of page.markers) {
      scanned++;
      if (users.length && !users.includes(marker.userId)) continue;
      if (!due(marker, deps.now())) continue;
      let operation = await deps.store.getOperation(marker.userId, marker.operationId);
      if (!operation || ["complete", "failed"].includes(operation.state)) continue;
      // A lost worker after this state may already have called Graph. Expiry is
      // evidence of uncertainty, never permission to call /send again.
      if (operation.state === "submitting"
          && (!operation.leaseUntilUtc || Date.parse(operation.leaseUntilUtc) <= deps.now())) {
        try {
          operation = await deps.store.scheduleOperation(operation.userId, operation.operationId,
            { state: "ambiguous", lastErrorCode: "submitting_lease_expired" },
            new Date(deps.now()).toISOString(), operation.etag, { nowMs: deps.now() });
        } catch (err) {
          if ([404, 412].includes(Number(err.statusCode))) { conflicts++; continue; }
          throw err;
        }
      }
      const kind = workKind(operation);
      if (!kind) continue;
      const currentMarker = typeof deps.store.getMarker === "function"
        ? await deps.store.getMarker(marker.userId, marker.operationId) : marker;
      const claimed = await deps.store.claimMarker(currentMarker,
        { nowMs: deps.now(), leaseSeconds: 120 });
      if (!claimed) { conflicts++; continue; }
      try {
        await deps.queue.enqueue(kind, marker.userId, marker.operationId);
        await deps.store.markEnqueued(marker.userId, marker.operationId,
          new Date(deps.now() + 120000).toISOString(), { nowMs: deps.now() });
        enqueued++;
      } catch {
        // The marker lease expires and makes the dispatch retryable. Throwing
        // here would abort the page and starve later due work.
      }
      if (enqueued >= 50) break;
    }
    continuation = page.continuationToken || "";
    if (!continuation) { wrapped = true; break; }
  }
  await deps.store.putRepairCursor(hash, continuation);
  const report = { enabled: true, scanned, enqueued, conflicts, wrapped };
  if (context.log) context.log("Direct-send repair", report);
  return report;
}

module.exports = async function (context) { await run(context); };
module.exports.run = run;
module.exports.workKind = workKind;
module.exports.due = due;
