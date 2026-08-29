"use strict";

const MAX_AHEAD_MS = 7 * 86400000;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/;

function scheduledInstant(value, nowMs, cancellationSeconds) {
  const raw = String(value || "").trim();
  if (!ISO_INSTANT.test(raw)) {
    const error = new Error("Choose a scheduled time as an ISO UTC instant.");
    error.statusCode = 400; error.code = "schedule_time_invalid"; throw error;
  }
  const when = Date.parse(raw), now = Number(nowMs);
  if (!Number.isFinite(when)) {
    const error = new Error("The scheduled time is not valid.");
    error.statusCode = 400; error.code = "schedule_time_invalid"; throw error;
  }
  const earliest = now + Math.max(0, Number(cancellationSeconds) || 0) * 1000;
  if (when < earliest) {
    const error = new Error("The scheduled time must leave the full cancellation window.");
    error.statusCode = 400; error.code = "schedule_time_too_soon"; throw error;
  }
  if (when > now + MAX_AHEAD_MS) {
    const error = new Error("Scheduled sends can start no more than 7 days from now.");
    error.statusCode = 400; error.code = "schedule_time_too_late"; throw error;
  }
  return new Date(when).toISOString();
}

function due(batch, nowMs = Date.now()) {
  const at = Date.parse(String(batch && batch.scheduledForUtc || ""));
  return Number.isFinite(at) && at <= Number(nowMs);
}

function messageDueUtc(batch, message, cfg) {
  const base = Date.parse(String(batch && batch.sendNotBeforeUtc || ""));
  if (!Number.isFinite(base)) return "";
  const position = Number(message && message.sendPosition) >= 0
    ? Number(message.sendPosition) : Math.max(0, Number(message && message.ordinal) || 0);
  const interval = Math.max(1, Number(cfg && cfg.mailboxIntervalSeconds) || 5);
  return new Date(base + position * interval * 1000).toISOString();
}
function currentRevision(batch, work) {
  return Number(batch && batch.scheduleRevision) > 0
    && Number(batch.scheduleRevision) === Number(work && work.scheduleRevision);
}

module.exports = { MAX_AHEAD_MS, scheduledInstant, due, messageDueUtc, currentRevision };