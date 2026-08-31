"use strict";
const crypto = require("crypto");
const zlib = require("zlib");
const { BlobServiceClient } = require("@azure/storage-blob");
const core = require("./email-core");
const RELEASE_DESCRIPTOR = require("./approved-recipient-release.json");
const CONTAINER = process.env.APPROVED_RECIPIENT_CONTAINER || "lookups";
const MANIFEST_BLOB = process.env.APPROVED_RECIPIENT_MANIFEST_BLOB
  || `approved_recipients/releases/${RELEASE_DESCRIPTOR.registryContentHash}/manifest.json`;
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
let manifestCache = null, manifestLoadedAt = 0, manifestEtag = "";
const shardCache = new Map();
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
    provenance: descriptor.provenance || {},
    shardManifestHash: descriptor.shardManifestHash };
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
    greetingSource: String((raw && raw.greetingSource) || "").slice(0, 120),
    greetingEvidenceHash: String((raw && raw.greetingEvidenceHash) || "").slice(0, 128),
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
function shardKey(crd) { return String(crd || "").trim().slice(0, 2).padEnd(2, "0"); }
function manifestCore(manifest) {
  return { schemaVersion: manifest.schemaVersion,
    registrySchemaVersion: manifest.registrySchemaVersion,
    registryContentHash: manifest.registryContentHash,
    recipientCount: manifest.recipientCount,
    ineligibleCount: manifest.ineligibleCount,
    provenance: manifest.provenance || {}, shards: manifest.shards || {} };
}
function shardCore(shard) {
  return { schemaVersion: shard.schemaVersion,
    registryContentHash: shard.registryContentHash, shardKey: shard.shardKey,
    recipients: shard.recipients || {}, ineligible: shard.ineligible || {} };
}
function assertManifest(manifest) {
  const descriptorHash = sha256(releaseDescriptorCore(RELEASE_DESCRIPTOR));
  const actual = sha256(manifestCore(manifest));
  if (Number(RELEASE_DESCRIPTOR.schemaVersion) !== 1
      || String(RELEASE_DESCRIPTOR.descriptorHash || "").toLowerCase() !== descriptorHash)
    throw failure(503, "recipient_registry_release_invalid",
      "This API release has an invalid approved-recipient descriptor.");
  if (Number(manifest.schemaVersion) !== 1
      || Number(manifest.registrySchemaVersion) !== 1
      || String(manifest.contentHash || "").toLowerCase() !== actual)
    throw failure(503, "recipient_registry_incompatible",
      "The approved-recipient shard manifest failed its content-integrity check.");
  const sameRelease = actual === String(RELEASE_DESCRIPTOR.shardManifestHash || "").toLowerCase()
    && String(manifest.registryContentHash || "").toLowerCase()
      === String(RELEASE_DESCRIPTOR.registryContentHash || "").toLowerCase()
    && Number(manifest.recipientCount) === Number(RELEASE_DESCRIPTOR.recipientCount)
    && Number(manifest.ineligibleCount) === Number(RELEASE_DESCRIPTOR.ineligibleCount)
    && canonicalJson(manifest.provenance || {})
      === canonicalJson(RELEASE_DESCRIPTOR.provenance || {});
  if (!sameRelease) throw failure(503, "recipient_registry_release_mismatch",
    "The approved-recipient shard manifest does not belong to this API release.");
  return { ...manifest, contentHash: actual, ready: true };
}
function assertShard(payload, key, manifest) {
  const entry = (manifest.shards || {})[key];
  const actual = sha256(shardCore(payload));
  const valid = entry && Number(payload.schemaVersion) === 1
    && String(payload.shardKey || "") === key
    && String(payload.registryContentHash || "").toLowerCase()
      === String(manifest.registryContentHash || "").toLowerCase()
    && String(payload.contentHash || "").toLowerCase() === actual
    && String(entry.contentHash || "").toLowerCase() === actual
    && Number(entry.recipientCount) === Object.keys(payload.recipients || {}).length
    && Number(entry.ineligibleCount) === Object.keys(payload.ineligible || {}).length;
  if (!valid) throw failure(503, "recipient_registry_incompatible",
    "An approved-recipient shard failed its release-integrity check.");
}
function blobClient(name) {
  const connection = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!connection) throw failure(503, "recipient_registry_unavailable",
    "Approved-recipient storage is not configured.");
  return BlobServiceClient.fromConnectionString(connection)
    .getContainerClient(CONTAINER).getBlobClient(name);
}
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function retryableDownload(error) {
  const code = String(error && error.code || "");
  const status = Number(error && error.statusCode) || 0;
  return status === 408 || status === 429 || status >= 500
    || ["ECONNRESET", "ETIMEDOUT", "EAI_AGAIN", "ENOTFOUND",
      "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT"].includes(code);
}
async function downloadJson(name, priorEtag = "", compressed = false) {
  let last;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const blob = blobClient(name), properties = await blob.getProperties();
      const etag = String(properties.etag || "");
      if (priorEtag && etag && etag === priorEtag) return { unchanged: true, etag };
      const raw = await blob.downloadToBuffer();
      return { payload: JSON.parse((compressed ? zlib.gunzipSync(raw) : raw).toString("utf8")), etag };
    } catch (error) {
      last = error;
      if (!retryableDownload(error) || attempt === 2) break;
      await wait(attempt === 0 ? 250 : 750);
    }
  }
  if (last && last.code && String(last.code).startsWith("recipient_registry_")) throw last;
  throw failure(503, "recipient_registry_unavailable",
    `The approved-recipient registry could not be refreshed: ${last && last.message || last}`);
}
async function loadManifest(options = {}) {
  const force = options === true || options.force === true;
  if (manifestCache && !force && Date.now() - manifestLoadedAt < TTL_MS) return manifestCache;
  const result = await downloadJson(MANIFEST_BLOB, manifestCache ? manifestEtag : "");
  if (result.unchanged) { manifestLoadedAt = Date.now(); return manifestCache; }
  manifestCache = assertManifest(result.payload); manifestEtag = result.etag || "";
  manifestLoadedAt = Date.now();
  // A new manifest makes every previously hydrated shard stale as a unit.
  shardCache.clear();
  return manifestCache;
}
async function loadShard(key, manifest, options = {}) {
  const force = options === true || options.force === true;
  const held = shardCache.get(key);
  if (held && !force && Date.now() - held.loadedAt < TTL_MS) return held.index;
  const entry = (manifest.shards || {})[key];
  if (!entry || !entry.blob) return { recipients: new Map(), ineligible: new Map(),
    contentHash: manifest.registryContentHash, ready: true };
  const result = await downloadJson(String(entry.blob), held ? held.etag : "", true);
  if (result.unchanged) { held.loadedAt = Date.now(); return held.index; }
  assertShard(result.payload, key, manifest);
  const pseudo = { schemaVersion: 1, recipients: result.payload.recipients || {},
    ineligible: result.payload.ineligible || {}, provenance: {} };
  pseudo.contentHash = expectedContentHash(pseudo);
  const index = hydratePayload(pseudo, false);
  index.contentHash = manifest.registryContentHash;
  shardCache.set(key, { etag: result.etag || "", loadedAt: Date.now(), index });
  return index;
}
async function load(options = {}) {
  // useIndex() deliberately retains the old whole-index contract for unit
  // tests. Production loads only the small PII-free manifest here; resolve()
  // downloads the one or few CRD shards a batch actually needs.
  if (cache) return cache;
  return loadManifest(options);
}
async function resolve(crd, options = {}) {
  const id = String(crd || "").trim();
  if (!id) throw failure(400, "recipient_crd_required", "Every advisor recipient needs a CRD.");
  const manifest = cache ? null : await loadManifest(options);
  const index = cache || await loadShard(shardKey(id), manifest, options);
  const record = index.recipients.get(id);
  if (!record) throw failure(409, "recipient_not_approved",
    `CRD ${id} does not have an approved email recipient.`,
    index.ineligible.get(id) || "not_in_approved_registry");
  return { ...record, registryHash: cache ? index.contentHash : manifest.registryContentHash };
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
  loadedAt = Date.now(); sourceEtag = "test";
  manifestCache = null; manifestLoadedAt = 0; manifestEtag = ""; shardCache.clear();
  return cache;
}
function reset() {
  cache = null; loadedAt = 0; sourceEtag = "";
  manifestCache = null; manifestLoadedAt = 0; manifestEtag = ""; shardCache.clear();
}
module.exports = { load, resolve, verify, allowedTeammates, verifyTeammates,
  verifyActPair, useIndex, reset, norm, failure, canonicalJson, expectedContentHash,
  hydrate, policy, shardKey, manifestCore, shardCore, assertManifest, assertShard };
