/* POST /api/log   record one call outcome
 * GET  /api/log?crd=123456   who here has touched this advisor, ever
 * GET  /api/log?mine=1       my recent activity
 *
 * A disposition is the only thing in this whole application authored by a
 * human, which makes it the only thing that cannot be regenerated from the SEC
 * feed. It is treated accordingly: written to durable storage under the
 * signed-in identity, or refused with an error the UI has to show.
 */
"use strict";

const store = require("../shared/store");
const actSync = require("../shared/act");

// A closed set, checked server-side. The client offers buttons, but the client
// is not the authority on what a valid outcome is -- and "do-not-call" in
// particular triggers a firm-wide suppression, so it cannot be a free string.
// Must agree with Dial.OUTCOMES in webapp/dial.js; audit.py checks that it does.
const DISPOSITIONS = new Set([
  "connected", "attempted", "voicemail", "wrong-number", "received",
  // "Try again later" -- the queue moves them to the end rather than dropping
  // them. Voicemail and a plain attempt usually mean "call back at four", and
  // without this the rep had to remember, hunt the person down and re-queue.
  "callback",
  "do-not-call", "skipped",
  // RETIRED, and deliberately still accepted. Events carrying these are already
  // in Table Storage; a queue synced from a phone that has not reloaded since
  // the vocabulary changed would otherwise have its outcomes refused at the one
  // moment the rep cannot re-enter them. Nothing offers these any more.
  "no-answer", "gatekeeper",
]);

// Why the call was made. Closed for the same reason dispositions are: it
// becomes the SUBJECT of an Act! history record, which is firm-visible text,
// and a client is not the authority on what may go there. Empty is valid and
// is what every call logged before this existed carries.
// Must agree with Dial.PURPOSES in webapp/dial.js; audit.py checks that it does.
const PURPOSES = new Set(["meeting", "materials", "check-in", "cold"]);

module.exports = async function (context, req) {
  try {
    const who = store.identity(req);

    if (req.method === "GET") {
      const crd = (req.query && req.query.crd) || "";
      if (!crd && String((req.query && req.query.summary) || "") === "1") {
        return store.ok(context, {
          generatedUtc: new Date().toISOString(),
          entries: await store.latestCallsForUser(who),
        });
      }
      // `since` powers cycle progress: which of this list have I already
      // handled on this pass. Higher ceiling than the default, because a
      // 250-person list worked twice is 500 rows and the answer has to be
      // complete or the progress count silently understates.
      const since = (req.query && req.query.since) || "";
      const cap = since ? 1000 : 100;
      // Clamped at BOTH ends. Math.min alone let a negative through: -5 is
      // truthy so it survived the `|| 25`, and Math.min(-5, cap) is -5.
      const asked = Number((req.query && req.query.limit) || 25) || 25;
      const limit = Math.max(1, Math.min(Math.floor(asked), cap));
      const events = crd
        ? await store.eventsForCrd(crd, limit)
        : await store.recentForUser(who, limit, since);
      const mine = events.map((e) => ({
        crd: e.crd, at: e.atUtc, who: e.userName, kind: e.kind,
        disposition: e.disposition, purpose: e.purpose || "",
        note: e.note, name: e.advisorName,
        act: e.actStatus || "",
        source: "app",
      }));

      /* THE FULLER HISTORY, for an advisor Act! knows about.
       *
       * Only on a single-advisor lookup, and only when asked. A rep's own
       * recent-activity feed has no use for it, and the queue's progress query
       * fetches a thousand rows -- putting a CRM round trip behind either
       * would be paying Act!'s latency for something nobody is reading.
       *
       * DE-DUPLICATION USES OUR OWN RECORD, NOT A MARKER IN THEIRS.
       *
       * `actStatus === "written"` means exactly one thing: this outcome was
       * successfully created in Act!, so Act! is about to return it to us. The
       * local copy is therefore the duplicate and it is the one that goes.
       * Every other status -- no-contact, failed, not-attributable, off, or a
       * call logged before sync was switched on -- means Act! has no copy, so
       * ours is the only record there is and it stays.
       *
       * Doing it this way means a missed match can only ever DUPLICATE a row,
       * never hide one. Hunting our own writes inside Act!'s payload would
       * have the opposite failure: a marker that misses silently deletes the
       * rep's evidence that a call happened.
       */
      const wantAct = crd && String((req.query && req.query.act) || "") === "1";
      if (!wantAct) return store.ok(context, { events: mine });

      const crm = await actSync.historyFor(crd);
      const kept = crm.ok ? mine.filter((e) => e.act !== "written") : mine;
      const merged = kept.concat(crm.events.map((h) => ({
        crd: String(crd), at: h.at, who: h.who, kind: "crm",
        disposition: "", purpose: "", note: h.details, name: "",
        act: "", source: "act", subject: h.subject, type: h.type,
      })));
      merged.sort((a, b) => String(b.at).localeCompare(String(a.at)));
      return store.ok(context, {
        events: merged,
        // Said out loud rather than inferred from an empty list. "Act! has no
        // history for them" and "Act! could not be reached" look identical on
        // screen and mean opposite things to a rep deciding whether to call.
        crm: { ok: crm.ok, why: crm.why, count: crm.events.length,
               hidden: crm.ok ? mine.length - kept.length : 0 },
      });
    }

    const body = req.body || {};
    if (!body.crd) {
      const err = new Error("crd is required.");
      err.statusCode = 400;
      throw err;
    }
    const disposition = String(body.disposition || "").toLowerCase();
    if (disposition && !DISPOSITIONS.has(disposition)) {
      const err = new Error(`Unknown disposition "${disposition}".`);
      err.statusCode = 400;
      throw err;
    }

    const purpose = String(body.purpose || "").toLowerCase();
    if (purpose && !PURPOSES.has(purpose)) {
      const err = new Error(`Unknown purpose "${purpose}".`);
      err.statusCode = 400;
      throw err;
    }

    const saved = await store.appendEvent(who, { ...body, disposition, purpose });

    // Suppression is a side effect of the disposition rather than a separate
    // call the client could forget to make.
    let dnc = null;
    if (disposition === "do-not-call") {
      dnc = await store.addDnc(who, body.crd, body.note || "", body.name);
    }

    // AFTER the durable write, and unable to affect it. Act! is a mirror of a
    // record that already exists; act.logCall resolves rather than throwing, so
    // an Act! outage costs the CRM a row and costs the rep nothing. The status
    // is stored so a failure is visible later rather than only in a log line
    // nobody reads -- and it is stored best-effort, because failing to record
    // "the mirror failed" must not fail the request either.
    const act = await actSync.logCall(who, { ...body, disposition, purpose });
    if (act !== "off" && act !== "not-an-outcome") {
      try { await store.setActStatus(who, saved.id, act); } catch { /* see above */ }
    }

    return store.ok(context, { ok: true, ...saved, dnc, by: who.name, act });
  } catch (err) {
    return store.fail(context, err);
  }
};
