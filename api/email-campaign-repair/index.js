"use strict";

/* Repair dispatch for campaign email.
 *
 * Azure Queue owns normal execution, independently of any browser. Queue
 * publication is nevertheless a separate storage write from message state: a
 * host termination in that narrow gap can leave valid work with no queue item.
 * This timer turns the durable message row back into a queue hint. Workers keep
 * the safety boundary: ETag leases, Graph application-id reconciliation and the
 * rule that an uncertain submission is reconciled rather than re-sent.
 *
 * The timer carries identifiers only. It never reads message content into logs,
 * calls Graph, or sends mail itself.
 */
const store = require("../shared/email-store");
const service = require("../shared/email-service");
const core = require("../shared/email-core");
const schedule = require("../shared/email-schedule");
const capacity = require("../shared/email-limit-guard");

const ACTIVE_BATCHES = new Set(["drafting", "sending", "scheduled", "schedule_held"]);
const TERMINAL_MESSAGES = new Set(["draft_ready", "sent", "failed", "canceled", "auth_required"]);
const MAX_BATCHES_PER_USER = 500;
const MAX_ENQUEUED = 50;
const MAX_ENQUEUED_PER_USER = 2;
const MAX_ATTEMPTS = 100;
const GRACE_MS = 120000;

function allowlist() {
  return new Set(String(process.env.EMAIL_CAMPAIGN_REPAIR_USER_IDS || "")
    .split(/[;,\s]+/).map((value) => value.trim().toLowerCase()).filter(Boolean));
}

function timestamp(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function workFor(message, batch, nowMs, intervalSeconds) {
  if (!message || TERMINAL_MESSAGES.has(message.state)) return null;
  const approvedAt = timestamp(batch.approvedUtc);
  if (!approvedAt) return null;
  if (message.leaseUntilUtc && timestamp(message.leaseUntilUtc) > nowMs) return null;
  if (message.retryAfterUtc && timestamp(message.retryAfterUtc) > nowMs) return null;
  if (timestamp(message.updatedUtc) > nowMs - GRACE_MS) return null;

  const position = Number(message.sendPosition) >= 0
    ? Number(message.sendPosition) : Number(message.ordinal) || 0;
  if (["draft_pending", "draft_ambiguous", "draft_creating"].includes(message.state)) {
    const due = timestamp(message.plannedSendUtc)
      || approvedAt + position * intervalSeconds * 1000;
    return due && due > nowMs ? null : "draft";
  }
  if (batch.mode !== "send") return null;
  const sendNotBefore = timestamp(batch.sendNotBeforeUtc);
  if (!sendNotBefore) return null;
  if (["submitted", "send_ambiguous"].includes(message.state)) return "reconcile";
  if (["send_scheduled", "sending"].includes(message.state)) {
    const due = timestamp(schedule.messageDueUtc(batch, message, { mailboxIntervalSeconds: intervalSeconds }));
    return due && due > nowMs ? null : "send";
  }
  return null;
}

function logger(context, message) {
  if (context && typeof context.log === "function") context.log(message);
}

async function run(context = {}, overrides = {}) {
  const deps = { store, enqueue: service.enqueue, core, capacity,
    now: () => Date.now(), ...overrides };
  const summary = { enabled: false, users: 0, batches: 0, messages: 0,
    eligible: 0, promoted: 0, conflicts: 0, attempted: 0, enqueued: 0, failed: 0 };
  if (process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED !== "1") {
    logger(context, "campaign email repair disabled");
    return summary;
  }
  summary.enabled = true;
  const permitted = allowlist();
  const interval = Math.max(1, Number(deps.core.config().mailboxIntervalSeconds) || 5);
  const nowMs = Number(deps.now());
  const connections = await deps.store.listConnections();
  // Rotate the starting mailbox every run. Together with the per-user cap this
  // bounds recovery latency even when the first several mailboxes all have a
  // backlog large enough to reach the global cap.
  const offset = connections.length
    ? Math.floor(nowMs / 300000) % connections.length : 0;
  const orderedConnections = connections.slice(offset).concat(connections.slice(0, offset));
  for (const connection of orderedConnections) {
    const userId = String(connection.userId || "");
    if (!userId || (permitted.size && !permitted.has(userId.toLowerCase()))) continue;
    summary.users++;
    let userEnqueued = 0;
    let recoverySlot = 0;
    const batches = await deps.store.listBatches(userId, MAX_BATCHES_PER_USER, true);
    for (const batch of batches) {
      if (!ACTIVE_BATCHES.has(batch.status)) continue;
      summary.batches++;
      let batchMessages = null;
      const capacityRepaired = new Set();
      if (batch.capacityPlanHash && batch.capacityReservationId) {
        batchMessages = await deps.store.listMessages(userId, batch.id);
        const missing = batchMessages.filter((message) => !message.plannedSendUtc
          && ["editing", "scheduled_pending"].includes(message.state));
        let assignments = new Map();
        let incomplete = false;
        if (missing.length) {
          try {
            const plan = await deps.capacity.assertReservation(userId,
              batch.capacityReservationId, batch.capacityPlanHash);
            assignments = new Map((plan.assignments || [])
              .map((assignment) => [assignment.key, assignment]));
          } catch {
            // Without the frozen assignment, inventing a day or an instant
            // would spend unreserved capacity. Scheduled preflight still runs
            // and turns this into its normal user-visible hold.
            summary.failed++;
            incomplete = true;
          }
        }
        for (let index = 0; !incomplete && index < batchMessages.length; index++) {
          const message = batchMessages[index];
          if (message.plannedSendUtc || !["editing", "scheduled_pending"].includes(message.state)) continue;
          const assignment = assignments.get(message.id);
          if (!assignment) { incomplete = true; continue; }
          try {
            const repaired = await deps.store.patchMessage(userId, batch.id, message.id, {
              state: batch.status === "scheduled" ? "scheduled_pending" : "draft_pending",
              queuedUtc: message.queuedUtc || batch.approvedUtc,
              sendPosition: assignment.tranchePosition,
              capacityDay: assignment.day, capacityUnits: assignment.units,
              plannedSendUtc: assignment.plannedSendUtc,
              capacityPlanHash: batch.capacityPlanHash,
              trancheIndex: assignment.trancheIndex,
              tranchePosition: assignment.tranchePosition,
              ...(batch.status === "scheduled"
                ? { scheduleRevision: Number(batch.scheduleRevision) } : {}),
              leaseUntilUtc: "",
            }, message.etag);
            batchMessages[index] = repaired;
            capacityRepaired.add(message.id);
            summary.promoted++;
          } catch (err) {
            if ([404, 412].includes(Number(err && err.statusCode))) summary.conflicts++;
            else summary.failed++;
            incomplete = true;
          }
        }
      }
      if (batch.status === "scheduled") {
        const due = timestamp(batch.scheduledForUtc);
        const retryAfter = timestamp(batch.scheduleRetryAfterUtc);
        const leaseExpired = !timestamp(batch.scheduleLeaseUntilUtc) || timestamp(batch.scheduleLeaseUntilUtc) <= nowMs;
        if (batch.scheduleState === "checking" && leaseExpired) {
          try { await deps.store.patchBatch(userId, batch.id,
            { scheduleState: "pending", scheduleLeaseUntilUtc: "" }, batch.etag); }
          catch (err) { if (Number(err && err.statusCode) === 412) summary.conflicts++; else summary.failed++; }
        }
        if (due && due <= nowMs && leaseExpired && (!retryAfter || retryAfter <= nowMs)) {
          summary.eligible++; summary.attempted++;
          try {
            await deps.enqueue({ kind: "preflight", userId, batchId: batch.id,
              scheduleRevision: Number(batch.scheduleRevision) });
            summary.enqueued++; userEnqueued++;
          } catch { summary.failed++; }
        }
        if (userEnqueued >= MAX_ENQUEUED_PER_USER || summary.attempted >= MAX_ATTEMPTS) break;
        continue;
      }
      if (batch.status === "schedule_held") {
        if (batch.scheduleNotificationState === "pending"
            || (["creating", "draft_ready", "submitting", "submitted", "ambiguous"].includes(batch.scheduleNotificationState)
              && timestamp(batch.updatedUtc) <= nowMs - GRACE_MS)) {
          summary.eligible++; summary.attempted++;
          try {
            await deps.enqueue({ kind: "schedule_notify", userId, batchId: batch.id,
              scheduleRevision: Number(batch.scheduleRevision) });
            summary.enqueued++; userEnqueued++;
          } catch { summary.failed++; }
        }
        if (userEnqueued >= MAX_ENQUEUED_PER_USER || summary.attempted >= MAX_ATTEMPTS) break;
        continue;
      }
      for (let message of batchMessages || await deps.store.listMessages(userId, batch.id)) {
        summary.messages++;
        let promoted = capacityRepaired.has(message.id);
        if (batch.capacityPlanHash && !message.plannedSendUtc
            && ["editing", "scheduled_pending"].includes(message.state)) continue;
        // Approval marks the batch first, then each message. If the HTTP host
        // dies between those writes, an approved batch can retain editing rows
        // that no ordinary worker is allowed to claim. Promote them
        // conditionally; an approval worker that got there first wins the ETag.
        const approvedAt = timestamp(batch.approvedUtc);
        if (!promoted && (message.state === "editing" || (message.state === "scheduled_pending" && batch.scheduleState === "passed")) && approvedAt
            && approvedAt <= nowMs - GRACE_MS) {
          try {
            message = await deps.store.patchMessage(userId, batch.id, message.id, {
              state: "draft_pending", queuedUtc: message.queuedUtc || batch.approvedUtc,
              leaseUntilUtc: "",
            }, message.etag);
            summary.promoted++;
            promoted = true;
          } catch (err) {
            if ([404, 412].includes(Number(err && err.statusCode))) summary.conflicts++;
            else summary.failed++;
            continue;
          }
        }
        // The conditional patch above is the durable obligation. Publish its
        // queue hint in this pass; if publication fails, the normal grace window
        // prevents a tight retry loop and the next timer pass picks it up.
        const kind = workFor(promoted ? { ...message, updatedUtc: "" } : message,
          batch, nowMs, interval);
        if (!kind) continue;
        summary.eligible++;
        summary.attempted++;
        try {
          // Preserve the same per-mailbox pacing used by approval. Recovery can
          // find many overdue drafts at once; zero-delay fan-out would recreate
          // the Graph mailbox concurrency burst this queue was built to avoid.
          await deps.enqueue({ kind, userId, batchId: batch.id, messageId: message.id,
            ...(Number(message.scheduleRevision) > 0 ? { scheduleRevision: Number(message.scheduleRevision) } : {}) },
            recoverySlot * interval);
          summary.enqueued++;
          userEnqueued++;
          recoverySlot++;
        } catch {
          // A later timer pass retries. Do not log recipient/message details.
          summary.failed++;
        }
        if (userEnqueued >= MAX_ENQUEUED_PER_USER || summary.attempted >= MAX_ATTEMPTS) break;
      }
      if (userEnqueued >= MAX_ENQUEUED_PER_USER || summary.attempted >= MAX_ATTEMPTS) break;
    }
    if (summary.enqueued >= MAX_ENQUEUED || summary.attempted >= MAX_ATTEMPTS) break;
  }
  logger(context, `campaign email repair: users ${summary.users}, batches ${summary.batches}, `
    + `messages ${summary.messages}, promoted ${summary.promoted}, eligible ${summary.eligible}, `
    + `enqueued ${summary.enqueued}, conflicts ${summary.conflicts}, failed ${summary.failed}`);
  return summary;
}

module.exports = async function (context) { await run(context); };
module.exports.run = run;
module.exports.workFor = workFor;
