"use strict";

const baseStore = require("../shared/store");
const auth = require("../shared/email-auth");

module.exports = async function (context, req) {
  try {
    // Microsoft sends the browser here as a cross-site navigation, on which the
    // Static Web Apps session cookie is not sent. Requiring a signed-in
    // principal made the platform answer 401, the config's 401 override bounce
    // the request to /.auth/login/aad, and the authorization code vanish -- the
    // rep landed back on a working app that had silently connected nothing.
    // Identity comes from the single-use state row instead; see complete().
    let who = null;
    try { who = baseStore.identity(req); } catch { who = null; }
    if (req.query.error) throw new Error(`Microsoft authorization failed: ${req.query.error_description || req.query.error}`);
    if (!req.query.code || !req.query.state) { const e = new Error("Microsoft authorization response is incomplete."); e.statusCode = 400; throw e; }
    const result = await auth.complete(who, req.query.code, req.query.state);
    context.res = { status: 302, headers: { Location: result.returnTo, "Cache-Control": "no-store" }, body: "" };
  } catch (err) {
    context.log.error(err);
    const message = encodeURIComponent(err.message || "Microsoft mailbox connection failed.");
    context.res = { status: 302, headers: { Location: `/?email=error&message=${message}`, "Cache-Control": "no-store" }, body: "" };
  }
};
