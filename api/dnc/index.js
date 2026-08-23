/* GET /api/dnc — the firm-wide do-not-call list.
 *
 * Read-only on purpose. Entries are created as a side effect of logging a
 * "do-not-call" disposition, so a suppression can never exist without the call
 * record that explains it. There is deliberately no delete: removing someone
 * from this list is a compliance decision, not a UI gesture, and should be done
 * deliberately in the storage account by someone who means it.
 *
 * The whole list ships to the client. For a firm this size it is a handful of
 * entries, and having it locally means a queue can be filtered before anyone
 * sees a name they should not be dialling -- rather than after.
 */
"use strict";

const store = require("../shared/store");

module.exports = async function (context, req) {
  try {
    store.identity(req);                    // authenticated, but not per-user
    const entries = await store.listDnc();
    return store.ok(context, { entries, count: entries.length });
  } catch (err) {
    return store.fail(context, err);
  }
};
