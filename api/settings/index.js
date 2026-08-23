/* GET  /api/settings   this rep's preferences, on any device
 * PUT  /api/settings   merge in the keys sent; unknown keys are dropped
 *
 * Server-side rather than localStorage because a preference that differs
 * between the desk and the phone is not a default -- it is two settings
 * sharing a name. This application already paid for that lesson once with the
 * active-list preference.
 *
 * Nothing here is safety-critical: a failure costs the rep an opening view,
 * not a call record. So both views treat a failed read as "no preferences set"
 * and carry on, which is why this endpoint never invents a value of its own.
 */
"use strict";

const store = require("../shared/store");

module.exports = async function (context, req) {
  try {
    const who = store.identity(req);

    if (req.method === "GET") {
      return store.ok(context, { settings: await store.getSettings(who) });
    }

    const body = req.body || {};
    // Named so a caller can see what it may send. Unknown keys are dropped
    // silently by putSettings, and reporting the accepted list here is what
    // makes that silence debuggable rather than mysterious.
    const saved = await store.putSettings(who, body);
    return store.ok(context, { ok: true, settings: saved,
                               accepts: Object.keys(store.SETTING_KEYS) });
  } catch (err) {
    return store.fail(context, err);
  }
};
