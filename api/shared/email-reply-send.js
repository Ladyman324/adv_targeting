"use strict";

/* One-to-one mail to an advisor: replying to something they sent, and starting
 * a new conversation with somebody who has gone quiet.
 *
 * This is NOT the campaign path. There is no template, no list, no batch, no
 * pacing -- a rep is writing to one person, usually because the queue told them
 * to. What it does share with the campaign path is every rule that protects the
 * advisor and the firm, because a rule that applies to a hundred recipients and
 * not to one is not a rule.
 *
 * WHY REPLY USES GRAPH createReply
 * --------------------------------
 * Because it keeps the CONVERSATION. A fresh message with "RE:" prefixed looks
 * like a reply to a human and is a new thread to every mail system involved,
 * which quietly costs us the strongest matching route we have: when the advisor
 * answers, `conversationId` no longer leads anywhere and the sweep falls back
 * to references or sender-only. Building it the obvious way would degrade the
 * feature it sits on top of.
 *
 * FOLLOW-UP IS DELIBERATELY A NEW CONVERSATION
 * -------------------------------------------
 * The queue produces `quiet_warm` -- somebody who answered once and has gone
 * quiet since. Reviving that on the old thread would make the advisor read a
 * conversation from three months ago to work out why we are writing. A new
 * subject is the honest shape, and it is a new relationship event rather than a
 * continuation of a finished one.
 *
 * ATTACHMENTS, AND WHY THEY ARE SAFE HERE
 * ---------------------------------------
 * Two sources: the approved document library, and a file off the rep's own
 * device. The second never touches our storage -- the bytes arrive on the
 * request, go onto the draft, and are gone.
 *
 * What makes that acceptable is the compliance blind copy, which fires on ANY
 * attachment to an external recipient and is computed by the same
 * core.complianceBcc() the campaign worker uses. Material reaching an advisor
 * reaches the compliance mailbox by the same mechanism whether it was sent to
 * one person or a hundred.
 *
 * NOT A MAIL CLIENT
 * -----------------
 * No folders, no categories, no search, no forwarding, no rules. Those exist
 * one tap away behind `Open in Outlook`, and the moment this needs them the
 * honest move is to send the rep there rather than reimplement them badly.
 */

const graph = require("./graph-mail");
const store = require("./email-store");
const auth = require("./email-auth");
const core = require("./email-core");
const suppress = require("./email-suppress");
const advisors = require("./advisor-lookup");

// Long enough for a real answer, short enough that nobody drafts a memo in a
// box with no formatting and no autosave.
const MAX_CHARS = 5000;
const MAX_SUBJECT = 200;
// Per message, across both sources. Graph and Exchange have their own ceilings;
// this is about keeping a phone on a bad connection from trying to push 40 MB.
const MAX_FILES = 5;

function httpError(status, message, code) {
  const err = new Error(message);
  err.statusCode = status;
  if (code) err.code = code;
  return err;
}

/* Plain text from the rep -> the HTML Graph wants.
 *
 * Escaped, not sanitised. The composer is a plain textarea: there is no
 * formatting to preserve, so every angle bracket the rep typed is literal text
 * and nothing they can type becomes markup in the advisor's client.
 */
function textToHtml(text) {
  const escaped = String(text || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const paragraphs = escaped.split(/\n{2,}/).map((block) =>
    `<p>${block.replace(/\n/g, "<br>")}</p>`).join("");
  return `<div>${paragraphs}</div>`;
}

/* Resolve what the rep asked to attach into something Graph can take.
 *
 * Approved documents are looked up by id and never trusted from the request --
 * a client that could name a size or a blob could name somebody else's.
 * Uploaded files are decoded and measured here, so the size limit is enforced
 * on the bytes we actually received rather than on what the browser claimed.
 */
async function resolveAttachments(input, deps) {
  const st = deps.store || store;
  const cfg = (deps.core || core).config();
  const maxBytes = Number(cfg.maxAttachmentBytes) || 10 * 1024 * 1024;

  const ids = (input.documentIds || []).map(String).filter(Boolean);
  const documents = ids.length ? await st.getDocuments(ids) : [];
  const missing = ids.filter((id) => !documents.some((d) => String(d.id) === id));
  if (missing.length) throw httpError(400, "An approved document in this message no longer exists.", "no_such_document");

  const files = [];
  for (const raw of input.files || []) {
    const bytes = Buffer.from(String(raw.data || ""), "base64");
    if (!bytes.length) continue;
    files.push({ name: String(raw.name || "attachment").slice(0, 200),
                 contentType: String(raw.contentType || "application/octet-stream").slice(0, 120),
                 bytes, size: bytes.length });
  }

  if (documents.length + files.length > MAX_FILES) {
    throw httpError(400, `At most ${MAX_FILES} attachments on a message from here. `
      + "Use Outlook for more.", "too_many_files");
  }
  const total = documents.reduce((n, d) => n + Number(d.size || 0), 0)
    + files.reduce((n, f) => n + f.size, 0);
  if (total > maxBytes) {
    throw httpError(400, `Attachments here are limited to ${Math.round(maxBytes / 1e6)} MB in total. `
      + "Use Outlook for anything larger.", "too_large");
  }
  return { documents, files, total };
}

/* Everything that must be true before an advisor is emailed from here.
 *
 * SUPPRESSION IS NOT THE SAME QUESTION FOR A REPLY AS FOR A CAMPAIGN, and
 * treating it as one was wrong.
 *
 * A suppression means "do not send this address marketing". Answering a message
 * somebody sent US -- or replying on a thread they were already a participant
 * in -- is not marketing; it is ordinary correspondence, and refusing to answer
 * an advisor because they once unsubscribed from a campaign is both unhelpful
 * and slightly rude.
 *
 * So the rule depends on who started it:
 *
 *   reply / reply-all   ALLOWED to a suppressed address. They wrote to us, or
 *                       they were on the thread. Surfaced to the rep, never
 *                       silent -- they should know, they just should not be
 *                       stopped.
 *   follow-up           BLOCKED. That is us INITIATING contact with somebody
 *                       who asked us not to, which is exactly what the
 *                       suppression list is for.
 *
 * Returns the address plus whether it is suppressed, so the caller can tell the
 * rep rather than quietly deciding for them.
 */
/* Refuse anything aimed at one of our own people.
 *
 * They are on the map because they are registered reps, not because they are
 * prospects. Mailing a colleague through the advisor tooling would put internal
 * correspondence on a firm-wide timeline; Outlook is the right place for it.
 */
async function refuseInternal(crd, deps) {
  const ad = deps.advisors || advisors;
  let internal = false;
  try { internal = await ad.isInternalCrd(crd); } catch { internal = false; }
  if (internal) {
    throw httpError(409, "That is a colleague rather than a prospect, so this tool does not "
      + "email them. Use Outlook.", "internal_contact");
  }
}

async function guard(to, deps, { initiating }) {
  const sp = deps.suppress || suppress;
  const address = String(to || "").trim().toLowerCase();
  if (!address) throw httpError(400, "No recipient address on that message.");
  const blocked = await sp.blockedAmong([address]);
  const suppressed = !!(blocked && blocked.size && blocked.get(address));
  if (suppressed && initiating) {
    throw httpError(409, "That address is on the suppression list, so a new "
      + "conversation cannot be started with it. Replying to a message they sent "
      + "is still allowed.", "suppressed");
  }
  return { address, suppressed };
}

/* Who a reply-all will actually reach.
 *
 * Reply-all pulls in every To and Cc from the original, and those are not
 * necessarily advisors -- an assistant, the advisor's own client, somebody
 * internal to their firm. A rep pressing Reply All in our app cannot see that
 * list the way they would in Outlook, so it is resolved and returned for them
 * to look at.
 *
 * Disclosure rather than refusal. Blocking on a suppressed participant would
 * stop a legitimate answer to a thread they are already on; showing the rep
 * exactly who is on it lets them decide, which is what Outlook would have done.
 */
async function replyAllAudience(original, deps) {
  const sp = deps.suppress || suppress;
  const seen = new Set();
  const people = [];
  const add = (entry, role) => {
    const address = String(((entry || {}).emailAddress || {}).address || "").trim().toLowerCase();
    if (!address || seen.has(address)) return;
    seen.add(address);
    people.push({ address, role, name: ((entry || {}).emailAddress || {}).name || "" });
  };
  add(original.from, "from");
  for (const r of original.toRecipients || []) add(r, "to");
  for (const r of original.ccRecipients || []) add(r, "cc");
  if (!people.length) return [];
  const blocked = await sp.blockedAmong(people.map((p) => p.address));
  return people.map((p) => ({ ...p, suppressed: !!(blocked && blocked.get(p.address)) }));
}

/* The compliance blind copy, from the ONE function that decides it.
 *
 * Recomputed here rather than trusted from anywhere, and given the REAL
 * attachment list -- core.complianceBcc() fires only when there is material and
 * the recipient is external, so handing it an empty array (as an earlier
 * version of this file did) silently disables the obligation.
 */
async function applyCompliance(token, draftId, to, attachments, deps) {
  const cr = deps.core || core;
  const gr = deps.graph || graph;
  const bcc = cr.complianceBcc({ recipientEmail: to, attachments });
  if (!bcc.length) return [];
  await gr.request(token.accessToken, "PATCH", `/me/messages/${encodeURIComponent(draftId)}`,
    { bccRecipients: bcc.map((address) => ({ emailAddress: { address } })) });
  return bcc;
}

async function attachAll(token, draftId, resolved, deps) {
  const gr = deps.graph || graph;
  if (resolved.documents.length) await gr.attachDocuments(token.accessToken, draftId, resolved.documents);
  if (resolved.files.length) await gr.attachFiles(token.accessToken, draftId, resolved.files);
}

/* Has this exact send already happened?
 *
 * THE FAILURE THIS CLOSES: the API sent the draft, Graph accepted it, and the
 * response was lost. The client sees an error, the rep presses Send again, and
 * the advisor gets the message twice. There is no undo on a sent email, and the
 * disabled button only protects within one page -- a reload defeats it.
 *
 * The campaign worker already solved this: stamp OUR id on the message, and
 * before doing anything, ask Graph whether a message carrying that id exists.
 * `operationId` is generated by the CLIENT and sent with the request, so a
 * retry carries the same one and can be recognised as the same intent.
 *
 * Returns the already-sent message when there is one, so the caller can report
 * success instead of sending a second copy.
 */
/* Put our operation id on the draft, so alreadySent() can find it later.
 *
 * createDraft() does this at creation time, but a REPLY draft is made by Graph
 * from the original message and carries none of our properties -- so it is
 * stamped here instead, after the body and attachments are on and immediately
 * before the send.
 *
 * Best effort. A failure here costs the retry check for this one message, and
 * is a much smaller problem than refusing to let a rep answer an advisor.
 */
async function stampOperation(token, draftId, operationId, deps) {
  if (!operationId) return;
  const gr = deps.graph || graph;
  try {
    await gr.request(token.accessToken, "PATCH", `/me/messages/${encodeURIComponent(draftId)}`, {
      singleValueExtendedProperties: [{ id: gr.APP_PROPERTY_ID, value: String(operationId) }],
    });
  } catch { /* see above */ }
}

async function alreadySent(token, operationId, deps) {
  if (!operationId) return null;
  const gr = deps.graph || graph;
  try {
    const found = await gr.findByAppId(token.accessToken, operationId);
    return found && !found.isDraft ? found : null;
  } catch {
    // A failed lookup must not block a legitimate first send. The cost of being
    // wrong here is one duplicate in a rare double failure; the cost of
    // throwing is that nobody can reply while Graph is unhappy.
    return null;
  }
}

function signatureFor(token, deps) {
  const cr = deps.core || core;
  return cr.corporateSignature ? cr.corporateSignature(token.profile || {}) : "";
}

/* Reply to a message already on this advisor's timeline. */
async function reply(who, input, deps = {}) {
  const gr = deps.graph || graph;
  const st = deps.store || store;
  const au = deps.auth || auth;

  const crd = String(input.crd || "").trim();
  const messageId = String(input.id || "").trim();
  const body = String(input.text || "");
  if (!crd || !messageId) throw httpError(400, "An advisor and a message are required.");
  if (!body.trim()) throw httpError(400, "The reply is empty.");
  if (body.length > MAX_CHARS) {
    throw httpError(400, `A message from here is limited to ${MAX_CHARS} characters. `
      + "Open it in Outlook for anything longer.", "too_long");
  }

  await refuseInternal(crd, deps);

  // Ownership before anything is created. Graph would refuse a message in
  // another mailbox anyway, but its refusal is a 404 that reads as "deleted".
  const owner = await st.activityOwner(crd, messageId);
  if (!owner) throw httpError(404, "That message is not in this advisor's activity.", "no_such_activity");
  if (String(owner) !== String(who.id || "")) {
    throw httpError(403, "That message is in another rep's mailbox.", "not_your_mailbox");
  }

  const token = await au.tokenFor(who.id);

  // Before anything: did a previous attempt already send this?
  const done = await alreadySent(token, input.operationId, deps);
  if (done) return { ok: true, alreadySent: true, to: "", conversationId: "" };

  const original = await gr.getMessageContent(token.accessToken, messageId);
  // Replying is RESPONSIVE, not initiating, so a suppressed address does not
  // block it -- see guard(). The rep is told, and decides.
  const { address: to, suppressed } = await guard(
    ((original.from || {}).emailAddress || {}).address, deps, { initiating: false });
  const resolved = await resolveAttachments(input, deps);

  const draft = await gr.createReply(token.accessToken, messageId, input.replyAll === true);
  if (!draft || !draft.id) throw httpError(502, "Microsoft did not return a reply draft.");

  await gr.updateDraftBody(token.accessToken, draft.id, textToHtml(body) + signatureFor(token, deps));
  await attachAll(token, draft.id, resolved, deps);
  const bcc = await applyCompliance(token, draft.id, to,
    [...resolved.documents, ...resolved.files], deps);
  await stampOperation(token, draft.id, input.operationId, deps);
  await gr.sendDraft(token.accessToken, draft.id);

  /* Recorded immediately rather than waiting for the sweep.
   *
   * The sweep would find it within fifteen minutes, but a rep who has just
   * pressed Send and sees no change concludes it did not work and sends again.
   * `recordActivity` is keyed on the Graph message id, so when the sweep sees
   * the same message it upserts over this row rather than adding a second.
   */
  await st.recordActivity({
    userId: who.id, direction: "outbound", source: "app_reply",
    classification: "sent", route: "own_mailbox", recipientRole: "to",
    advisorCrd: crd, advisorEmail: to, occurredAt: new Date().toISOString(),
    subject: original.subject || "", conversationId: original.conversationId || "",
    graphMessageId: draft.id,
  });


  /* Advance the queue immediately.
   *
   * Without this the timeline updated but "Needs attention" went on saying the
   * advisor needed a reply that had just been sent -- for up to fifteen minutes,
   * until a sweep happened to touch them. A rep who has just answered somebody
   * and still sees them at the top of the queue stops believing the queue.
   *
   * Best effort: the message is already sent, and a stale projection is a
   * nuisance where a thrown error here would look like a failed send.
   */
  try {
    const eng = deps.engagement || require("./email-engagement");
    await eng.refresh(who.id, crd, { store: st });
    await eng.setReplyState(who.id, crd, "reviewed", { store: st });
  } catch { /* see above */ }

  return { ok: true, to, suppressed, conversationId: original.conversationId || "",
           attachments: resolved.documents.length + resolved.files.length,
           complianceCopied: bcc.length > 0 };
}

/* Start a NEW conversation with an advisor.
 *
 * Blank sheet: no template, no approved subject line. This is the message a rep
 * writes because somebody has gone quiet, and a templated re-engagement reads
 * exactly like what it is. The signature still comes through, because that is
 * firm identity rather than content.
 */
async function followUp(who, input, deps = {}) {
  const gr = deps.graph || graph;
  const st = deps.store || store;
  const au = deps.auth || auth;

  const crd = String(input.crd || "").trim();
  const subject = String(input.subject || "").trim();
  const body = String(input.text || "");
  if (!crd) throw httpError(400, "An advisor is required.");
  if (!subject) throw httpError(400, "A subject is required.");
  if (subject.length > MAX_SUBJECT) throw httpError(400, `The subject is limited to ${MAX_SUBJECT} characters.`);
  if (!body.trim()) throw httpError(400, "The message is empty.");
  if (body.length > MAX_CHARS) {
    throw httpError(400, `A message from here is limited to ${MAX_CHARS} characters. `
      + "Use Outlook for anything longer.", "too_long");
  }

  /* The address is resolved SERVER-SIDE from the advisor universe.
   *
   * The client names a CRD and never an address, so there is nothing to verify
   * and no way to turn this endpoint into a relay for arbitrary mail out of a
   * rep's mailbox. That property is the whole reason it works this way.
   *
   * It used to come from the ACTIVITY LOG, which has the same security
   * property and one fatal flaw: the log is empty until the sweep has observed
   * somebody, so on day one a follow-up could reach nobody at all. A rep got
   * "no email address has been observed for this advisor" -- which described
   * the symptom and blamed the advisor, when the cause was that the code had
   * nowhere else to look.
   *
   * The log is still consulted as a fallback, and it is worth keeping: an
   * advisor who wrote to us from an address the pipeline does not hold is
   * reachable at the address they actually used.
   */
  await refuseInternal(crd, deps);

  const ad = deps.advisors || advisors;
  let target = "";
  try {
    target = await ad.emailForCrd(crd);
  } catch { /* the fallback below is the point */ }
  if (!target) {
    const known = await st.listActivity(crd, 200);
    const mine = known.filter((e) => String(e.userId || "") === String(who.id || "") && e.advisorEmail);
    target = (mine[0] || known.find((e) => e.advisorEmail) || {}).advisorEmail || "";
  }
  if (!target) {
    throw httpError(409, "We hold no email address for this advisor, so a follow-up cannot be "
      + "sent from here. Open them in Outlook if you have an address for them.", "no_known_address");
  }

  const token = await au.tokenFor(who.id);

  const done = await alreadySent(token, input.operationId, deps);
  if (done) return { ok: true, alreadySent: true, to: "", conversationId: "" };

  // A follow-up INITIATES contact, so suppression blocks it. That is the whole
  // difference between this and a reply.
  const { address: to } = await guard(target, deps, { initiating: true });
  const resolved = await resolveAttachments(input, deps);

  const draft = await gr.createDraft(token.accessToken, {
    // createDraft stamps this as both the EICMessageId extended property and an
    // X-EIC-Message-Id header, which is what makes the retry check above work.
    id: String(input.operationId || `followup-${Date.now()}-${crd}`),
    subject, bodyHtml: textToHtml(body), signatureHtml: signatureFor(token, deps),
    recipientEmail: to, recipientName: input.name || "",
  });
  if (!draft || !draft.id) throw httpError(502, "Microsoft did not return a draft.");

  await attachAll(token, draft.id, resolved, deps);
  const bcc = await applyCompliance(token, draft.id, to,
    [...resolved.documents, ...resolved.files], deps);
  await gr.sendDraft(token.accessToken, draft.id);

  await st.recordActivity({
    userId: who.id, direction: "outbound", source: "app_followup",
    classification: "sent", route: "own_mailbox", recipientRole: "to",
    advisorCrd: crd, advisorEmail: to, occurredAt: new Date().toISOString(),
    subject, conversationId: draft.conversationId || "", graphMessageId: draft.id,
  });


  /* Advance the queue immediately.
   *
   * Without this the timeline updated but "Needs attention" went on saying the
   * advisor needed a reply that had just been sent -- for up to fifteen minutes,
   * until a sweep happened to touch them. A rep who has just answered somebody
   * and still sees them at the top of the queue stops believing the queue.
   *
   * Best effort: the message is already sent, and a stale projection is a
   * nuisance where a thrown error here would look like a failed send.
   */
  try {
    const eng = deps.engagement || require("./email-engagement");
    await eng.refresh(who.id, crd, { store: st });
    await eng.setReplyState(who.id, crd, "reviewed", { store: st });
  } catch { /* see above */ }

  return { ok: true, to, conversationId: draft.conversationId || "",
           attachments: resolved.documents.length + resolved.files.length,
           complianceCopied: bcc.length > 0 };
}

/* The route's view of replyAllAudience: ownership-checked, then resolved.
 *
 * Same rule as reading a message -- a rep may only ask about mail in their own
 * mailbox -- and the same reason for checking it here rather than letting Graph
 * refuse: a 404 from Graph reads as "deleted".
 */
async function audienceFor(who, { crd, id }, deps = {}) {
  const st = deps.store || store;
  const gr = deps.graph || graph;
  const au = deps.auth || auth;
  const advisorCrd = String(crd || "").trim();
  const messageId = String(id || "").trim();
  if (!advisorCrd || !messageId) throw httpError(400, "An advisor and a message are required.");

  const owner = await st.activityOwner(advisorCrd, messageId);
  if (!owner) throw httpError(404, "That message is not in this advisor's activity.", "no_such_activity");
  if (String(owner) !== String(who.id || "")) {
    throw httpError(403, "That message is in another rep's mailbox.", "not_your_mailbox");
  }
  const token = await au.tokenFor(who.id);
  const original = await gr.getMessageContent(token.accessToken, messageId);
  const people = await replyAllAudience(original, deps);
  return {
    // The sender alone is who a plain Reply reaches; everyone is who Reply All
    // reaches. Both are returned so the client can label the choice honestly.
    reply: people.filter((p) => p.role === "from"),
    replyAll: people,
    suppressedCount: people.filter((p) => p.suppressed).length,
  };
}

module.exports = { reply, followUp, replyAllAudience, audienceFor, textToHtml,
                   resolveAttachments, guard, stampOperation, alreadySent,
                   MAX_CHARS, MAX_SUBJECT, MAX_FILES };
