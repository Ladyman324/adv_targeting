/* GET /api/flags     — every key contact and due-diligence contact
 * PUT /api/flags     — set or clear one flag on one advisor
 *
 * FIRM-WIDE, like /api/dnc and unlike /api/queue. Which person at a firm runs
 * manager due diligence is a fact about that firm rather than a private note,
 * so one rep working it out saves the next rep from working it out again. Each
 * row records who set it and when, so the knowledge has a source.
 *
 * Unlike a do-not-call entry these can be cleared: a key contact who moves firm
 * is stale sales knowledge, not a compliance suppression. Clearing BOTH flags
 * deletes the row rather than leaving one that says "this person is nothing".
 *
 * The whole set ships to the client on load. It is a few hundred rows at most,
 * and having it locally means the map can draw a star without a round trip per
 * pin.
 */
"use strict";

const store = require("../shared/store");

module.exports = async function (context, req) {
  try {
    const who = store.identity(req);
    if (req.method === "GET") {
      const entries = await store.listFlags();
      return store.ok(context, { entries, count: entries.length });
    }
    const body = req.body || {};
    const crd = String(body.crd || "").trim();
    if (!crd) {
      const err = new Error("An advisor CRD is required.");
      err.statusCode = 400;
      throw err;
    }
    const saved = await store.setFlag(who, crd, body.kind, body.on === true,
                                      body.name, body.firmCrd);
    return store.ok(context, { saved });
  } catch (err) {
    return store.fail(context, err);
  }
};
