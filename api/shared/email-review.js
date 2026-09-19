"use strict";

// Checking Outlook is deliberately separate from authorizing another send.
const store = require("./email-store");
const auth = require("./email-auth");
const graph = require("./graph-mail");
const suppress = require("./email-suppress");
const capacity = require("./email-limit-guard");
const CHECK_MAX_AGE = 10 * 60 * 1000;
const ACTIVE = new Set(["draft_creating", "sending", "submitted", "send_ambiguous"]);
const fail = (message, code = "review_required", statusCode = 409) =>
  Object.assign(new Error(message), { code, statusCode });
const leased = (m, now) => Date.parse(m.leaseUntilUtc || "") > now;

function eligibility(batch, m, now = Date.now()) {
  if (m.handledManuallyUtc) return { reason: "Handled manually; excluded from sending." };
  if (m.bounceKind || m.bounceAtUtc) return { reason: "Bounced; not eligible for retry." };
  if (["sent", "submitted"].includes(m.state)) return { reason: "Already submitted or sent." };
  if (["started", "accepted"].includes(m.sendOutcome)
      || ["send_outcome_unknown", "reconciliation_failed", "reconciliation_pending"].includes(m.failureCode)
      || m.state === "send_ambiguous") return { reason: "Send outcome uncertain. Check status; do not resend." };
  if (leased(m, now) || ACTIVE.has(m.state)) return { reason: "A worker is processing this message." };
  if (!batch.approvedUtc || !["send", "drafts"].includes(batch.mode)) return { reason: "Not an approved batch." };
  if (!["partial_failure", "action_required", "completed"].includes(batch.status))
    return { reason: "Retry is available after active work finishes. Paused work must be reviewed separately." };
  let phase = "";
  if (m.state === "failed") {
    if (["draft_retryable_exhausted", "draft_permanent_failure"].includes(m.failureCode)
        && !m.sendAttempts && !m.sendStartedUtc) phase = "draft";
    if (m.failureCode === "send_retryable_exhausted") phase = "send";
  }
  if (m.state === "auth_required") {
    if (m.failureCode === "auth_required_draft") phase = "draft";
    if (m.failureCode === "auth_required_send") phase = "send";
  }
  if (!phase) return { reason: "This state is not a safely retryable failure." };
  // An old allocation must never be silently spent on a different business day.
  if (m.capacityDay && m.capacityDay !== capacity.easternDay(now))
    return { reason: "Reserved sending day has passed or is still ahead. Review in a new batch with a new delivery plan." };
  const checked = Date.parse(m.outlookCheckedUtc || "");
  if (!(checked <= now && checked >= now - CHECK_MAX_AGE)) return { reason: "Check Outlook status first (valid for 10 minutes)." };
  if (m.outlookCheckStatus !== "draft") return { reason: "No confirmed original Outlook draft. Missing is not proof of unsent." };
  return { phase, reason: phase === "draft" ? "Original draft found; retry preparation." : "Original draft found; safe send retry." };
}

function dependencies(overrides) { return { store, auth, graph, suppress, now: () => Date.now(), ...overrides }; }
async function records(who, input, d) {
  const batch = await d.store.getBatch(who.id, input.batchId);
  if (!batch) throw fail("Email batch not found.", "not_found", 404);
  const message = await d.store.getMessage(who.id, batch.id, input.messageId);
  if (!message) throw fail("Email message not found.", "not_found", 404);
  return { batch, message };
}

async function check(who, input, overrides = {}) {
  const d = dependencies(overrides), { batch, message: m } = await records(who, input, d);
  if (leased(m, d.now()) || ACTIVE.has(m.state))
    throw fail("This message is being processed. Wait for the worker to finish before checking it.");
  const token = await d.auth.tokenFor(who.id);
  if (String(token.mailboxId).toLowerCase() !== String(batch.graphMailboxId).toLowerCase())
    throw fail("The connected mailbox differs from the approved mailbox.");
  let remote, status;
  try {
    const known = m.graphMessageId ? await d.graph.getMessage(token.accessToken, m.graphMessageId)
      .catch(e => { if (Number(e.statusCode) === 404) return null; throw e; }) : null;
    // Search even when a draft exists: legacy work may have both a draft and a
    // sent message carrying this id. Positive evidence of sending wins.
    const found = await d.graph.findByAppId(token.accessToken, m.id);
    remote = [known, found].find(x => x && x.isDraft === false && x.sentDateTime) || known || found;
    status = !remote ? "not_found" : remote.isDraft === false && remote.sentDateTime
      ? "sent" : remote.isDraft === true ? "draft" : "inconclusive";
  } catch (error) {
    await d.store.patchMessage(who.id, batch.id, m.id,
      { outlookCheckStatus: "unavailable", outlookCheckedUtc: new Date(d.now()).toISOString() }, m.etag);
    throw fail("Outlook could not be checked. Nothing was sent. Try again later or reconnect Microsoft 365.", "outlook_check_unavailable");
  }
  const patch = { outlookCheckStatus: status, outlookCheckedUtc: new Date(d.now()).toISOString() };
  if (status === "sent" && !m.handledManuallyUtc) Object.assign(patch, {
    state: "sent", graphMessageId: remote.id, graphInternetMessageId: remote.internetMessageId || m.graphInternetMessageId || "",
    graphConversationId: remote.conversationId || m.graphConversationId || "",
    submittedUtc: remote.sentDateTime, failureCode: "", failureMessage: "", leaseUntilUtc: "",
  });
  if (status === "draft") patch.graphMessageId = remote.id;
  const updated = await d.store.patchMessage(who.id, batch.id, m.id, patch, m.etag);
  await d.store.audit(who.id, batch.id, "outlook_status_checked", { messageId: m.id, result: status });
  return updated;
}

async function markManual(who, input, overrides = {}) {
  if (input.confirmHandledManually !== true) throw fail("Confirm that you already sent this message outside the app.", "confirmation_required", 400);
  const d = dependencies(overrides), { batch, message: m } = await records(who, input, d);
  if (m.handledManuallyUtc) return m;
  if (leased(m, d.now()) || ACTIVE.has(m.state))
    throw fail("A send may be in progress. Pause remaining work and wait for processing to finish; this action cannot recall mail.");
  if (m.state === "sent") throw fail("The application already records this message as sent.");
  if (["drafting", "sending", "scheduled", "schedule_held"].includes(batch.status))
    throw fail("Pause or cancel the batch before marking a message handled manually.");
  const updated = await d.store.patchMessage(who.id, batch.id, m.id, {
    state: "canceled", handledManuallyUtc: new Date(d.now()).toISOString(),
    handledManuallyBy: who.id, workerLeaseId: "", leaseUntilUtc: "", retryAfterUtc: "",
  }, m.etag);
  await d.store.audit(who.id, batch.id, "message_handled_manually", { messageId: m.id, actor: who.id });
  return updated;
}

async function retry(who, input, overrides = {}) {
  const d = dependencies(overrides);
  if (input.confirmNotSentElsewhere !== true) throw fail("Confirm that the selected emails were not already sent outside the app.", "confirmation_required", 400);
  const ids = [...new Set(Array.isArray(input.messageIds) ? input.messageIds.map(String) : [])];
  if (!ids.length || ids.length > 100) throw fail("Select between 1 and 100 messages to retry.", "selection_required", 400);
  let batch = await d.store.getBatch(who.id, input.batchId);
  if (!batch) throw fail("Email batch not found.", "not_found", 404);
  const messages = await d.store.listMessages(who.id, batch.id);
  if (messages.some(m => leased(m, d.now()) || ["draft_pending", "draft_ambiguous", "draft_creating", "send_scheduled", "sending", "submitted", "send_ambiguous", "scheduled_pending"].includes(m.state)))
    throw fail("This batch still has active work. Wait for it to finish before retrying selected failures.");
  const selected = ids.map(id => messages.find(m => m.id === id));
  if (selected.some(m => !m)) throw fail("A selected message does not belong to this batch.", "not_found", 404);
  for (const m of selected) {
    const result = eligibility(batch, m, d.now());
    if (!result.phase) throw fail(result.reason);
  }
  const connection = await d.auth.status(who.id);
  if (!connection.connected) throw fail("Reconnect Microsoft 365 before retrying.");
  const blocked = await d.suppress.blockedAmong(selected.flatMap(m =>
    [{ email: m.recipientEmail, contactId: m.contactId }, ...(m.teammateCc || []).map(email => ({ email }))]));
  if (blocked.size) throw fail("A selected recipient is suppressed. Nothing was queued.");
  // ETag on the batch serializes retry with pause/cancel/another retry.
  batch = await d.store.patchBatch(who.id, batch.id,
    { status: batch.mode === "send" ? "sending" : "drafting" }, batch.etag);
  const results = [];
  for (let i = 0; i < selected.length; i++) {
    const m = selected[i], phase = eligibility({ ...batch, status: "partial_failure" }, m, d.now()).phase;
    try {
      if (!phase) throw fail("Status check expired; check this message again.");
      await d.store.patchMessage(who.id, batch.id, m.id, {
        state: phase === "draft" ? "draft_pending" : "send_scheduled",
        failureCode: "", failureMessage: "", retryAfterUtc: "", leaseUntilUtc: "", workerLeaseId: "",
        outlookCheckStatus: "", outlookCheckedUtc: "",
        ...(phase === "draft" ? { draftAttempts: 0 } : { sendAttempts: 0 }),
      }, m.etag);
      // Durable state above is recoverable even if queue publication fails.
      let queued = true;
      try { await d.enqueue({ kind: phase, userId: who.id, batchId: batch.id, messageId: m.id }, i * 10); }
      catch { queued = false; }
      results.push({ messageId: m.id, result: queued ? "queued" : "awaiting_recovery" });
    } catch { results.push({ messageId: m.id, result: "not_queued_changed" }); }
  }
  await d.store.audit(who.id, batch.id, "selected_failures_retried", { results, confirmedNotSentElsewhere: true });
  return results;
}

module.exports = { check, markManual, retry, eligibility };
