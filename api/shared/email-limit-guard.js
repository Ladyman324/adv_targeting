"use strict";

const crypto = require("crypto");
const { TableClient, odata } = require("@azure/data-tables");

const TIME_ZONE = "America/New_York";
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const formatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
let policyClient, ledgerClient;

async function clients() {
  const conn = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!conn) throw Object.assign(new Error("Email storage is not configured."), { statusCode: 503 });
  policyClient ||= TableClient.fromConnectionString(conn, "EmailPolicy", { allowInsecureConnection: false });
  ledgerClient ||= TableClient.fromConnectionString(conn, "EmailSendLedger", { allowInsecureConnection: false });
  await Promise.all([policyClient.createTable(), ledgerClient.createTable()]
    .map((p) => p.catch((e) => { if (e.statusCode !== 409) throw e; })));
  return { policy: policyClient, ledger: ledgerClient };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const pad = (n) => String(n).padStart(2, "0");
const parseJson = (value, fallback) => {
  try { const parsed = JSON.parse(String(value || "")); return parsed == null ? fallback : parsed; }
  catch { return fallback; }
};
const problem = (message, statusCode, code, extra = {}) =>
  Object.assign(new Error(message), { statusCode, code, ...extra });

function easternParts(value = Date.now()) {
  const date = value instanceof Date ? value : new Date(Number(value));
  const parts = Object.fromEntries(formatter.formatToParts(date)
    .filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return { year: Number(parts.year), month: Number(parts.month), day: Number(parts.day),
    hour: Number(parts.hour), minute: Number(parts.minute), second: Number(parts.second) };
}

function easternDay(value = Date.now()) {
  const p = easternParts(value);
  return `${p.year}-${pad(p.month)}-${pad(p.day)}`;
}

function dayParts(day) {
  if (!DAY_RE.test(String(day || ""))) throw problem("Invalid Eastern calendar day.", 400, "capacity_day_invalid");
  const [year, month, date] = String(day).split("-").map(Number);
  return { year, month, day: date };
}

function addDays(day, count) {
  const p = dayParts(day);
  const date = new Date(Date.UTC(p.year, p.month - 1, p.day + Number(count || 0), 12));
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
}

function isBusinessDay(day) {
  const p = dayParts(day);
  const weekday = new Date(Date.UTC(p.year, p.month - 1, p.day, 12)).getUTCDay();
  return weekday !== 0 && weekday !== 6;
}

function easternInstant(day, hour, minute = 0, second = 0) {
  const p = dayParts(day);
  const desired = Date.UTC(p.year, p.month - 1, p.day, hour, minute, second);
  let guess = desired;
  for (let i = 0; i < 4; i++) {
    const actual = easternParts(guess);
    const seen = Date.UTC(actual.year, actual.month - 1, actual.day,
      actual.hour, actual.minute, actual.second);
    guess += desired - seen;
  }
  const check = easternParts(guess);
  if (check.year !== p.year || check.month !== p.month || check.day !== p.day
      || check.hour !== hour || check.minute !== minute)
    throw problem("That Eastern local time does not exist.", 400, "capacity_time_invalid");
  return new Date(guess).toISOString();
}

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort()
    .map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
const digest = (value) => crypto.createHash("sha256").update(stable(value)).digest("hex");

function normalizeOrdered(ordered) {
  const seen = new Set();
  return (Array.isArray(ordered) ? ordered : []).map((entry) => {
    const key = String(entry && entry.key || "").trim();
    const units = Math.max(0, Math.floor(Number(entry && entry.units) || 0));
    if (!key || seen.has(key)) throw problem("Capacity plan entries must have unique keys.", 400,
      "capacity_entries_invalid");
    seen.add(key); return { key, units };
  });
}

function usageMap(value) {
  if (value instanceof Map) return new Map(value);
  return new Map(Object.entries(value || {}).map(([key, count]) => [key, Number(count) || 0]));
}

function previewPlan(ordered, options = {}) {
  const entries = normalizeOrdered(ordered);
  const limit = Math.max(1, Math.floor(Number(options.limit) || 0));
  const interval = Math.max(1, Math.floor(Number(options.mailboxIntervalSeconds) || 5));
  const nowMs = Number(options.nowMs == null ? Date.now() : options.nowMs);
  const usage = usageMap(options.usage);
  const approvalDay = easternDay(nowMs);
  const horizonDay = options.horizonDay || addDays(approvalDay,
    Math.max(0, Math.floor(Number(options.horizonDays == null ? 7 : options.horizonDays))));
  const requestedMs = Date.parse(String(options.startUtc || ""));
  const requested = Number.isFinite(requestedMs) ? requestedMs : nowMs;
  const requestedParts = easternParts(requested);
  let day = easternDay(requested);
  let firstMinutes = requestedParts.hour * 60 + requestedParts.minute;
  let exactFirstUtc = new Date(requested).toISOString();
  if (day < approvalDay) { day = approvalDay; firstMinutes = 9 * 60; exactFirstUtc = ""; }
  while (!isBusinessDay(day)) { day = addDays(day, 1); firstMinutes = 9 * 60; exactFirstUtc = ""; }
  if (firstMinutes < 9 * 60) { firstMinutes = 9 * 60; exactFirstUtc = ""; }
  if (firstMinutes >= 17 * 60) {
    do { day = addDays(day, 1); } while (!isBusinessDay(day));
    firstMinutes = 9 * 60; exactFirstUtc = "";
  }

  const assignments = [], summaries = [];
  let cursor = 0, trancheIndex = 0;
  while (cursor < entries.length && day <= horizonDay) {
    if (!isBusinessDay(day)) { day = addDays(day, 1); firstMinutes = 9 * 60; continue; }
    const used = Math.max(0, Number(usage.get(day)) || 0);
    let unitsLeft = Math.max(0, limit - used);
    const startMinute = trancheIndex === 0 ? firstMinutes : 9 * 60;
    // Keep seconds and milliseconds on the first tranche. Rounding down to the
    // minute could silently consume most of the cancellation window.
    const startUtc = trancheIndex === 0 && exactFirstUtc
      ? exactFirstUtc : easternInstant(day, Math.floor(startMinute / 60), startMinute % 60);
    const endUtc = Date.parse(easternInstant(day, 17, 0));
    const maxMessages = Math.max(0, Math.floor((endUtc - Date.parse(startUtc)) / (interval * 1000)) + 1);
    let position = 0, units = 0;
    while (cursor < entries.length && position < maxMessages) {
      const entry = entries[cursor];
      if (entry.units > limit)
        throw problem("One email reaches more external recipients than the daily allowance.",
          400, "capacity_message_too_large");
      if (entry.units > unitsLeft) break;
      const plannedSendUtc = new Date(Date.parse(startUtc) + position * interval * 1000).toISOString();
      assignments.push({ ...entry, day, plannedSendUtc, trancheIndex, tranchePosition: position });
      unitsLeft -= entry.units; units += entry.units; position++; cursor++;
    }
    if (position) summaries.push({ day, startUtc, messageCount: position, units,
      lastSendUtc: assignments[assignments.length - 1].plannedSendUtc });
    day = addDays(day, 1); firstMinutes = 9 * 60; trancheIndex++;
  }
  const externalUnits = entries.reduce((sum, entry) => sum + entry.units, 0);
  const scheduledUnits = assignments.reduce((sum, entry) => sum + entry.units, 0);
  const hashCore = { schemaVersion: 2, limit, ordered: entries,
    assignments: assignments.map(({ key, units, day, trancheIndex, tranchePosition }) =>
      ({ key, units, day, trancheIndex, tranchePosition })),
    scheduledStart: options.bindStart === true ? String(options.startUtc || "") : "" };
  const planHash = digest(hashCore);
  return { schemaVersion: 2, planHash, timeZone: TIME_ZONE, dailyLimit: limit,
    recipientCount: entries.length, externalUnits, scheduledCount: assignments.length,
    scheduledUnits, excessCount: entries.length - assignments.length,
    excessUnits: externalUnits - scheduledUnits, fit: assignments.length === entries.length,
    multiDay: summaries.length > 1, firstSendUtc: assignments[0] && assignments[0].plannedSendUtc || "",
    lastSendUtc: assignments.length && assignments[assignments.length - 1].plannedSendUtc || "",
    assignments, days: summaries, horizonDay };
}

async function acquireLock(policy, userId, owner) {
  for (let attempt = 0; attempt < 20; attempt++) {
    let row = null;
    try { row = await policy.getEntity("limit-lock", userId); }
    catch (e) { if (e.statusCode !== 404) throw e; }
    const leaseUntilUtc = new Date(Date.now() + 15000).toISOString();
    if (!row) {
      try { await policy.createEntity({ partitionKey: "limit-lock", rowKey: userId,
        owner, leaseUntilUtc }); return; }
      catch (e) { if (e.statusCode !== 409) throw e; }
    } else if (!row.owner || Date.parse(row.leaseUntilUtc || "") < Date.now()) {
      try { await policy.updateEntity({ partitionKey: "limit-lock", rowKey: userId,
        owner, leaseUntilUtc }, "Merge", { etag: row.etag }); return; }
      catch (e) { if (e.statusCode !== 412) throw e; }
    }
    await wait(50 + Math.floor(Math.random() * 100));
  }
  throw problem("Could not acquire the per-user send-limit guard; retry approval.", 409,
    "capacity_lock_busy");
}

async function renewLock(policy, userId, owner) {
  const row = await policy.getEntity("limit-lock", userId);
  if (row.owner !== owner) throw problem("Daily capacity changed while approval was being checked.",
    409, "capacity_plan_changed");
  await policy.updateEntity({ partitionKey: "limit-lock", rowKey: userId, owner,
    leaseUntilUtc: new Date(Date.now() + 15000).toISOString() }, "Merge", { etag: row.etag });
}

async function releaseLock(policy, userId, owner) {
  try {
    const row = await policy.getEntity("limit-lock", userId);
    if (row.owner === owner) await policy.updateEntity({ partitionKey: "limit-lock", rowKey: userId,
      owner: "", leaseUntilUtc: new Date(0).toISOString() }, "Merge", { etag: row.etag });
  } catch {}
}

function rowAllocations(row) {
  if (Number(row && row.schemaVersion) === 2) {
    if (row.state === "released") return [];
    const released = new Set(parseJson(row.releasedKeysJson, []));
    return parseJson(row.allocationsJson, []).filter((entry) => !released.has(String(entry.key)))
      .map((entry) => ({ key: String(entry.key), day: String(entry.day), units: Number(entry.units) || 0 }));
  }
  const units = Number(row && row.externalCount) || 0;
  return units && row.reservedUtc
    ? [{ key: String(row.rowKey || "legacy"), day: easternDay(Date.parse(row.reservedUtc)), units }] : [];
}

async function readUsage(ledger, userId, excludeId = "") {
  const usage = new Map();
  for await (const row of ledger.listEntities({ queryOptions: {
    filter: odata`PartitionKey eq ${userId}` } })) {
    if (excludeId && String(row.rowKey) === String(excludeId)) continue;
    for (const entry of rowAllocations(row))
      usage.set(entry.day, (usage.get(entry.day) || 0) + entry.units);
  }
  return usage;
}

async function capacitySnapshot(userId, options = {}) {
  const { ledger } = await clients();
  const nowMs = Number(options.nowMs == null ? Date.now() : options.nowMs);
  const limit = Math.max(1, Math.floor(Number(options.limit) || 0));
  const usage = await readUsage(ledger, userId, options.excludeReservationId || "");
  const today = easternDay(nowMs), days = [];
  for (let i = 0; i <= Math.max(0, Number(options.horizonDays == null ? 7 : options.horizonDays)); i++) {
    const day = addDays(today, i), committed = Math.max(0, usage.get(day) || 0);
    days.push({ day, committed, remaining: Math.max(0, limit - committed),
      businessDay: isBusinessDay(day) });
  }
  const current = days[0];
  return { available: true, timeZone: TIME_ZONE, today, dailyLimit: limit,
    committedToday: current.committed, remainingToday: current.remaining, days };
}

async function getOptional(ledger, userId, id) {
  try { return await ledger.getEntity(userId, id); }
  catch (error) { if (error.statusCode === 404) return null; throw error; }
}

function storedPlan(row) {
  const saved = parseJson(row && row.planJson, null);
  if (saved) return saved;
  return { schemaVersion: 2, planHash: row.planHash || "", timeZone: TIME_ZONE,
    dailyLimit: Number(row.limit) || 0, externalUnits: Number(row.externalCount) || 0,
    assignments: parseJson(row.allocationsJson, []), days: [] };
}

async function reservePlan(userId, reservationId, ordered, options = {}) {
  const { policy, ledger } = await clients(), owner = crypto.randomUUID();
  await acquireLock(policy, userId, owner);
  try {
    const existing = await getOptional(ledger, userId, reservationId);
    if (existing) {
      if (Number(existing.schemaVersion) !== 2)
        return replayReservation(existing, normalizeOrdered(ordered)
          .reduce((sum, entry) => sum + entry.units, 0));
      const plan = storedPlan(existing);
      const requested = normalizeOrdered(ordered);
      const frozen = (plan.assignments || []).map(({ key, units }) => ({ key, units }));
      if (stable(requested) !== stable(frozen))
        throw problem("This operation id is already reserved for a different delivery plan. Start a new send operation.",
          409, "idempotency_conflict");
      if (options.expectedPlanHash && plan.planHash !== options.expectedPlanHash)
        throw problem("Daily capacity changed. Review the updated delivery plan before approving.",
          409, "capacity_plan_changed", { deliveryPlan: plan });
      return { ...plan, alreadyReserved: true };
    }
    const usage = await readUsage(ledger, userId, reservationId);
    const plan = previewPlan(ordered, { ...options, usage });
    if (options.expectedPlanHash && plan.planHash !== options.expectedPlanHash)
      throw problem("Daily capacity changed. Review the updated delivery plan before approving.",
        409, "capacity_plan_changed", { deliveryPlan: plan });
    if (!plan.fit) throw problem(`${plan.scheduledCount} of ${plan.recipientCount} emails fit within the seven-day delivery window. Remove at least ${plan.excessCount} or create Outlook drafts.`,
      409, "capacity_horizon_exceeded", { deliveryPlan: plan });
    await renewLock(policy, userId, owner);
    const at = new Date().toISOString();
    await ledger.createEntity({ partitionKey: userId, rowKey: reservationId,
      schemaVersion: 2, kind: options.kind || "campaign", state: "active",
      timeZone: TIME_ZONE, limit: plan.dailyLimit, planHash: plan.planHash,
      allocationsJson: JSON.stringify(plan.assignments.map(({ key, day, units }) => ({ key, day, units }))),
      planJson: JSON.stringify(plan), releasedKeysJson: "[]",
      externalCount: plan.externalUnits, reservedUtc: at, activatedUtc: at });
    return { ...plan, alreadyReserved: false };
  } finally { await releaseLock(policy, userId, owner); }
}

async function assertReservation(userId, reservationId, expectedPlanHash) {
  const { ledger } = await clients();
  const row = await getOptional(ledger, userId, reservationId);
  if (!row || row.state === "released")
    throw problem("The approved daily-capacity reservation is unavailable.", 409,
      "capacity_reservation_missing");
  if (Number(row.schemaVersion) === 2 && expectedPlanHash && row.planHash !== expectedPlanHash)
    throw problem("The approved daily-capacity plan changed.", 409, "capacity_plan_changed");
  return Number(row.schemaVersion) === 2 ? storedPlan(row) : row;
}

async function releaseAllocations(userId, reservationId, keys) {
  const wanted = new Set((keys || []).map(String));
  if (!wanted.size) return { released: 0 };
  const { policy, ledger } = await clients(), owner = crypto.randomUUID();
  await acquireLock(policy, userId, owner);
  try {
    const row = await getOptional(ledger, userId, reservationId);
    if (!row || Number(row.schemaVersion) !== 2) return { released: 0 };
    const allocations = parseJson(row.allocationsJson, []);
    const released = new Set(parseJson(row.releasedKeysJson, []));
    let count = 0;
    for (const entry of allocations) if (wanted.has(String(entry.key)) && !released.has(String(entry.key))) {
      released.add(String(entry.key)); count++;
    }
    await ledger.updateEntity({ partitionKey: userId, rowKey: reservationId,
      releasedKeysJson: JSON.stringify([...released]),
      state: released.size >= allocations.length ? "released" : "active",
      releasedUtc: new Date().toISOString() }, "Merge", { etag: row.etag });
    return { released: count };
  } finally { await releaseLock(policy, userId, owner); }
}

function replayReservation(existing, requestedExternalCount) {
  const reserved = Number(existing && existing.externalCount) || 0;
  const requested = Number(requestedExternalCount) || 0;
  if (reserved !== requested) throw problem("This operation id is already reserved for a different recipient count. Start a new send operation.",
    409, "idempotency_conflict");
  return { alreadyReserved: true, externalCount: reserved };
}

async function reserve(userId, id, externalCount, limit) {
  if (process.env.EMAIL_CALENDAR_CAPACITY_ENABLED !== "1") {
    // Compatibility behavior for an intentionally staged deployment.
    const { policy, ledger } = await clients(), owner = crypto.randomUUID();
    await acquireLock(policy, userId, owner);
    try {
      const existing = await getOptional(ledger, userId, id);
      if (existing) return replayReservation(existing, externalCount);
      const since = Date.now() - 86400000; let rolling = 0;
      const today = easternDay();
      for await (const row of ledger.listEntities({ queryOptions: { filter: odata`PartitionKey eq ${userId}` } })) {
        if (Number(row.schemaVersion) === 2) {
          rolling += rowAllocations(row).filter((entry) => entry.day === today)
            .reduce((sum, entry) => sum + entry.units, 0);
        } else if (Date.parse(row.reservedUtc || "") >= since) rolling += Number(row.externalCount) || 0;
      }
      if (rolling + externalCount > limit) throw problem(`This send needs ${externalCount} external recipients and you have ${Math.max(0, limit - rolling)} left of the ${limit} allowed in any 24-hour period. Creating Outlook drafts instead is unaffected.`,
        429, "rolling_limit");
      await ledger.createEntity({ partitionKey: userId, rowKey: id,
        externalCount: Number(externalCount) || 0, reservedUtc: new Date().toISOString() });
      return { alreadyReserved: false, externalCount, rollingAfter: rolling + externalCount };
    } finally { await releaseLock(policy, userId, owner); }
  }
  const { policy, ledger } = await clients(), owner = crypto.randomUUID();
  await acquireLock(policy, userId, owner);
  try {
    const existing = await getOptional(ledger, userId, id);
    if (existing) return replayReservation(existing, externalCount);
    const today = easternDay(), usage = await readUsage(ledger, userId);
    const used = usage.get(today) || 0, remaining = Math.max(0, limit - used);
    if (used + Number(externalCount) > limit) throw problem(`This send needs ${externalCount} external recipients and you have ${remaining} of ${limit} available today. Daily capacity resets at midnight Eastern.`,
      429, "daily_limit");
    const now = new Date().toISOString(), plan = { schemaVersion: 2,
      planHash: digest({ id, today, externalCount }), timeZone: TIME_ZONE, dailyLimit: limit,
      recipientCount: 1, externalUnits: Number(externalCount) || 0, scheduledCount: 1,
      excessCount: 0, fit: true, multiDay: false, firstSendUtc: now, lastSendUtc: now,
      assignments: [{ key: id, units: Number(externalCount) || 0, day: today,
        plannedSendUtc: now, trancheIndex: 0, tranchePosition: 0 }],
      days: [{ day: today, startUtc: now, messageCount: 1, units: Number(externalCount) || 0 }] };
    await ledger.createEntity({ partitionKey: userId, rowKey: id, schemaVersion: 2,
      kind: "direct", state: "active", timeZone: TIME_ZONE, limit,
      planHash: plan.planHash, allocationsJson: JSON.stringify(plan.assignments),
      planJson: JSON.stringify(plan), releasedKeysJson: "[]",
      externalCount: Number(externalCount) || 0, reservedUtc: now, activatedUtc: now });
    return { alreadyReserved: false, externalCount, dailyAfter: used + Number(externalCount) };
  } finally { await releaseLock(policy, userId, owner); }
}

function __setClientsForTest(policy, ledger) {
  policyClient = policy || null;
  ledgerClient = ledger || null;
}

module.exports = { TIME_ZONE, easternDay, easternInstant, addDays, isBusinessDay,
  previewPlan, capacitySnapshot, reservePlan, assertReservation, releaseAllocations,
  reserve, replayReservation, rowAllocations, __setClientsForTest };
