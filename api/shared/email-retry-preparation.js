"use strict";

const crypto = require("node:crypto");
const store = require("./email-store");
const auth = require("./email-auth");
const core = require("./email-core");
const suppress = require("./email-suppress");
const materials = require("./email-materials");
const capacity = require("./email-limit-guard");
const review = require("./email-review");
const fail = (message, code = "retry_preparation_required", statusCode = 409) =>
  Object.assign(new Error(message), { code, statusCode });

function stableId(...parts) {
  const h = crypto.createHash("sha256").update(JSON.stringify(parts)).digest("hex");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-5${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`;
}

function sourceEvidence(m) {
  return m.retryBatchId ? { ...m, state: m.retryOriginalState, retryBatchId: "" } : m;
}

async function prepare(who, input, overrides = {}) {
  const d = { store, auth, core, suppress, materials, capacity, now: () => Date.now(), ...overrides };
  if (input.confirmNotSentElsewhere !== true)
    throw fail("Confirm that you have not already sent the selected messages outside the app.", "confirmation_required", 400);
  const ids = [...new Set(Array.isArray(input.messageIds) ? input.messageIds.map(String) : [])].sort();
  if (!ids.length || ids.length > 100) throw fail("Select between 1 and 100 failed messages.", "selection_required", 400);
  const parent = await d.store.getBatch(who.id, input.batchId);
  if (!parent) throw fail("Email batch not found.", "not_found", 404);
  const childId = stableId("retry-review-v1", who.id, parent.id, ids);
  let child = await d.store.getBatch(who.id, childId);
  // Lost HTTP acknowledgements and double-clicks reopen ONE review, even after
  // its approval. Never recreate or reset its content/status.
  if (child && child.status !== "building") return { batchId: childId, existing: true };
  const all = await d.store.listMessages(who.id, parent.id);
  let selected = ids.map(id => all.find(m => m.id === id));
  if (selected.some(m => !m)) throw fail("A selected message does not belong to this batch.", "not_found", 404);
  if (all.some(m => !m.retryBatchId && (Date.parse(m.leaseUntilUtc || "") > d.now()
      || ["draft_pending", "draft_ambiguous", "draft_creating", "send_scheduled", "sending", "submitted", "send_ambiguous", "scheduled_pending"].includes(m.state))))
    throw fail("This batch still has active work. Wait for it to finish first.");
  for (const m of selected) {
    if (m.retryBatchId && m.retryBatchId !== childId)
      throw fail("A selected message already belongs to another retry review. Open the linked batch.", "retry_review_exists");
    // A later sent observation must not be hidden by the original-state snapshot.
    if (m.retryBatchId && m.state !== "canceled") throw fail("An original message changed after preparation. Check its status.");
    const eligible = review.prepareEligibility(parent, sourceEvidence(m), d.now());
    if (!eligible.ready) throw fail(eligible.reason);
  }
  const connection = await d.auth.status(who.id);
  if (!connection.connected || !connection.profile) throw fail("Reconnect Microsoft 365 before preparing a retry.");
  if (String(connection.profile.id).toLowerCase() !== String(parent.graphMailboxId).toLowerCase())
    throw fail("The connected mailbox differs from the original batch mailbox.");
  const blocked = await d.suppress.blockedAmong(selected.flatMap(m => [
    { email: m.recipientEmail, contactId: m.contactId },
    ...(m.teammateCc || []).map((email, i) => ({ email, contactId: (m.teammateCcCrds || [])[i] })),
  ]));
  if (blocked.size) throw fail("A selected recipient or copied teammate is suppressed. Remove that selection.");
  const template = await d.store.getTemplate(parent.templateId);
  if (!template || template.published === false) throw fail("The original template is no longer approved. Ask an email administrator to review it.");
  const attachmentIds = [...new Set(selected.flatMap(m => (m.attachments || []).map(a => a.id)))];
  const documents = await d.store.getDocuments(attachmentIds);
  const docs = new Map(documents.map(doc => [doc.id, doc]));
  if (attachmentIds.some(id => !docs.has(id) || !d.materials.currentDocument(docs.get(id))))
    throw fail("An original attachment is no longer current approved material. Update the material before preparing this retry.", "attachment_unavailable");
  const cfg = d.core.config();
  if (!child) {
    try {
      await d.store.createBatch(who, { id: childId, status: "building",
        name: `Retry review — ${parent.name || "email batch"}`,
        retrySourceBatchId: parent.id, retrySourceMessageIds: ids,
        templateId: parent.templateId, templateName: parent.templateName, templateVersion: template.version,
        commonSubject: parent.commonSubject, commonBodyText: parent.commonBodyText,
        recipientCount: selected.length, externalCount: selected.filter(m => d.core.isExternal(m.recipientEmail, cfg)).length,
        attachmentIds, attachmentSummary: documents.map(({ id, name, size, contentType, version }) => ({ id, name, size, contentType, version })),
        graphMailboxId: parent.graphMailboxId, graphMailbox: parent.graphMailbox,
        senderMail: connection.profile.mail || connection.profile.userPrincipalName || parent.senderMail,
        signatureHtml: d.core.corporateSignature(connection.profile, "", cfg),
        copySelf: parent.copySelf, copyInternal: parent.copyInternal,
        copyInternalTo: parent.copyInternalTo, ccColleague: parent.ccColleague,
        warningMessage: "Prepared for retry. Nothing has been sent. Review preserved wording, current attachments, and a new delivery plan before approval.",
      });
    } catch (error) { if (Number(error.statusCode) !== 409) throw error; }
    child = await d.store.getBatch(who.id, childId);
    if (!child) throw fail("Preparation could not be confirmed. Repeat the same selection; no send was authorized.");
    if (child.status !== "building") return { batchId: childId, existing: true };
  }

  if (selected.some(m => !m.retryBatchId)) {
    if (selected.some(m => m.retryBatchId)) throw fail("Retry ownership is inconsistent. No replacement was authorized.");
    // All original rows move together, or none do. Losing the transaction's
    // response leaves the same deterministic building child safe to resume.
    try { await d.store.reserveRetryMessages(who.id, parent.id, selected, childId); }
    catch (error) {
      const latest = await d.store.listMessages(who.id, parent.id);
      if (!ids.every(id => latest.find(m => m.id === id)?.retryBatchId === childId)) throw error;
    }
  }
  selected = await Promise.all(ids.map(id => d.store.getMessage(who.id, parent.id, id)));
  for (let i = 0; i < selected.length; i++) {
    const m = selected[i];
    if (!m || m.retryBatchId !== childId || m.handledManuallyUtc || m.bounceKind || m.bounceAtUtc || m.state !== "canceled")
      throw fail("An original message changed. Preparation remains on hold; nothing was sent.");
    const messageId = stableId(childId, m.id);
    if (await d.store.getMessage(who.id, childId, messageId)) continue;
    try {
      await d.store.createMessage(who.id, childId, {
        id: messageId, ordinal: i, state: "editing", contactId: m.contactId,
        recipientName: m.recipientName, recipientEmail: m.recipientEmail, companyName: m.companyName,
        greetingName: m.greetingName, recipientLastName: m.recipientLastName,
        recipientRegistryHash: m.recipientRegistryHash, recipientRoutingHash: m.recipientRoutingHash,
        recipientTier: m.recipientTier, recipientSource: m.recipientSource,
        recipientMatchScore: m.recipientMatchScore, recipientMatchGap: m.recipientMatchGap,
        recipientPolicyVersion: m.recipientPolicyVersion,
        teammateCcJson: JSON.stringify(m.teammateCc || []), teammateCcCrdsJson: JSON.stringify(m.teammateCcCrds || []),
        teammatesAvailableJson: JSON.stringify(m.teammatesAvailable || []),
        subject: m.subject, bodyText: m.bodyText, bodyHtml: m.bodyHtml, inlineImages: m.inlineImages,
        subjectOverridden: m.subjectOverridden, bodyOverridden: m.bodyOverridden, baseRevision: 1,
        signatureHtml: d.core.corporateSignature(connection.profile, d.suppress.manageUrl(m.recipientEmail, m.contactId), cfg),
        attachments: (m.attachments || []).map(a => docs.get(a.id)),
        retryOfBatchId: parent.id, retryOfMessageId: m.id, followUpOfGraphId: m.followUpOfGraphId,
      });
    } catch (error) { if (Number(error.statusCode) !== 409) throw error; }
  }
  // A concurrent request may already have finalized this child. Never validate
  // or overwrite a review the representative has begun editing or approved.
  child = await d.store.getBatch(who.id, childId);
  if (child.status !== "building") return { batchId: childId, existing: true };
  if ((await d.store.listMessages(who.id, childId)).length !== ids.length)
    throw fail("Retry preparation is incomplete. Repeat the same selection to finish it.");
  // Original routing is revalidated at normal approval and by the workers.
  // No approval, old capacity allocation, or Graph message id is copied.
  try { await d.store.patchBatch(who.id, childId, { status: "editing" }, child.etag); }
  catch (error) {
    if (Number(error.statusCode) !== 412) throw error;
    if ((await d.store.getBatch(who.id, childId)).status === "building") throw error;
  }
  if (parent.capacityReservationId) {
    try { await d.capacity.releaseAllocations(who.id, parent.capacityReservationId, ids); }
    catch { /* Conservative: old allocation remains counted until cleaned up. */ }
  }
  await d.store.audit(who.id, childId, "retry_review_prepared", {
    sourceBatchId: parent.id, sourceMessageIds: ids, confirmedNotSentElsewhere: true, actor: who.id,
  });
  return { batchId: childId, existing: false };
}

// Before creating or sending a replacement, check the original chain again.
// A representative may have sent the original Outlook draft after preparation.
async function assertOriginalUnsent(message, batch, token, deps) {
  let current = message, expectedChild = batch.id;
  for (let depth = 0; current.retryOfBatchId || current.retryOfMessageId; depth++) {
    if (depth >= 20 || !current.retryOfBatchId || !current.retryOfMessageId)
      throw fail("Retry history is incomplete; manual review is required.", "retry_source_invalid");
    const source = await deps.store.getMessage(batch.userId, current.retryOfBatchId, current.retryOfMessageId);
    if (!source || source.retryBatchId !== expectedChild || source.handledManuallyUtc || source.bounceKind || source.bounceAtUtc
        || source.state !== "canceled" || source.submittedUtc || ["started", "accepted"].includes(source.sendOutcome))
      throw fail("The original message was sent, handled manually, or changed. This replacement was not sent.", "retry_source_changed");
    const known = source.graphMessageId ? await deps.graph.getMessage(token, source.graphMessageId)
      .catch(error => { if (Number(error.statusCode) === 404) return null; throw error; }) : null;
    const found = await deps.graph.findByAppId(token, source.id);
    const matches = [known, found].filter(Boolean);
    if (matches.some(m => m.isDraft !== true))
      throw fail("The original Outlook message is sent or inconclusive. This replacement was not sent.", "retry_original_not_draft");
    if (!matches.length && source.sendAttempts !== 0)
      throw fail("The original draft could not be found and has send-stage history. Review before resending.", "retry_original_missing");
    expectedChild = current.retryOfBatchId;
    current = source;
  }
}

module.exports = { prepare, stableId, assertOriginalUnsent };
