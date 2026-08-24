"use strict";

/* Durable repair for the EmailEngagement projection.
 *
 * This timer reads only Azure Tables. It deliberately has no Graph/auth import
 * and cannot touch a mailbox. Activity writers create the source marker in the
 * same-partition transaction as the event; this worker leases, folds, and
 * conditionally deletes that marker.
 */
const crypto = require("crypto");
const store = require("../shared/email-store");
const engagement = require("../shared/email-engagement");

const PAGE_SIZE = 50;
const MAX_SCANNED = 500;
const MAX_REPAIRS = 50;
const LEASE_SECONDS = 180;

function allowlist() {
  return new Set(String(process.env.EMAIL_ENGAGEMENT_REPAIR_USER_IDS || "")
    .split(/[;,\s]+/).map((value) => value.trim().toLowerCase()).filter(Boolean));
}

function scopeFor(ids) {
  if (!ids.size) return "all";
  return "users:" + crypto.createHash("sha256").update([...ids].sort().join("\n")).digest("hex");
}

function logger(context, level, message) {
  const target = context && context.log;
  if (!target) return;
  if (level && typeof target[level] === "function") target[level](message);
  else if (typeof target === "function") target(message);
}

async function run(context, overrides = {}) {
  const deps = { store, engagement, now: () => new Date(), ...overrides };
  const summary = { scanned: 0, eligible: 0, claimed: 0, repaired: 0,
    conflicts: 0, failed: 0, unmatched: 0, poison: 0, deferred: 0,
    leased: 0, oldestDirtyAtUtc: "" };
  if (process.env.EMAIL_ENGAGEMENT_REPAIR_ENABLED !== "1") {
    logger(context, "", "engagement repair disabled (EMAIL_ENGAGEMENT_REPAIR_ENABLED != 1)");
    return { ...summary, disabled: true };
  }

  const ids = allowlist();
  const scope = scopeFor(ids);
  const savedCursor = await deps.store.getEngagementRepairCursor();
  let token = savedCursor && savedCursor.scope === scope
    ? String(savedCursor.continuationToken || "") : "";
  let nextToken = token;
  let finished = false;

  while (summary.scanned < MAX_SCANNED && summary.claimed < MAX_REPAIRS && !finished) {
    const room = Math.min(PAGE_SIZE, MAX_SCANNED - summary.scanned);
    const page = await deps.store.listEngagementDirtyPage(nextToken, room);
    nextToken = String(page.continuationToken || "");
    finished = !nextToken;
    if (!page.rows.length) break;

    for (const marker of page.rows) {
      summary.scanned++;
      if (ids.size && !ids.has(String(marker.userId || "").toLowerCase())) {
        summary.unmatched++;
        continue;
      }
      const at = deps.now();
      const atMs = at instanceof Date ? at.getTime() : new Date(at).getTime();
      const dirtyAt = String(marker.dirtyAtUtc || "");
      if (dirtyAt && (!summary.oldestDirtyAtUtc || dirtyAt < summary.oldestDirtyAtUtc))
        summary.oldestDirtyAtUtc = dirtyAt;
      if (marker.status === "poison") { summary.poison++; continue; }
      if (marker.status === "processing" && marker.leaseUntilUtc
          && new Date(marker.leaseUntilUtc).getTime() > atMs) {
        summary.leased++;
        continue;
      }
      if (marker.status === "retry" && marker.retryAfterUtc
          && new Date(marker.retryAfterUtc).getTime() > atMs) {
        summary.deferred++;
        continue;
      }
      const claimed = await deps.store.claimEngagementDirty(marker,
        { now: at, leaseSeconds: LEASE_SECONDS });
      if (!claimed) { summary.conflicts++; continue; }
      summary.eligible++;
      summary.claimed++;
      try {
        const result = await deps.engagement.refreshDirty(claimed, { store: deps.store });
        if (result.acknowledged) summary.repaired++;
        else summary.conflicts++;
      } catch (err) {
        summary.failed++;
        const updated = await deps.store.failEngagementDirty(claimed, err, { now: deps.now() });
        if (!updated) summary.conflicts++;
        logger(context, "warn", `engagement repair failed (${String((err && err.code) || "unknown").slice(0, 80)})`);
      }
      if (summary.claimed >= MAX_REPAIRS) break;
    }
  }

  // Advancing across skipped retry/poison/unmatched rows is intentional: they
  // cannot pin the scan forever. An empty token wraps on the next invocation.
  await deps.store.putEngagementRepairCursor(scope, nextToken);
  if (ids.size && summary.scanned && summary.unmatched === summary.scanned)
    logger(context, "", "engagement repair canary allowlist matched no dirty markers in this page");
  logger(context, "", `engagement repair: scanned ${summary.scanned}, claimed ${summary.claimed}, `
    + `repaired ${summary.repaired}, failed ${summary.failed}, conflicts ${summary.conflicts}, `
    + `poison ${summary.poison}, deferred ${summary.deferred}, leased ${summary.leased}, `
    + `oldest ${summary.oldestDirtyAtUtc || "none"}`);
  return summary;
}

module.exports = async function (context) { await run(context); };
module.exports.run = run;
module.exports.scopeFor = scopeFor;
