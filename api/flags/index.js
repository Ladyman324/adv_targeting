/* GET /api/flags     — every key-person, analyst, and scheduler contact
 * PUT /api/flags     — set or clear one flag on one advisor
 *
 * FIRM-WIDE, like /api/dnc and unlike /api/queue. Which person at a firm runs
 * manager due diligence is a fact about that firm rather than a private note,
 * so one rep working it out saves the next rep from working it out again. Each
 * row records who set it and when, so the knowledge has a source.
 *
 * Unlike a do-not-call entry these can be cleared: a key contact who moves firm
 * is stale sales knowledge, not a compliance suppression. Clearing every flag
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
    if (req.method === "POST") {
      const entries = body.entries;
      if (!["key", "dd", "scheduler"].includes(body.kind)
          || !Array.isArray(entries) || !entries.length || entries.length > 200
          || entries.some(entry => !entry || !/^\d{1,32}$/.test(String(entry.crd || ""))
            || String(entry.name || "").length > 256
            || !/^\d{0,32}$/.test(String(entry.firmCrd || "")))) {
        const err = new Error("Choose one role and 1 to 200 valid advisor records.");
        err.statusCode = 400;
        throw err;
      }
      const unique = new Set(entries.map(entry => String(entry.crd)));
      if (unique.size !== entries.length) {
        const err = new Error("Each advisor may appear only once in a bulk role update.");
        err.statusCode = 400;
        throw err;
      }
      const failed = [];
      let updated = 0;
      // Bounded concurrency keeps a 200-person import responsive without
      // flooding Table Storage. setFlag joins this rep's membership and is
      // idempotent, so a partial result can be retried safely.
      for (let i = 0; i < entries.length; i += 8) {
        const chunk = entries.slice(i, i + 8);
        const results = await Promise.allSettled(chunk.map(entry =>
          store.setFlag(who, entry.crd, body.kind, true,
                        entry.name, entry.firmCrd)));
        results.forEach((result, n) => {
          if (result.status === "fulfilled") updated++;
          else failed.push(String(chunk[n].crd));
        });
      }
      return store.ok(context, { updated, failed });
    }
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
