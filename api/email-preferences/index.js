/* /api/email-preferences — the unsubscribe page reached from the email footer.
 *
 * ANONYMOUS by necessity: the people who click it are advisors outside the firm
 * who have no account here and never will.
 *
 * The GET NEVER suppresses. It renders a page with a button, and only the POST
 * writes. This is not politeness -- corporate mail gateways (Mimecast, Proofpoint,
 * Defender for Office, and the scanners at exactly the wirehouses this app mails)
 * pre-fetch every link in an inbound message to check it for malware. A GET that
 * acted would unsubscribe a large share of every batch before a human read a
 * word of it, and the damage would be invisible: the suppressions would look
 * like genuine opt-outs.
 *
 * The token in the URL names the address and is signed. The page never accepts
 * an address as input, so this endpoint cannot be used to suppress a third party
 * or to probe whether a given address is on our list.
 */
"use strict";

const suppress = require("../shared/email-suppress");
const act = require("../shared/act");

function page(context, title, message, { token = "", showButton = false, status = 200 } = {}) {
  const esc = (v) => String(v || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const body = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>${esc(title)} — Equity Investment Corporation</title>
<style>
  :root{color-scheme:light dark}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#f4f6f8;color:#1d2530;
    font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
  main{max-width:520px;margin:24px;padding:28px 26px;background:#fff;border-radius:14px;
    box-shadow:0 10px 40px rgba(15,25,40,.10)}
  h1{margin:0 0 12px;font-size:20px}
  p{margin:0 0 14px;color:#48525f}
  .addr{font-weight:650;color:#1d2530;overflow-wrap:anywhere}
  button{font:inherit;font-size:15px;padding:12px 20px;border:0;border-radius:9px;
    background:#0c6f62;color:#fff;cursor:pointer}
  button:hover{background:#0a5b50}
  .fine{font-size:12.5px;color:#7a8593;margin-top:18px}
  @media (prefers-color-scheme:dark){
    body{background:#11161c;color:#e7ecf1}
    main{background:#182029;box-shadow:none}
    p{color:#9aa5b2} .addr{color:#e7ecf1} .fine{color:#7d8794}
  }
</style></head><body><main>
<h1>${esc(title)}</h1>
${message}
${showButton ? `<form method="post"><input type="hidden" name="t" value="${esc(token)}">
  <button type="submit">Unsubscribe me</button></form>` : ""}
<p class="fine">Equity Investment Corporation &middot; This page manages email only.
  It does not affect any account or advisory relationship.</p>
</main></body></html>`;
  context.res = { status, headers: { "Content-Type": "text/html; charset=utf-8",
                                     "Cache-Control": "no-store",
                                     "X-Robots-Tag": "noindex, nofollow",
                                     "X-Content-Type-Options": "nosniff" }, body };
}

// The token can arrive in the query (the emailed link) or the form body (the
// button on the page we just rendered).
function tokenFrom(req) {
  if (req.query && req.query.t) return String(req.query.t);
  const b = req.body;
  if (!b) return "";
  if (typeof b === "string") {
    const found = new URLSearchParams(b).get("t");
    return found || "";
  }
  return String(b.t || "");
}

module.exports = async function (context, req) {
  try {
    const claim = suppress.readToken(tokenFrom(req));
    if (!claim) {
      // Deliberately vague and identical for a tampered token and an
      // unconfigured server: this page is public, and a precise error is a
      // free oracle for anyone poking at it.
      return page(context, "This link is not valid",
        `<p>This preference link could not be read. It may have been altered in transit,
          or split across lines by an email client.</p>
         <p>Reply to the message you received and we will take care of it.</p>`,
        { status: 400 });
    }
    const { email, crd } = claim;
    const esc = (v) => String(v || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

    if (req.method === "POST") {
      // Order is load-bearing. Our own list is written FIRST and the response is
      // never made to wait on Act!: the advisor's request is honoured the moment
      // this row exists, because every send path checks this list. Act! is a
      // best-effort mirror for the humans working the CRM, and a CRM outage must
      // not turn into "we could not unsubscribe you, try again".
      const result = await suppress.suppress(email, { source: "unsubscribe-link" });
      context.log(`email preference opt-out: ${email} (new=${result.added})`);
      try {
        const pushed = await act.markDoNotEmail(email, crd);
        if (pushed.ok) await suppress.markActSynced(email);
        else context.log.warn(`Act! do-not-email not applied for ${email}: ${pushed.reason || "unknown"}`);
      } catch (err) {
        // Left unsynced on purpose -- pendingActSync() can pick it up later.
        context.log.error(`Act! do-not-email push failed for ${email}: ${err.message}`);
      }
      return page(context, "You have been unsubscribed",
        `<p>We have removed <span class="addr">${esc(email)}</span> from our email list.</p>
         <p>You will not receive further email from our team at this address. Please allow
            one business day for this to take effect across our systems.</p>`);
    }

    return page(context, "Manage your email preferences",
      `<p>This will stop all further email from Equity Investment Corporation to
         <span class="addr">${esc(email)}</span>.</p>`,
      { token: tokenFrom(req), showButton: true });
  } catch (err) {
    context.log.error(err);
    return page(context, "Something went wrong",
      `<p>We could not update your preferences just now. Please reply to the message
        you received and we will handle it directly.</p>`, { status: 500 });
  }
};
