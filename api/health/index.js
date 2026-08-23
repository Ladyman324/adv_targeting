/* GET /api/health — is logging actually going to work, and who am I?
 *
 * Exists so the front end can find out at load time rather than at the moment a
 * rep presses a disposition button. If storage is unconfigured, the dialer says
 * so up front and refuses to start a session -- far better than accepting an
 * afternoon of calls and failing on every save.
 */
"use strict";

const store = require("../shared/store");
const actSync = require("../shared/act");

module.exports = async function (context, req) {
  try {
    const who = store.identity(req);
    let storageOk = false;
    let detail = "";
    if (store.configured()) {
      try {
        await store.getQueue(who);          // cheapest real round trip
        storageOk = true;
      } catch (e) {
        detail = e.message || String(e);
      }
    } else {
      detail = "AZURE_STORAGE_CONNECTION_STRING is not set.";
    }
    return store.ok(context, {
      user: { id: who.id, name: who.name, dev: who.dev },
      configured: store.configured(),
      storageOk,
      detail,
      // Diagnostic only, and deliberately says nothing the caller could not
      // already infer. "Why did my call not appear in Act!" has several
      // answers that all look identical from the outside -- not deployed, not
      // switched on, no Act! account for this person, no contact match for that
      // advisor -- and guessing between them costs an afternoon each time.
      //
      // No secret is exposed: whether a setting is present, never its value.
      act: await actSync.diagnose(who),
    });
  } catch (err) {
    return store.fail(context, err);
  }
};
