/* /api/email-preferences — the unsubscribe page reached from the email footer.
 *
 * ANONYMOUS by necessity: the people who click it are advisors outside the firm
 * who have no account here and never will.
 *
 * The GET NEVER suppresses. It renders a page with a form, and only the POST
 * writes. This is not politeness -- corporate mail gateways (Mimecast, Proofpoint,
 * Defender for Office, and the scanners at exactly the wirehouses this app mails)
 * pre-fetch every link in an inbound message to check it for malware. A GET that
 * acted would unsubscribe a large share of every batch before a human read a
 * word of it, and the damage would be invisible: the suppressions would look
 * like genuine opt-outs.
 *
 * THE POST IS NOT ENOUGH ON ITS OWN, and we learned that the expensive way.
 * Three Raymond James recipients were suppressed without any of them clicking.
 * The telemetry showed the same shape both times: a GET, a ~60 second dwell, a
 * second GET, then a POST six seconds later -- a detonation sandbox opening the
 * page and pressing the only control on it. Every "does this look like a real
 * browser" defence (cookies, JavaScript, dwell time, interaction events) loses
 * that argument eventually, because convincingly imitating a browser is the
 * scanner's entire job. A dwell test would have failed outright here: the
 * sandbox sat on the page for a full minute before it clicked.
 *
 * So the form asks for something THE PAGE DOES NOT CONTAIN: the address the
 * message was sent to. A scanner pressing Submit sends an empty field and
 * nothing happens. This is the pattern Wells Fargo uses for the same problem.
 *
 * Two rules make that safe:
 *   - The typed address only GATES. What gets suppressed is the address inside
 *     the signed token, so a stranger cannot type someone else's address here
 *     and silence their inbox.
 *   - The address is NEVER rendered on this page. If the answer were printed
 *     above the box, a form-filling sandbox could copy it down, and the whole
 *     defence would be theatre.
 */
"use strict";

const suppress = require("../shared/email-suppress");
const act = require("../shared/act");

function page(context, title, message, {
  token = "", showForm = false, error = "", status = 200,
} = {}) {
  const esc = (v) => String(v || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  // The sentence Wells Fargo puts on the same page, and it is not boilerplate:
  // this list governs the mail we send as a firm. A rep writing to an advisor
  // personally is a different thing, and someone who meant only to stop the
  // commentaries should not silently lose the person they actually deal with.
  const transactional = `<p class="fine">This action does not opt you out from transactional
    emails from an individual sender. To update your contact preferences for those messages,
    reply to that person directly and let them know.</p>`;
  const form = `<form method="post" novalidate>
  <input type="hidden" name="t" value="${esc(token)}">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" inputmode="email" autocomplete="email"
         autocapitalize="off" autocorrect="off" spellcheck="false" required
         aria-describedby="hint${error ? " err" : ""}"${error ? ' aria-invalid="true"' : ""}>
  <p id="hint" class="hint">Enter the address this message was sent to.</p>
  ${error ? `<p id="err" class="err" role="alert">${esc(error)}</p>` : ""}
  <button type="submit">Unsubscribe</button>
</form>`;
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
  label{display:block;font-weight:650;font-size:14px;margin:18px 0 6px;color:#1d2530}
  /* 16px, or iOS zooms the page on focus and the button leaves the screen. */
  input[type=email]{width:100%;box-sizing:border-box;font:inherit;font-size:16px;
    padding:11px 12px;border:1px solid #c3ccd6;border-radius:9px;background:#fff;color:#1d2530}
  input[type=email]:focus{outline:2px solid #0c6f62;outline-offset:1px;border-color:#0c6f62}
  input[aria-invalid=true]{border-color:#b4342a}
  .hint{font-size:13px;color:#7a8593;margin:6px 0 0}
  .err{font-size:13.5px;color:#b4342a;margin:8px 0 0;font-weight:600}
  button{font:inherit;font-size:15px;padding:12px 20px;border:0;border-radius:9px;
    margin-top:18px;background:#0c6f62;color:#fff;cursor:pointer}
  button:hover{background:#0a5b50}
  .fine{font-size:12.5px;color:#7a8593;margin-top:18px}
  @media (prefers-color-scheme:dark){
    body{background:#11161c;color:#e7ecf1}
    main{background:#182029;box-shadow:none}
    p{color:#9aa5b2} label{color:#e7ecf1} .fine{color:#7d8794} .hint{color:#7d8794}
    input[type=email]{background:#11161c;border-color:#39434f;color:#e7ecf1}
    .err{color:#f2857a} input[aria-invalid=true]{border-color:#f2857a}
  }
</style></head><body><main>
<h1>${esc(title)}</h1>
${message}
${showForm ? form : ""}
${showForm ? transactional : ""}
<p class="fine">Equity Investment Corporation &middot; This page manages email only.
  It does not affect any account or advisory relationship.</p>
</main></body></html>`;
  context.res = { status, headers: { "Content-Type": "text/html; charset=utf-8",
                                     "Cache-Control": "no-store",
                                     "X-Robots-Tag": "noindex, nofollow",
                                     "X-Content-Type-Options": "nosniff" }, body };
}

// A field can arrive in the query (the emailed link) or the form body (the page
// we just rendered). Azure hands the body over as a parsed object or as a raw
// urlencoded string depending on the Content-Type, so both are handled.
function fieldFrom(req, name) {
  if (req.query && req.query[name]) return String(req.query[name]);
  const b = req.body;
  if (!b) return "";
  if (typeof b === "string") return new URLSearchParams(b).get(name) || "";
  return String(b[name] || "");
}

const tokenFrom = (req) => fieldFrom(req, "t");

/* Who asked.
 *
 * Application Insights masks client_IP to 0.0.0.0 and records no user agent for
 * Functions, so the platform telemetry could not tell a person from a scanner.
 * Three advisors were wrongly suppressed and the only evidence available was the
 * SHAPE of the timestamps -- a GET, a minute's dwell, a second GET, a POST. That
 * is a week of inference to reach what one header would have said outright.
 *
 * Logged on every path, and the useful one is the REJECTED submit: a blank or
 * mismatched address now does no harm, which turns this endpoint into a free
 * detector. If a gateway is exercising these links, the log says so before an
 * advisor pays for it.
 */
function who(req) {
  const h = (name) => {
    const bag = req.headers || {};
    return String(bag[name] || bag[name.toUpperCase()] || "").slice(0, 256);
  };
  // x-forwarded-for is a chain; the client is the first entry, the rest are
  // proxies. Front Door and the SWA edge both append to it.
  let ip = h("x-forwarded-for").split(",")[0].trim();
  const bracketed = ip.match(/^\[([^\]]+)\](?::\d+)?$/);
  if (bracketed) ip = bracketed[1];
  else if ((ip.match(/:/g) || []).length === 1) ip = ip.replace(/:\d+$/, "");
  ip = ip.replace(/[^0-9a-f:.]/gi, "").slice(0, 64);
  // A form POST normally refers back to /api/email-preferences?t=<token>.
  // The token is encrypted but still grants the power to unsubscribe one
  // address, so query strings and fragments must never enter telemetry.
  const rawRef = h("referer");
  let ref = rawRef.split(/[?#]/, 1)[0];
  try {
    const url = new URL(rawRef);
    ref = `${url.origin}${url.pathname}`;
  } catch { /* a relative or malformed referrer is still stripped above */ }
  return `agent=${JSON.stringify(h("user-agent") || "-")} `
       + `ip=${JSON.stringify(ip || "-")} ref=${JSON.stringify(ref || "-")}`;
}

module.exports = async function (context, req) {
  try {
    const token = tokenFrom(req);
    const claim = suppress.readToken(token);
    context.log(`email preference ${req.method}: ${who(req)}`);
    if (!claim) {
      // Deliberately vague and identical for a tampered token and an
      // unconfigured server: this page is public, and a precise error is a
      // free oracle for anyone poking at it.
      context.log(`email preference: unreadable token, ${who(req)}`);
      return page(context, "This link is not valid",
        `<p>This preference link could not be read. It may have been altered in transit,
          or split across lines by an email client.</p>
         <p>Reply to the message you received and we will take care of it.</p>`,
        { status: 400 });
    }
    const { email, crd } = claim;

    if (req.method === "POST") {
      const typed = suppress.norm(fieldFrom(req, "email"));
      if (!typed || typed !== suppress.norm(email)) {
        // The failure text must not narrow the answer -- no "close", no partial
        // reveal, and the same words whether the box was blank or wrong. A
        // scanner submits an empty field and lands here, which is the point.
        //
        // Status stays 200. A 4xx would let anything watching response codes
        // tell a wrong guess from a right one without reading the page.
        context.log(`email preference REJECTED (blank or mismatched address): ${who(req)}`);
        return page(context, "Manage your email preferences",
          `<p>To stop all further email from Equity Investment Corporation, confirm the
             address this message was sent to.</p>`,
          { token, showForm: true,
            error: "That address does not match the one this link was sent to. "
                 + "Please check it and try again, or simply reply to the message "
                 + "you received and we will take care of it." });
      }

      // Order is load-bearing. Our own list is written FIRST and the response is
      // never made to wait on Act!: the advisor's request is honoured the moment
      // this row exists, because every send path checks this list. Act! is a
      // best-effort mirror for the humans working the CRM, and a CRM outage must
      // not turn into "we could not unsubscribe you, try again".
      //
      // The address written is the TOKEN's, never the typed one. The typing
      // proved a person is present; it does not get to name who is suppressed.
      const result = await suppress.suppress(email, { source: "unsubscribe-form" });
      context.log(`email preference opt-out: ${email} (new=${result.added}) ${who(req)}`);
      try {
        const pushed = await act.markDoNotEmail(email, crd);
        if (pushed.ok) await suppress.markActSynced(email);
        else context.log.warn(`Act! do-not-email not applied for ${email}: ${pushed.reason || "unknown"}`);
      } catch (err) {
        // Left unsynced on purpose -- pendingActSync() can pick it up later.
        context.log.error(`Act! do-not-email push failed for ${email}: ${err.message}`);
      }
      // Still no address on the page: they typed it, so they know it, and
      // printing it back hands it to whatever opens this link afterwards.
      return page(context, "You have been unsubscribed",
        `<p>We have removed that address from our email list.</p>
         <p>You will not receive further email from our team at this address. Please allow
            one business day for this to take effect across our systems.</p>`);
    }

    return page(context, "Manage your email preferences",
      `<p>This will stop all further email from Equity Investment Corporation to the
         address this message was sent to.</p>`,
      { token, showForm: true });
  } catch (err) {
    context.log.error(err);
    return page(context, "Something went wrong",
      `<p>We could not update your preferences just now. Please reply to the message
        you received and we will handle it directly.</p>`, { status: 500 });
  }
};
