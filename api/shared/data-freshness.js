"use strict";
const fs = require("node:fs");
const path = require("node:path");
const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA = /^[a-f0-9]{64}$/;
const COMMIT = /^[a-f0-9]{7,40}$/;
const STATUS = new Set(["success", "succeeded", "available", "acquired", "imported", "reused", "failed", "skipped", "unavailable", "incomplete", "never_loaded", "unknown"]);
const safeId = (v) => typeof v === "string" && ID.test(v) ? v : null;
const hash = (v) => typeof v === "string" && SHA.test(v) ? v : null;
function date(v) {
  return typeof v === "string" && /^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?$/.test(v)
    && Number.isFinite(Date.parse(v)) && new Date(v.slice(0, 10)).toISOString().slice(0, 10) === v.slice(0, 10)
    ? v.length === 10 ? v : new Date(v).toISOString().replace(".000Z", "Z") : null;
}
function label(v) {
  return typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9 &'!(),.+_-]{0,119}$/.test(v) ? v : null;
}
function isAdmin(who) {
  return (who.roles || []).some((role) => ["administrator", "emailadministrator"].includes(String(role).toLowerCase()));
}
function project(raw, now = new Date()) {
  const unavailable = { schemaVersion: 1, available: false, candidate: null, publication: null, sources: [] };
  if (!raw || raw.schemaVersion !== 1 || !Array.isArray(raw.sources) || raw.sources.length > 250) return unavailable;
  const v = raw.candidate || {};
  const candidate = { id: safeId(v.id), createdUtc: date(v.createdUtc),
    codeCommit: typeof v.codeCommit === "string" && COMMIT.test(v.codeCommit) ? v.codeCommit : null,
    staticDataFingerprint: safeId(v.staticDataFingerprint), recipientRegistryHash: hash(v.recipientRegistryHash) };
  if (!candidate.id || !candidate.createdUtc) return unavailable;
  const seen = new Set(), sources = [];
  for (const row of raw.sources) {
    if (!row || typeof row.id !== "string" || !SOURCE_ID.test(row.id) || !label(row.label) || seen.has(row.id)) return unavailable;
    seen.add(row.id);
    const freshnessDays = Number.isInteger(row.freshnessDays) && row.freshnessDays > 0 && row.freshnessDays <= 3660 ? row.freshnessDays : null;
    const sourceDate = date(row.sourceDate), lastSuccessUtc = date(row.lastSuccessUtc), retrievedUtc = date(row.retrievedUtc);
    const snapshotDate = date(row.snapshotDate);
    const basis = sourceDate || snapshotDate || lastSuccessUtc || retrievedUtc;
    const ageDays = basis && Date.parse(basis) <= now.getTime()
      ? Math.floor((now.getTime() - Date.parse(basis)) / 86400000) : null;
    const attemptStatus = STATUS.has(row.attemptStatus) ? row.attemptStatus : "unknown";
    const status = STATUS.has(row.status) ? row.status : "unknown";
    const freshness = attemptStatus === "failed" || status === "failed" ? "failed"
      : !basis ? (status === "never_loaded" || status === "unavailable" ? "never_loaded" : "unknown")
      : freshnessDays === null || ageDays === null ? "unknown" : ageDays > freshnessDays ? "old" : "current";
    sources.push({ id: row.id, label: row.label, sourceDate, snapshotDate, edition: label(row.edition), retrievedUtc,
      lastSuccessUtc, lastAttemptUtc: date(row.lastAttemptUtc), attemptStatus, status,
      records: Number.isSafeInteger(row.records) && row.records >= 0 ? row.records : null,
      sha256: hash(row.sha256), snapshotId: safeId(row.snapshotId), freshnessDays, ageDays, freshness,
      errorCode: typeof row.errorCode === "string" && /^[A-Z][A-Z0-9_]{0,63}$/.test(row.errorCode) ? row.errorCode : null,
      candidateIncluded: !!hash(row.sha256), publishedIncluded: false });
  }
  const p = raw.publication || {};
  const publicationValid = p.verified === true && date(p.publishedUtc) && p.candidateId === candidate.id
    && candidate.staticDataFingerprint && p.staticDataFingerprint === candidate.staticDataFingerprint
    && candidate.recipientRegistryHash && p.recipientRegistryHash === candidate.recipientRegistryHash
    && candidate.codeCommit && p.apiCommit === candidate.codeCommit
    && typeof p.apiCommit === "string" && COMMIT.test(p.apiCommit) && p.sourceHashes && typeof p.sourceHashes === "object"
    && sources.some((row) => row.candidateIncluded)
    && sources.filter((row) => row.candidateIncluded).every((row) => p.sourceHashes[row.id] === row.sha256);
  const publication = publicationValid ? { verified: true, publishedUtc: date(p.publishedUtc), candidateId: candidate.id,
    staticDataFingerprint: candidate.staticDataFingerprint, recipientRegistryHash: candidate.recipientRegistryHash,
    apiCommit: p.apiCommit } : null;
  for (const row of sources) row.publishedIncluded = !!publication && row.candidateIncluded;
  return { schemaVersion: 1, available: true, candidate, publication, sources };
}
function read(file = path.join(__dirname, "data-freshness.json"), now = new Date(),
  publicationOverride = process.env.DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON) {
  try {
    if (fs.statSync(file).size > 512 * 1024) return project(null, now);
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    if (publicationOverride !== undefined && publicationOverride !== "") {
      // Deployment attestation lives outside the immutable candidate package.
      // A bad/stale setting must not hide source metadata or imply publication.
      raw.publication = null;
      if (typeof publicationOverride === "string" && Buffer.byteLength(publicationOverride, "utf8") <= 64 * 1024) {
        try { raw.publication = JSON.parse(publicationOverride); } catch { /* unverified */ }
      }
    }
    return project(raw, now);
  } catch { return project(null, now); }
}
module.exports = { project, read, isAdmin };
