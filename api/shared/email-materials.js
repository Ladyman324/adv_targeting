"use strict";
const crypto = require("crypto"), fs = require("fs"), path = require("path");
const CHANNELS = Object.freeze(["generic", "ubs", "mswm", "ml", "rj"]), CHANNEL_SET = new Set(CHANNELS);
const AUDIENCES = Object.freeze(["advisor_only", "client"]), AUDIENCE_SET = new Set(AUDIENCES);
const STRATEGIES = Object.freeze(["general", "acv", "lcv", "combined"]), STRATEGY_SET = new Set(STRATEGIES);
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
function audience(v) {
  const raw = String(v || "advisor_only").trim().toLowerCase().replace(/[ -]+/g, "_");
  const x = raw === "advisor" ? "advisor_only" : raw;
  if (!AUDIENCE_SET.has(x)) throw error(`Unknown material audience "${raw}".`);
  return x;
}
function strategy(v) {
  const raw = String(v || "general").trim().toLowerCase().replace(/[ -]+/g, "_");
  const x = ["all_cap", "allcap", "mf", "mutual_fund"].includes(raw) ? "acv"
    : ["large_cap", "largecap"].includes(raw) ? "lcv" : raw;
  if (!STRATEGY_SET.has(x)) throw error(`Unknown material strategy "${raw}".`);
  return x;
}
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
function normalizeMetadata(input = {}) {
  const periodKind = String(input.periodKind || "").trim().toLowerCase();
  if (periodKind && !["quarter", "month", "year", "evergreen", "as_of"].includes(periodKind))
    throw error(`Unknown material period kind "${periodKind}".`);
  return { familyId: cleanId(input.familyId), category: String(input.category || "").trim().slice(0, 80),
    channel: channel(input.channel || "generic"), audience: audience(input.audience || "advisor_only"),
    strategy: strategy(input.strategy || "general"),
    periodKey: String(input.periodKey || "").trim().slice(0, 20), periodKind,
    asOfDate: String(input.asOfDate || "").trim().slice(0, 20),
    freshness: (() => { const value = String(input.freshness || "current").trim().toLowerCase();
      return ["current", "stale", "superseded", "expired", "withdrawn", "future"].includes(value) ? value : "current"; })(),
    genericFallbackChannels: [...new Set((input.genericFallbackChannels || input.fallbackChannels || [])
      .map(channel).filter((x) => x !== "generic"))].sort() };
}
function materialSlotKey(input = {}) {
  const m = normalizeMetadata(input);
  const periodIdentity = m.periodKey ? `${m.periodKind}|${m.periodKey}` : `${m.periodKind}|${m.asOfDate}`;
  return [m.familyId, m.channel, m.strategy, periodIdentity].join("|");
}
function replacementMetadata(input = {}, existing = {}) {
  const own = (key) => Object.prototype.hasOwnProperty.call(input, key);
  return normalizeMetadata({
    familyId: own("familyId") ? input.familyId : existing.familyId,
    category: own("category") ? input.category : existing.category,
    channel: own("channel") ? input.channel : existing.channel,
    audience: own("audience") ? input.audience : existing.audience,
    strategy: own("strategy") ? input.strategy : existing.strategy,
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
function bestDocument(documents) {
  return [...documents].sort((a, b) =>
    String(b.periodKey || b.asOfDate || "").localeCompare(String(a.periodKey || a.asOfDate || ""))
    || (audience(b.audience) === "client" ? 1 : 0) - (audience(a.audience) === "client" ? 1 : 0)
    || Number(b.version || 0) - Number(a.version || 0))[0];
}
function strategySelection(matches, target, held, familyId) {
  const available = new Set(matches.map((d) => strategy(d.strategy)));
  const hasSpecific = [...available].some((value) => value !== "general");
  if (!hasSpecific) return [bestDocument(matches)];
  const pick = (value) => {
    const doc = bestDocument(matches.filter((d) => strategy(d.strategy) === value));
    if (!doc) throw error(`No current approved ${target.toUpperCase()} ${value.toUpperCase()} material is available for ${familyId}.`, "material_variant_unavailable");
    return doc;
  };
  if (target === "ubs") return [pick("combined")];
  if (target === "rj") {
    const desired = [];
    if (held.has("acv")) desired.push("acv");
    if (held.has("lcv")) desired.push("lcv");
    if (!desired.length) desired.push("lcv");
    return desired.map(pick);
  }
  if (available.has("general")) return [pick("general")];
  if (available.has("combined")) return [pick("combined")];
  throw error(`No general current approved ${target.toUpperCase()} material is available for ${familyId}.`, "material_variant_unavailable");
}
function resolveFamilies(documents, familyIds, recipientEmail, policy, options = {}) {
  const wanted = [...new Set((familyIds || []).map(cleanId).filter(Boolean))];
  const target = resolveChannel(recipientEmail, policy), chosen = [];
  const held = new Set((options.strategies || []).map(strategy).filter((value) => value === "acv" || value === "lcv"));
  for (const familyId of wanted) {
    const candidates = documents.filter((d) => d.familyId === familyId && currentDocument(d));
    let matches = candidates.filter((d) => d.channel === target);
    let selectedTarget = target;
    if (!matches.length && target !== "generic") {
      matches = candidates.filter((d) => d.channel === "generic" && (d.genericFallbackChannels || []).includes(target));
      selectedTarget = "generic";
    }
    if (!matches.length) throw error(`No current approved ${target.toUpperCase()} material is available for ${familyId}.`, "material_variant_unavailable");
    chosen.push(...strategySelection(matches, selectedTarget, held, familyId));
  }
  return { channel: target, documents: [...new Map(chosen.map((doc) => [doc.id, doc])).values()] };
}

/* Template requirements used to point at one PDF id. Once documents gained a
 * family and client-group channel, that made a template accidentally require
 * (for example) the UBS PDF instead of "Case for Value". Translate those old
 * requirements at the boundary: a family document becomes a family
 * requirement, while a genuinely standalone document remains exact. New
 * templates store requiredMaterialFamilyIds explicitly; the translation keeps
 * every existing template working until an administrator next saves it. */
function templateRequirements(template = {}, documents = []) {
  const explicitFamilies = (Array.isArray(template.requiredMaterialFamilyIds)
    ? template.requiredMaterialFamilyIds : []).map(cleanId).filter(Boolean);
  const requiredDocumentIds = Array.isArray(template.requiredDocumentIds)
    && template.requiredDocumentIds.length
    ? template.requiredDocumentIds : (template.defaultAttachmentIds || []);
  const legacyIds = [...new Set(requiredDocumentIds
    .map((value) => String(value || "").trim()).filter(Boolean))];
  const byId = new Map((documents || []).map((doc) => [String(doc.id || ""), doc]));
  const familyIds = [...explicitFamilies], documentIds = [], missingDocumentIds = [];
  for (const id of legacyIds) {
    const doc = byId.get(id);
    if (!doc) { missingDocumentIds.push(id); continue; }
    const familyId = cleanId(doc.familyId);
    if (familyId) familyIds.push(familyId);
    else documentIds.push(id);
  }
  return {
    familyIds: [...new Set(familyIds)],
    documentIds: [...new Set(documentIds)],
    missingDocumentIds,
  };
}

module.exports = { CHANNELS, AUDIENCES, STRATEGIES, channel, audience, strategy,
  domain, emailDomain, hash, validateRoutes, loadSeed, resolveChannel,
  normalizeMetadata, materialSlotKey, replacementMetadata, latestCompletedQuarter,
  freshnessOf, currentDocument, resolveFamilies, templateRequirements };
