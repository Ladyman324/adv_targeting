"use strict";

/* Durable one-to-one reply/follow-up orchestration.
 *
 * The HTTP request owns reversible draft preparation because uploaded bytes
 * exist only there. A queue worker owns the irreversible /send call. Once an
 * operation reaches `submitting`, uncertainty can move only toward
 * reconciliation -- never back to a state that may submit again.
 */

const crypto = require("crypto");
const graph = require("./graph-mail");
const auth = require("./email-auth");
const core = require("./email-core");
const suppress = require("./email-suppress");
const advisors = require("./advisor-lookup");
const limitGuard = require("./email-limit-guard");
const mailboxGate = require("./email-mailbox-gate");
const activityStore = require("./email-store");
const engagement = require("./email-engagement");
const opsStore = require("./email-direct-store");
const workQueue = require("./email-direct-queue");
const replyTools = require("./email-reply-send");

const RETRY_SECONDS = 30;
const RECONCILE_HORIZON_MS = 6 * 60 * 60 * 1000;

function httpError(statusCode, message, code) {
  const err = new Error(message);
  err.statusCode = statusCode;
  if (code) err.code = code;
  return err;
}

function dependencies(overrides = {}) {
  const merged = {
    graph, auth, core, suppress, advisors, limitGuard, mailboxGate,
    activityStore, engagement, opsStore, workQueue,
    wait: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    ...overrides,
  };
  // The existing, heavily-tested reply preparation gates call this dependency
  // `store`; direct orchestration names it activityStore to distinguish it from
  // the operation ledger. Keep one explicit alias so tests and production can
  // never accidentally fall back to a different Table client.
  if (!merged.store) merged.store = merged.activityStore;
  return merged;
}

function allowedUsers() {
  return new Set(String(process.env.EMAIL_DIRECT_SEND_OPS_USER_IDS || "")
    .split(/[,;\s]+/).map((value) => value.trim()).filter(Boolean));
}

function requireSendFeature(userId) {
  if (process.env.EMAIL_DIRECT_SEND_OPS_ENABLED !== "1") {
    throw httpError(503, "One-to-one sending is temporarily unavailable while its durable "
      + "confirmation path is disabled.", "direct_send_ops_disabled");
  }
  const allow = allowedUsers();
  if (allow.size && !allow.has(String(userId))) {
    throw httpError(403, "One-to-one sending is not enabled for this mailbox yet.",
      "direct_send_ops_not_allowlisted");
  }
}

function sendFeatureError(userId) {
  try { requireSendFeature(userId); return null; }
  catch (err) { return err; }
}

function hmacKey() {
  const secret = String(process.env.EMAIL_DIRECT_SEND_HMAC_KEY || "");
  if (Buffer.byteLength(secret, "utf8") < 32) {
    throw httpError(503, "The direct-send idempotency secret is not configured.",
      "direct_send_hmac_unavailable");
  }
  return secret;
}

function digest(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function intentHash(userId, kind, input, prepared) {
  const files = (prepared.resolved.files || []).map((file) => ({
    name: String(file.name || ""), contentType: String(file.contentType || ""),
    bytes: Number(file.size) || file.bytes.length, sha256: digest(file.bytes),
  })).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  const documents = (prepared.resolved.documents || []).map((doc) => ({
    id: String(doc.id || ""), version: Number(doc.version) || 0,
    bytes: Number(doc.size) || 0, sha256: String(doc.sha256 || ""),
  })).sort((a, b) => a.id.localeCompare(b.id));
  const value = {
    v: 1, userId: String(userId), kind, operationId: prepared.operationId,
    advisorCrd: prepared.crd, sourceGraphMessageId: prepared.messageId || "",
    replyAll: input.replyAll === true, subject: prepared.subject,
    text: String(input.text || ""), recipients: [...prepared.recipients].sort(),
    documents, files,
  };
  return crypto.createHmac("sha256", hmacKey())
    .update(JSON.stringify(value), "utf8").digest("hex");
}

function publicStatus(operation) {
  if (!operation) throw httpError(404, "Direct-send operation not found.", "direct_send_not_found");
  const map = {
    preparing: "preparing", prepared: "queued", submitting: "sending",
    submitted: "confirming", reconciled: "confirming", complete: "sent", failed: "failed",
    ambiguous: operation.needsVerification ? "needs_verification" : "confirming",
  };
  const status = map[operation.state] || "confirming";
  const pending = !["sent", "failed", "needs_verification"].includes(status);
  const messages = {
    preparing: "Preparing a recoverable Outlook draft.",
    queued: "Draft prepared and queued for sending.",
    sending: "Submitting the prepared Outlook draft.",
    confirming: "Submitted to Outlook; confirming the sent item.",
    needs_verification: "Delivery status is uncertain. Do not resend; verify in Outlook.",
    sent: "Outlook confirmed the sent message.",
    failed: "The message was not sent.",
  };
  return { ok: status !== "failed", operationId: operation.operationId, status, pending,
    alreadySent: status === "sent", retryAfterSeconds: pending ? 5 : 0,
    message: messages[status], errorCode: status === "failed" ? operation.lastErrorCode || "failed" : undefined };
}

async function status(who, operationId, overrides = {}) {
  const deps = dependencies(overrides);
  const id = replyTools.operationId({ operationId });
  // Partition key is always the authenticated caller. There is deliberately no
  // userId query parameter and no administrator bypass for message operations.
  return publicStatus(await deps.opsStore.getOperation(who.id, id));
}

async function enqueueOperation(deps, kind, operation, seconds = 0) {
  await deps.workQueue.enqueue(kind, operation.userId, operation.operationId, seconds);
  await deps.opsStore.markEnqueued(operation.userId, operation.operationId,
    new Date(Date.now() + Math.max(120, seconds) * 1000).toISOString());
}

async function preflightReply(who, input, deps) {
  const crd = String(input.crd || "").trim();
  const messageId = String(input.id || "").trim();
  const text = String(input.text || "");
  const operationId = replyTools.operationId(input);
  if (!crd || !messageId) throw httpError(400, "An advisor and a message are required.");
  if (!text.trim()) throw httpError(400, "The reply is empty.");
  if (text.length > replyTools.MAX_CHARS) throw httpError(400,
    `A message from here is limited to ${replyTools.MAX_CHARS} characters.`, "too_long");
  await replyTools.refuseInternal(crd, deps);
  const owner = await deps.activityStore.activityOwner(crd, messageId);
  if (!owner) throw httpError(404, "That message is not in this advisor's activity.", "no_such_activity");
  if (String(owner) !== String(who.id)) throw httpError(403,
    "That message is in another rep's mailbox.", "not_your_mailbox");
  const token = await deps.auth.tokenFor(who.id);
  const original = await deps.graph.getMessageContent(token.accessToken, messageId);
  const guarded = await replyTools.guard(
    (((original || {}).from || {}).emailAddress || {}).address, deps, { initiating: false });
  const resolved = await replyTools.resolveAttachments(input, deps);
  const compliance = replyTools.complianceAddresses(guarded.address, resolved, deps);
  const recipients = replyTools.uniqueAddresses([
    ...replyTools.effectiveReplyRecipients(original, input.replyAll === true, token, guarded.address),
    ...compliance,
  ]);
  return { kind: "reply", operationId, crd, messageId, text, subject: original.subject || "",
    token, original, resolved, recipients, to: guarded.address, suppressed: guarded.suppressed };
}

async function preflightFollowUp(who, input, deps) {
  const crd = String(input.crd || "").trim();
  const subject = String(input.subject || "").trim();
  const text = String(input.text || "");
  const operationId = replyTools.operationId(input);
  if (!crd) throw httpError(400, "An advisor is required.");
  if (!subject) throw httpError(400, "A subject is required.");
  if (subject.length > replyTools.MAX_SUBJECT) throw httpError(400,
    `The subject is limited to ${replyTools.MAX_SUBJECT} characters.`);
  if (!text.trim()) throw httpError(400, "The message is empty.");
  if (text.length > replyTools.MAX_CHARS) throw httpError(400,
    `A message from here is limited to ${replyTools.MAX_CHARS} characters.`, "too_long");
  await replyTools.refuseInternal(crd, deps);
  let target = "";
  try { target = await deps.advisors.emailForCrd(crd); } catch { /* activity fallback below */ }
  if (!target) {
    const known = await deps.activityStore.listActivity(crd, 200);
    const mine = known.filter((row) => String(row.userId) === String(who.id) && row.advisorEmail);
    target = (mine[0] || known.find((row) => row.advisorEmail) || {}).advisorEmail || "";
  }
  if (!target) throw httpError(409, "We hold no email address for this advisor.", "no_known_address");
  const guarded = await replyTools.guard(target, deps, { initiating: true });
  const token = await deps.auth.tokenFor(who.id);
  const resolved = await replyTools.resolveAttachments(input, deps);
  const compliance = replyTools.complianceAddresses(guarded.address, resolved, deps);
  return { kind: "follow_up", operationId, crd, messageId: "", text, subject,
    token, resolved, recipients: replyTools.uniqueAddresses([guarded.address, ...compliance]),
    to: guarded.address, suppressed: false };
}

async function createUnstampedFollowUp(prepared, input, deps) {
  const result = await deps.graph.request(prepared.token.accessToken, "POST", "/me/messages", {
    subject: prepared.subject,
    body: { contentType: "HTML", content: replyTools.textToHtml(prepared.text)
      + replyTools.signatureFor(prepared.token, deps) },
    toRecipients: [{ emailAddress: { address: prepared.to,
      name: String(input.name || "") || undefined } }],
  }, { timeoutMs: 45000 });
  return { ...(result.data || {}), requestId: result.requestId || "" };
}

async function recoverPrepared(prepared, deps) {
  let found;
  try { found = await deps.graph.findByAppId(prepared.token.accessToken, prepared.operationId); }
  catch {
    throw httpError(503, "Microsoft could not verify the prepared operation, so nothing was sent.",
      "send_status_unavailable");
  }
  return found || null;
}

async function start(who, input, kind, overrides = {}) {
  const deps = dependencies(overrides);
  requireSendFeature(who.id);
  const prepared = kind === "reply"
    ? await preflightReply(who, input, deps)
    : await preflightFollowUp(who, input, deps);
  const hash = intentHash(who.id, kind, input, prepared);
  const begun = await deps.opsStore.createOperation(who.id, {
    operationId: prepared.operationId, kind, intentHash: hash, advisorCrd: prepared.crd,
    sourceGraphMessageId: prepared.messageId, replyAll: input.replyAll === true,
    attachmentCount: prepared.resolved.documents.length + prepared.resolved.files.length,
  });
  let operation = begun.operation;
  if (!begun.created) {
    if (operation.state !== "preparing") return publicStatus(operation);
    operation = await deps.opsStore.claimOperation(who.id, prepared.operationId, ["preparing"],
      { nextState: "preparing", phase: "prepare", leaseSeconds: 300 });
    if (!operation) return publicStatus(await deps.opsStore.getOperation(who.id, prepared.operationId));
  }

  let preparationMayExist = false;
  try {
    await replyTools.enforceDirectSendPolicy(who, prepared.recipients,
      prepared.operationId, deps);
    // From the first Outlook lookup onward, an earlier attempt may already
    // have left a stamped draft or sent item. Any uncertainty after this point
    // must be recovered under the same operation id, never exposed as a fresh
    // send opportunity.
    preparationMayExist = true;
    let draft = await recoverPrepared(prepared, deps);
    if (draft && !draft.isDraft) {
      const next = await deps.opsStore.scheduleOperation(who.id, prepared.operationId, {
        state: draft.sentDateTime ? "reconciled" : "submitted",
        graphDraftId: draft.id, graphMessageId: draft.id,
        graphInternetMessageId: draft.internetMessageId || "",
        graphConversationId: draft.conversationId || "",
        canonicalSentDateTime: draft.sentDateTime || "", subject: draft.subject || prepared.subject,
        reconciledUtc: draft.sentDateTime ? new Date().toISOString() : "",
      }, new Date().toISOString(), operation.etag);
      await enqueueOperation(deps, draft.sentDateTime ? "direct_finalize" : "direct_reconcile", next);
      return publicStatus(next);
    }
    if (!draft) {
      draft = kind === "reply"
        ? await deps.graph.createReply(prepared.token.accessToken, prepared.messageId,
          input.replyAll === true)
        : await createUnstampedFollowUp(prepared, input, deps);
      if (!draft || !draft.id) throw httpError(502, "Microsoft did not return an Outlook draft.");
      if (kind === "reply") await deps.graph.updateDraftBody(prepared.token.accessToken, draft.id,
        replyTools.textToHtml(prepared.text) + replyTools.signatureFor(prepared.token, deps));
      await replyTools.attachAll(prepared.token, draft.id, prepared.resolved, deps);
      await replyTools.applyCompliance(prepared.token, draft.id, prepared.to,
        [...prepared.resolved.documents, ...prepared.resolved.files], deps);
      // The stamp is deliberately last. Finding it later means body,
      // attachments and compliance were all prepared; a retry never attaches
      // the same uploaded file twice.
      await replyTools.stampOperation(prepared.token, draft.id, prepared.operationId, deps);
    }
    const canonicalDraft = await deps.graph.getMessage(prepared.token.accessToken, draft.id);
    const next = await deps.opsStore.scheduleOperation(who.id, prepared.operationId, {
      state: "prepared", graphDraftId: canonicalDraft.id || draft.id,
      graphMessageId: canonicalDraft.id || draft.id,
      graphInternetMessageId: canonicalDraft.internetMessageId || "",
      graphConversationId: canonicalDraft.conversationId || "",
      graphRequestId: draft.requestId || "", subject: canonicalDraft.subject || prepared.subject,
      preparedUtc: new Date().toISOString(), complianceCopied:
        replyTools.complianceAddresses(prepared.to, prepared.resolved, deps).length > 0,
    }, new Date().toISOString(), operation.etag);
    await enqueueOperation(deps, "direct_send", next);
    return publicStatus(next);
  } catch (err) {
    const latest = await deps.opsStore.getOperation(who.id, prepared.operationId).catch(() => null);
    // A transaction may have committed even if its response or the following
    // queue write was lost. Its durable state is more authoritative than the
    // exception observed by this HTTP invocation.
    if (latest && latest.state !== "preparing") return publicStatus(latest);
    if (latest && preparationMayExist) {
      const seconds = RETRY_SECONDS;
      try {
        const next = await deps.opsStore.scheduleOperation(who.id, prepared.operationId, {
          state: "preparing", lastErrorCode: err.graphCode || err.code || "prepare_recovery_deferred",
        }, new Date(Date.now() + seconds * 1000).toISOString(), latest.etag);
        await enqueueOperation(deps, "direct_recover", next, seconds);
        return publicStatus(next);
      } catch {
        // The atomic q| marker was created with the operation and remains due.
        // Returning its pending status keeps the browser on the same id while
        // the repair timer recovers dispatch after a transient storage outage.
        return publicStatus(latest);
      }
    }
    // Policy rejection occurs before Outlook is touched and is therefore a
    // definitive failure. Storage uncertainty deliberately leaves the row for
    // the durable repair path instead of guessing.
    if (latest && err && err.code && !String(err.code).startsWith("direct_send_storage")) {
      await deps.opsStore.failOperation(who.id, prepared.operationId,
        { lastErrorCode: err.code || "prepare_failed" }, latest.etag).catch(() => {});
    }
    throw err;
  }
}

function graphRecipients(message) {
  return replyTools.uniqueAddresses([
    ...((message.toRecipients || []).map((entry) => (((entry || {}).emailAddress || {}).address))),
    ...((message.ccRecipients || []).map((entry) => (((entry || {}).emailAddress || {}).address))),
    ...((message.bccRecipients || []).map((entry) => (((entry || {}).emailAddress || {}).address))),
  ]);
}

function graphToRecipients(message) {
  return replyTools.uniqueAddresses((message.toRecipients || [])
    .map((entry) => (((entry || {}).emailAddress || {}).address)));
}

function directMessage(gr, token, id) {
  return (gr.getDirectMessage || gr.getMessage)(token, id);
}

function retrySeconds(operation) {
  const attempt = Math.max(1, Number(operation.reconcileAttempts) || 1);
  return Math.min(900, RETRY_SECONDS * (2 ** Math.min(attempt - 1, 5)));
}

async function processSend(operation, deps) {
  const featureError = sendFeatureError(operation.userId);
  if (featureError) {
    // Turning off a canary after preparation is a pause, not a programming
    // failure and not permission for the Functions host to poison the item.
    try {
      await deps.opsStore.scheduleOperation(operation.userId, operation.operationId,
        { state: "prepared", lastErrorCode: featureError.code },
        new Date(Date.now() + 300000).toISOString(), operation.etag);
    } catch (err) { if (![404, 412].includes(Number(err.statusCode))) throw err; }
    return;
  }
  const claimed = await deps.opsStore.claimOperation(operation.userId, operation.operationId,
    ["prepared"], { nextState: "prepared", phase: "send", leaseSeconds: 300 });
  if (!claimed) return;
  let token;
  try {
    token = await deps.auth.tokenFor(operation.userId);
    let draft = await directMessage(deps.graph, token.accessToken, claimed.graphDraftId).catch(async (err) => {
      if (Number(err.statusCode) !== 404) throw err;
      const found = await deps.graph.findByAppId(token.accessToken, claimed.operationId);
      return found ? directMessage(deps.graph, token.accessToken, found.id) : null;
    });
    if (draft && !draft.isDraft) {
      const next = await deps.opsStore.scheduleOperation(claimed.userId, claimed.operationId,
        { state: draft.sentDateTime ? "reconciled" : "submitted", graphMessageId: draft.id,
          graphInternetMessageId: draft.internetMessageId || "",
          graphConversationId: draft.conversationId || "",
          canonicalSentDateTime: draft.sentDateTime || "", subject: draft.subject || claimed.subject,
          reconciledUtc: draft.sentDateTime ? new Date().toISOString() : "" },
        new Date().toISOString(), claimed.etag);
      await enqueueOperation(deps, draft.sentDateTime ? "direct_finalize" : "direct_reconcile", next);
      return;
    }
    if (!draft || !draft.isDraft) throw httpError(409,
      "The prepared Outlook draft could not be found.", "prepared_draft_missing");
    const recipients = graphRecipients(draft);
    if (claimed.kind === "follow_up") {
      for (const recipient of graphToRecipients(draft))
        await replyTools.guard(recipient, deps, { initiating: true });
    }
    const cfg = await replyTools.enforceDirectSendPolicy({ id: claimed.userId }, recipients,
      claimed.operationId, deps);
    await replyTools.waitForMailbox({ id: claimed.userId }, cfg, deps);
    // Gates and pacing precede the irreversible state. A crash after this ETag
    // write is ambiguous by definition and recovery must never call /send.
    const submitting = await deps.opsStore.patchOperation(claimed.userId, claimed.operationId, {
      state: "submitting", sendStartedUtc: new Date().toISOString(),
      leaseId: claimed.leaseId, leaseUntilUtc: claimed.leaseUntilUtc,
    }, claimed.etag);
    try {
      const result = await deps.graph.sendDraft(token.accessToken, draft.id);
      const next = await deps.opsStore.scheduleOperation(submitting.userId, submitting.operationId, {
        state: "submitted", submittedUtc: new Date().toISOString(),
        graphRequestId: (result && result.requestId) || "",
      }, new Date(Date.now() + 5000).toISOString(), submitting.etag);
      await enqueueOperation(deps, "direct_reconcile", next, 5);
    } catch (err) {
      const ambiguous = err && (err.ambiguous || Number(err.statusCode) >= 500 || !err.statusCode);
      if (ambiguous) {
        const next = await deps.opsStore.scheduleOperation(submitting.userId, submitting.operationId, {
          state: "ambiguous", lastErrorCode: err.graphCode || "send_ambiguous",
          graphRequestId: err.requestId || "",
        }, new Date(Date.now() + 10000).toISOString(), submitting.etag);
        await enqueueOperation(deps, "direct_reconcile", next, 10);
      } else if (Number(err.statusCode) === 429) {
        const seconds = Math.max(5, Number(err.retryAfter) || RETRY_SECONDS);
        const next = await deps.opsStore.scheduleOperation(submitting.userId, submitting.operationId, {
          state: "prepared", lastErrorCode: err.graphCode || "throttled",
        }, new Date(Date.now() + seconds * 1000).toISOString(), submitting.etag);
        await enqueueOperation(deps, "direct_send", next, seconds);
      } else {
        await deps.opsStore.failOperation(submitting.userId, submitting.operationId,
          { lastErrorCode: err.graphCode || "send_rejected" }, submitting.etag);
      }
    }
  } catch (err) {
    const latest = await deps.opsStore.getOperation(claimed.userId, claimed.operationId).catch(() => null);
    if (!latest || latest.state !== "prepared") throw err;
    const due = new Date(Date.now() + RETRY_SECONDS * 1000).toISOString();
    await deps.opsStore.scheduleOperation(latest.userId, latest.operationId,
      { state: "prepared", lastErrorCode: err.code || "pre_send_deferred" }, due, latest.etag);
  }
}

async function findCanonical(operation, token, deps) {
  let found = null;
  if (operation.graphMessageId || operation.graphDraftId) {
    try { found = await directMessage(deps.graph, token.accessToken,
      operation.graphMessageId || operation.graphDraftId); }
    catch (err) { if (Number(err.statusCode) !== 404) throw err; }
  }
  if (!found || found.isDraft) {
    const byApp = await deps.graph.findByAppId(token.accessToken, operation.operationId);
    if (byApp && !byApp.isDraft) {
      try { found = await directMessage(deps.graph, token.accessToken, byApp.id); }
      catch { found = byApp; }
    }
    else if (!found) found = byApp;
  }
  return found;
}

async function processReconcile(operation, deps) {
  const claimed = await deps.opsStore.claimOperation(operation.userId, operation.operationId,
    ["submitted", "ambiguous"], { nextState: operation.state,
      phase: "reconcile", leaseSeconds: 180 });
  if (!claimed) return;
  try {
    const token = await deps.auth.tokenFor(claimed.userId);
    const found = await findCanonical(claimed, token, deps);
    if (found && !found.isDraft && found.sentDateTime) {
      const next = await deps.opsStore.scheduleOperation(claimed.userId, claimed.operationId, {
        state: "reconciled", graphMessageId: found.id,
        graphInternetMessageId: found.internetMessageId || "",
        graphConversationId: found.conversationId || "",
        canonicalSentDateTime: found.sentDateTime, subject: found.subject || claimed.subject,
        reconciledUtc: new Date().toISOString(), needsVerification: false, lastErrorCode: "",
      }, new Date().toISOString(), claimed.etag);
      await enqueueOperation(deps, "direct_finalize", next);
      return;
    }
    const age = Date.now() - Date.parse(claimed.sendStartedUtc || claimed.createdUtc || new Date());
    const needsVerification = age >= RECONCILE_HORIZON_MS;
    const seconds = needsVerification ? 21600 : retrySeconds(claimed);
    const next = await deps.opsStore.scheduleOperation(claimed.userId, claimed.operationId, {
      state: claimed.state === "submitted" && !needsVerification ? "submitted" : "ambiguous",
      needsVerification, lastErrorCode: needsVerification ? "send_needs_verification" : "",
    }, new Date(Date.now() + seconds * 1000).toISOString(), claimed.etag);
    if (!needsVerification) await enqueueOperation(deps, "direct_reconcile", next, seconds);
  } catch (err) {
    const latest = await deps.opsStore.getOperation(claimed.userId, claimed.operationId).catch(() => null);
    if (!latest || !["submitted", "ambiguous"].includes(latest.state)) throw err;
    const seconds = retrySeconds(latest);
    await deps.opsStore.scheduleOperation(latest.userId, latest.operationId,
      { state: latest.state, lastErrorCode: err.graphCode || err.code || "reconcile_deferred" },
      new Date(Date.now() + seconds * 1000).toISOString(), latest.etag);
  }
}

async function processFinalize(operation, deps) {
  const claimed = await deps.opsStore.claimOperation(operation.userId, operation.operationId,
    ["reconciled"], { nextState: "reconciled", phase: "finalize", leaseSeconds: 180 });
  if (!claimed) return;
  try {
    const token = await deps.auth.tokenFor(claimed.userId);
    const message = await findCanonical(claimed, token, deps);
    if (!message || message.isDraft || !message.sentDateTime) throw httpError(503,
      "The canonical sent item is not available yet.", "sent_item_unavailable");
    let advisorEmail = "";
    try { advisorEmail = await deps.advisors.emailForCrd(claimed.advisorCrd); } catch { /* metadata only */ }
    if (!advisorEmail) advisorEmail = graphRecipients(message)[0] || "";
    const recorded = await deps.activityStore.recordActivity({
      userId: claimed.userId, direction: "outbound",
      source: claimed.kind === "reply" ? "app_reply" : "app_followup",
      classification: "sent", route: "own_mailbox", recipientRole: "to",
      advisorCrd: claimed.advisorCrd, advisorEmail,
      occurredAt: message.sentDateTime, subject: message.subject || claimed.subject,
      conversationId: message.conversationId || claimed.graphConversationId,
      internetMessageId: message.internetMessageId || claimed.graphInternetMessageId,
      graphMessageId: message.id || claimed.graphMessageId,
    });
    if (recorded && recorded.dirtyMarker && typeof deps.engagement.refreshDirty === "function")
      await deps.engagement.refreshDirty(recorded.dirtyMarker, { store: deps.activityStore });
    else await deps.engagement.refresh(claimed.userId, claimed.advisorCrd,
      { store: deps.activityStore });
    await deps.engagement.completeOutbound(claimed.userId, claimed.advisorCrd,
      { store: deps.activityStore, actedAt: message.sentDateTime });
    await deps.opsStore.completeOperation(claimed.userId, claimed.operationId, {
      graphMessageId: message.id, graphInternetMessageId: message.internetMessageId || "",
      graphConversationId: message.conversationId || "",
      canonicalSentDateTime: message.sentDateTime, subject: message.subject || claimed.subject,
    }, claimed.etag);
  } catch (err) {
    const latest = await deps.opsStore.getOperation(claimed.userId, claimed.operationId).catch(() => null);
    if (!latest || latest.state !== "reconciled") throw err;
    const seconds = retrySeconds(latest);
    await deps.opsStore.scheduleOperation(latest.userId, latest.operationId,
      { state: "reconciled", lastErrorCode: err.code || "finalize_deferred" },
      new Date(Date.now() + seconds * 1000).toISOString(), latest.etag);
  }
}

async function processRecover(operation, deps) {
  const claimed = await deps.opsStore.claimOperation(operation.userId, operation.operationId,
    ["preparing"], { nextState: "preparing", phase: "prepare", leaseSeconds: 180 });
  if (!claimed) return;
  try {
    const token = await deps.auth.tokenFor(claimed.userId);
    const found = await deps.graph.findByAppId(token.accessToken, claimed.operationId);
    if (!found) {
      // No stamped message means the irreversible boundary was never reached.
      // An unowned partial Outlook draft may remain, but nothing was sent and a
      // fresh browser operation can prepare again safely.
      await deps.opsStore.failOperation(claimed.userId, claimed.operationId,
        { lastErrorCode: "preparation_incomplete" }, claimed.etag);
      return;
    }
    const nextState = !found.isDraft && found.sentDateTime
      ? "reconciled" : (!found.isDraft ? "submitted" : "prepared");
    const next = await deps.opsStore.scheduleOperation(claimed.userId, claimed.operationId, {
      state: nextState, graphDraftId: found.id, graphMessageId: found.id,
      graphInternetMessageId: found.internetMessageId || "",
      graphConversationId: found.conversationId || "",
      canonicalSentDateTime: found.sentDateTime || "", subject: found.subject || claimed.subject,
      preparedUtc: found.isDraft ? new Date().toISOString() : claimed.preparedUtc,
      reconciledUtc: nextState === "reconciled" ? new Date().toISOString() : "",
    }, new Date().toISOString(), claimed.etag);
    await enqueueOperation(deps, nextState === "prepared" ? "direct_send"
      : (nextState === "reconciled" ? "direct_finalize" : "direct_reconcile"), next);
  } catch (err) {
    const latest = await deps.opsStore.getOperation(claimed.userId, claimed.operationId).catch(() => null);
    if (!latest || latest.state !== "preparing") throw err;
    await deps.opsStore.scheduleOperation(latest.userId, latest.operationId,
      { state: "preparing", lastErrorCode: err.graphCode || err.code || "prepare_recovery_deferred" },
      new Date(Date.now() + RETRY_SECONDS * 1000).toISOString(), latest.etag);
  }
}

async function processWork(raw, overrides = {}) {
  const deps = dependencies(overrides);
  const work = deps.workQueue.parse(raw);
  if (!work || Number(work.v) !== 1 || !String(work.userId || "")
      || !String(work.operationId || "")) throw httpError(400,
    "Malformed direct-send work item.", "direct_send_work_malformed");
  const operation = await deps.opsStore.getOperation(work.userId, work.operationId);
  if (!operation || ["complete", "failed"].includes(operation.state)) return;
  if (work.kind === "direct_send") return processSend(operation, deps);
  if (work.kind === "direct_reconcile") return processReconcile(operation, deps);
  if (work.kind === "direct_finalize") return processFinalize(operation, deps);
  if (work.kind === "direct_recover") return processRecover(operation, deps);
  throw httpError(400, "Unknown direct-send work kind.", "direct_send_work_unknown");
}

module.exports = { start, status, processWork, publicStatus, intentHash,
  preflightReply, preflightFollowUp, graphRecipients, graphToRecipients,
  requireSendFeature };
