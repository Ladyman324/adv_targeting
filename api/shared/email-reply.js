"use strict";

/* Deciding what an inbound message IS, and who it is from.
 *
 * Two separate judgements, kept separate on purpose:
 *
 *   classify()  is this a human reply, an out-of-office, or a bounce?
 *   match()     which message of ours does it answer, and how sure are we?
 *
 * Conflating them is how "advisor replied!" ends up celebrating an Outlook
 * away message, which would cost this feature its credibility faster than any
 * other single mistake it could make.
 *
 * NOTHING IS GUESSED
 * ------------------
 * Every result carries the ROUTE that produced it, the same way phone_kind and
 * city_source do in the pipeline. A caller can then treat a thread match and a
 * sender-only sighting differently, because they are different claims:
 *
 *   thread_match      the message quotes a conversationId we sent on
 *   references_match  In-Reply-To / References names a message-id we sent
 *   sender_only       an advisor wrote to us, unprompted or on a thread we
 *                     have no record of -- REAL activity, but it is NOT a
 *                     reply to a campaign and must never be shown as one
 *
 * Subject matching is deliberately absent. Subjects are reusable and editable;
 * "RE: EIC Strategy" is evidence of nothing, and a confident wrong answer here
 * attributes a reply to a campaign that never earned it.
 */

const AUTO_HEADERS = ["auto-submitted", "x-auto-response-suppress", "x-autoreply",
                      "x-autorespond", "precedence"];

function headerMap(message) {
  const out = {};
  for (const h of (message && message.internetMessageHeaders) || [])
    out[String(h.name || "").toLowerCase()] = String(h.value || "");
  return out;
}

function addressOf(recipient) {
  return String(((recipient || {}).emailAddress || {}).address || "").trim().toLowerCase();
}

function senderOf(message) {
  return addressOf((message || {}).from);
}

/* Everyone the message went to, and in what capacity.
 *
 * The ROLE is kept because it is worth seeing on a timeline -- "copied" and
 * "written to" are different facts about a relationship. It does NOT change
 * whether the contact counts: a rep copying somebody is almost always copying
 * a member of the same practice, and touching the team is the thing that
 * matters. So a cc is real contact, labelled as a cc.
 */
function recipientsOf(message) {
  return [
    ...((message || {}).toRecipients || []).map((r) => ({ address: addressOf(r), role: "to" })),
    ...((message || {}).ccRecipients || []).map((r) => ({ address: addressOf(r), role: "cc" })),
  ].filter((r) => r.address);
}

/* Strip the angle brackets Internet message ids are written with, so
 * "<abc@host>" and "abc@host" compare equal. Every id this module handles goes
 * through here, on both sides of every comparison. */
function bareId(value) {
  return String(value || "").trim().replace(/^<|>$/g, "").toLowerCase();
}

/** Every message-id an inbound message says it is answering. */
function referencedIds(message) {
  const headers = headerMap(message);
  const ids = [];
  for (const key of ["in-reply-to", "references"]) {
    for (const found of String(headers[key] || "").matchAll(/<([^>]+)>/g)) ids.push(bareId(found[1]));
    // A References header without brackets is malformed but does happen.
    if (!/[<>]/.test(headers[key] || "") && headers[key]) ids.push(bareId(headers[key]));
  }
  return [...new Set(ids.filter(Boolean))];
}

/* reply | auto_reply | bounce | unknown
 *
 * `bounce` here means only "this looks like a delivery report, not a person".
 * It is deliberately NOT the bounce sweeper's verdict: that module decides
 * whether an address is genuinely dead, which is a permanent and destructive
 * call, and it must go on owning it alone. All this does is keep an NDR out of
 * the reply count.
 */
function classify(message, isNdr) {
  if (isNdr) return "bounce";
  const headers = headerMap(message);

  // RFC 3834. "auto-replied" and "auto-generated" are both machines; "no" is an
  // explicit statement that a human wrote it.
  const submitted = String(headers["auto-submitted"] || "").toLowerCase();
  if (submitted && submitted !== "no") return "auto_reply";

  // Exchange sets this on its own OOF messages.
  if (headers["x-auto-response-suppress"]) return "auto_reply";
  if (headers["x-autoreply"] || headers["x-autorespond"]) return "auto_reply";
  if (/^(auto_reply|bulk|list|junk)$/i.test(String(headers.precedence || "").trim())) return "auto_reply";

  const subject = String((message || {}).subject || "");
  if (/^\s*(automatic reply|out of office|autoreply|auto:)/i.test(subject)) return "auto_reply";

  return "reply";
}

/* Which of our sent messages does this answer?
 *
 * `sent` is the index from store.sentByInternetId(): internetMessageId -> our
 * message row. `byConversation` maps conversationId -> the same rows.
 *
 * Order matters. conversationId is the strongest signal Graph gives us and
 * survives subject edits and forwards; References is the standards-based
 * fallback for clients that break threading; sender identity alone is last and
 * claims the least.
 */
function match(message, { sent, byConversation }) {
  const conversationId = String((message || {}).conversationId || "");
  if (conversationId && byConversation && byConversation.has(conversationId))
    return { route: "thread_match", message: byConversation.get(conversationId) };

  for (const id of referencedIds(message)) {
    if (sent && sent.has(id)) return { route: "references_match", message: sent.get(id) };
  }
  return { route: "sender_only", message: null };
}

/* Should this message be recorded at all, and as what?
 *
 * Returns null for anything that is not ours to keep -- which is the majority
 * of a rep's mailbox and the reason this function exists. A null here is a
 * message that leaves no trace anywhere: not stored, not logged, not counted
 * beyond an anonymous tally.
 */
function assess(message, { lookup, sent, byConversation, mailbox, isNdr }) {
  const from = senderOf(message);
  const self = String(mailbox || "").trim().toLowerCase();

  /* The rep's own outbound.
   *
   * Where it came FROM matters. Every message the emailer creates carries
   * X-EIC-Message-Id, so a send it made is identifiable here and is labelled as
   * ours rather than as manual Outlook correspondence. Without that a scheduled
   * campaign would appear on the advisor's timeline as though the rep had typed
   * it personally, which is a different fact about the relationship and the one
   * a rep would act on.
   *
   * The header is used rather than the EICMessageId extended property because
   * headers are already in this message -- the sweep selects
   * internetMessageHeaders for auto-reply detection -- while the extended
   * property would need its own $expand on every message read.
   */
  if (from && self && from === self) {
    const advisors = recipientsOf(message)
      .map((r) => ({ address: r.address, role: r.role, who: lookup(r.address) }))
      // "internal" is excluded alongside "unknown": a colleague on a message is
      // not advisor activity, and recording it would put internal
      // correspondence on a firm-wide timeline.
      .filter((r) => r.who.kind !== "unknown" && r.who.kind !== "internal");
    if (!advisors.length) return null;
    const appMessageId = String(headerMap(message)["x-eic-message-id"] || "").trim();
    return { direction: "outbound", from, advisors, classification: "sent",
             route: "own_mailbox",
             source: appMessageId ? "app" : "outlook",
             appMessageId,
             conversationId: String(message.conversationId || ""),
             internetMessageId: bareId(message.internetMessageId) };
  }

  const who = lookup(from);
  if (who.kind === "unknown" || who.kind === "internal") return null;

  const matched = match(message, { sent, byConversation });
  return {
    direction: "inbound",
    from,
    who,
    classification: classify(message, isNdr),
    route: matched.route,
    answers: matched.message,
    conversationId: String((message || {}).conversationId || ""),
    internetMessageId: bareId((message || {}).internetMessageId),
    receivedAt: String((message || {}).receivedDateTime || ""),
    subject: String((message || {}).subject || "").slice(0, 400),
  };
}

module.exports = { classify, match, assess, referencedIds, senderOf, recipientsOf,
                   headerMap, bareId, addressOf };
