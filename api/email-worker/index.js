"use strict";

const auth = require("../shared/email-auth");
const store = require("../shared/email-store");
const graph = require("../shared/graph-mail");
const service = require("../shared/email-service");
const core = require("../shared/email-core");
const mailboxGate = require("../shared/email-mailbox-gate");
const suppress = require("../shared/email-suppress");

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

async function failOrRetry(work, claimed, err, phase, deps) {
  if (["graph_not_connected", "graph_reconnect_required"].includes(err.code)) {
    await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "auth_required",
      failureCode: `auth_required_${phase}`, failureMessage: err.message, leaseUntilUtc: "" }, claimed.etag);
    await deps.store.audit(work.userId, work.batchId, "microsoft_reconnect_required", { messageId: work.messageId, phase });
    return;
  }
  const retryable = err.statusCode === 429 || err.ambiguous || (err.statusCode >= 500);
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
    await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "failed",
      failureCode: err.graphCode || "graph_failure", failureMessage: err.message,
      graphRequestId: err.requestId || "", leaseUntilUtc: "" }, claimed.etag);
  }
  await deps.store.audit(work.userId, work.batchId, `${phase}_failed`, { messageId: work.messageId,
    retryable, code: err.graphCode || "", requestId: err.requestId || "" });
}

async function draft(work, deps) {
  const batch = await deps.store.getBatch(work.userId, work.batchId);
  if (!batch || ["canceled"].includes(batch.status)) return;
  const claimed = await deps.store.claimMessage(work.userId, work.batchId, work.messageId,
    ["draft_pending", "draft_ambiguous", "draft_creating"], "draft_creating", 300, "draft");
  if (!claimed) return;
  try {
    const token = await deps.auth.tokenFor(work.userId);
    if (String(token.mailboxId).toLowerCase() !== String(batch.graphMailboxId).toLowerCase())
      throw service.httpError(403, "Mailbox identity changed after batch creation; refusing to create a draft.");
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
      const slot = claimed.sendPosition >= 0 ? claimed.sendPosition : claimed.ordinal;
      const due = new Date(batch.sendNotBeforeUtc).getTime() + slot * deps.core.config().mailboxIntervalSeconds * 1000;
      await deps.enqueue({ kind: "send", userId: work.userId, batchId: work.batchId, messageId: work.messageId },
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
  const due = new Date(batch.sendNotBeforeUtc).getTime();
  if (due > Date.now()) { await deps.enqueue(work, Math.ceil((due - Date.now()) / 1000)); return; }
  const claimed = await deps.store.claimMessage(work.userId, work.batchId, work.messageId,
    ["send_scheduled", "send_ambiguous", "sending"], "sending", 180, "send");
  if (!claimed) return;
  try {
    const token = await deps.auth.tokenFor(work.userId);
    if (String(token.mailboxId).toLowerCase() !== String(batch.graphMailboxId).toLowerCase())
      throw service.httpError(403, "Mailbox identity changed after approval; refusing to send.");
    let remote = claimed.graphMessageId ? await deps.graph.getMessage(token.accessToken, claimed.graphMessageId).catch((e) => {
      if (e.statusCode === 404) return null; throw e;
    }) : null;
    if (!remote) remote = await deps.graph.findByAppId(token.accessToken, claimed.id);
    if (!remote) throw new graph.GraphError("The known Outlook draft could not be reconciled.", { ambiguous: true });
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
      const [policy, blocked] = await Promise.all([
        deps.store.policy(),
        deps.suppress.blockedAmong([{ email: claimed.recipientEmail, contactId: claimed.contactId }]),
      ]);

      if (blocked.size) {
        // Final for this message. The recipient asked not to be emailed; there is
        // no state in which retrying that is correct.
        const why = blocked.get(String(claimed.recipientEmail || "").toLowerCase()) || "opted out";
        await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "canceled",
          failureCode: "recipient_opted_out", leaseUntilUtc: "",
          failureMessage: `Not sent: ${claimed.recipientEmail} ${why}. `
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
    } else if (message.state === "send_ambiguous") {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "send_scheduled",
        leaseUntilUtc: "" }, message.etag);
      await deps.enqueue({ ...work, kind: "send" }, 5);
    } else if (message.reconcileAttempts < RETRY_CEILING) {
      await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { leaseUntilUtc: "" }, message.etag);
      await deps.enqueue(work, 60);
    } else await deps.store.patchMessage(work.userId, work.batchId, work.messageId, { state: "failed",
      leaseUntilUtc: "", failureCode: "sent_item_not_confirmed", failureMessage: "Graph accepted the send, but the Sent Items copy could not be confirmed. Do not retry automatically." }, message.etag);
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
  const deps = { auth, store, graph, enqueue: service.enqueue, core, mailboxGate, suppress, ...overrides };
  if (!work.userId || !work.batchId || !work.messageId) throw new Error("Incomplete email queue message.");
  if (work.kind === "draft") return draft(work, deps);
  if (work.kind === "send") return send(work, deps);
  if (work.kind === "reconcile") return reconcile(work, deps);
  throw new Error(`Unknown email work kind ${work.kind}.`);
}

module.exports = async function (context, workItem) { await processWork(workItem); };
module.exports.processWork = processWork;
module.exports.parseWork = parseWork;
module.exports.refreshBatch = refreshBatch;
