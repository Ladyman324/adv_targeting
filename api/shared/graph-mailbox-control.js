"use strict";

const crypto = require("node:crypto");
const { TableClient } = require("@azure/data-tables");

function mailboxKey(token) {
  // Used only to coordinate an already-issued delegated token, never to authorize it.
  const claims = JSON.parse(Buffer.from(String(token).split(".")[1] || "", "base64url"));
  if (!claims.tid || !claims.oid) throw new Error("Graph mailbox identity is missing.");
  return crypto.createHash("sha256").update(claims.tid + ":" + claims.oid).digest("hex");
}

function deferred(code, seconds) {
  return Object.assign(new Error(code === "mailbox_cooldown"
    ? "Waiting for Microsoft's mailbox cooldown."
    : "Waiting for the next mailbox operation."), {
    code, graphCode: code, deferred: true, safeToRetry: true,
    retryAfter: Math.max(1, Math.ceil(seconds)), ambiguous: false,
  });
}

// A single CAS-protected row coordinates ALL Graph mail operations across hosts.
// The lease exceeds the bounded HTTP operation, including reading its response.
// A timed-out operation leaves a cooldown to let server-side work drain.
function createControl(table, now = Date.now) {
  async function change(key, decide) {
    for (let tries = 0; tries < 8; tries++) {
      let row;
      try { row = await table.getEntity("graph-mailbox", key); }
      catch (error) { if (error.statusCode !== 404) throw error; }
      const patch = decide(row || {});
      if (!patch) return;
      const entity = { partitionKey: "graph-mailbox", rowKey: key, ...patch };
      try {
        if (row) await table.updateEntity(entity, "Merge", { etag: row.etag });
        else await table.createEntity(entity);
        return patch;
      } catch (error) { if (![409, 412].includes(error.statusCode)) throw error; }
    }
    throw deferred("mailbox_busy", 3);
  }
  return {
    async acquire(key, { timeoutMs = 30000, sending = false, intervalSeconds = 10 } = {}) {
      const owner = crypto.randomUUID();
      const at = now();
      const patch = await change(key, row => {
        if (Number(row.cooldownUntil) > at)
          throw deferred("mailbox_cooldown", (row.cooldownUntil - at) / 1000);
        if (Number(row.leaseUntil) > at)
          throw deferred("mailbox_busy", Math.min(10, (row.leaseUntil - at) / 1000));
        if (sending && Number(row.nextSendAt) > at)
          throw deferred("mailbox_send_spacing", (row.nextSendAt - at) / 1000);
        return { owner, leaseUntil: at + timeoutMs + 30000,
          ...(sending ? { nextSendAt: at + Math.max(10, intervalSeconds) * 1000 } : {}) };
      });
      return { key, owner, expiresAt: patch.leaseUntil, sending,
        intervalSeconds: Math.max(10, intervalSeconds) };
    },
    async cooldown(key, seconds) {
      await change(key, row => ({ cooldownUntil: Math.max(Number(row.cooldownUntil) || 0,
        now() + Math.max(1, seconds) * 1000) }));
    },
    async release(lease) {
      await change(lease.key, row => row.owner === lease.owner ? {
        owner: "", leaseUntil: 0,
        // Space from completion too: a slow table write cannot compress actual sends.
        ...(lease.sending ? { nextSendAt: Math.max(Number(row.nextSendAt) || 0,
          now() + lease.intervalSeconds * 1000) } : {}),
      } : null);
    },
  };
}

let singleton;
function control() {
  if (!singleton) {
    const connection = process.env.AZURE_STORAGE_CONNECTION_STRING;
    if (!connection) throw new Error("Mailbox coordination storage is not configured.");
    singleton = createControl(TableClient.fromConnectionString(connection, "EmailPolicy"));
  }
  return singleton;
}

module.exports = { mailboxKey, createControl, control };
