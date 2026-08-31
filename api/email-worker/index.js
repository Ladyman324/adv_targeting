"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const auth = require("../shared/email-auth");
const store = require("../shared/email-store");
const graph = require("../shared/graph-mail");
const service = require("../shared/email-service");
const core = require("../shared/email-core");
const mailboxGate = require("../shared/email-mailbox-gate");
const suppress = require("../shared/email-suppress");
const recipientRegistry = require("../shared/recipient-registry");
const materials = require("../shared/email-materials");
const schedule = require("../shared/email-schedule");
const capacity = require("../shared/email-limit-guard");

function releaseMetadata() {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "release.json"), "utf8"));
    return {
      id: /^[A-Za-z0-9._-]{1,128}$/.test(String(value.id || "")) ? String(value.id) : "development",
      commit: /^[0-9a-f]{7,40}$/i.test(String(value.commit || "")) ? String(value.commit).toLowerCase() : "",
    };
  } catch { return { id: "development", commit: "" }; }
}

const RELEASE = releaseMetadata();

function diagnosticId() {
  return `spf-${crypto.randomUUID()}`;
}

function sanitizedDiagnosticText(value, limit) {
  return String(value || "")
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[email]")
    .replace(/\b[0-9]{5,10}\b/g, "[id]")
    .replace(/\b(?:Bearer\s+)?eyJ[A-Za-z0-9._-]+/gi, "[token]")
    .replace(/([?&](?:token|sig|code)=)[^&\s]+/gi, "$1[redacted]")
    .slice(0, limit);
}

function reportPreflightFailure(work, err, failure, reference, deps) {
  const logger = deps.logger;
  if (!logger) return;
  const unexpected = failure.code === "unexpected_error" || failure.stage === "unknown";
  const event = unexpected ? "scheduled_preflight_exception" : "scheduled_preflight_rejected";
  const detail = {
    event, reference, batchId: String(work.batchId || ""), userId: String(work.userId || ""),
    scheduleRevision: Number(work.scheduleRevision) || 0,
    invocationId: String(deps.invocationId || ""), attempt: Number(deps.preflightAttempt) || 0,
    code: failure.code, stage: failure.stage, statusCode: failure.statusCode,
    errorType: sanitizedDiagnosticText(err && err.name || "Error", 120),
    releaseId: RELEASE.id, releaseCommit: RELEASE.commit,
  };
  if (unexpected) {
    detail.errorMessage = sanitizedDiagnosticText(err && err.message, 600);
    detail.stack = sanitizedDiagnosticText(err && err.stack, 4000);
  }
  const line = `${event} ${JSON.stringify(detail)}`;
  const method = unexpected ? "error" : "warn";
  // Diagnostics must never weaken the fail-safe. If the host logger itself is
  // unavailable, the durable hold and audit record still have to complete.
  try {
    if (typeof logger[method] === "function") logger[method](line);
    else if (typeof logger === "function") logger(line);
  } catch {}
}

function parseWork(value) {
  if (value && typeof value === "object") return value;
  try { return JSON.parse(String(value)); } catch {}
  try { return JSON.parse(Buffer.from(String(value), "base64").toString("utf8")); } catch {}
  throw new Error("Invalid email queue message.");
}

async function refreshBatch(userId, batchId, deps) {
  const batch = await deps.store.getBatch(userId, batchId);
  if (!batch) return;
  const messages = await deps.store.listMessages(userId, batchId);
  let status = batch.status;
  if (batch.status === "canceled" || batch.status === "paused") return;
  if (messages.some((m) => m.state === "auth_required")) status = "action_required";
  else if (batch.mode === "drafts" && messages.every((m) => ["draft_ready", "failed", "canceled"].includes(m.state)))
    status = messages.some((m) => m.state === "failed") ? "partial_failure" : "drafts_ready";
  else if (batch.mode === "send" && messages.every((m) => ["sent", "failed", "canceled"].includes(m.state)))
    status = messages.some((m) => m.state === "failed") ? "partial_failure" : "completed";
  else if (batch.mode === "send" && messages.some((m) => ["send_scheduled", "sending", "submitted", "sent", "send_ambiguous"].includes(m.state))) status = "sending";
  /* Campaign health, computed from the messages rather than incremented.
   *
   * A rep working a stale list can burn the firm's sending reputation before
   * anyone reads a report, and the damage lands on eicatlanta.com for ALL mail,
   * not just this batch. Counting here is both cheaper and safer than a counter:
   * refreshBatch already has every message in hand, and two workers finishing at
   * once cannot double-count.
   *
   * It PAUSES, never cancels. Pausing is reversible by someone who can look at
   * the bounces and judge; cancelling would destroy a half-sent campaign on an
   * automated percentage. Stopping to ask is the entire point.
   */
  const sentCount = messages.filter((m) => ["submitted", "sent"].includes(m.state)).length;
  const hardBounceCount = messages.filter((m) => m.bounceKind === "hard").length;
  const health = deps.core.campaignHealth(sentCount, hardBounceCount);
  const patch = { sentCount, hardBounceCount };
  if (health.pause && batch.mode === "send") {
    status = "paused";
    patch.warningLevel = "blocked";
    patch.warningMessage = health.reason;
    await deps.store.audit(userId, batchId, "batch_paused_bounce_rate",
      { rate: Number(health.rate.toFixed(2)), bounced: hardBounceCount, sent: sentCount });
  }
  if (status !== batch.status) patch.status = status;
  if (Object.keys(patch).length) await deps.store.patchBatch(userId, batchId, patch, batch.etag);
}

const RETRY_CEILING = 6;
function terminalFailureCode(err, phase, retryable) {
  if (phase === "send" && err.ambiguous && err.safeToRetry !== true)
    return "send_outcome_unknown";
  if (retryable) return `${phase}_retryable_exhausted`;
  return err.graphCode || err.code || `${phase}_permanent_failure`;
}


async function verifyIdentity(message, batch, deps, options = {}) {
  if (!message.contactId) {
    if (String(message.recipientEmail).toLowerCase() !== String(batch.graphMailbox).toLowerCase())
      throw service.httpError(409, "A non-advisor recipient is not the connected mailbox.",
        "recipient_not_approved");
    // A connected-mailbox self-test has no advisor registry row and therefore
    // no teammates. Keep the return shape identical to the advisor path.
    return { approved: null, teammates: [] };
  }
  const approved = await deps.recipientRegistry.verify(
    message.contactId, message.recipientEmail, options);
  if (!message.recipientRoutingHash
      || message.recipientRoutingHash !== approved.routingHash)
    throw service.httpError(409,
      "The advisor's approved recipient routing changed after approval.",
      "recipient_routing_changed");
  const requestedMates = (message.teammateCc || []).map((email, index) => ({
    crd: (message.teammateCcCrds || [])[index] || "", email,
  }));
  const teammates = await deps.recipientRegistry.verifyTeammates(
    message.contactId, requestedMates);
  return { approved, teammates };
}

function graphAddresses(remote, field) {
  return (remote[field] || []).map((entry) =>
    String((((entry || {}).emailAddress || {}).address) || "").trim().toLowerCase()).filter(Boolean).sort();
}

function sameAddresses(actual, expected) {
  return JSON.stringify(actual.slice().sort()) ===
    JSON.stringify((expected || []).map((x) => String(x).toLowerCase()).sort());
}

function assertRouting(remote, recipientEmail, copies) {
  if (!sameAddresses(graphAddresses(remote, "toRecipients"), [recipientEmail])
      || !sameAddresses(graphAddresses(remote, "ccRecipients"), copies.cc)
      || !sameAddresses(graphAddresses(remote, "bccRecipients"), copies.bcc))
    throw service.httpError(409,
      "The Outlook draft recipients do not match the approved routing.",
      "recipient_routing_changed");
}

async function routingView(found, token, deps) {
  if (found && ["toRecipients", "ccRecipients", "bccRecipients"]
      .some((field) => Array.isArray(found[field]))) return found;
  return (deps.graph.getDirectMessage || deps.graph.getMessage)(token, found.id);
}

async function failOrRetry(work, claimed, err, phase, deps) {
  if (["graph_not_connected", "graph_reconnect_required"].includes(err.code)) {
    await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "auth_required",
      failureCode: `auth_required_${phase}`, failureMessage: err.message, leaseUntilUtc: "" }, claimed.etag);
    await deps.store.audit(work.userId, work.batchId, "microsoft_reconnect_required", { messageId: work.messageId, phase });
    return;
  }
  const retryable = err.safeToRetry === true || err.statusCode === 429
    || err.ambiguous || (err.statusCode >= 500);
  // This phase's own budget. Sharing one counter meant several draft retries
  // could leave the first send attempt with no allowance at all -- the message
  // failed permanently on a transient error it had never actually hit while
  // sending.
  const tries = Number(claimed[`${phase}Attempts`]) || 0;
  if (retryable && tries < RETRY_CEILING) {
    const seconds = Math.max(Number(err.retryAfter) || 0, err.ambiguous ? 120 : Math.min(300, 2 ** tries * 5));
    await deps.store.patchMessage(work.userId, work.batchId, work.messageId, {
      state: `${phase}_ambiguous`, failureCode: err.graphCode || (err.statusCode === 429 ? "throttled" : "ambiguous"),
      failureMessage: err.message, graphRequestId: err.requestId || "", leaseUntilUtc: "",
      retryAfterUtc: new Date(Date.now() + seconds * 1000).toISOString(),
    }, claimed.etag);
    await deps.enqueue({ ...work, kind: phase === "draft" ? "draft" : "reconcile" }, seconds);
  } else {
    const terminalCode = terminalFailureCode(err, phase, retryable);
    await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "failed",
      failureCode: terminalCode, failureMessage: err.message,
      graphRequestId: err.requestId || "", leaseUntilUtc: "" }, claimed.etag);
  }
  await deps.store.audit(work.userId, work.batchId, `${phase}_failed`, { messageId: work.messageId,
    retryable, safeToRetry: retryable
      && terminalFailureCode(err, phase, retryable) !== "send_outcome_unknown",
    code: err.graphCode || "", requestId: err.requestId || "" });
}

function safeHold(err) {
  const original = String(err && (err.code || err.errorCode) || "");
  if (["graph_not_connected", "graph_reconnect_required", "graph_connection_changed",
    "interaction_required"].includes(original))
    return { code: "schedule_mailbox_changed", message: "The Microsoft 365 mailbox must be reconnected." };
  if (original === "recipient_registry_unavailable")
    return { code: "schedule_preflight_unavailable",
      message: "Recipient safety data could not be refreshed. Review and reschedule this batch." };
  if (["rolling_limit", "daily_limit", "capacity_plan_changed",
    "capacity_reservation_missing"].includes(original))
    return { code: "schedule_capacity_changed",
      message: "The current sending allowance cannot accommodate this batch." };
  const allowed = new Set(["schedule_template_changed", "schedule_mailbox_changed",
    "schedule_validation_changed", "schedule_recipient_suppressed", "schedule_sending_disabled",
    "schedule_allowlist_changed", "schedule_preflight_unavailable", "schedule_capacity_changed"]);
  const code = allowed.has(original) ? original : "schedule_preflight_failed";
  const messages = {
    schedule_template_changed: "The approved template changed.",
    schedule_mailbox_changed: "The Microsoft 365 mailbox must be reconnected.",
    schedule_validation_changed: "A recipient, attachment, or rendered message changed.",
    schedule_recipient_suppressed: "A recipient is now suppressed.",
    schedule_sending_disabled: "Direct sending is currently disabled.",
    schedule_allowlist_changed: "A recipient is outside the current sending allowlist.",
    schedule_preflight_unavailable: "Recipient safety data could not be refreshed. Review and reschedule this batch.",
    schedule_capacity_changed: "The current sending allowance cannot accommodate this batch.",
    schedule_preflight_failed: "The scheduled safety check could not be completed.",
  };
  return { code, message: messages[code] };
}

/* Calendar-capacity work carries an absolute, DST-safe send instant and an
 * Eastern date. Queue visibility is only a hint: repair jobs, retries and
 * duplicate deliveries can all wake work early or after its reserved day.
 * Check the durable assignment before touching Graph. */
async function waitForPlannedTime(work, batch, message, deps) {
  if (!message || !message.plannedSendUtc) return false;
  const due = Date.parse(message.plannedSendUtc);
  if (Number.isFinite(due) && due > Date.now()) {
    await deps.enqueue(work, Math.max(1, Math.ceil((due - Date.now()) / 1000)));
    return true;
  }
  if (message.capacityDay && deps.capacity.easternDay(Date.now()) !== message.capacityDay) {
    const claimed = await deps.store.claimMessage(work.userId, work.batchId, work.messageId,
      [message.state], message.state, 30);
    if (claimed) {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, {
        state: "failed", failureCode: "capacity_day_expired",
        failureMessage: "This email did not start on its reserved Eastern business day. Review it in a new batch before sending.",
        leaseUntilUtc: "",
      }, claimed.etag);
      await deps.store.audit(work.userId, work.batchId, "capacity_day_expired",
        { messageId: work.messageId, capacityDay: message.capacityDay });
      await refreshBatch(work.userId, work.batchId, deps);
    }
    return true;
  }
  if (batch.capacityPlanHash && message.capacityPlanHash
      && batch.capacityPlanHash !== message.capacityPlanHash) return true;
  return false;
}

const PREFLIGHT_ATTEMPTS = 3;
function preflightFailure(err) {
  const code = String(err && (err.code || err.errorCode || err.graphCode) || "unexpected_error").slice(0, 80);
  const stage = String(err && err.preflightStage || "unknown").slice(0, 80);
  const statusCode = Math.max(0, Number(err && err.statusCode) || 0);
  const permanent = new Set(["recipient_registry_release_invalid",
    "recipient_registry_release_mismatch", "recipient_registry_incompatible",
    "rolling_limit", "daily_limit", "capacity_plan_changed",
    "capacity_reservation_missing", "idempotency_conflict"]);
  const network = new Set(["ECONNRESET", "ETIMEDOUT", "EAI_AGAIN", "ENOTFOUND",
    "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT"]);
  const retryable = !permanent.has(code) && (err && err.safeToRetry === true
    || statusCode === 408 || statusCode === 429 || statusCode >= 500
    || code === "recipient_registry_unavailable" || code === "graph_connection_changed"
    || network.has(code));
  return { code, stage, statusCode, retryable };
}

async function preflight(work, deps) {
  let currentStage = "batch_read";
  let batch = await deps.store.getBatch(work.userId, work.batchId);
  if (!batch || batch.status !== "scheduled" || batch.scheduleState !== "pending"
      || !schedule.currentRevision(batch, work)) return;
  if (!schedule.due(batch)) {
    // Azure Queue caps initial visibility at seven elapsed days, while the UI
    // promises seven Eastern calendar dates. Around DST—or simply later in the
    // day—the capped hint can wake early. Keep the normal queue path durable
    // instead of depending on the five-minute repair timer to notice it later.
    const dueAt = Date.parse(batch.scheduledForUtc || "");
    if (Number.isFinite(dueAt))
      await deps.enqueue(work, Math.max(1, Math.ceil((dueAt - Date.now()) / 1000)));
    return;
  }
  const retryAfter = Date.parse(batch.scheduleRetryAfterUtc || "") || 0;
  if (retryAfter > Date.now()) {
    await deps.enqueue(work, Math.max(1, Math.ceil((retryAfter - Date.now()) / 1000)));
    return;
  }
  const attempt = (Number(batch.schedulePreflightAttempts) || 0) + 1;
  try {
    currentStage = "batch_claim";
    batch = await deps.store.patchBatch(work.userId, work.batchId, {
      scheduleState: "checking", scheduleLeaseUntilUtc: new Date(Date.now() + 300000).toISOString(),
      schedulePreflightAttempts: attempt, scheduleRetryAfterUtc: "",
    }, batch.etag);
  } catch (err) { if (Number(err && err.statusCode) === 412) return; throw err; }
  try {
    currentStage = "scheduled_validation";
    const checked = await deps.service.preflightScheduled(work.userId, work.batchId,
      work.scheduleRevision, deps);
    currentStage = "batch_refresh";
    const latest = await deps.store.getBatch(work.userId, work.batchId);
    if (!latest || latest.status !== "scheduled" || latest.scheduleState !== "checking"
        || !schedule.currentRevision(latest, work)) return;
    currentStage = "batch_transition";
    await deps.store.patchBatch(work.userId, work.batchId, { status: "drafting", scheduleState: "passed",
      schedulePassedUtc: new Date().toISOString(), scheduleLeaseUntilUtc: "",
      scheduleHoldCode: "", scheduleHoldMessage: "", scheduleRetryAfterUtc: "",
      scheduleLastErrorCode: "", scheduleLastErrorStage: "", scheduleLastErrorId: "" }, latest.etag);
    currentStage = "queue_configuration";
    const interval = deps.core.config().mailboxIntervalSeconds;
    for (const message of checked.messages) {
      currentStage = "message_refresh";
      const current = await deps.store.getMessage(work.userId, work.batchId, message.id);
      if (!current || current.state !== "scheduled_pending"
          || Number(current.scheduleRevision) !== Number(work.scheduleRevision)) continue;
      currentStage = "message_transition";
      await deps.store.patchMessage(work.userId, work.batchId, message.id, {
        state: "draft_pending", leaseUntilUtc: "", queuedUtc: new Date().toISOString(),
      }, current.etag);
      currentStage = "message_enqueue";
      await deps.enqueue({ kind: "draft", userId: work.userId, batchId: work.batchId,
        messageId: message.id, scheduleRevision: Number(work.scheduleRevision) },
      message.plannedSendUtc
        ? Math.max(0, Math.ceil((Date.parse(message.plannedSendUtc) - Date.now()) / 1000))
        : Math.max(0, Number(message.sendPosition) || 0) * interval);
    }
    currentStage = "pass_audit";
    await deps.store.audit(work.userId, work.batchId, "scheduled_preflight_passed",
      { scheduleRevision: Number(work.scheduleRevision), recipientCount: checked.messages.length });
  } catch (err) {
    if (!err.preflightStage) err.preflightStage = currentStage;
    const failure = preflightFailure(err);
    const reference = diagnosticId();
    reportPreflightFailure(work, err, failure, reference,
      { ...deps, preflightAttempt: attempt });
    const latest = await deps.store.getBatch(work.userId, work.batchId);
    if (!latest || latest.status !== "scheduled" || latest.scheduleState !== "checking"
        || !schedule.currentRevision(latest, work)) return;
    if (failure.retryable && attempt < PREFLIGHT_ATTEMPTS) {
      const delay = attempt === 1 ? 15 : 45;
      const retryAfterUtc = new Date(Date.now() + delay * 1000).toISOString();
      await deps.store.patchBatch(work.userId, work.batchId, {
        scheduleState: "pending", scheduleLeaseUntilUtc: "", scheduleRetryAfterUtc: retryAfterUtc,
        scheduleLastErrorCode: failure.code, scheduleLastErrorStage: failure.stage,
        scheduleLastErrorId: reference,
      }, latest.etag);
      await deps.store.audit(work.userId, work.batchId, "scheduled_preflight_retry", {
        scheduleRevision: Number(work.scheduleRevision), attempt,
        code: failure.code, stage: failure.stage, statusCode: failure.statusCode,
        reference,
        retryAfterUtc,
      });
      await deps.enqueue({ kind: "preflight", userId: work.userId, batchId: work.batchId,
        scheduleRevision: Number(work.scheduleRevision) }, delay);
      return;
    }
    const hold = safeHold(err);
    const notificationId = `schedule-hold-${latest.id}-r${latest.scheduleRevision}`;
    await deps.store.patchBatch(work.userId, work.batchId, { status: "schedule_held",
      scheduleState: "held", scheduleLeaseUntilUtc: "", scheduleHeldUtc: new Date().toISOString(),
      scheduleHoldCode: hold.code, scheduleHoldMessage: hold.message,
      scheduleRetryAfterUtc: "", scheduleLastErrorCode: failure.code,
      scheduleLastErrorStage: failure.stage, scheduleLastErrorId: reference,
      scheduleNotificationState: latest.scheduleNotificationSentUtc ? "sent" : "pending",
      scheduleNotificationId: notificationId,
      warningLevel: "blocked", warningMessage: hold.message }, latest.etag);
    await deps.store.audit(work.userId, work.batchId, "scheduled_preflight_held",
      { scheduleRevision: Number(work.scheduleRevision), reason: hold.code, attempt,
        originalCode: failure.code, stage: failure.stage, statusCode: failure.statusCode,
        reference });
    await deps.enqueue({ kind: "schedule_notify", userId: work.userId, batchId: work.batchId,
      scheduleRevision: Number(work.scheduleRevision) });
  }
}

function scheduleLink(batchId) {
  try {
    const url = new URL(String(process.env.EMAIL_PUBLIC_BASE_URL || ""));
    if (url.protocol !== "https:") return "";
    // This anonymous relay preserves the held-batch destination through a
    // fresh Static Web Apps login. Linking straight to the protected root made
    // the platform's generic 401 override discard the query string.
    url.pathname = "/review.html";
    url.search = `?emailBatch=${encodeURIComponent(batchId)}`; url.hash = "";
    return url.toString();
  } catch { return ""; }
}

async function terminalizeNotification(work, batch, deps, reason) {
  const latest = await deps.store.getBatch(work.userId, work.batchId);
  if (!latest || !schedule.currentRevision(latest, work)
      || ["sent", "outcome_unknown", "failed"].includes(latest.scheduleNotificationState)) return;
  await deps.store.patchBatch(work.userId, work.batchId, {
    scheduleNotificationState: "outcome_unknown",
    scheduleNotificationCompletedUtc: new Date().toISOString(),
  }, latest.etag);
  await deps.store.audit(work.userId, work.batchId, "schedule_notification_outcome_unknown",
    { scheduleRevision: Number(work.scheduleRevision), reason: String(reason || "reconcile_horizon_expired") });
}
async function notifyScheduleHold(work, deps) {
  let batch = await deps.store.getBatch(work.userId, work.batchId);
  if (!batch || batch.status !== "schedule_held"
      || ["sent", "outcome_unknown", "failed"].includes(batch.scheduleNotificationState)
      || !schedule.currentRevision(batch, work)) return;
  try {
    const connection = await deps.auth.status(work.userId);
    const token = await deps.auth.tokenFor(work.userId);
    const owner = String(connection.profile && (connection.profile.mail || connection.profile.userPrincipalName)
      || connection.mailbox || "").toLowerCase();
    if (!connection.connected || !owner
        || owner !== String(batch.senderMail || batch.graphMailbox || "").toLowerCase()
        || String(token.mailboxId || "").toLowerCase() !== String(batch.graphMailboxId || "").toLowerCase()) return;
    const notificationId = batch.scheduleNotificationId
      || `schedule-hold-${batch.id}-r${batch.scheduleRevision}`;
    let mayCreate = false;
    if (batch.scheduleNotificationState === "pending") {
      try {
        batch = await deps.store.patchBatch(work.userId, work.batchId, {
          scheduleNotificationState: "creating", scheduleNotificationPhase: "create",
          scheduleNotificationId: notificationId,
          scheduleNotificationReconcileUntilUtc: new Date(Date.now() + 86400000).toISOString(),
        }, batch.etag);
        mayCreate = true;
      } catch (err) { if (Number(err && err.statusCode) === 412) return; throw err; }
    }
    let remote = batch.scheduleNotificationGraphId
      ? await deps.graph.getMessage(token.accessToken, batch.scheduleNotificationGraphId).catch((err) => {
        if (Number(err && err.statusCode) === 404) return null; throw err;
      }) : await deps.graph.findByAppId(token.accessToken, notificationId);
    if (!remote && mayCreate) {
      const link = scheduleLink(batch.id);
      remote = await deps.graph.createDraft(token.accessToken, { id: notificationId,
        subject: "Scheduled email batch needs review", recipientEmail: owner,
        recipientName: batch.userName || "", signatureHtml: "",
        bodyHtml: `<p>Your scheduled email batch is on hold. No advisor emails were started.</p>`
          + `<p>${batch.scheduleHoldMessage || "Open the application to review it."}</p>`
          + (batch.scheduleLastErrorId
            ? `<p>Support reference: <code>${batch.scheduleLastErrorId}</code></p>` : "")
          + (link ? `<p><a href="${link}">Review the held batch</a></p>` : ""),
      });
      const latest = await deps.store.getBatch(work.userId, work.batchId);
      if (!latest || !schedule.currentRevision(latest, work)) return;
      batch = await deps.store.patchBatch(work.userId, work.batchId, {
        scheduleNotificationState: "draft_ready", scheduleNotificationPhase: "create",
        scheduleNotificationGraphId: remote.id, scheduleNotificationCreatedUtc: new Date().toISOString(),
      }, latest.etag);
    } else if (!remote) {
      const latest = await deps.store.getBatch(work.userId, work.batchId);
      if (!latest || !schedule.currentRevision(latest, work)) return;
      const horizon = Date.parse(latest.scheduleNotificationReconcileUntilUtc || "");
      if (Number.isFinite(horizon) && horizon <= Date.now()) {
        await terminalizeNotification(work, latest, deps, "create_reconcile_horizon_expired");
        return;
      }
      await deps.store.patchBatch(work.userId, work.batchId, {
        scheduleNotificationState: "ambiguous",
      }, latest.etag);
      await deps.enqueue({ ...work, kind: "schedule_notify" }, 30);
      return;
    }
    if (remote.isDraft === false) {
      const latest = await deps.store.getBatch(work.userId, work.batchId);
      if (latest && schedule.currentRevision(latest, work)) await deps.store.patchBatch(work.userId, work.batchId, {
        scheduleNotificationState: "sent", scheduleNotificationGraphId: remote.id,
        scheduleNotificationSentUtc: remote.sentDateTime || new Date().toISOString(),
      }, latest.etag);
      return;
    }
    const latest = await deps.store.getBatch(work.userId, work.batchId);
    if (!latest || !schedule.currentRevision(latest, work)) return;
    if (latest.scheduleNotificationPhase === "submit"
        && ["submitting", "submitted", "ambiguous"].includes(latest.scheduleNotificationState)) {
      const horizon = Date.parse(latest.scheduleNotificationReconcileUntilUtc || "");
      if (Number.isFinite(horizon) && horizon > Date.now())
        await deps.enqueue({ ...work, kind: "schedule_notify" }, 30);
      else await terminalizeNotification(work, latest, deps, "submit_reconcile_horizon_expired");
      return;
    }
    batch = await deps.store.patchBatch(work.userId, work.batchId, {
      scheduleNotificationState: "submitting", scheduleNotificationPhase: "submit",
      scheduleNotificationGraphId: remote.id,
    }, latest.etag);
    try {
      await deps.graph.sendDraft(token.accessToken, remote.id);
      const after = await deps.store.getBatch(work.userId, work.batchId);
      if (after && schedule.currentRevision(after, work)) await deps.store.patchBatch(work.userId, work.batchId, {
        scheduleNotificationState: "submitted", scheduleNotificationSubmittedUtc: new Date().toISOString(),
      }, after.etag);
    } catch {
      const after = await deps.store.getBatch(work.userId, work.batchId);
      if (after && schedule.currentRevision(after, work)) await deps.store.patchBatch(work.userId, work.batchId,
        { scheduleNotificationState: "ambiguous" }, after.etag).catch(() => {});
    }
    await deps.enqueue({ ...work, kind: "schedule_notify" }, 30);
  } catch { /* the durable in-app hold is the primary notification */ }
}
async function draft(work, deps) {
  const batch = await deps.store.getBatch(work.userId, work.batchId);
  if (!batch || ["canceled"].includes(batch.status)) return;
  if (batch.status === "paused") { await deps.enqueue(work, 30); return; }
  if (Number(work.scheduleRevision) > 0 && (!schedule.currentRevision(batch, work) || batch.scheduleState !== "passed")) return;
  const pending = await deps.store.getMessage(work.userId, work.batchId, work.messageId);
  if (await waitForPlannedTime(work, batch, pending, deps)) return;
  const claimed = await deps.store.claimMessage(work.userId, work.batchId, work.messageId,
    ["draft_pending", "draft_ambiguous", "draft_creating"], "draft_creating", 300, "draft");
  if (!claimed) return;
  try {
    const token = await deps.auth.tokenFor(work.userId);
    if (String(token.mailboxId).toLowerCase() !== String(batch.graphMailboxId).toLowerCase())
      throw service.httpError(403, "Mailbox identity changed after batch creation; refusing to create a draft.");
    await verifyIdentity(claimed, batch, deps, { force: true });
    const attachmentIds = (claimed.attachments || []).map((doc) => doc.id);
    const currentAttachments = attachmentIds.length ? await deps.store.getDocuments(attachmentIds) : [];
    const currentById = new Map(currentAttachments.map((doc) => [doc.id, doc]));
    for (const frozen of claimed.attachments || []) {
      const current = currentById.get(frozen.id);
      if (!current || !materials.currentDocument(current) || current.version !== frozen.version
          || current.sha256 !== frozen.sha256)
        throw service.httpError(409,
          `${frozen.name || "An attachment"} is no longer the currently approved version.`,
          "attachment_unavailable");
    }
    let found = claimed.graphMessageId ? await deps.graph.getMessage(token.accessToken, claimed.graphMessageId).catch((e) => {
      if (e.statusCode === 404) return null; throw e;
    }) : await deps.graph.findByAppId(token.accessToken, claimed.id);
    /* The compliance blind copy is RECOMPUTED here rather than trusted from the
     * stored row: it is an obligation, and the authoritative moment for it is
     * the one where the draft is actually created.
     *
     * The rep's own copy preferences come the other way -- off the BATCH, as
     * they were when it was built. A rep who changes the setting mid-send
     * should not end up with half a batch copied and half not.
     */
    const copies = core.extraRecipients(claimed,
      { copySelf: batch.copySelf, copyInternal: batch.copyInternal,
        copyInternalTo: batch.copyInternalTo, ccColleague: batch.ccColleague },
      { mail: batch.senderMail });
    if (!found && claimed.followUpOfGraphId) {
      /* A FOLLOW-UP REPLIES TO OUR OWN SENT MESSAGE, and that needs care.
       *
       * createReply builds a draft Exchange knows is part of the conversation:
       * same conversationId, quoted original, correct References. The advisor's
       * client threads it under the mail they already have, which is the whole
       * point -- a fabricated "RE:" would start a new conversation and degrade
       * every later reply match to references or sender-only.
       *
       * THE TRAP: createReply addresses the draft to the SENDER of the original.
       * The original here is ours, so a naive reply goes to the rep. It looks
       * entirely correct in testing -- mail arrives, it is threaded, it is from
       * the right person -- and reaches nobody. So the recipients are set
       * explicitly afterwards, which also drops the reply-all fan-out Graph
       * would otherwise inherit from the original's Cc list.
       */
      const draft = await deps.graph.createReply(token.accessToken, claimed.followUpOfGraphId, false);
      await deps.graph.patchDraftRecipients(token.accessToken, draft.id, {
        toRecipients: [{ emailAddress: { address: claimed.recipientEmail } }],
        ccRecipients: copies.cc.map((a) => ({ emailAddress: { address: a } })),
        bccRecipients: copies.bcc.map((a) => ({ emailAddress: { address: a } })),
        subject: claimed.subject,
      });
      // Prepended, so the rep's line sits above the quoted original rather than
      // replacing it -- see updateDraftBody().
      await deps.graph.updateDraftBody(token.accessToken, draft.id,
        claimed.bodyHtml + (claimed.signatureHtml || ""));
      found = await deps.graph.getMessage(token.accessToken, draft.id);
    }
    if (!found) found = await deps.graph.createDraft(token.accessToken,
      { ...claimed, ...copies });
    const routed = await routingView(found, token.accessToken, deps);
    assertRouting(routed, claimed.recipientEmail, copies);
    await deps.store.patchMessage(work.userId, work.batchId, work.messageId, {
      graphMessageId: found.id, graphInternetMessageId: found.internetMessageId || "",
      // Captured at draft time because it is the only moment we are certain to
      // hold it. It is what a later reply is matched back to; a message stored
      // without one can never be tied to its answer.
      graphConversationId: found.conversationId || "",
      graphRequestId: found.requestId || "", draftCreatedUtc: claimed.draftCreatedUtc || new Date().toISOString(),
      leaseUntilUtc: "",
    }, (await deps.store.getMessage(work.userId, work.batchId, work.messageId)).etag);

    await deps.graph.attachDocuments(token.accessToken, found.id, claimed.attachments);
    // Inline charts go on after the documents and before any send. The body
    // already carries <img src="cid:...">, so a draft sent without this step
    // would reach the advisor with a broken image where the chart should be.
    if ((claimed.inlineImages || []).length) {
      await deps.graph.attachInlineImages(token.accessToken, found.id, claimed.inlineImages,
        (image) => deps.store.templateImageBytes(image));
    }
    const latest = await deps.store.getMessage(work.userId, work.batchId, work.messageId);
    const latestBatch = await deps.store.getBatch(work.userId, work.batchId);
    if (latestBatch.status === "canceled") {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId,
        { state: "canceled", leaseUntilUtc: "" }, latest.etag);
    } else if (batch.mode === "send") {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "send_scheduled", leaseUntilUtc: "" }, latest.etag);
      // sendPosition, not ordinal: ordinal is list order, and lists arrive grouped
      // by firm, so pacing on it sent 130 consecutive messages to one wirehouse.
      // Falls back to ordinal for batches approved before positions existed.
      const due = Date.parse(schedule.messageDueUtc(batch, claimed, deps.core.config()));
      await deps.enqueue({ kind: "send", userId: work.userId, batchId: work.batchId, messageId: work.messageId,
        ...(Number(work.scheduleRevision) > 0 ? { scheduleRevision: Number(work.scheduleRevision) } : {}) },
        Math.max(0, Math.ceil((due - Date.now()) / 1000)));
    } else await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "draft_ready", leaseUntilUtc: "" }, latest.etag);
    await deps.store.audit(work.userId, work.batchId, "draft_ready", { messageId: work.messageId, graphMessageId: found.id });
  } catch (err) { await failOrRetry(work, await deps.store.getMessage(work.userId, work.batchId, work.messageId), err, "draft", deps); }
  await refreshBatch(work.userId, work.batchId, deps);
}

async function send(work, deps) {
  const batch = await deps.store.getBatch(work.userId, work.batchId);
  if (!batch || batch.status === "paused") { if (batch) await deps.enqueue(work, 30); return; }
  if (batch.status === "canceled") return;
  if (Number(work.scheduleRevision) > 0 && (!schedule.currentRevision(batch, work) || batch.scheduleState !== "passed")) return;
  const pending = await deps.store.getMessage(work.userId, work.batchId, work.messageId);
  if (await waitForPlannedTime(work, batch, pending, deps)) return;
  const due = Date.parse(schedule.messageDueUtc(batch, pending, deps.core.config()));
  if (due > Date.now()) { await deps.enqueue(work, Math.ceil((due - Date.now()) / 1000)); return; }
  const claimed = await deps.store.claimMessage(work.userId, work.batchId, work.messageId,
    ["send_scheduled", "send_ambiguous", "sending"], "sending", 180, "send");
  if (!claimed) return;
  try {
    const token = await deps.auth.tokenFor(work.userId);
    if (String(token.mailboxId).toLowerCase() !== String(batch.graphMailboxId).toLowerCase())
      throw service.httpError(403, "Mailbox identity changed after approval; refusing to send.");
    let remote = claimed.graphMessageId ? await (deps.graph.getDirectMessage || deps.graph.getMessage)(
      token.accessToken, claimed.graphMessageId).catch((e) => {
      if (e.statusCode === 404) return null; throw e;
    }) : null;
    if (!remote) {
      const found = await deps.graph.findByAppId(token.accessToken, claimed.id);
      if (found) remote = await (deps.graph.getDirectMessage || deps.graph.getMessage)(
        token.accessToken, found.id);
    }
    if (!remote) throw new graph.GraphError("The known Outlook draft could not be reconciled.",
      { ambiguous: true, safeToRetry: true });
    if (!remote.isDraft) {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "sent",
        submittedUtc: claimed.submittedUtc || remote.sentDateTime || new Date().toISOString(), leaseUntilUtc: "",
        // Free backfill: `remote` was just fetched, and a message drafted before
        // conversationId was captured would otherwise stay unmatchable forever.
        // Only fills a blank -- never overwrites what the draft already stored.
        ...(claimed.graphConversationId ? {} : { graphConversationId: remote.conversationId || "" }),
        failureCode: "", failureMessage: "" }, claimed.etag);
    } else {
      const delay = await deps.mailboxGate.acquire(work.userId, deps.core.config().mailboxIntervalSeconds);
      if (delay) {
        await deps.store.patchMessage(work.userId, work.batchId, work.messageId,
          { state: "send_scheduled", leaseUntilUtc: "" }, claimed.etag);
        await deps.enqueue({ ...work, kind: "send" }, delay);
        return;
      }
      const latestBatch = await deps.store.getBatch(work.userId, work.batchId);
      if (latestBatch.status === "canceled") {
        await deps.store.patchMessage(work.userId, work.batchId, work.messageId,
          { state: "canceled", leaseUntilUtc: "" }, claimed.etag);
        return;
      }
      if (latestBatch.status === "paused") {
        await deps.store.patchMessage(work.userId, work.batchId, work.messageId,
          { state: "send_scheduled", leaseUntilUtc: "" }, claimed.etag);
        await deps.enqueue({ ...work, kind: "send" }, 30);
        return;
      }
      const identity = await verifyIdentity(claimed, latestBatch, deps, { force: true });
      const copies = deps.core.extraRecipients(claimed,
        { copySelf: latestBatch.copySelf, copyInternal: latestBatch.copyInternal,
          copyInternalTo: latestBatch.copyInternalTo, ccColleague: latestBatch.ccColleague },
        { mail: latestBatch.senderMail });
      assertRouting(remote, claimed.recipientEmail, copies);

      /* THE LAST MOMENT ANYTHING CAN BE STOPPED.
       *
       * Approval is not that moment, which is what this block exists to correct.
       * Messages are paced apart by mailboxIntervalSeconds, so at the default of
       * five seconds a 250-recipient batch is still sending some twenty minutes
       * after it was approved. Everything checked at approval -- the suppression
       * list and the administrator's kill switch -- was therefore checked against
       * a world that no longer exists by the time most of the batch goes out.
       *
       * Someone who unsubscribes in minute three was still being mailed in minute
       * twelve, and an emergency switch that only blocks new approvals is not an
       * emergency switch.
       *
       * Both checks fail CLOSED: an error here aborts the send rather than
       * proceeding on the assumption that nothing changed.
       */
      const identityRecipients = [
        { email: claimed.recipientEmail, contactId: claimed.contactId },
        ...identity.teammates.map((mate) => ({ email: mate.email, contactId: mate.crd })),
      ];
      const [policy, blocked] = await Promise.all([
        deps.store.policy(),
        deps.suppress.blockedAmong(identityRecipients),
      ]);

      if (blocked.size) {
        // Final for this message. The recipient asked not to be emailed; there is
        // no state in which retrying that is correct.
        const blockedAddress = identityRecipients.find((recipient) =>
          blocked.has(String(recipient.email || "").toLowerCase()));
        const address = (blockedAddress && blockedAddress.email) || claimed.recipientEmail;
        const why = blocked.get(String(address || "").toLowerCase()) || "opted out";
        await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "canceled",
          failureCode: "recipient_opted_out", leaseUntilUtc: "",
          failureMessage: `Not sent: ${address} ${why}. `
            + `This was recorded after the batch was approved.` }, claimed.etag);
        await deps.store.audit(work.userId, work.batchId, "send_blocked_recipient_opted_out",
          { messageId: work.messageId, reason: why });
        return;
      }

      if (policy.killed) {
        // Pauses the whole batch rather than failing this one message, so it
        // reuses the resume path a rep already understands: the administrator
        // clears the switch, the rep presses Resume. Nothing sends meanwhile.
        await deps.store.patchMessage(work.userId, work.batchId, work.messageId,
          { state: "send_scheduled", leaseUntilUtc: "" }, claimed.etag);
        if (latestBatch.status !== "paused") {
          await deps.store.patchBatch(work.userId, work.batchId, { status: "paused",
            warningLevel: "blocked",
            warningMessage: policy.reason
              || "Sending was stopped by the administrator kill switch." }, latestBatch.etag);
          await deps.store.audit(work.userId, work.batchId, "send_halted_by_kill_switch",
            { messageId: work.messageId, reason: policy.reason || "" });
        }
        await deps.enqueue({ ...work, kind: "send" }, 60);
        return;
      }

      const result = await deps.graph.sendDraft(token.accessToken, remote.id);
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "submitted",
        sendStartedUtc: claimed.sendStartedUtc || new Date().toISOString(), submittedUtc: new Date().toISOString(),
        graphRequestId: result.requestId || "", leaseUntilUtc: "", failureCode: "", failureMessage: "" }, claimed.etag);
      await deps.enqueue({ ...work, kind: "reconcile" }, 30);
    }
  } catch (err) { await failOrRetry(work, await deps.store.getMessage(work.userId, work.batchId, work.messageId), err, "send", deps); }
  await refreshBatch(work.userId, work.batchId, deps);
}

async function reconcile(work, deps) {
  const initial = await deps.store.getMessage(work.userId, work.batchId, work.messageId);
  if (!initial || !["submitted", "send_ambiguous"].includes(initial.state)) return;
  const message = await deps.store.claimMessage(work.userId, work.batchId, work.messageId,
    ["submitted", "send_ambiguous"], initial.state, 90, "reconcile");
  if (!message) return;
  const batch = await deps.store.getBatch(work.userId, work.batchId);
  if (!batch) return;
  try {
    const token = await deps.auth.tokenFor(work.userId);
    let remote = message.graphMessageId ? await deps.graph.getMessage(token.accessToken, message.graphMessageId).catch((e) => {
      if (e.statusCode === 404) return null; throw e;
    }) : null;
    if (!remote) remote = await deps.graph.findByAppId(token.accessToken, message.id);
    if (remote && !remote.isDraft) {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "sent",
        submittedUtc: message.submittedUtc || remote.sentDateTime || new Date().toISOString(),
        leaseUntilUtc: "", failureCode: "", failureMessage: "" }, message.etag);
      await deps.store.audit(work.userId, work.batchId, "send_reconciled", { messageId: message.id,
        status: "no_known_failure" });
    } else if (message.reconcileAttempts < RETRY_CEILING) {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { leaseUntilUtc: "" }, message.etag);
      await deps.enqueue(work, 60);
    } else {
      const unknown = message.state === "send_ambiguous";
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "failed",
        leaseUntilUtc: "", failureCode: unknown ? "send_outcome_unknown" : "sent_item_not_confirmed",
        failureMessage: unknown
          ? "Microsoft Graph did not provide a definitive send outcome. The message was not submitted again."
          : "Graph accepted the send, but the Sent Items copy could not be confirmed. Do not retry automatically." },
        message.etag);
    }
  } catch (err) {
    if (message.reconcileAttempts < RETRY_CEILING) {
      const latest = await deps.store.getMessage(work.userId, work.batchId, work.messageId);
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { leaseUntilUtc: "",
        failureCode: "reconciliation_pending", failureMessage: err.message || "Sent Items reconciliation is pending." }, latest.etag);
      await deps.enqueue(work, Math.max(err.retryAfter || 0, 60));
    } else await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "failed",
      leaseUntilUtc: "", failureCode: "reconciliation_failed", failureMessage: "Could not reconcile the submitted message; it was not resubmitted." }, message.etag);
  }
  await refreshBatch(work.userId, work.batchId, deps);
}

async function processWork(raw, overrides = {}) {
  const work = parseWork(raw);
  const deps = { auth, store, graph, enqueue: service.enqueue, core, mailboxGate, suppress, capacity,
    recipientRegistry, service, ...overrides };
  if (!work.userId || !work.batchId || (!work.messageId && !["preflight", "schedule_notify"].includes(work.kind)))
    throw new Error("Incomplete email queue message.");
  if (work.kind === "preflight") return preflight(work, deps);
  if (work.kind === "schedule_notify") return notifyScheduleHold(work, deps);
  if (work.kind === "draft") return draft(work, deps);
  if (work.kind === "send") return send(work, deps);
  if (work.kind === "reconcile") return reconcile(work, deps);
  throw new Error(`Unknown email work kind ${work.kind}.`);
}

module.exports = async function (context, workItem) {
  await processWork(workItem, {
    logger: context && context.log,
    invocationId: context && (context.invocationId
      || context.executionContext && context.executionContext.invocationId),
  });
};
module.exports.processWork = processWork;
module.exports.parseWork = parseWork;
module.exports.refreshBatch = refreshBatch;
module.exports.preflight = preflight;
module.exports.notifyScheduleHold = notifyScheduleHold;
module.exports.preflightFailure = preflightFailure;
module.exports.scheduleLink = scheduleLink;
