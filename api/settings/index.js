/* GET  /api/settings   this rep's preferences, on any device
 * PUT  /api/settings   merge in the keys sent; unknown keys are dropped
 *
 * Server-side rather than localStorage because a preference that differs
 * between the desk and the phone is not a default -- it is two settings
 * sharing a name. This application already paid for that lesson once with the
 * active-list preference.
 *
 * Most settings are cosmetic, but ACT email write-back is a server-enforced
 * opt-in. A failed read must prevent CRM writing, never imply consent.
 */
"use strict";

const store = require("../shared/store");

module.exports = async function (context, req) {
  try {
    const who = store.identity(req);

    if (req.method === "GET") {
      return store.ok(context, { settings: await store.getSettings(who),
        features: { actEmailWrite: process.env.ACT_EMAIL_HISTORY_SYNC === "1" } });
    }

    const body = req.body || {};
    if (Object.prototype.hasOwnProperty.call(body, "actEmailWrite")) {
      if (process.env.ACT_EMAIL_HISTORY_SYNC !== "1") {
        const err = new Error("ACT! email write-back is disabled by the administrator.");
        err.statusCode = 403;
        throw err;
      }
      if (body.actEmailWrite !== "0" && body.actEmailWrite !== "1") {
        const err = new Error("ACT! email write-back must be on or off.");
        err.statusCode = 400;
        throw err;
      }
    }
    // Named so a caller can see what it may send. Unknown keys are dropped
    // silently by putSettings, and reporting the accepted list here is what
    // makes that silence debuggable rather than mysterious.
    const saved = await store.putSettings(who, body);
    return store.ok(context, { ok: true, settings: saved,
                               features: { actEmailWrite: process.env.ACT_EMAIL_HISTORY_SYNC === "1" },
                               accepts: Object.keys(store.SETTING_KEYS) });
  } catch (err) {
    return store.fail(context, err);
  }
};
