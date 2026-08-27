"use strict";

const { QueueClient } = require("@azure/storage-queue");
const core = require("./email-core");
const store = require("./email-store");
const auth = require("./email-auth");
const limitGuard = require("./email-limit-guard");
const suppress = require("./email-suppress");
const health = require("./email-health");
const recipientRegistry = require("./recipient-registry");
// The rep's own account settings live in the app store, not the email store --
// same table the map's default scope and call list come from.
const appStore = require("./store");

const QUEUE_NAME = "email-work"; // must match the queueTrigger binding

function httpError(statusCode, message, code) {
  const e = new Error(message); e.statusCode = statusCode; if (code) e.code = code; return e;
}

function queueClient() {
  const conn = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!conn) throw httpError(503, "Email queue storage is not configured.");
  // No messageEncoding option here: that is a .NET SDK setting. The JavaScript
  // Storage SDK silently ignores unknown options, so passing it looked correct
  // and did nothing -- messages went on as plain text while the Functions host
  // defaults to Base64. The host could not decode them, so it never invoked the
  // worker at all and moved each message straight to the poison queue: no
  // exception, no invocation, nothing in Application Insights. Encoding is done
  // explicitly in enqueue() below, where it is visible.
  return new QueueClient(conn, QUEUE_NAME);
}

async function enqueue(work, visibilityTimeout = 0) {
  const q = queueClient();
  await q.createIfNotExists();
  // Base64 to match the host's default queue MessageEncoding. email-worker
  // decodes base64 first and falls back to raw JSON, so both forms are read
  // correctly -- but everything written from here is encoded.
  const payload = Buffer.from(JSON.stringify(work), "utf8").toString("base64");
  await q.sendMessage(payload, { visibilityTimeout: Math.max(0, Math.min(visibilityTimeout, 7 * 86400)) });
}


/* Sender health for every connected rep.
 *
 * Sends are counted from the MESSAGE records rather than from a running
 * counter: a counter would need incrementing at exactly the moment a send
 * succeeds, which is the moment most likely to be interrupted, and it could
 * drift silently for months. Recomputing is slower and always right, and this
 * is an occasional admin screen rather than a hot path.
 */
async function senderHealth(days = 90) {
  const since = new Date(Date.now() - Math.max(1, Number(days) || 90) * 86400000).toISOString();
  const connections = await store.listConnections();
  const sends = [];
  const events = [];

  for (const c of connections) {
    for (const batch of await store.listBatches(c.userId, 200)) {
      if (batch.createdUtc && batch.createdUtc < since) continue;
      for (const m of await store.listMessages(c.userId, batch.id)) {
        // Only what actually reached Exchange. A draft that was never approved
        // is not a delivery attempt and would flatter every rate here.
        if (!["submitted", "sent"].includes(m.state)) continue;
        const address = String(m.recipientEmail || "").toLowerCase();
        sends.push({ userId: c.userId, userName: batch.userName || c.mailbox,
                     address, domain: address.split("@")[1] || "",
                     sentUtc: m.submittedUtc || batch.approvedUtc || "" });
      }
    }
    for (const e of await store.deliveryEvents(c.userId, since)) {
      events.push({ ...e, userId: c.userId });
    }
  }

  // Unsubscribes are attributed through the message they came from, which is
  // the only honest link -- the suppression row itself records an address, not
  // a sender.
  // Attributed through whoever most recently mailed that address. An opt-out
  // with no matching send is left OUT rather than filed under a blank rep --
  // inventing a sender to keep a total tidy is how a dashboard starts lying.
  const lastSender = new Map();
  for (const s of sends) {
    const prev = lastSender.get(s.address);
    if (!prev || String(s.sentUtc) > String(prev.sentUtc)) lastSender.set(s.address, s);
  }
  const optOuts = [];
  for (const row of await suppress.list()) {
    if (row.source !== "unsubscribe-link") continue;
    if (since && row.addedUtc && row.addedUtc < since) continue;
    const hit = lastSender.get(String(row.address || "").toLowerCase());
    if (hit) optOuts.push({ userId: hit.userId, domain: hit.domain });
  }

  return { since, days: Number(days) || 90,
           reps: health.summarise(sends, events, optOuts),
           totals: { sends: sends.length, events: events.length,
                     connections: connections.length } };
}

async function catalog(who) {
  const [connection, templates, documents, policy, rollingUsed] = await Promise.all([
    auth.status(who.id), store.listTemplates(), store.listDocuments(), store.policy(),
    // What this rep has already spent of their 24-hour allowance. Sent to the
    // client so the composer can say what is possible BEFORE a batch is built,
    // rather than only at the moment approval is refused.
    store.rollingExternalCount(who.id).catch(() => 0),
  ]);
  const cfg = core.config();
  return { connection, templates, documents,
    // Addresses a rep may copy, straight from the App Setting. Sent with the
    // catalog so the Settings picker cannot offer anything the server would
    // then refuse.
    internalRecipients: cfg.internalRecipients,
    signatureHtml: connection.profile ? core.corporateSignature(connection.profile) : "",
    policy: { directSendAvailable: cfg.directSendEnvironmentEnabled && !policy.killed,
      killed: policy.killed, reason: policy.reason,
      directSendBlockedBy: core.directSendBlockedBy(cfg, policy.killed, policy.reason) }, limits: {
      directBatchMax: cfg.directBatchMax, rollingExternalLimit: cfg.rollingExternalLimit,
      cancellationSeconds: cfg.cancellationSeconds, mailboxIntervalSeconds: cfg.mailboxIntervalSeconds,
      maxMessageBytes: cfg.maxMessageBytes, maxAttachmentBytes: cfg.maxAttachmentBytes,
      absoluteBatchStop: core.ABSOLUTE_BATCH_STOP,
      reviewSummaryOver: cfg.reviewSummaryOver, reviewLargeOver: cfg.reviewLargeOver,
      reviewElevatedOver: cfg.reviewElevatedOver, draftsOnlyOver: cfg.draftsOnlyOver,
      // The threshold only, never the code itself. The client needs to know
      // when to show the field; it has no business knowing what goes in it.
      passcodeOver: cfg.passcode ? cfg.passcodeOver : null,
      rollingUsed, rollingRemaining: Math.max(0, cfg.rollingExternalLimit - rollingUsed),
    } };
}

// The advisor's practice, as the CLIENT sees it: the teammate list lives in
// contacts.json, which is a static asset the API never loads. Accepted as a
// hint and nothing more -- every address is re-checked for shape, suppression
// and duplication before it reaches a draft. See teammateCc().
const MAX_TEAMMATE_CC = 8;

function recipientSnapshot(raw) {
  return { contactId: String(raw.contactId || raw.crd || "").slice(0, 80),
    name: String(raw.name || "").slice(0, 256), email: String(raw.email || "").trim().toLowerCase().slice(0, 320),
    firm: String(raw.companyName || raw.firm || "").slice(0, 256),
    firstName: String(raw.firstName || "").slice(0, 120), lastName: String(raw.lastName || "").slice(0, 120),
    teammates: (Array.isArray(raw.teammates) ? raw.teammates : [])
      .map((a) => String(a || "").trim().toLowerCase().slice(0, 320))
      .filter((a) => core.validEmail(a)).slice(0, MAX_TEAMMATE_CC),
    // Named, for the per-message picker. Same untrusted hint, same re-checks.
    teammatesFull: (Array.isArray(raw.teammatesFull) ? raw.teammatesFull : [])
      .map((t) => ({ name: String((t && t.name) || "").slice(0, 120),
                     email: String((t && t.email) || "").trim().toLowerCase().slice(0, 320) }))
      .filter((t) => core.validEmail(t.email)).slice(0, 25) };
}

async function canonicalRecipient(raw, connection) {
  const crd = String(raw.contactId || raw.crd || "").trim();
  if (!crd) {
    const mailbox = String(connection.mailbox || "").trim().toLowerCase();
    const email = String(raw.email || "").trim().toLowerCase();
    if (!mailbox || email !== mailbox) throw httpError(400,
      "Every external recipient needs an approved advisor CRD.", "recipient_crd_required");
    const profile = connection.profile || {}, split = core.splitName(profile.displayName || "");
    return { contactId: "", name: profile.displayName || connection.mailbox, email: mailbox,
      firm: "", firstName: profile.givenName || split.first, lastName: profile.surname || split.last,
      teammates: [], teammatesFull: [], recipientKind: "self_test",
      registryHash: "", routingHash: "" };
  }
  const approved = await recipientRegistry.resolve(crd);
  const mates = await recipientRegistry.allowedTeammates(crd);
  const split = core.splitName(approved.name);
  return { contactId: crd, name: approved.name, email: approved.email, firm: approved.firm,
    firstName: approved.greetingName || split.first, lastName: approved.lastName || split.last,
    teammates: mates.map((m) => m.email),
    teammatesFull: mates.map((m) => ({ crd: m.crd, name: m.name, email: m.email })),
    registryHash: approved.registryHash, routingHash: approved.routingHash };
}

/* Which of an advisor's teammates may actually be copied.
 *
 * A CC is a real email to a real person, so it goes through the same gates the
 * To address does:
 *
 *   suppressed / unsubscribed   SKIPPED. Copying somebody who asked us to stop
 *                               is the same failure whichever header they are
 *                               in -- and the unsubscribe footer is signed for
 *                               the recipient, so a CC'd advisor could not opt
 *                               out of a message they never asked for.
 *   already the recipient       skipped, so nobody is on a message twice
 *   capped at MAX_TEAMMATE_CC   a 40-person practice is a mailing list, not a
 *                               copy, and would quietly multiply the batch
 */
async function teammateCc(recipients) {
  const wanted = new Set();
  for (const r of recipients) for (const a of r.teammates || []) wanted.add(a);
  if (!wanted.size) return new Map();
  const blocked = await suppress.blockedAmong([...wanted].map((email) => ({ email })));
  const out = new Map();
  for (const r of recipients) {
    const list = (r.teammates || []).filter((a) => !blocked.has(a) && a !== r.email);
    if (list.length) out.set(r.contactId || r.email, list);
  }
  return out;
}

/* Nobody receives the same batch twice.
 *
 * Two advisors on one practice, both in the recipient list, with teammate
 * copies on: her message copies him and his copies her, so each of them gets it
 * twice. Somebody has to go, and the choice here is to keep the COPY and drop
 * the DIRECT SEND -- one email per person, addressed to whoever came first in
 * the list.
 *
 * Worth being clear about the cost, because it is not nothing: the person
 * dropped loses their own name in the greeting, their own signed unsubscribe
 * link, an outreach logged against their CRD, and a place in the batch count.
 * The rep is warned before generating, and the review screen names everyone
 * removed.
 *
 * ORDER RESOLVES THE CYCLE. Walking the list once and only dropping people
 * copied by someone ALREADY KEPT means two mutual teammates settle as one
 * message rather than removing each other and leaving none.
 */
function dropCopiedRecipients(recipients, teamCc) {
  const kept = [], removed = [];
  const copied = new Set();
  for (const r of recipients) {
    if (r.email && copied.has(r.email)) { removed.push(r); continue; }
    kept.push(r);
    for (const a of teamCc.get(r.contactId || r.email) || []) copied.add(a);
  }
  return { kept, removed };
}

async function validateMessage(message, duplicateEmails, cfg, currentDocsById, commonRevision = 0,
                               knownImageIds = null, identityErrors = []) {
  const errors = [...identityErrors], warnings = [];
  if (!core.validEmail(message.recipientEmail)) errors.push({ code: "invalid_recipient", message: "Recipient email is missing or invalid." });
  if (duplicateEmails.has(message.recipientEmail)) errors.push({ code: "duplicate_recipient", message: "This recipient appears more than once in the batch." });
  if (!String(message.subject || "").trim()) errors.push({ code: "missing_subject", message: "Subject is required." });
  if (!String(message.bodyText || "").trim()) errors.push({ code: "missing_body", message: "Body is required." });
  // Image placeholders legitimately survive in bodyText -- they are resolved
  // into <img src="cid:..."> in bodyHtml rather than substituted out of the
  // text. Left in this list they would report every chart as an unresolved
  // merge field and block the batch.
  const tokens = [...`${message.subject}\n${message.bodyText}`.matchAll(/\{\{\s*([^}]+)\s*\}\}/g)]
    .map((m) => String(m[1]).trim());
  const unresolved = [], badImages = [];
  for (const token of tokens) {
    const isImage = /^image:/i.test(token);
    if (!isImage) { unresolved.push(token); continue; }
    // Exempt only ids that ACTUALLY EXIST on this template.
    //
    // This used to exempt anything beginning "image:", so a typo --
    // {{image:performace}} for {{image:performance}} -- passed every check and
    // was mailed as the literal text "{{image:performace}}". The token stays in
    // bodyText by design (it is resolved into bodyHtml rather than substituted
    // out), so nothing downstream noticed either.
    //
    // knownImageIds null means the caller could not load the template; exempting
    // every image then keeps the old lax behaviour rather than blocking a whole
    // batch over a lookup failure.
    const id = token.slice(6).trim().toLowerCase();
    if (knownImageIds && !knownImageIds.has(id)) badImages.push(id);
  }
  if (unresolved.length) errors.push({ code: "unresolved_merge_fields", message: `Resolve template fields: ${[...new Set(unresolved)].join(", ")}.` });
  if (badImages.length) errors.push({ code: "unknown_image",
    message: `No chart on this template is called ${[...new Set(badImages)].map((t) => `"${t}"`).join(", ")}. `
      + `It would be sent to the advisor as literal text. Check the spelling, or remove the placeholder.` });
  if (message.recipientEmail && await store.getSuppression(message.recipientEmail))
    errors.push({ code: "permanent_bounce_suppression", message: "This address has a known permanent bounce and is suppressed until corrected." });
  let attachmentBytes = 0;
  for (const doc of message.attachments || []) {
    const current = currentDocsById.get(doc.id);
    if (!doc.approved || !doc.blobName || !current || current.version !== doc.version || current.sha256 !== doc.sha256)
      errors.push({ code: "attachment_unavailable", message: `${doc.name || "An attachment"} is no longer the currently approved version.` });
    if (Number(doc.size) > cfg.maxAttachmentBytes) errors.push({ code: "attachment_too_large", message: `${doc.name} exceeds the application attachment limit.` });
    attachmentBytes += Number(doc.size) || 0;
  }
  const estimated = Buffer.byteLength(message.subject || "") + Buffer.byteLength(message.bodyHtml || "")
    + Buffer.byteLength(message.signatureHtml || "") + Math.ceil(attachmentBytes * 4 / 3) + 4096;
  if (estimated > cfg.maxMessageBytes) errors.push({ code: "message_too_large", message: "Estimated message size exceeds the configured application limit; the Exchange tenant may be lower still." });
  if (String(message.bodyText || "").length > cfg.maxBodyChars) errors.push({ code: "body_too_large", message: "Body exceeds the application limit." });
  /* No "not reviewed" warning. It said exactly what the tally row at the top of
   * the composer already says, and said it as a banner that appeared on open
   * and vanished a moment later once the message was marked read -- which
   * reflowed the whole panel under the rep's cursor. A duplicate of a permanent
   * counter is not worth a layout shift. The reviewed flag itself is untouched;
   * the count and the approval dialog both still use it. */
  // The quiet one. A rep edits three messages individually, later spots a typo
  // in the common text and fixes it -- and those three keep the typo, because
  // an override is never overwritten. Nothing used to say so at the point it
  // mattered. This does, per message and again at approval.
  if (commonRevision && Number(message.baseRevision || 0) < commonRevision)
    warnings.push({ code: "behind_common_text",
      message: "This message keeps its own wording and does NOT include your latest change to the common template." });
  return { errors, warnings, estimatedBytes: estimated };
}

function identityPresentationRefresh(message, approved, batch, sender, images) {
  const split = core.splitName(approved.name);
  const presentation = {
    recipientName: approved.name,
    greetingName: approved.greetingName || split.first,
    recipientLastName: approved.lastName || split.last,
    companyName: approved.firm,
  };
  const changed = Object.entries(presentation)
    .some(([key, value]) => String(message[key] || "") !== String(value || ""));
  if (!changed) return { changed: false, blocked: false, patch: presentation };
  if (message.subjectOverridden || message.bodyOverridden)
    return { changed: true, blocked: true, patch: {} };
  const recipient = {
    name: presentation.recipientName,
    firstName: presentation.greetingName,
    lastName: presentation.recipientLastName,
    firm: presentation.companyName,
  };
  const subject = core.renderTemplate(batch.commonSubject, recipient, sender).rendered;
  const bodyText = core.renderTemplate(batch.commonBodyText, recipient, sender).rendered;
  return { changed: true, blocked: false, patch: {
    ...presentation, subject, bodyText,
    bodyHtml: core.plainTextToSafeHtml(bodyText, images),
  } };
}

async function validateBatch(who, batchId, options = {}) {
  const batch = await store.getBatch(who.id, batchId);
  if (!batch) throw httpError(404, "Email batch not found.");
  const messages = await store.listMessages(who.id, batchId);
  const counts = new Map();
  for (const m of messages) counts.set(m.recipientEmail, (counts.get(m.recipientEmail) || 0) + 1);
  const duplicates = new Set([...counts].filter(([, n]) => n > 1).map(([email]) => email));
  const cfg = core.config(), updated = [];
  await recipientRegistry.load({ force: options.identityForce === true });
  const currentDocsById = new Map((await store.getDocuments(batch.attachmentIds)).map((d) => [d.id, d]));
  const tpl = await store.getTemplate(batch.templateId);
  const images = (tpl && tpl.images) || [];
  const knownImageIds = tpl ? new Set(images.map((i) => String(i.id).toLowerCase())) : null;
  const sender = (await auth.status(who.id).catch(() => null) || {}).profile || null;
  for (const message of messages) {
    const identityErrors = [];
    const identityPatch = {};
    let presentationChanged = false;
    try {
      if (message.contactId) {
        const approved = await recipientRegistry.verify(message.contactId, message.recipientEmail);
        const requestedMates = (message.teammateCc || []).map((email, index) => ({
          crd: (message.teammateCcCrds || [])[index] || "", email,
        }));
        const approvedMates = await recipientRegistry.verifyTeammates(
          message.contactId, requestedMates);
        const blockedMates = approvedMates.length
          ? await suppress.blockedAmong(approvedMates.map((mate) => ({
              email: mate.email, contactId: mate.crd,
            }))) : new Map();
        if (blockedMates.size) throw httpError(409,
          "A copied teammate is now suppressed and must be removed before approval.",
          "teammate_suppressed");
        const available = await recipientRegistry.allowedTeammates(message.contactId);
        identityPatch.recipientRegistryHash = approved.registryHash;
        identityPatch.recipientRoutingHash = approved.routingHash;
        identityPatch.teammateCcJson = JSON.stringify(approvedMates.map((mate) => mate.email));
        identityPatch.teammateCcCrdsJson = JSON.stringify(approvedMates.map((mate) => mate.crd));
        identityPatch.teammatesAvailableJson = JSON.stringify(available.map((mate) => ({
          crd: mate.crd, name: mate.name, email: mate.email,
        })));
        const refresh = identityPresentationRefresh(
          message, approved, batch, sender, images);
        presentationChanged = refresh.changed;
        if (refresh.blocked) {
          identityErrors.push({
            code: "recipient_presentation_override_stale",
            message: "This advisor's approved name or greeting changed. Restore the "
              + "approved subject/body wording (or recreate the recipient) before approval.",
          });
        } else Object.assign(identityPatch, refresh.patch);
      } else if (String(message.recipientEmail).toLowerCase() !== String(batch.graphMailbox).toLowerCase()) {
        throw httpError(409, "A non-advisor recipient is not the connected mailbox.",
          "recipient_not_approved");
      }
    } catch (err) {
      identityErrors.push({ code: err.code || "recipient_identity_unavailable",
        message: err.message || "Recipient identity could not be verified." });
    }
    const reviewed = presentationChanged ? false
      : (options.reviewed === true ? true : message.reviewed);
    const candidate = { ...message, ...identityPatch, reviewed };
    const validation = await validateMessage(candidate, duplicates, cfg, currentDocsById,
      Number(batch.commonRevision) || 0, knownImageIds, identityErrors);
    updated.push(await store.patchMessage(who.id, batchId, message.id, { ...identityPatch, reviewed, validation,
      state: validation.errors.length ? "invalid" : (message.state === "invalid" ? "editing" : message.state) }, message.etag));
  }
  const errors = updated.flatMap((m) => m.validation.errors.map((v) => ({ messageId: m.id,
    recipient: m.recipientEmail, ...v })));
  const warnings = updated.flatMap((m) => m.validation.warnings.map((v) => ({ messageId: m.id,
    recipient: m.recipientEmail, ...v })));
  // The blind copy is computed, never stored, so the preview can never drift
  // from what the worker will actually put on the draft -- both call the same
  // function on the same message.
  const b = await store.getBatch(who.id, batchId);
  const prefs = { copySelf: b.copySelf, copyInternal: b.copyInternal,
                  copyInternalTo: b.copyInternalTo, ccColleague: b.ccColleague };
  const withCopies = updated.map((m) => ({ ...m,
    ...core.extraRecipients(m, prefs, { mail: b.senderMail }, cfg) }));
  return { batch: b, messages: withCopies,
    valid: errors.length === 0, errors, warnings,
    // What "Restore approved wording" restores TO. Sent from the server so the
    // client restores the template as published rather than whatever it happens
    // to be holding.
    approvedText: tpl ? { subject: tpl.subject, bodyText: tpl.bodyText } : null,
    // The chart ids that are actually valid here, so the composer can name them
    // instead of leaving a rep to remember the spelling.
    templateImages: tpl ? (tpl.images || []).map(({ id, name }) => ({ id, name })) : [] };
}

/* WHO STILL NEEDS FOLLOWING UP, and who must not be.
 *
 * The rep's rule is "everyone who did not reply". That is right, and it is not
 * sufficient -- three other groups have to come off the list, and each of them
 * is a different kind of mistake if it does not:
 *
 *   replied      the point of the exercise. An OUT-OF-OFFICE is NOT a reply:
 *                classify() separates them, and an auto-responder tells you
 *                nothing about whether the person read it.
 *   bounced      the address is dead. A second send guarantees a second bounce
 *                and spends sender reputation to learn what we already know.
 *   suppressed   they opted out between the send and now. Compliance, not
 *                preference, and the one exclusion that is not a judgement call.
 *   not sent     a message that failed or is still queued was never a first
 *                touch, so there is nothing to follow up ON.
 *
 * Computed fresh every time this is asked, never cached: it is asked once when
 * the follow-up is drafted and AGAIN immediately before the drafts are built,
 * because a rep may sit on the review screen while somebody replies.
 */
async function followUpCandidates(who, batchId, deps = {}) {
  const st = deps.store || store;
  const batch = await st.getBatch(who.id, batchId);
  if (!batch) throw httpError(404, "Email batch not found.");
  if (batch.parentBatchId)
    throw httpError(409, "This batch is already a follow-up. Follow up on the original instead.", "already_follow_up");

  const messages = await st.listMessages(who.id, batchId);
  const sent = messages.filter((m) => ["sent", "submitted"].includes(m.state));
  const notSent = messages.length - sent.length;

  const replied = [], bounced = [], pending = [];
  for (const m of sent) {
    if (m.bounceKind === "hard") { bounced.push(m); continue; }
    // A reply is an INBOUND row on the conversation this message started.
    // Keyed on conversationId rather than on the advisor, so a reply to a
    // different campaign does not silence this one.
    let answered = false;
    if (m.contactId) {
      const rows = await st.listActivity(String(m.contactId), 200).catch(() => []);
      /* Matched on the CONVERSATION this message started, falling back to the
       * batch it belongs to. Either is specific enough that a reply to a
       * different campaign cannot silence this one -- which matching on the
       * advisor alone would do. */
      answered = rows.some((a) =>
        String(a.direction) === "inbound"
        && String(a.classification) === "reply"          // not auto_reply, not bounce
        && ((m.graphConversationId && String(a.conversationId || "") === m.graphConversationId)
            || String(a.batchId || "") === batchId));
    }
    (answered ? replied : pending).push(m);
  }

  // Opt-outs since the send. Checked by address AND contact id, as everywhere.
  const blocked = pending.length
    ? await suppress.blockedAmong(pending.map((m) => ({ email: m.recipientEmail, contactId: m.contactId })))
    : new Set();
  const suppressed = pending.filter((m) => blocked.has(m.recipientEmail));
  const remaining = pending.filter((m) => !blocked.has(m.recipientEmail));

  const brief = (m) => ({ messageId: m.id, crd: m.contactId || "",
    name: m.recipientName || "", email: m.recipientEmail || "",
    graphMessageId: m.graphMessageId || "", subject: m.subject || "" });

  return {
    batchId, batchName: batch.name || "", followUpDays: batch.followUpDays || 0,
    followUpSentUtc: batch.followUpSentUtc || "",
    counts: { sent: sent.length, replied: replied.length, bounced: bounced.length,
              suppressed: suppressed.length, notSent, remaining: remaining.length },
    replied: replied.map(brief), bounced: bounced.map(brief),
    suppressed: suppressed.map(brief), remaining: remaining.map(brief),
  };
}

/* Build the follow-up as a REAL BATCH, derived from the parent.
 *
 * Not a bespoke bulk-send path. A batch inherits everything already built and
 * tested around one: the per-recipient preview on the review step, the
 * suppression checks, the firm-domain pacing that stops 22 messages hitting one
 * gateway in a row, teammate copies, the compliance blind copy, activity
 * logging and retry idempotency. A second send path would re-implement all of
 * that slightly differently, and the differences are where the bugs live.
 *
 * THE FOOTER IS THE CAMPAIGN FOOTER. A follow-up is outreach we initiated, not
 * an answer to something they sent, so it carries its own per-recipient
 * unsubscribe link exactly as the original did.
 *
 * ATTACHMENTS DEFAULT OFF. Re-sending the same PDF to people who already have
 * it is how a thread ends up in a spam folder, and it re-triggers the forced
 * compliance blind copy on every message. On when the document IS the point.
 */
const FOLLOW_UP_DEFAULT_TEXT = "Just following up on the note below in case it "
  + "reached you at a busy moment.";

async function createFollowUp(who, input, deps = {}) {
  const st = deps.store || store;
  const registry = deps.recipientRegistry || recipientRegistry;
  const emailAuth = deps.auth || auth;
  const cfg = core.config();
  const parentId = String(input.batchId || "").trim();
  const parent = await st.getBatch(who.id, parentId);
  if (!parent) throw httpError(404, "Email batch not found.");
  // One follow-up per campaign. Without this, running it twice puts a THIRD
  // touch on people who have now had two and answered neither.
  if (parent.followUpSentUtc)
    throw httpError(409, "A follow-up has already been created for this batch.", "follow_up_exists");

  const connection = await emailAuth.status(who.id);
  if (!connection.connected || !connection.profile)
    throw httpError(409, "Connect your Microsoft 365 mailbox first.", "graph_not_connected");
  const profile = connection.profile;

  // Recomputed HERE, not taken from the client. The rep may have been looking
  // at the review screen for an hour while somebody replied.
  const fresh = await followUpCandidates(who, parentId, deps);
  if (!fresh.remaining.length)
    throw httpError(409, "Everybody has replied, bounced or opted out — there is nobody to follow up.", "nobody_to_follow_up");

  await registry.load({ force: true });
  const verifiedRemaining = [];
  for (const row of fresh.remaining) {
    const approved = await registry.verify(row.crd, row.email);
    verifiedRemaining.push({ row, approved });
  }
  const note = String(input.text || FOLLOW_UP_DEFAULT_TEXT).slice(0, cfg.maxBodyChars);
  if (!note.trim()) throw httpError(400, "The follow-up needs something to say.");
  const withAttachments = input.includeAttachments === true;
  const documents = withAttachments && parent.attachmentIds.length
    ? await st.getDocuments(parent.attachmentIds) : [];

  const batchId = st.id();
  await st.createBatch(who, { id: batchId, status: "editing",
    name: `Follow-up — ${parent.name || "campaign"}`,
    templateId: parent.templateId, templateName: parent.templateName,
    commonSubject: "", commonBodyText: note, commonRevision: 1,
    attachmentIds: documents.map((d) => d.id),
    attachmentSummary: documents.map((d) => ({ id: d.id, name: d.name, bytes: d.bytes })),
    recipientCount: verifiedRemaining.length, externalCount: verifiedRemaining.length,
    parentBatchId: parentId,
    graphMailboxId: profile.id, graphMailbox: connection.mailbox,
    ccTeammates: "", ccColleague: "",
    copySelf: String(parent.copySelf || ""), copyInternal: String(parent.copyInternal || ""),
    copyInternalTo: String(parent.copyInternalTo || ""),
    senderMail: String(profile.mail || profile.userPrincipalName || ""),
    recipientRegistryHash: (verifiedRemaining[0] && verifiedRemaining[0].approved.registryHash) || "" });

  for (let i = 0; i < verifiedRemaining.length; i++) {
    const { row: r, approved } = verifiedRemaining[i];
    const split = core.splitName(approved.name);
    // "RE:" and the threading both come from Graph's createReply in the worker.
    // The subject here is what the REVIEW SCREEN shows, so it has to read the
    // way the advisor will see it.
    const subject = /^re:/i.test(r.subject) ? r.subject : `RE: ${r.subject}`;
    await st.createMessage(who.id, batchId, { id: st.id(), ordinal: i,
      contactId: r.crd, recipientName: approved.name, recipientEmail: approved.email,
      companyName: approved.firm,
      greetingName: approved.greetingName || split.first,
      recipientLastName: approved.lastName || split.last,
      recipientRegistryHash: approved.registryHash,
      recipientRoutingHash: approved.routingHash,
      teammateCcJson: "[]", teammateCcCrdsJson: "[]",
      teammatesAvailableJson: JSON.stringify((await registry.allowedTeammates(r.crd))
        .map((mate) => ({ crd: mate.crd, name: mate.name, email: mate.email }))),
      // The sent message this replies to. The worker needs the Graph id, not
      // the conversation id, because it replies to a MESSAGE.
      followUpOfGraphId: r.graphMessageId,
      subject, bodyText: note,
      bodyHtml: core.plainTextToSafeHtml(note, []),
      inlineImages: [],
      signatureHtml: core.corporateSignature(profile,
        suppress.manageUrl(r.email, r.crd), cfg),
      baseRevision: 1, attachments: documents,
      validation: { errors: r.graphMessageId ? [] : [{ code: "no_original",
        message: "The original message is no longer in the mailbox, so this cannot be threaded." }],
        warnings: [] } });
  }

  await st.patchBatch(who.id, parentId, { followUpSentUtc: new Date().toISOString() },
    parent.etag).catch(() => {});
  await st.audit(who.id, batchId, "follow_up_created",
    { parentBatchId: parentId, recipients: fresh.remaining.length,
      replied: fresh.counts.replied, bounced: fresh.counts.bounced,
      suppressed: fresh.counts.suppressed, attachments: documents.length });
  return getBatchDetail(who, batchId);
}

async function createBatch(who, input) {
  const requestedRecipients = (Array.isArray(input.recipients) ? input.recipients : []).map(recipientSnapshot);
  if (!requestedRecipients.length) throw httpError(400, "Select at least one recipient.");
  if (requestedRecipients.length >= core.ABSOLUTE_BATCH_STOP) throw httpError(400, "15,000 recipients is a campaign-sized use case and is blocked.");
  const connection = await auth.status(who.id);
  if (!connection.connected || !connection.profile) throw httpError(409, "Connect your Microsoft 365 mailbox first.", "graph_not_connected");
  await recipientRegistry.load();
  const recipients = [];
  for (const raw of requestedRecipients) recipients.push(await canonicalRecipient(raw, connection));
  const template = await store.getTemplate(String(input.templateId || ""));
  if (!template) throw httpError(400, "Choose an approved email template.");
  // Required attachments come from the template and cannot be dropped by the
  // rep. In the Word library this was a line of prose -- "Attachments required:
  // most recent approved Case for Value" -- which is guidance a busy person
  // skips. Here it is the set the batch is built with.
  const required = [...new Set((template.requiredDocumentIds || template.defaultAttachmentIds || []).map(String))];
  const requested = [...new Set([...required, ...(input.attachmentIds || []).map(String)])];
  const documents = await store.getDocuments(requested);
  const missingDocs = requested.filter((x) => !documents.some((d) => d.id === x));
  if (missingDocs.length) throw httpError(400,
    missingDocs.some((x) => required.includes(x))
      ? `This template requires attachments that are no longer in the approved catalog: ${missingDocs.join(", ")}. An email administrator needs to republish them.`
      : `Approved attachments are unavailable: ${missingDocs.join(", ")}.`);
  // Anyone who has opted out is removed HERE, before the batch exists, rather
  // than being filtered at send time. A rep who sees 60 recipients and gets 57
  // sent has no idea who the other three were; a rep told up front that three
  // were dropped for opting out has an accurate picture of what they are about
  // to send, which is the whole premise of the review step.
  // Checked by address AND by CRM contact id -- see email-suppress.js for why the
  // second one is not redundant.
  const blocked = await suppress.blockedAmong(recipients);
  const dropped = recipients.filter((r) => blocked.has(r.email));
  const notBlocked = recipients.filter((r) => !blocked.has(r.email));
  /* Resolved BEFORE the batch is created, not after.
   *
   * Everything the batch records -- recipientCount, externalCount, the rolling
   * external allowance -- is computed from this list, so somebody removed here
   * has to be gone before any of that is counted. Doing it afterwards would
   * have the batch claim more recipients than it has messages.
   */
  /* No teammate is copied at BATCH creation any more.
   *
   * There used to be a batch-level "copy the advisor's teammates" checkbox that
   * pre-populated every message with every teammate. It was the wrong shape --
   * all recipients, all teammates, decided before the rep had seen any of the
   * individual emails -- and it has been replaced by a per-message picker in
   * Step 2 (updateMessageCc).
   *
   * The map stays empty here, and dropCopiedRecipients() below still runs
   * against it: a rep who later copies somebody who IS in this batch has them
   * removed from the direct list at that moment instead, which is the same
   * guarantee made at the point the decision is actually taken.
   */
  const teamCc = new Map();
  const { kept, removed: copiedInstead } = dropCopiedRecipients(notBlocked, teamCc);
  if (!kept.length) throw httpError(400, dropped.length
    ? "Every recipient selected has asked not to receive email from us."
    : "Select at least one recipient.");

  const cfg = core.config(), batchId = store.id();
  const warning = core.guardrail(kept.length, "drafts", cfg);
  // Batch-level signature is the preview copy and carries no preference link --
  // a link is only meaningful bound to one address, and each message gets its
  // own below.
  // Refreshed rather than taken from the connection row: that row's profile was
  // captured when the mailbox was first connected and may predate fields the
  // signature now uses. Falls back to the stored copy if Microsoft is unhappy.
  const profile = (await auth.refreshProfile(who.id)) || connection.profile;
  // Read ONCE, here, so every message in the batch is copied the same way even
  // if the rep edits the setting while the batch is being built.
  const prefs = await appStore.getSettings(who).catch(() => ({}));
  const signatureHtml = core.corporateSignature(profile, "", cfg);
  const batch = await store.createBatch(who, { id: batchId,
    name: input.name || `${template.name} — ${new Date().toLocaleDateString("en-US")}`,
    templateId: template.id, templateName: template.name, commonSubject: template.subject,
    commonBodyText: template.bodyText, attachmentIds: requested,
    attachmentSummary: documents.map(({ id, name, size, contentType, version }) => ({ id, name, size, contentType, version })),
    recipientCount: kept.length, externalCount: kept.filter((r) => core.isExternal(r.email, cfg)).length,
    warningLevel: warning.level, warningMessage: warning.message, signatureHtml,
    suppressedCount: dropped.length,
    // Named, with the reason. "3 removed" teaches a rep nothing; "two asked to
    // unsubscribe, one is bouncing" tells them whether to chase it.
    copiedInsteadNote: copiedInstead.length
      ? `${copiedInstead.length} recipient${copiedInstead.length === 1 ? " is" : "s are"} `
        + `copied on a teammate's message instead of receiving their own: `
        + `${copiedInstead.slice(0, 6).map((r) => r.name || r.email).join("; ")}`
        + `${copiedInstead.length > 6 ? `; and ${copiedInstead.length - 6} more` : ""}.`
      : "",
    suppressedNote: dropped.length
      ? `${dropped.length} recipient${dropped.length === 1 ? " was" : "s were"} removed: `
        + `${dropped.slice(0, 4).map((r) => `${r.name || r.email} (${blocked.get(r.email)})`).join("; ")}`
        + `${dropped.length > 4 ? `; and ${dropped.length - 4} more` : ""}.`
      : "",
    graphMailboxId: connection.profile.id, graphMailbox: connection.mailbox,
    // Whoever the rep asked to be copied, frozen at this moment. See the
    // comment on these fields in email-store.createBatch().
    /* Per-batch copies, distinct from the standing preferences above.
     *
     * ccColleague is re-checked against the allowlist HERE as well as in
     * extraRecipients: a batch should not be created carrying an address the
     * server would refuse to send to, or a rep would see it on the review
     * screen and then watch it silently vanish. */
    // Retained on the row for batches created before the per-message picker.
    ccTeammates: "",
    ccColleague: cfg.internalRecipients.some((r) => r.address ===
        String(input.ccColleague || "").trim().toLowerCase())
      ? String(input.ccColleague).trim().toLowerCase() : "",
    copySelf: String(prefs.copySelf || ""),
    copyInternal: String(prefs.copyInternal || ""),
    copyInternalTo: String(prefs.copyInternalTo || ""),
    senderMail: String(profile.mail || profile.userPrincipalName || ""),
    recipientRegistryHash: (kept.find((r) => r.registryHash) || {}).registryHash || "" });
  for (let i = 0; i < kept.length; i++) {
    const r = kept[i], subject = core.renderTemplate(template.subject, r, profile),
      body = core.renderTemplate(template.bodyText, r, profile);
    await store.createMessage(who.id, batchId, { id: store.id(), ordinal: i, contactId: r.contactId,
      recipientName: r.name, recipientEmail: r.email, companyName: r.firm,
      greetingName: r.firstName, recipientLastName: r.lastName,
      recipientRegistryHash: r.registryHash, recipientRoutingHash: r.routingHash,
      // Stored per message: each advisor has their own practice, so this is the
      // one copy decision that cannot be a batch-level field.
      teammateCcJson: JSON.stringify(teamCc.get(r.contactId || r.email) || []),
      // The whole practice, so the review screen can offer them one at a time.
      // Stored rather than recomputed: contacts.json is a client asset, and a
      // rep three screens into a batch should not need it reloaded to pick a
      // teammate. Suppression is re-checked on every actual copy.
      teammatesAvailableJson: JSON.stringify((r.teammatesFull || []).slice(0, 25)),
      teammateCcCrdsJson: "[]",
      subject: subject.rendered, bodyText: body.rendered,
      bodyHtml: core.plainTextToSafeHtml(body.rendered, template.images || []),
      inlineImages: template.images || [],
      // Per-recipient: the footer's preference link is signed for THIS address.
      signatureHtml: core.corporateSignature(profile,
        suppress.manageUrl(r.email, r.contactId), cfg),
      baseRevision: 1, attachments: documents, validation: { errors: [
        ...subject.missing.map((f) => ({ code: "missing_merge_value", message: `Missing ${f}.` })),
        ...body.missing.map((f) => ({ code: "missing_merge_value", message: `Missing ${f}.` })),
      ], warnings: [] } });
  }
  await store.audit(who.id, batchId, "batch_created", { recipientCount: kept.length,
    suppressedCount: dropped.length,
    templateId: template.id, attachmentIds: requested });
  return validateBatch(who, batchId);
}

async function updateCommon(who, input) {
  const batch = await store.getBatch(who.id, input.batchId);
  if (!batch) throw httpError(404, "Email batch not found.");
  if (!["editing", "invalid"].includes(batch.status)) throw httpError(409, "This batch can no longer be edited.");
  const subjectTemplate = String(input.subject == null ? batch.commonSubject : input.subject).slice(0, 500);
  const bodyTemplate = String(input.bodyText == null ? batch.commonBodyText : input.bodyText).slice(0, core.config().maxBodyChars);
  const template = await store.getTemplate(batch.templateId);
  const images = (template && template.images) || [];
  /* The sender's own profile, for {{sender_name}} and {{sender_title}}.
   * Without it a rep editing the common body would re-render every message
   * with those two fields blank -- the batch would have been built correctly
   * and then quietly emptied by an unrelated edit. */
  const sender = (await auth.status(who.id).catch(() => null) || {}).profile || null;

  /* Rep edits pass the SAME lint the approved template had to pass.
   *
   * lintTemplate ran only when an administrator published a template, so the
   * wording that cleared compliance was protected and the moment a rep touched
   * it every one of those rules stopped applying. The costly case is a single
   * brace: {first_name} is not a token, so it renders as literal text and the
   * unresolved-field check never sees it. "Hi {first_name}," goes out.
   */
  const lint = core.lintTemplate({ subject: subjectTemplate, bodyText: bodyTemplate,
    maxBodyChars: core.config().maxBodyChars });
  const knownIds = new Set(images.map((i) => String(i.id).toLowerCase()));
  for (const m of `${subjectTemplate}
${bodyTemplate}`.matchAll(core.IMAGE_TOKEN)) {
    const id = String(m[1]).toLowerCase();
    if (!knownIds.has(id)) lint.errors.push({ code: "unknown_image",
      message: `No chart on this template is called "${id}". It would be sent as literal text.` });
  }
  if (lint.errors.length) {
    const err = new Error(lint.errors.map((e) => e.message).join(" "));
    err.statusCode = 400; err.code = "common_text_invalid"; err.errors = lint.errors;
    throw err;
  }

  /* What the edit REMOVED.
   *
   * A typo checker cannot see a deletion, and deletion is the likelier mistake:
   * a rep tidying the opening line drops {{first_name}} and every email in the
   * batch now begins "Hi ,". Reported as a warning rather than blocked, because
   * removing a field is sometimes exactly what was intended.
   */
  const tokensIn = (text) => new Set([...String(text || "")
    .matchAll(/\{\{\s*([^}]+)\s*\}\}/g)].map((m) => String(m[1]).trim().toLowerCase()));
  const wasThere = new Set([...tokensIn(template && template.subject),
                            ...tokensIn(template && template.bodyText)]);
  const stillThere = new Set([...tokensIn(subjectTemplate), ...tokensIn(bodyTemplate)]);
  const removed = [...wasThere].filter((t) => !stillThere.has(t));

  const revision = batch.commonRevision + 1;
  const behind = [];
  for (const m of await store.listMessages(who.id, batch.id)) {
    const recipient = { name: m.recipientName, firstName: m.greetingName,
      lastName: m.recipientLastName, firm: m.companyName };
    /* `overwriteAll` replaces individually edited messages too.
     *
     * Normally an override is sacred -- a rep wrote those words on purpose and
     * a later common edit must not silently eat them. But that leaves no way
     * back: a rep who personalised six messages and then rewrote the template
     * had six emails permanently carrying the old wording, and the only remedy
     * was to open each one. This is the deliberate, asked-for exception, and it
     * is destructive, so nothing reaches it without an explicit confirmation
     * naming how many messages it will overwrite.
     */
    const overwrite = input.overwriteAll === true;
    const took = overwrite || !m.subjectOverridden || !m.bodyOverridden;
    // baseRevision was previously stamped onto EVERY message, including the
    // individually edited ones -- which erased the only evidence that they had
    // not received the change. A message whose baseRevision trails the batch's
    // commonRevision is now precisely "did not get the latest common edit",
    // which is what makes the divergence reportable instead of silent.
    const patch = { reviewed: false, state: "editing" };
    if (took) patch.baseRevision = revision;
    else behind.push({ id: m.id, name: m.recipientName || m.recipientEmail });
    if (overwrite || !m.subjectOverridden) {
      patch.subject = core.renderTemplate(subjectTemplate, recipient, sender).rendered;
      if (overwrite) patch.subjectOverridden = false;
    }
    if (overwrite || !m.bodyOverridden) {
      patch.bodyText = core.renderTemplate(bodyTemplate, recipient, sender).rendered;
      patch.bodyHtml = core.plainTextToSafeHtml(patch.bodyText, images);
      if (overwrite) patch.bodyOverridden = false;
    }
    await store.patchMessage(who.id, batch.id, m.id, patch, m.etag);
  }
  await store.patchBatch(who.id, batch.id, { commonSubject: subjectTemplate, commonBodyText: bodyTemplate,
    commonRevision: revision, status: "editing", reviewedUtc: "" }, batch.etag);
  await store.audit(who.id, batch.id, "common_content_updated",
    { revision, keptOwnText: behind.length, removedTokens: removed,
      overwroteIndividualEdits: input.overwriteAll === true });
  return { ...await validateBatch(who, batch.id), keptOwnText: behind, removedTokens: removed,
    lintWarnings: lint.warnings || [] };
}

async function updateMessage(who, input) {
  const batch = await store.getBatch(who.id, input.batchId);
  const message = batch && await store.getMessage(who.id, batch.id, input.messageId);
  if (!batch || !message) throw httpError(404, "Email message not found.");
  if (!["editing", "invalid"].includes(batch.status)) throw httpError(409, "This batch can no longer be edited.");
  const patch = { reviewed: !!input.reviewed, state: "editing" };
  if ("subject" in input) { patch.subject = String(input.subject || "").slice(0, 500); patch.subjectOverridden = true; }
  const tpl = await store.getTemplate(batch.templateId);
  const tplImages = (tpl && tpl.images) || [];
  const sender = (await auth.status(who.id).catch(() => null) || {}).profile || null;
  if ("bodyText" in input) { patch.bodyText = String(input.bodyText || "").slice(0, core.config().maxBodyChars);
    patch.bodyHtml = core.plainTextToSafeHtml(patch.bodyText, tplImages); patch.bodyOverridden = true; }
  const mergeRecipient = { name: message.recipientName, firstName: message.greetingName,
    lastName: message.recipientLastName, firm: message.companyName };
  if (input.resetSubject) { patch.subject = core.renderTemplate(batch.commonSubject, mergeRecipient, sender).rendered; patch.subjectOverridden = false; }
  if (input.resetBody) { patch.bodyText = core.renderTemplate(batch.commonBodyText, mergeRecipient, sender).rendered;
    patch.bodyHtml = core.plainTextToSafeHtml(patch.bodyText, tplImages); patch.bodyOverridden = false; }
  await store.patchMessage(who.id, batch.id, message.id, patch, message.etag);
  await store.audit(who.id, batch.id, "individual_message_updated", { messageId: message.id,
    subjectOverridden: patch.subjectOverridden, bodyOverridden: patch.bodyOverridden });
  return validateBatch(who, batch.id);
}

/* Copy specific teammates on ONE message.
 *
 * The batch-level switch copies every teammate of every recipient, which is the
 * right default and the wrong tool for "put Kelly on this one". Chosen here,
 * per message, at the point the rep is reading that message.
 *
 * A TEAMMATE COPIED HERE STOPS RECEIVING THEIR OWN EMAIL. If they are also a
 * recipient of this batch, their message is deleted -- otherwise they get the
 * batch twice, once addressed and once copied. That is the rep's explicit
 * instruction, and the message it removes is named in the response so the
 * screen can say what happened rather than a number quietly changing.
 */
async function updateMessageCc(who, input) {
  const batch = await store.getBatch(who.id, input.batchId);
  const message = batch && await store.getMessage(who.id, batch.id, input.messageId);
  if (!batch || !message) throw httpError(404, "Email message not found.");
  if (!["editing", "invalid"].includes(batch.status))
    throw httpError(409, "This batch can no longer be edited.");

  const wanted = (Array.isArray(input.teammates) ? input.teammates : [])
    .slice(0, MAX_TEAMMATE_CC)
    .map((item) => typeof item === "object"
      ? { crd: String(item.crd || "").trim(),
          email: String(item.email || "").trim().toLowerCase() }
      : { crd: "", email: String(item || "").trim().toLowerCase() })
    .filter((item) => (item.crd || core.validEmail(item.email))
      && item.email !== message.recipientEmail);
  // Suppression applies to a copy exactly as it does to the To line: the
  // unsubscribe footer is signed for the recipient, so somebody copied here
  // could not opt out of a message they never asked for.
  if (!message.contactId && wanted.length) throw httpError(409,
    "A self-test message cannot copy advisor teammates.", "teammate_not_approved");
  const approved = message.contactId
    ? await recipientRegistry.verifyTeammates(message.contactId, wanted, { force: true }) : [];
  if (wanted.length && approved.length !== wanted.length) throw httpError(409,
    "A requested teammate is no longer approved for this advisor.", "teammate_not_approved");
  const blocked = approved.length
    ? await suppress.blockedAmong(approved.map((mate) => ({ email: mate.email, contactId: mate.crd }))) : new Map();
  const selected = approved.filter((mate) => !blocked.has(mate.email));
  const cc = selected.map((mate) => mate.email);

  await store.patchMessage(who.id, batch.id, message.id,
    { teammateCcJson: JSON.stringify(cc),
      teammateCcCrdsJson: JSON.stringify(selected.map((mate) => mate.crd)) }, message.etag);

  // Anyone now copied who was also being written to loses their own message.
  const removed = [];
  for (const other of await store.listMessages(who.id, batch.id)) {
    if (other.id === message.id) continue;
    if (!cc.includes(String(other.recipientEmail || "").toLowerCase())) continue;
    await store.deleteMessage(who.id, batch.id, other.id);
    removed.push(other.recipientName || other.recipientEmail);
  }
  if (removed.length) {
    const left = (await store.listMessages(who.id, batch.id)).length;
    await store.patchBatch(who.id, batch.id, { recipientCount: left },
      (await store.getBatch(who.id, batch.id)).etag);
  }
  const detail = await getBatchDetail(who, batch.id);
  return { ...detail, ccApplied: cc, ccRemovedRecipients: removed,
           ccSuppressed: approved.filter((mate) => blocked.has(mate.email))
             .map((mate) => mate.email) };
}

async function removeRecipient(who, input) {
  const batch = await store.getBatch(who.id, input.batchId);
  const message = batch && await store.getMessage(who.id, batch.id, input.messageId);
  if (!batch || !message) throw httpError(404, "Email message not found.");
  // Only before approval. After that a message may already have a draft in the
  // mailbox, and deleting our record would orphan it rather than remove it.
  if (!["editing", "invalid"].includes(batch.status))
    throw httpError(409, "This batch has been approved; cancel it instead of editing the recipients.");
  if (batch.recipientCount <= 1) throw httpError(400, "A batch needs at least one recipient. Cancel it instead.");
  await store.deleteMessage(who.id, batch.id, message.id);
  const remaining = await store.listMessages(who.id, batch.id);
  const cfg = core.config();
  await store.patchBatch(who.id, batch.id, { recipientCount: remaining.length,
    externalCount: remaining.filter((m) => core.isExternal(m.recipientEmail, cfg)).length }, undefined);
  await store.audit(who.id, batch.id, "recipient_removed",
    { messageId: message.id, recipient: message.recipientEmail });
  return validateBatch(who, batch.id);
}

async function approve(who, input) {
  const mode = input.mode === "send" ? "send" : "drafts";
  const existingBatch = await store.getBatch(who.id, input.batchId);
  if (!existingBatch) throw httpError(404, "Email batch not found.");
  // Approval is itself idempotent. If an HTTP response was lost or queue
  // publishing stopped halfway, repeat only the missing draft work; workers
  // reconcile the application property before any Graph create call.
  if (!["editing", "invalid"].includes(existingBatch.status)) {
    if (existingBatch.mode !== mode) throw httpError(409, "This batch was already approved in a different mode.");
    if (["drafting", "sending", "paused"].includes(existingBatch.status)) {
      // Re-approving an in-flight batch re-queues whatever has not drafted yet,
      // and it has to be paced for the same reason the first approval is: this
      // is the path a rep takes after a throttled batch, so queueing the
      // survivors all at once would re-create the pile-up that stranded them.
      // Position within THIS retry, since the remaining messages are a subset.
      //
      // core.config() locally: this branch returns before the `const cfg` that
      // the main approval path declares further down, so reading that binding
      // here is a temporal dead zone -- a ReferenceError at runtime that
      // node --check cannot see.
      const paceSeconds = core.config().mailboxIntervalSeconds;
      let slot = 0;
      for (const message of await store.listMessages(who.id, existingBatch.id)) {
        if (!["editing", "draft_pending", "draft_ambiguous", "draft_creating"].includes(message.state)) continue;
        if (message.state === "editing") await store.patchMessage(who.id, existingBatch.id, message.id,
          { state: "draft_pending", queuedUtc: message.queuedUtc || new Date().toISOString() }, message.etag);
        await enqueue({ kind: "draft", userId: who.id, batchId: existingBatch.id, messageId: message.id },
          slot * paceSeconds);
        slot += 1;
      }
    }
    return getBatchDetail(who, existingBatch.id);
  }
  const validation = await validateBatch(who, input.batchId,
    { reviewed: input.reviewed === true, identityForce: true });
  const batch = validation.batch;
  if (!input.confirmation || Number(input.confirmation.recipientCount) !== batch.recipientCount)
    throw httpError(400, "Confirm the exact recipient count before approval.");
  const confirmedAttachments = [...new Set(input.confirmation.attachmentIds || [])].map(String).sort();
  const actualAttachments = [...batch.attachmentIds].map(String).sort();
  if (JSON.stringify(confirmedAttachments) !== JSON.stringify(actualAttachments))
    throw httpError(400, "Confirm the exact approved attachment set before approval.");
  if (validation.errors.length) throw httpError(400, `The batch has ${validation.errors.length} validation problem(s).`);
  if (validation.messages.some((m) => !m.reviewed)) throw httpError(400, "Approve the final previews before continuing.");
  const cfg = core.config(), warning = core.guardrail(batch.recipientCount, mode, cfg);
  if (warning.blocked) throw httpError(400, warning.message);

  // The passcode. Its job is to interrupt a reflex, not to withstand an
  // attacker: the person typing it is already signed in and already authorized
  // to send. What it stops is the muscle-memory approval of a 400-person batch
  // that was meant to be a test of three. It is checked AFTER validation so a
  // rep is not asked for a code to approve something that was going to fail
  // anyway, and the attempt counter is per user.
  if (core.passcodeRequired(batch.recipientCount, cfg)) {
    const attempt = await store.passcodeAttempts(who.id);
    if (attempt.lockedOut) throw httpError(429,
      `Too many incorrect passcodes. Approval is locked for ${attempt.minutesRemaining} more minute(s).`,
      "passcode_locked");
    if (!input.passcode) throw httpError(428,
      `Batches over ${cfg.passcodeOver} recipients need the approval passcode.`, "passcode_required");
    if (!core.passcodeMatches(input.passcode, cfg)) {
      const after = await store.recordPasscodeFailure(who.id);
      throw httpError(403, after.lockedOut
        ? "That passcode was incorrect. Approval is now locked for 15 minutes."
        : `That passcode was incorrect. ${after.remaining} attempt(s) left before approval locks.`,
        "passcode_wrong");
    }
    await store.clearPasscodeFailures(who.id);
  }

  // Re-checked at the moment of approval, not just at batch creation. A batch
  // can sit half-edited for an hour, and an opt-out that arrives in that window
  // must still be honoured -- mailing someone who unsubscribed 20 minutes ago is
  // exactly as wrong as mailing someone who unsubscribed last week.
  const nowBlocked = await suppress.blockedAmong(validation.messages
    .map((m) => ({ email: m.recipientEmail, contactId: m.contactId })));
  if (nowBlocked.size) {
    const names = validation.messages.filter((m) => nowBlocked.has(m.recipientEmail))
      .map((m) => `${m.recipientName || m.recipientEmail} (${nowBlocked.get(m.recipientEmail)})`);
    throw httpError(409, `${names.length} recipient(s) must not be emailed `
      + `(${names.slice(0, 4).join("; ")}${names.length > 4 ? "; and more" : ""}). `
      + `Remove them from the batch and approve again.`, "recipient_opted_out");
  }
  const currentPolicy = await store.policy();
  if (mode === "send") {
    if (!cfg.directSendEnvironmentEnabled || currentPolicy.killed) throw httpError(403,
      currentPolicy.reason || "Direct sending is disabled by environment policy or the administrator kill switch.");
    if (cfg.testAllowlist.size && validation.messages.some((m) => !cfg.testAllowlist.has(m.recipientEmail)))
      throw httpError(403, "At least one recipient is outside the configured production test allowlist.");
    await limitGuard.reserve(who.id, batch.id, batch.externalCount, cfg.rollingExternalLimit);
  }
  const approvedUtc = new Date().toISOString();
  const sendNotBeforeUtc = mode === "send" ? new Date(Date.now() + cfg.cancellationSeconds * 1000).toISOString() : "";
  await store.patchBatch(who.id, batch.id, { status: "drafting", mode, approvedUtc,
    reviewedUtc: approvedUtc, sendNotBeforeUtc, warningLevel: warning.level, warningMessage: warning.message }, batch.etag);
  // Send order is decided HERE, once, and stored. Deciding it in the worker
  // would need global knowledge of the batch on every message, and a retry could
  // reshuffle the queue underneath a send already scheduled.
  const order = new Map(core.interleaveByDomain(validation.messages).map((m, i) => [m.id, i]));
  for (const m of validation.messages) {
    const slot = order.get(m.id);
    await store.patchMessage(who.id, batch.id, m.id, { state: "draft_pending", queuedUtc: approvedUtc,
      sendPosition: slot, leaseUntilUtc: "" }, m.etag);
    /* PACED, on the same clock as the send that follows it.
     *
     * Every draft used to be enqueued with no delay, so a batch fanned out to
     * as many simultaneous Graph calls as the platform had instances -- and the
     * DRAFT phase is where the attachment is uploaded. Graph allows roughly four
     * concurrent operations per mailbox: an eleven-recipient batch carrying a
     * PDF sent four and failed seven, some refused outright as
     * ApplicationThrottled / MailboxConcurrency and the rest timing out behind
     * the same wall.
     *
     * mailboxIntervalSeconds already spaced the SENDS; it simply never reached
     * the step doing the expensive work. Reusing the same slot puts both phases
     * on one timeline, which also removes a second pile-up: the send schedule is
     * absolute (sendNotBeforeUtc + slot * interval), so a batch whose drafting
     * ran long had every send slot fall due at once.
     */
    await enqueue({ kind: "draft", userId: who.id, batchId: batch.id, messageId: m.id },
      slot * cfg.mailboxIntervalSeconds);
  }
  await store.audit(who.id, batch.id, mode === "send" ? "direct_send_approved" : "draft_creation_approved",
    { recipientCount: batch.recipientCount, attachmentIds: batch.attachmentIds, sendNotBeforeUtc });
  return getBatchDetail(who, batch.id);
}

async function getBatchDetail(who, batchId) {
  const batch = await store.getBatch(who.id, batchId);
  if (!batch) throw httpError(404, "Email batch not found.");
  const messages = await store.listMessages(who.id, batchId), counts = {};
  for (const m of messages) counts[m.state] = (counts[m.state] || 0) + 1;
  /* The envelope, on EVERY read of a batch -- not only after a review pass.
   *
   * This returned the stored rows untouched, and stored rows carry no cc/bcc:
   * both are COMPUTED. So ticking a teammate saved the address, re-rendered
   * from this function, and Step 3 -- the step whose entire job is "exactly
   * what they receive" -- showed no Cc line at all. The copy was real and
   * invisible, which is the worse of the two ways this can be wrong.
   *
   * Same call, same arguments as the review path, because those two disagreeing
   * about who is on a message is the exact failure extraRecipients exists to
   * prevent. */
  const cfg = core.config();
  const prefs = { copySelf: batch.copySelf, copyInternal: batch.copyInternal,
                  copyInternalTo: batch.copyInternalTo, ccColleague: batch.ccColleague };
  const withCopies = messages.map((m) => ({ ...m,
    ...core.extraRecipients(m, prefs, { mail: batch.senderMail }, cfg) }));
  return { batch, messages: withCopies, counts };
}

async function control(who, input) {
  const batch = await store.getBatch(who.id, input.batchId);
  if (!batch) throw httpError(404, "Email batch not found.");
  if (input.action === "pause") {
    if (batch.mode !== "send") throw httpError(409, "Only a direct-send batch can be paused.");
    await store.patchBatch(who.id, batch.id, { status: "paused", pausedUtc: new Date().toISOString() }, batch.etag);
    await store.audit(who.id, batch.id, "remaining_paused", {});
  } else if (input.action === "cancel") {
    await store.patchBatch(who.id, batch.id, { status: "canceled", canceledUtc: new Date().toISOString() }, batch.etag);
    const messages = await store.listMessages(who.id, batch.id);
    for (const m of messages.filter((x) => !["sent", "submitted"].includes(x.state)))
      await store.patchMessage(who.id, batch.id, m.id, { state: "canceled", leaseUntilUtc: "" }, m.etag);
    await store.audit(who.id, batch.id, "remaining_canceled", {});
  } else if (input.action === "retry") {
    const connection = await auth.status(who.id);
    if (!connection.connected) throw httpError(409, "Reconnect Microsoft 365 before retrying remaining work.");
    const messages = await store.listMessages(who.id, batch.id);
    let nextStatus = batch.mode === "send" ? "sending" : "drafting";
    await store.patchBatch(who.id, batch.id, { status: nextStatus }, batch.etag);
    for (const m of messages.filter((x) => x.state === "auth_required")) {
      const draftPhase = m.failureCode === "auth_required_draft";
      await store.patchMessage(who.id, batch.id, m.id, { state: draftPhase ? "draft_pending" : "send_scheduled",
        failureCode: "", failureMessage: "", leaseUntilUtc: "" }, m.etag);
      await enqueue({ kind: draftPhase ? "draft" : "send", userId: who.id, batchId: batch.id, messageId: m.id });
    }
    await store.audit(who.id, batch.id, "remaining_retried_after_reconnect", {});
  } else if (input.action === "resume") {
    if (batch.status !== "paused") throw httpError(409, "This batch is not paused.");
    await store.patchBatch(who.id, batch.id, { status: "sending", pausedUtc: "" }, batch.etag);
    for (const m of await store.listMessages(who.id, batch.id)) {
      if (!["draft_ready", "send_scheduled"].includes(m.state)) continue;
      if (m.state === "draft_ready") await store.patchMessage(who.id, batch.id, m.id, { state: "send_scheduled" }, m.etag);
      await enqueue({ kind: "send", userId: who.id, batchId: batch.id, messageId: m.id });
    }
    await store.audit(who.id, batch.id, "remaining_resumed", {});
  } else throw httpError(400, "Unknown batch control action.");
  return getBatchDetail(who, batch.id);
}

// One place decides how large an attachment may be, so the upload screen and
// the send-time validator cannot disagree about it.
const attachmentLimit = () => core.config().maxAttachmentBytes;

module.exports = {
  senderHealth, catalog, createBatch, updateCommon, updateMessage, updateMessageCc,
  validateBatch, removeRecipient,
  approve, getBatchDetail, control, enqueue, httpError, attachmentLimit,
  followUpCandidates, createFollowUp, identityPresentationRefresh };
