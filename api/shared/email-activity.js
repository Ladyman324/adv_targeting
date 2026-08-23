"use strict";

/* The READ side of the advisor activity log.
 *
 * The sweep writes relationship events; this turns them into something a rep
 * can look at before a call, and fetches the actual message from Exchange only
 * when they ask for it.
 *
 * WHOSE ACTIVITY A REP CAN SEE
 * ----------------------------
 * The TIMELINE is shared, like /api/flags and /api/dnc. That two advisors at
 * one firm were emailed by Kate last week is exactly the sort of thing the next
 * rep needs to know before mailing them again, and hiding it is how two people
 * work the same advisor in the same week.
 *
 * The CONTENT is not shared. Reading a message needs the mailbox it lives in,
 * and each rep's Graph token reaches only their own -- so Microsoft enforces
 * this whatever we do. It is ALSO checked here, before the call, because a
 * refusal a rep can understand ("that message is in Kate's mailbox") beats a
 * 404 from Graph that looks like the message was deleted.
 *
 * WHAT THE TIMELINE MUST NEVER SAY
 * --------------------------------
 * `route` travels with every row and decides the wording. A `sender_only`
 * sighting means an advisor wrote to us; it does NOT mean they answered a
 * campaign, and rendering it as one would credit a campaign that never earned
 * it. `classification` does the same job for auto-replies: an out-of-office is
 * shown as an out-of-office, never counted as a reply.
 */

const graph = require("./graph-mail");
const store = require("./email-store");
const auth = require("./email-auth");
const advisors = require("./advisor-lookup");

function httpError(status, message, code) {
  const err = new Error(message);
  err.statusCode = status;
  if (code) err.code = code;
  return err;
}

/* How a row should read on screen. Deliberately computed once, here, rather
 * than in the client: two apps render this and the desk and the phone must not
 * disagree about whether something was a reply. */
function label(entry) {
  const route = String(entry.route || "");
  const classification = String(entry.classification || "");
  if (entry.direction === "outbound") {
    return entry.source === "outlook" ? "Email sent (Outlook)" : "Email sent";
  }
  if (classification === "auto_reply") return "Automatic reply";
  if (classification === "bounce") return "Delivery failure";
  if (route === "thread_match" || route === "references_match") return "Reply received";
  // An advisor wrote to us on a thread we have no record of, or out of the
  // blue. Real, valuable, and NOT a reply to anything we sent.
  return "Email received";
}

/* Why we believe the row, in words a rep can act on. Shown as a tooltip rather
 * than a badge: it matters when somebody is deciding whether to trust a link to
 * a campaign, and is noise the rest of the time. */
function basis(entry) {
  switch (String(entry.route || "")) {
    case "thread_match": return "Matched to the message it replies to by conversation.";
    case "references_match": return "Matched by the message id it references.";
    case "own_mailbox": return "Sent from this rep's mailbox.";
    case "sender_only":
      return entry.advisorCrd
        ? "From a known advisor address, but not on a thread we sent. Not linked to a campaign."
        : "From an address several advisors share, or one we cannot attribute to a person.";
    default: return "";
  }
}

async function timeline(who, crd, deps = {}) {
  const st = deps.store || store;
  const advisorCrd = String(crd || "").trim();
  if (!advisorCrd) throw httpError(400, "An advisor CRD is required.");

  /* OUR OWN PEOPLE ARE NOT TRACKED, and the UI is told so explicitly.
   *
   * The timeline is firm-wide. Showing a colleague's correspondence on it would
   * let every rep read when the others emailed each other, which is nobody's
   * business and is far more likely to be sensitive than anything an advisor
   * sends us. Their addresses are already excluded from the lookup, so there is
   * nothing to show -- but an unexplained empty list looks like a bug, and
   * somebody would eventually "fix" it.
   */
  const ad = deps.advisors || advisors;
  let internal = false;
  try { internal = await ad.isInternalCrd(advisorCrd); } catch { internal = false; }
  if (internal) {
    return { crd: advisorCrd, entries: [], count: 0, observed: false, internal: true };
  }

  const rows = await st.listActivity(advisorCrd, Number(deps.limit) || 200);
  const entries = rows.map((e) => ({
    id: e.graphMessageId || "",
    direction: e.direction || "",
    source: e.source || "",
    classification: e.classification || "",
    route: e.route || "",
    label: label(e),
    basis: basis(e),
    occurredAt: e.occurredAt || "",
    subject: e.subject || "",
    advisorEmail: e.advisorEmail || "",
    advisorCrd: e.advisorCrd || "",
    firmCrd: e.firmCrd || "",
    batchId: e.batchId || "",
    // Whether THIS rep can open the message. See the module docstring: the
    // timeline is shared, the content is not.
    mine: String(e.userId || "") === String(who.id || ""),
    ambiguous: !e.advisorCrd,
  }));

  return {
    crd: advisorCrd,
    entries,
    count: entries.length,
    /* Deliberately NOT "no activity". The sweep only sees mailboxes that are
     * connected, and only since it was switched on, so an empty timeline means
     * we have not observed anything -- which is a different claim from nothing
     * having happened, and the UI must not upgrade one into the other. */
    observed: entries.length > 0,
  };
}

async function messageContent(who, { crd, id: rawId }, deps = {}) {
  const st = deps.store || store;
  const gr = deps.graph || graph;
  const au = deps.auth || auth;

  const id = String(rawId || "").trim();
  const advisorCrd = String(crd || "").trim();
  if (!id) throw httpError(400, "A message id is required.");
  if (!advisorCrd) throw httpError(400, "An advisor CRD is required.");

  /* Ownership, before the network call.
   *
   * Graph would refuse anyway -- a rep's delegated token cannot reach another
   * mailbox -- but its refusal is a 404 that reads as "the message was
   * deleted", and a rep chasing a missing email deserves the real reason.
   *
   * Scoped by advisor rather than looked up by message id alone: the activity
   * table is partitioned by advisor, so this is one small partition read. A
   * global lookup would be a table scan on every click.
   */
  const owner = await st.activityOwner(advisorCrd, id);
  if (!owner) throw httpError(404, "That message is not in this advisor's activity.", "no_such_activity");
  if (String(owner) !== String(who.id || "")) {
    throw httpError(403, "That message is in another rep's mailbox. Ask them to forward it, or open it from their account.",
                    "not_your_mailbox");
  }

  const token = await au.tokenFor(who.id);
  const message = await gr.getMessageContent(token.accessToken, id);
  const from = ((message.from || {}).emailAddress || {}).address || "";
  return {
    id: message.id || id,
    subject: message.subject || "",
    from,
    // Whether the rep sent this themselves. The composer is offered only on
    // INBOUND mail -- replying to our own sent message would mail ourselves,
    // and the button being there at all would suggest otherwise.
    isOwn: !!from && from.toLowerCase() === String(token.mailbox || "").toLowerCase(),
    fromName: ((message.from || {}).emailAddress || {}).name || "",
    to: (message.toRecipients || []).map((r) => ((r || {}).emailAddress || {}).address).filter(Boolean),
    cc: (message.ccRecipients || []).map((r) => ((r || {}).emailAddress || {}).address).filter(Boolean),
    sentAt: message.sentDateTime || "",
    receivedAt: message.receivedDateTime || "",
    conversationId: message.conversationId || "",
    // Plain text by request. Nothing here is markup, so nothing here can become
    // script when the client puts it on the page.
    text: String(((message.uniqueBody || {}).content) || "").trim(),
    webLink: message.webLink || "",
  };
}

module.exports = { timeline, messageContent, label, basis };
