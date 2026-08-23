"use strict";

const { TableClient } = require("@azure/data-tables");

let client;
async function policyTable() {
  const conn = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!conn) throw new Error("Email storage is not configured.");
  if (!client) client = TableClient.fromConnectionString(conn, "EmailPolicy", { allowInsecureConnection: false });
  await client.createTable().catch((e) => { if (e.statusCode !== 409) throw e; });
  return client;
}

async function acquire(userId, intervalSeconds) {
  const table = await policyTable();
  for (let attempt = 0; attempt < 5; attempt++) {
    let current = null;
    try { current = await table.getEntity("mailbox-rate", userId); }
    catch (e) { if (e.statusCode !== 404) throw e; }
    const now = Date.now();
    if (current) {
      const next = new Date(current.lastSlotUtc || 0).getTime() + intervalSeconds * 1000;
      if (next > now) return Math.max(1, Math.ceil((next - now) / 1000));
      try {
        await table.updateEntity({ partitionKey: "mailbox-rate", rowKey: userId,
          lastSlotUtc: new Date(now).toISOString() }, "Merge", { etag: current.etag });
        return 0;
      } catch (e) { if (e.statusCode !== 412) throw e; }
    } else {
      try {
        await table.createEntity({ partitionKey: "mailbox-rate", rowKey: userId,
          lastSlotUtc: new Date(now).toISOString() }); return 0;
      } catch (e) { if (e.statusCode !== 409) throw e; }
    }
  }
  return 1;
}

module.exports = { acquire };
