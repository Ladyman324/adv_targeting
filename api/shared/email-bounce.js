"use strict";

/* Non-delivery report parsing.
 *
 * Kept free of Graph and storage so it can be tested against real NDR text,
 * which is the only way to have any confidence in it: bounce formats are a
 * museum of thirty years of mail servers half-agreeing on RFC 3464, and the
 * cost of getting it wrong is asymmetric. A missed bounce means we mail a dead
 * address again next quarter. A FALSE bounce means we permanently suppress an
 * advisor who is perfectly reachable, silently, and nobody finds out until they
 * ask why they stopped hearing from us.
 *
 * So this errs hard toward "not a bounce" everywhere it is unsure.
 */

// 5.x.x is permanent, 4.x.x is transient. RFC 3463 enhanced status codes, which
// Exchange, Google and every serious MTA emit in the delivery-status part.
const STATUS = /\bStatus:\s*([245])\.(\d{1,3})\.(\d{1,3})/i;
// The bare SMTP reply is the fallback when no enhanced code is present.
const SMTP_REPLY = /\b(5\d{2})[ -]\d\.\d\.\d\b|\bsmtp;\s*(5\d{2})\b/i;

// A permanent failure that is NOT about the address existing. Mailbox full is
// 5.2.2 on some servers and transient in practice; a message too large says
// nothing about the recipient. Suppressing on these would be wrong.
const NOT_THE_ADDRESSES_FAULT = new Set([
  "5.2.2",   // mailbox full
  "5.2.3",   // message too large for the mailbox
  "5.3.4",   // message too big for the system
  "5.7.1",   // refused by policy -- often OUR reputation, not their address
  "5.7.13",  // sender denied
  "5.7.26",  // authentication/DMARC failure at their end
  "5.7.708", // blocked by reputation
]);

const POSTMASTER = /^(postmaster|mailer-daemon|microsoftexchange[0-9a-f]*)@/i;

/** Does this inbox message even look like a delivery report? */
function looksLikeNdr(message) {
  const from = String((((message || {}).from || {}).emailAddress || {}).address || "").toLowerCase();
  const subject = String((message || {}).subject || "");
  const headers = headerMap(message);
  const contentType = String(headers["content-type"] || "").toLowerCase();
  // Any ONE of these is enough to look at it more closely; none of them alone
  // causes a suppression, because classify() still has to find a 5.x.x code.
  return POSTMASTER.test(from)
    || contentType.includes("report-type=delivery-status")
    || !!headers["x-ms-exchange-message-is-ndr"]
    || /^(undeliverable|delivery status notification|returned mail|mail delivery failed)/i.test(subject);
}

function headerMap(message) {
  const out = {};
  for (const h of (message && message.internetMessageHeaders) || [])
    out[String(h.name || "").toLowerCase()] = String(h.value || "");
  return out;
}

/** Every message-id mentioned anywhere in the report. */
function referencedMessageIds(message) {
  const headers = headerMap(message);
  const haystack = [headers.references, headers["in-reply-to"],
                    headers["x-ms-exchange-original-message-id"],
                    bodyText(message)].filter(Boolean).join("\n");
  return [...new Set([...haystack.matchAll(/<([^<>@\s]+@[^<>\s]+)>/g)].map((m) => m[1].toLowerCase()))];
}

function bodyText(message) {
  const body = (message || {}).body || {};
  const raw = String(body.content || "");
  return body.contentType === "html"
    ? raw.replace(/<[^>]+>/g, " ").replace(/&[a-z]+;/gi, " ")
    : raw;
}

/** The address the report says failed, if it names one. */
function failedRecipient(message) {
  const text = bodyText(message);
  const named = text.match(/Final-Recipient:\s*rfc822;\s*([^\s<>]+@[^\s<>]+)/i)
    || text.match(/Original-Recipient:\s*rfc822;\s*([^\s<>]+@[^\s<>]+)/i);
  return named ? named[1].trim().toLowerCase().replace(/[.,;]+$/, "") : "";
}

/**
 * hard   -- the address does not exist or refuses mail permanently. Suppress.
 * soft   -- transient. Ignore entirely; it recovers on its own.
 * policy -- permanent, but not evidence the address is bad. Never suppress.
 * null   -- not a bounce, or not confidently one.
 */
function classify(message) {
  if (!looksLikeNdr(message)) return null;
  const text = bodyText(message);
  const enhanced = text.match(STATUS);
  if (enhanced) {
    const code = `${enhanced[1]}.${Number(enhanced[2])}.${Number(enhanced[3])}`;
    if (enhanced[1] === "4") return { kind: "soft", code };
    if (enhanced[1] === "2") return null;                    // a success report
    return { kind: NOT_THE_ADDRESSES_FAULT.has(code) ? "policy" : "hard", code };
  }
  // No enhanced code. Only a bare 5xx reply is enough, and only then.
  const reply = text.match(SMTP_REPLY);
  if (reply) return { kind: "hard", code: reply[1] || reply[2] };
  // Looked like an NDR but said nothing definite. Deliberately not a bounce:
  // an out-of-office from a strangely named mailbox lands here, and suppressing
  // on it would be a silent, lasting mistake.
  return null;
}

/**
 * What to do about one inbox message, given the messages we sent.
 * `sentByInternetId` maps a lowercased internet message id to our own record.
 */
function assess(message, sentByInternetId) {
  const verdict = classify(message);
  if (!verdict) return { act: false, reason: "not-a-bounce", verdict: null };

  // Resolve the recipient for EVERY kind of report, not only the hard ones.
  //
  // Soft bounces and policy rejections were previously dropped the moment they
  // were classified, so nothing recorded which rep sent them or which domain
  // deferred them -- and those are precisely the signals that show a receiving
  // gateway starting to throttle you, weeks before it turns into refusals.
  // Acting is still reserved for hard bounces; observing is not.
  let ours = null;
  for (const id of referencedMessageIds(message)) {
    const found = sentByInternetId.get(id);
    if (found) { ours = found; break; }
  }
  if (!ours) return { act: false, reason: "unmatched", verdict };

  // The address is taken from OUR record, never from the report. An NDR is
  // attacker-influenced text -- anyone can send us one -- and reading the
  // address out of it would let a stranger suppress an advisor by forging a
  // bounce. The report is only ever used to decide THAT something failed.
  const named = failedRecipient(message);
  if (named && named !== String(ours.recipientEmail || "").toLowerCase())
    return { act: false, reason: "recipient-mismatch", verdict, named };

  const address = String(ours.recipientEmail || "").toLowerCase();
  const domain = address.split("@")[1] || "";
  const common = { verdict, message: ours, address, domain,
                   reason: `${verdict.code}${named ? ` for ${named}` : ""}` };

  // Recorded either way; suppressed only when the address is genuinely dead.
  // ...common last would clobber `reason`, which carries its own.
  return verdict.kind === "hard"
    ? { act: true, record: true, ...common }
    : { ...common, act: false, record: true, reason: verdict.kind,
        detail: verdict.code };
}

module.exports = { looksLikeNdr, classify, assess, referencedMessageIds,
                   failedRecipient, bodyText, headerMap, NOT_THE_ADDRESSES_FAULT };
