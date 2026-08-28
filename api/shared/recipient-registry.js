"use strict";
const crypto = require("crypto");
const zlib = require("zlib");
const { BlobServiceClient } = require("@azure/storage-blob");
const core = require("./email-core");
const RELEASE_DESCRIPTOR = require("./approved-recipient-release.json");
const CONTAINER = process.env.APPROVED_RECIPIENT_CONTAINER || "lookups";
const BLOB = process.env.APPROVED_RECIPIENT_BLOB || "approved_recipients.json.gz";
const TTL_MS = Number(process.env.APPROVED_RECIPIENT_TTL_MS || 60 * 1000);
/* WHICH IDENTITY TIERS MAY BE ADDRESSED.
 *
 * The registry carries the superset the exporter produced; this is what the
 * running API will actually accept, and it is deliberately re-checked here so a
 * stale or hand-built blob cannot widen eligibility on its own.
 *
 *   confirmed  the firm stated this CRD and the SEC record agrees
 *   high       a strong name match with no CRD asserted anywhere, measured at
 *              about 0.989 precision -- roughly one in ninety is not the person
 *              named, which at scale is a real count of misdirected mail
 *
 * Settable because the previous hardcoded value could only be changed by
 * rebuilding the API, re-exporting the blob, and matching the two by content
 * hash -- so the only available response to "this is too tight" was a release.
 * Narrowing it costs an app setting and a restart.
 */
const SAFE_TIERS = new Set(String(process.env.APPROVED_RECIPIENT_TIERS || "confirmed,high")
  .split(",").map((tier) => tier.trim().toLowerCase()).filter(Boolean));
const BLOCKED_HIGH_SOURCES = new Set(
  String(process.env.APPROVED_RECIPIENT_BLOCKED_SOURCES || "")
    .split(",").map((source) => source.trim().toLowerCase()).filter(Boolean));
const POLICY_TIERS = [...SAFE_TIERS].sort();
const POLICY_BLOCKED_SOURCES = [...BLOCKED_HIGH_SOURCES].sort();
const POLICY_VERSION = crypto.createHash("sha256")
  .update(JSON.stringify({ schemaVersion: 1, tiers: POLICY_TIERS,
    blockedHighSources: POLICY_BLOCKED_SOURCES }), "utf8").digest("hex").slice(0, 16);
let cache = null, loadedAt = 0, sourceEtag = "";
function norm(value) { return String(value || "").trim().toLowerCase(); }
function failure(statusCode, code, message, detail = "") {
  const err = new Error(message); err.statusCode = statusCode; err.code = code;
  if (detail) err.detail = detail;
  return err;
}
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function sha256(value) {
  return crypto.createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}
function expectedContentHash(payload) {
  return sha256({ schemaVersion: payload.schemaVersion,
    recipients: payload.recipients, ineligible: payload.ineligible || {},
    provenance: payload.provenance || {} });
}
function releaseDescriptorCore(descriptor) {
  return { schemaVersion: descriptor.schemaVersion,
    registrySchemaVersion: descriptor.registrySchemaVersion,
    registryContentHash: descriptor.registryContentHash,
    recipientCount: descriptor.recipientCount,
    ineligibleCount: descriptor.ineligibleCount,
    provenance: descriptor.provenance || {} };
}
function assertReleaseBinding(payload) {
  const descriptor = RELEASE_DESCRIPTOR;
  const descriptorHash = sha256(releaseDescriptorCore(descriptor));
  if (Number(descriptor.schemaVersion) !== 1
      || Number(descriptor.registrySchemaVersion) !== 1
      || !/^[a-f0-9]{64}$/i.test(String(descriptor.descriptorHash || ""))
      || String(descriptor.descriptorHash).toLowerCase() !== descriptorHash)
    throw failure(503, "recipient_registry_release_invalid",
      "This API release has an invalid approved-recipient descriptor.");
  const registryHash = String(payload.contentHash || "").toLowerCase();
  const expectedHash = String(descriptor.registryContentHash || "").toLowerCase();
  const sameProvenance = canonicalJson(payload.provenance || {})
    === canonicalJson(descriptor.provenance || {});
  const sameCounts = Object.keys(payload.recipients || {}).length
      === Number(descriptor.recipientCount)
    && Object.keys(payload.ineligible || {}).length
      === Number(descriptor.ineligibleCount);
  if (registryHash !== expectedHash || !sameProvenance || !sameCounts)
    throw failure(503, "recipient_registry_release_mismatch",
      "The approved-recipient registry does not belong to this API release.");
}
function cleanRecord(crd, raw) {
  const email = norm(raw && raw.email), tier = String((raw && raw.tier) || "").toLowerCase();
  if (!email || !core.validEmail(email) || (raw && raw.eligible) === false
      || !SAFE_TIERS.has(tier)) return null;
  const id = String(crd || "").trim();
  if (!/^\d{3,12}$/.test(id)) return null;
  const metric = (longName, shortName) => {
    const value = Number(raw && (raw[longName] ?? raw[shortName]));
    return Number.isFinite(value) ? value : null;
  };
  const record = { crd: id, email,
    name: String((raw && raw.name) || "").slice(0, 256),
    greetingName: String((raw && (raw.greetingName || raw.firstName)) || "").slice(0, 120),
    lastName: String((raw && raw.lastName) || "").slice(0, 120),
    firm: String((raw && raw.firm) || "").slice(0, 256), tier,
    source: String((raw && raw.source) || "").slice(0, 120),
    actContactId: String((raw && raw.actContactId) || "").slice(0, 120),
    matchScore: metric("matchScore", "ms"),
    matchGap: metric("matchGap", "mg"),
    routingHash: String((raw && raw.routingHash) || "").slice(0, 128),
    teammates: (Array.isArray(raw && raw.teammates) ? raw.teammates : []).map((mate) => ({
      crd: String((mate && mate.crd) || "").trim(), email: norm(mate && mate.email),
      name: String((mate && mate.name) || "").slice(0, 256),
    })).filter((mate) => /^\d{3,12}$/.test(mate.crd) && core.validEmail(mate.email)) };
  const calculatedRoutingHash = sha256({
    crd: record.crd, email: record.email, actContactId: record.actContactId,
    teammates: record.teammates.map((mate) => [mate.crd, mate.email]).sort(),
  });
  if (record.routingHash && record.routingHash !== calculatedRoutingHash) return null;
  record.routingHash = calculatedRoutingHash;
  return record;
}
function policy() {
  return { schemaVersion: 1, version: POLICY_VERSION, tiers: POLICY_TIERS.slice(),
    blockedHighSources: POLICY_BLOCKED_SOURCES.slice() };
}
function hydratePayload(payload, enforceReleaseBinding) {
  if (!payload || Number(payload.schemaVersion) !== 1 || !payload.recipients
      || typeof payload.recipients !== "object") throw failure(503,
    "recipient_registry_incompatible",
    "The approved-recipient registry is missing or has an unsupported schema.");
  const expected = expectedContentHash(payload);
  if (!/^[a-f0-9]{64}$/i.test(String(payload.contentHash || ""))
      || String(payload.contentHash).toLowerCase() !== expected)
    throw failure(503, "recipient_registry_incompatible",
      "The approved-recipient registry failed its content-integrity check.");
  if (enforceReleaseBinding) assertReleaseBinding(payload);
  const recipients = new Map(), ineligible = new Map(
    Object.entries(payload.ineligible || {}).map(([k, v]) => [String(k), String(v)]));
  for (const [crd, raw] of Object.entries(payload.recipients)) {
    /* Internal colleagues are addressable only when explicitly admitted.
     *
     * They are exported rather than dropped so rehearsal batches -- always
     * addressed to this firm -- have somewhere to go. EMAIL_INTERNAL_RECIPIENT_
     * ALLOWLIST takes addresses or a bare domain, so "eicatlanta.com" admits
     * the whole staff, including people who join later.
     *
     * NOT testAllowlist, which would have been the obvious reuse and is a trap:
     * that list gates production sending, and any non-empty value requires
     * every direct-send recipient to appear in it -- admitting five colleagues
     * would have blocked every advisor campaign.
     */
    if (raw && raw.internal === true
        && !core.internalRecipientAllowed(norm(raw.email))) {
      ineligible.set(String(crd), "internal_recipient_not_allowlisted");
      continue;
    }
    const rawTier = String((raw && raw.tier) || "").trim().toLowerCase();
    const rawSource = String((raw && raw.source) || "").trim().toLowerCase();
    if (rawTier === "high" && BLOCKED_HIGH_SOURCES.has(rawSource)) {
      ineligible.set(String(crd), "high_tier_source_blocked"); continue;
    }
    const record = cleanRecord(crd, raw);
    if (record) recipients.set(String(crd), record);
    else ineligible.set(String(crd), "invalid_or_ineligible_registry_record");
  }
  // Treat an ambiguous email as unsafe even if a malformed producer ever lets
  // it through. One address may not represent two CRDs on an outbound message.
  const byEmail = new Map();
  for (const record of recipients.values()) {
    if (!byEmail.has(record.email)) byEmail.set(record.email, []);
    byEmail.get(record.email).push(record.crd);
  }
  for (const crds of byEmail.values()) if (crds.length > 1) {
    for (const crd of crds) { recipients.delete(crd); ineligible.set(crd, "duplicate_email"); }
  }
  // A duplicated Act GUID does not make the approved email unusable, but it
  // makes every CRM write ambiguous. Blank it so verifyActPair() fails closed.
  const byAct = new Map();
  for (const record of recipients.values()) if (record.actContactId) {
    if (!byAct.has(record.actContactId)) byAct.set(record.actContactId, []);
    byAct.get(record.actContactId).push(record);
  }
  for (const records of byAct.values()) if (records.length > 1)
    for (const record of records) record.actContactId = "";
  return { schemaVersion: Number(payload.schemaVersion), contentHash: expected,
    generated: String(payload.generated || ""), recipients,
    ineligible, provenance: payload.provenance || {},
    ready: true };
}
function hydrate(payload) { return hydratePayload(payload, true); }
async function download() {
  const connection = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!connection) throw failure(503, "recipient_registry_unavailable",
    "Approved-recipient storage is not configured.");
  const blob = BlobServiceClient.fromConnectionString(connection)
    .getContainerClient(CONTAINER).getBlobClient(BLOB);
  // Forced checks happen immediately before sends. Reading properties is enough
  // when the blob did not change, so paced batches do not redownload the full
  // registry for every message.
  const properties = await blob.getProperties();
  const etag = String(properties.etag || "");
  if (cache && etag && sourceEtag === etag) return { unchanged: true, etag };
  const payload = JSON.parse(zlib.gunzipSync(await blob.downloadToBuffer()).toString("utf8"));
  return { payload, etag };
}
async function load(options = {}) {
  const force = options === true || options.force === true;
  if (cache && !force && Date.now() - loadedAt < TTL_MS) return cache;
  let result;
  try { result = await download(); }
  catch (err) {
    if (err && err.code && String(err.code).startsWith("recipient_registry_")) throw err;
    throw failure(503, "recipient_registry_unavailable",
      `The approved-recipient registry could not be refreshed: ${err.message || err}`);
  }
  if (result.unchanged) { loadedAt = Date.now(); return cache; }
  cache = hydrate(result.payload); sourceEtag = result.etag || "";
  loadedAt = Date.now(); return cache;
}
async function resolve(crd, options = {}) {
  const id = String(crd || "").trim();
  if (!id) throw failure(400, "recipient_crd_required", "Every advisor recipient needs a CRD.");
  const index = await load(options), record = index.recipients.get(id);
  if (!record) throw failure(409, "recipient_not_approved",
    `CRD ${id} does not have an approved email recipient.`,
    index.ineligible.get(id) || "not_in_approved_registry");
  return { ...record, registryHash: index.contentHash };
}
async function verify(crd, email, options = {}) {
  const record = await resolve(crd, options);
  if (record.email !== norm(email)) throw failure(409, "recipient_identity_changed",
    `The approved email for CRD ${record.crd} no longer matches this message.`);
  return record;
}
async function allowedTeammates(crd, options = {}) {
  const primary = await resolve(crd, options), out = [];
  for (const hint of primary.teammates) {
    let current;
    try { current = await resolve(hint.crd); } catch { continue; }
    if (current.email === hint.email && current.crd !== primary.crd) out.push(current);
  }
  return out;
}
async function verifyTeammates(crd, requested, options = {}) {
  const allowed = await allowedTeammates(crd, options);
  const byEmail = new Map(allowed.map((r) => [r.email, r]));
  const byCrd = new Map(allowed.map((r) => [r.crd, r]));
  const selected = [];
  for (const item of requested || []) {
    const id = typeof item === "object" ? String(item.crd || "").trim() : "";
    const email = norm(typeof item === "object" ? item.email : item);
    const record = (id && byCrd.get(id)) || (email && byEmail.get(email));
    if (!record || (email && record.email !== email)) throw failure(409,
      "teammate_not_approved", "A requested teammate is no longer approved for this advisor.");
    if (!selected.some((r) => r.email === record.email)) selected.push(record);
  }
  return selected;
}
async function verifyActPair(crd, email, options = {}) {
  const record = await verify(crd, email, options);
  if (!record.actContactId) throw failure(409, "recipient_has_no_act_contact",
    `CRD ${record.crd} has no approved Act contact.`);
  return record;
}
function useIndex(payload) {
  const seeded = { schemaVersion: 1, recipients: {}, ineligible: {}, provenance: {}, ...payload };
  seeded.contentHash = expectedContentHash(seeded);
  // Explicit test seam: production blob loads always call strict hydrate().
  cache = hydratePayload(seeded, false);
  loadedAt = Date.now(); sourceEtag = "test"; return cache;
}
function reset() { cache = null; loadedAt = 0; sourceEtag = ""; }
module.exports = { load, resolve, verify, allowedTeammates, verifyTeammates,
  verifyActPair, useIndex, reset, norm, failure, canonicalJson, expectedContentHash,
  hydrate, policy };
