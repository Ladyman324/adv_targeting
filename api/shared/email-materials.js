"use strict";
const crypto = require("crypto"), fs = require("fs"), path = require("path");
const CHANNELS = Object.freeze(["generic", "ubs", "mswm", "ml", "rj"]), CHANNEL_SET = new Set(CHANNELS);
const DOMAIN_RE = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const SECOND_LEVEL_PUBLIC_SUFFIXES = Object.freeze({
  uk: ["co", "org", "ac", "gov", "net", "ltd", "plc", "me", "sch"],
  au: ["com", "net", "org", "edu", "gov", "asn", "id"],
  nz: ["co", "net", "org", "ac", "gov", "geek", "maori", "iwi", "school"],
  jp: ["co", "ne", "or", "ac", "go", "ad", "ed", "gr", "lg"],
  br: ["com", "net", "org", "edu", "gov"],
  za: ["co", "org", "net", "ac", "gov", "school"],
  mx: ["com", "org", "net", "edu", "gob"],
});
const PUBLIC_SUFFIXES = new Set(Object.entries(SECOND_LEVEL_PUBLIC_SUFFIXES)
  .flatMap(([country, labels]) => labels.map((label) => `${label}.${country}`)));
function error(message, code = "material_policy_invalid") { const e = new Error(message); e.statusCode = 400; e.code = code; return e; }
function channel(v) { const x = String(v || "generic").trim().toLowerCase(); if (!CHANNEL_SET.has(x)) throw error(`Unknown material channel "${x}".`); return x; }
function domain(v) { const x = String(v || "").trim().toLowerCase().replace(/^@/, "").replace(/\.$/, ""); if (!DOMAIN_RE.test(x) || PUBLIC_SUFFIXES.has(x)) throw error(`Invalid material-routing domain "${v}".`); return x; }
function emailDomain(v) { const x = String(v || "").trim().toLowerCase(), at = x.lastIndexOf("@"); return at > 0 ? x.slice(at + 1) : ""; }
function canonical(v) { if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`; if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`; return JSON.stringify(v); }
function hash(v) { return crypto.createHash("sha256").update(canonical(v)).digest("hex"); }
function safeLabel(value, fallback) {
  const out = String(value || fallback || "").replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 80);
  return out || fallback || "";
}
function validateRoutes(input) {
  const rows = Array.isArray(input) ? input : (Array.isArray(input && input.rules) ? input.rules : []), seen = new Map(), rules = [];
  const seedVersion = safeLabel(input && input.seedVersion, "");
  for (const raw of rows) {
    if (!raw) continue;
    const d = domain(raw.domain), c = channel(raw.channel || raw.audienceCode);
    const rawStatus = String(raw.status || (raw.disabled ? "disabled" : "active")).toLowerCase();
    const status = ["active", "disabled", "pending"].includes(rawStatus) ? rawStatus : "active";
    const disabled = raw.disabled === true || status !== "active";
    if (c === "generic") throw error("Generic materials are a fallback, not a domain-routing rule.");
    if (seen.has(d) && seen.get(d) !== c) throw error(`${d} cannot route to both ${seen.get(d)} and ${c}.`, "material_domain_conflict");
    if (!seen.has(d)) {
      seen.set(d, c);
      const evidenceCount = Math.max(0, Math.min(1000000000, Math.trunc(Number(raw.evidenceCount) || 0)));
      const source = safeLabel(raw.source, raw.sourceSlugs ? "Roster seed" : "Administrator");
      rules.push({ domain: d, channel: c, status, source, evidenceCount,
        ...(disabled ? { disabled: true } : {}), ...(seedVersion ? { seedVersion } : {}) });
    }
  }
  rules.sort((a, b) => a.domain.localeCompare(b.domain));
  const core = { schemaVersion: 1, rules, ...(seedVersion ? { seedVersion } : {}) };
  return { ...core, hash: hash(core) };
}function loadSeed() { try { const p = path.resolve(__dirname, "material-domain-seed.json"), j = JSON.parse(fs.readFileSync(p, "utf8")); return validateRoutes({ seedVersion: j.seedVersion, rules: (j.rules || []).filter((r) => String(r.status || "").toLowerCase() === "active") }); } catch { return validateRoutes([]); } }
function resolveChannel(email, policy) { const host = emailDomain(email); if (!host) throw error("A canonical recipient email is required for material routing.", "material_recipient_invalid"); const hits = validateRoutes(policy).rules.filter((r) => r.status === "active" && !r.disabled && (host === r.domain || host.endsWith(`.${r.domain}`))).sort((a, b) => b.domain.length - a.domain.length); return hits.length ? hits[0].channel : "generic"; }
function cleanId(v) { return String(v || "").trim().toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 80); }
function normalizeMetadata(input = {}) { const periodKind = String(input.periodKind || "").trim().toLowerCase(); if (periodKind && !["quarter", "month", "year", "evergreen", "as_of"].includes(periodKind)) throw error(`Unknown material period kind "${periodKind}".`); return { familyId: cleanId(input.familyId), category: String(input.category || "").trim().slice(0, 80), channel: channel(input.channel || "generic"), periodKey: String(input.periodKey || "").trim().slice(0, 20), periodKind, asOfDate: String(input.asOfDate || "").trim().slice(0, 20), freshness: (() => { const value = String(input.freshness || "current").trim().toLowerCase(); return ["current", "stale", "superseded", "expired", "withdrawn", "future"].includes(value) ? value : "current"; })(), genericFallbackChannels: [...new Set((input.genericFallbackChannels || input.fallbackChannels || []).map(channel).filter((x) => x !== "generic"))].sort() }; }
function replacementMetadata(input = {}, existing = {}) {
  const own = (key) => Object.prototype.hasOwnProperty.call(input, key);
  return normalizeMetadata({
    familyId: own("familyId") ? input.familyId : existing.familyId,
    category: own("category") ? input.category : existing.category,
    channel: own("channel") ? input.channel : existing.channel,
    periodKey: own("periodKey") ? input.periodKey : existing.periodKey,
    periodKind: own("periodKind") ? input.periodKind : existing.periodKind,
    asOfDate: own("asOfDate") ? input.asOfDate : existing.asOfDate,
    freshness: own("freshness") ? input.freshness : existing.freshness,
    genericFallbackChannels: own("genericFallbackChannels")
      ? input.genericFallbackChannels : existing.genericFallbackChannels,
  });
}function latestCompletedQuarter(at = Date.now()) {
  const date = new Date(at), currentQuarter = Math.floor(date.getUTCMonth() / 3) + 1;
  return currentQuarter === 1 ? `${date.getUTCFullYear() - 1}-Q4`
    : `${date.getUTCFullYear()}-Q${currentQuarter - 1}`;
}
function freshnessOf(d, at = Date.now()) {
  if (!d || d.approved === false) return "withdrawn";
  const explicit = String(d.freshness || "current").toLowerCase();
  if (["stale", "superseded", "expired", "withdrawn", "future"].includes(explicit)) return explicit;
  const start = Date.parse(d.effectiveFrom || ""), end = Date.parse(d.effectiveUntil || "");
  if (Number.isFinite(start) && start > at) return "future";
  if (Number.isFinite(end) && end < at) return "expired";
  if (d.periodKind === "quarter" && /^\d{4}-Q[1-4]$/.test(String(d.periodKey || ""))) {
    const latest = latestCompletedQuarter(at);
    if (d.periodKey < latest) return "stale";
    if (d.periodKey > latest) return "future";
  }
  return "current";
}
function currentDocument(d, at = Date.now()) { return freshnessOf(d, at) === "current"; }
function resolveFamilies(documents, familyIds, recipientEmail, policy) { const wanted = [...new Set((familyIds || []).map(cleanId).filter(Boolean))], target = resolveChannel(recipientEmail, policy), chosen = []; for (const familyId of wanted) { const candidates = documents.filter((d) => d.familyId === familyId && currentDocument(d)); let matches = candidates.filter((d) => d.channel === target); if (!matches.length && target !== "generic") matches = candidates.filter((d) => d.channel === "generic" && (d.genericFallbackChannels || []).includes(target)); if (!matches.length) throw error(`No current approved ${target.toUpperCase()} material is available for ${familyId}.`, "material_variant_unavailable"); matches.sort((a, b) => String(b.periodKey || "").localeCompare(String(a.periodKey || "")) || Number(b.version || 0) - Number(a.version || 0)); chosen.push(matches[0]); } return { channel: target, documents: chosen }; }
module.exports = { CHANNELS, channel, domain, emailDomain, hash, validateRoutes, loadSeed, resolveChannel, normalizeMetadata, replacementMetadata, latestCompletedQuarter, freshnessOf, currentDocument, resolveFamilies };
