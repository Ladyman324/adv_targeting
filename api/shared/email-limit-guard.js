"use strict";

const crypto = require("crypto");
const { TableClient, odata } = require("@azure/data-tables");

let policyClient, ledgerClient;
async function clients() {
  const conn = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!conn) throw new Error("Email storage is not configured.");
  policyClient ||= TableClient.fromConnectionString(conn, "EmailPolicy", { allowInsecureConnection: false });
  ledgerClient ||= TableClient.fromConnectionString(conn, "EmailSendLedger", { allowInsecureConnection: false });
  await Promise.all([policyClient.createTable(), ledgerClient.createTable()].map((p) => p.catch((e) => { if (e.statusCode !== 409) throw e; })));
  return { policy: policyClient, ledger: ledgerClient };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function acquireLock(policy, userId, owner) {
  for (let attempt = 0; attempt < 20; attempt++) {
    let row = null;
    try { row = await policy.getEntity("limit-lock", userId); }
    catch (e) { if (e.statusCode !== 404) throw e; }
    const leaseUntilUtc = new Date(Date.now() + 15000).toISOString();
    if (!row) {
      try { await policy.createEntity({ partitionKey: "limit-lock", rowKey: userId, owner, leaseUntilUtc }); return; }
      catch (e) { if (e.statusCode !== 409) throw e; }
    } else if (!row.owner || new Date(row.leaseUntilUtc || 0).getTime() < Date.now()) {
      try { await policy.updateEntity({ partitionKey: "limit-lock", rowKey: userId, owner, leaseUntilUtc },
        "Merge", { etag: row.etag }); return; }
      catch (e) { if (e.statusCode !== 412) throw e; }
    }
    await wait(50 + Math.floor(Math.random() * 100));
  }
  const err = new Error("Could not acquire the per-user send-limit guard; retry approval.");
  err.statusCode = 409; throw err;
}

async function releaseLock(policy, userId, owner) {
  try {
    const row = await policy.getEntity("limit-lock", userId);
    if (row.owner === owner) await policy.updateEntity({ partitionKey: "limit-lock", rowKey: userId,
      owner: "", leaseUntilUtc: new Date(0).toISOString() }, "Merge", { etag: row.etag });
  } catch { /* the short lease is the recovery path */ }
}

/* A reservation id is also an idempotency key for the amount it reserved.
 *
 * Returning success for the same id with a different count lets a caller first
 * reserve one recipient and later use that reservation for Reply All. The
 * rolling total then understates the irreversible send. The durable send
 * ledger will bind the whole intent; until then this pure check binds the part
 * this ledger is responsible for.
 */
function replayReservation(existing, requestedExternalCount) {
  const reserved = Number(existing && existing.externalCount) || 0;
  const requested = Number(requestedExternalCount) || 0;
  if (reserved !== requested) {
    const err = new Error("This operation id is already reserved for a different recipient count. "
      + "Start a new send operation.");
    err.statusCode = 409;
    err.code = "idempotency_conflict";
    throw err;
  }
  return { alreadyReserved: true, externalCount: reserved };
}

async function reserve(userId, batchId, externalCount, limit) {
  const { policy, ledger } = await clients();
  const owner = crypto.randomUUID();
  await acquireLock(policy, userId, owner);
  try {
    try {
      const existing = await ledger.getEntity(userId, batchId);
      if (existing) return replayReservation(existing, externalCount);
    } catch (e) { if (e.statusCode !== 404) throw e; }
    const since = Date.now() - 86400000;
    let rolling = 0;
    const window = [];
    for await (const row of ledger.listEntities({ queryOptions: { filter: odata`PartitionKey eq ${userId}` } })) {
      const at = new Date(row.reservedUtc).getTime();
      const n = Number(row.externalCount) || 0;
      if (at >= since) { rolling += n; window.push({ at, n }); }
    }
    if (rolling + externalCount > limit) {
      /* Say WHEN, not just no.
       *
       * The window is rolling rather than per calendar day, deliberately: a
       * midnight reset would permit 500 at 23:50 and 500 at 00:10, which is the
       * twenty-minute burst from one mailbox that the limit exists to prevent.
       *
       * But "would exceed the limit" alone is a dead end -- the rep cannot tell
       * whether to wait ten minutes or a day. Each reservation ages out exactly
       * 24 hours after it was made, so the moment this batch becomes possible is
       * computable, and it belongs in the message.
       */
      const remaining = Math.max(0, limit - rolling);
      const needed = rolling + externalCount - limit;
      let freed = 0, readyAt = null;
      for (const entry of window.sort((a, b) => a.at - b.at)) {
        freed += entry.n;
        if (freed >= needed) { readyAt = new Date(entry.at + 86400000); break; }
      }
      const when = readyAt
        ? readyAt.toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit" })
        : null;
      const err = new Error(
        `This batch needs ${externalCount} external recipients and you have ${remaining} left `
        + `of the ${limit} allowed in any 24-hour period. `
        + (when ? `Enough capacity returns ${when}. ` : "")
        + `Creating Outlook drafts instead is unaffected.`);
      err.statusCode = 429; err.code = "rolling_limit"; throw err;
    }
    await ledger.createEntity({ partitionKey: userId, rowKey: batchId,
      externalCount: Number(externalCount) || 0, reservedUtc: new Date().toISOString() });
    return { alreadyReserved: false, externalCount, rollingAfter: rolling + externalCount };
  } finally { await releaseLock(policy, userId, owner); }
}

module.exports = { reserve, replayReservation };
