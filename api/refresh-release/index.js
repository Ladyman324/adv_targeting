"use strict";
// Machine publication probe. Azure Functions requires this function's key.
// No user identity is fabricated and no business record is written.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { TableClient } = require("@azure/data-tables");
const freshness = require("../shared/data-freshness");
const registry = require("../shared/recipient-registry");
const releaseMetadata = require("../health").releaseMetadata;
const capacity = require("../shared/email-limit-guard");
const ROOT = path.join(__dirname, "..");
const TERMINAL = new Set(["sent", "failed", "canceled", "draft_ready", "auth_required", "editing", "ready"]);
const SELECT = ["PartitionKey", "RowKey", "state", "leaseUntilUtc", "retryAfterUtc", "plannedSendUtc", "capacityDay", "updatedUtc"];
const sha = file => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
function quiet(rows, now = Date.now(), horizonMinutes = 15, within = capacity.withinSendingWindow) {
  const end = now + horizonMinutes * 60000;
  let blocking = 0;
  for (const row of rows) {
    if (TERMINAL.has(row.state)) continue;
    if (Date.parse(row.leaseUntilUtc || "") > now) { blocking++; continue; }
    const due = Math.max(Date.parse(row.plannedSendUtc || "") || 0, Date.parse(row.retryAfterUtc || "") || 0);
    if (due > end) continue;
    if (row.state === "send_scheduled" && row.capacityDay) {
      let open = false;
      for (let t = now; t <= end; t += 60000) open = open || within(t);
      if (!open) continue;
    }
    blocking++;
  }
  return { quiet: blocking === 0, blockingWork: blocking, inspectedWork: rows.length,
    horizonMinutes, metadataOnly: true };
}
async function workMetadata() {
  const connection = process.env.AZURE_STORAGE_CONNECTION_STRING;
  if (!connection) throw new Error("storage_unavailable");
  const client = TableClient.fromConnectionString(connection, "EmailMessages", { allowInsecureConnection: false });
  const rows = [];
  const filter = "state ne 'sent' and state ne 'failed' and state ne 'canceled' and state ne 'draft_ready' and state ne 'auth_required' and state ne 'editing' and state ne 'ready'";
  for await (const row of client.listEntities({ queryOptions: { filter, select: SELECT } })) {
    rows.push(row);
    if (rows.length > 5000) throw new Error("work_metadata_limit");
  }
  const batches = TableClient.fromConnectionString(connection, "EmailBatches", { allowInsecureConnection: false });
  for await (const row of batches.listEntities({ queryOptions: {
    filter: "status eq 'scheduled'", select: ["scheduledForUtc", "scheduleLeaseUntilUtc"] } })) {
    rows.push({ state: "scheduled_preflight", plannedSendUtc: row.scheduledForUtc, leaseUntilUtc: row.scheduleLeaseUntilUtc });
    if (rows.length > 5000) throw new Error("work_metadata_limit");
  }
  return rows;
}
async function probe(context, req) {
  const headers = { "Content-Type": "application/json", "Cache-Control": "no-store" };
  if (String(req.method || "GET").toUpperCase() !== "GET") {
    context.res = { status: 405, headers, body: { available: false } }; return;
  }
  try {
    const file = path.join(ROOT, "shared/data-freshness.json");
    const data = freshness.read(file);
    const release = releaseMetadata(path.join(ROOT, "release.json"));
    const manifest = await registry.load({ force: true });
    if (!data.available || !release.available || release.dirty || release.id !== data.candidate.id
        || release.commit !== data.candidate.codeCommit || manifest.registryContentHash !== data.candidate.recipientRegistryHash)
      throw new Error("release_mismatch");
    const act = JSON.parse(fs.readFileSync(path.join(ROOT, "shared/act_contacts.json"), "utf8"));
    const body = { schemaVersion: 1, available: true, storageOk: true, release, candidate: data.candidate,
      descriptorSha256: sha(file), recipientEligibility: registry.policy(),
      actContactsLoaded: Object.keys(act.contacts || {}).length,
      actContactsSha256: sha(path.join(ROOT, "shared/act_contacts.json")),
      actMailCodesSha256: sha(path.join(ROOT, "shared/act_mail_codes.json")),
      publication: data.publication };
    if (req.query && req.query.window === "1") body.deploymentWindow = quiet(await workMetadata());
    context.res = { status: 200, headers, body };
  } catch {
    // Browser headers, storage credentials and provider errors never enter logs.
    context.res = { status: 503, headers, body: { schemaVersion: 1, available: false, reason: "release_probe_unavailable" } };
  }
}
module.exports = probe;
module.exports.quiet = quiet;
module.exports.SELECT = SELECT;
