/* Act! CRM — write one call outcome as the rep who made it.
 *
 * WHY THIS IS NOT `POST /api/History`
 * -----------------------------------
 * Because that route cannot say who made the call. It stamps whichever account
 * authenticated, and this application authenticates once on behalf of everyone,
 * so every rep's calls would land under one name. `recordManagerID`,
 * `createUserID`, the name strings, and a follow-up PATCH were all tested
 * against the live database on 2026-08-13; all four were ignored, and the PATCH
 * returned success while changing nothing.
 *
 * Act! creates history as a CONSEQUENCE of clearing an activity, and the
 * scheduling endpoint takes a user id in the path:
 *
 *     POST /api/organizers/{userId}/tasks     scheduledForId = the rep
 *     PUT  /api/tasks/{taskId}/clear          result.id      = the outcome
 *
 * The resulting history carries recordManager = the rep and createUserName =
 * this integration account, which is the correct split: the work is theirs, the
 * record is honest about what wrote it. Proven 2026-08-13, and re-proven across
 * all four Call results on 2026-08-14 by `act_write_test.py --outcome-map`.
 *
 * TWO RULES THIS FILE WILL NOT BREAK
 * ----------------------------------
 * 1. A FAILURE HERE NEVER FAILS THE REP'S LOG. The call outcome is already in
 *    Table Storage before anything below runs. Act! is a mirror, and a mirror
 *    that is briefly out of date is a much smaller harm than an outcome the rep
 *    was told did not save. Everything here resolves; nothing throws upward.
 *
 * 2. NOTHING IS WRITTEN UNDER THE WRONG NAME OR ONTO THE WRONG CONTACT. If the
 *    rep has no Act! account, or the advisor has no high-confidence Act! match,
 *    this writes NOTHING and says why. Silence is the correct behaviour; a call
 *    filed against a stranger cannot be found again or undone.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const BASE = process.env.ACT_BASE || "https://apius.act.com/act.web.api";
const USER = process.env.ACT_USER || "";
const PASSWORD = process.env.ACT_PASSWORD || "";
const DATABASE = process.env.ACT_DB || "";
// Deploy dark by default. The credentials being present is not the same as
// somebody having decided this should be writing to the firm's CRM.
const ENABLED = process.env.ACT_SYNC === "1";

// A COMMA-SEPARATED ALLOW-LIST OF CRDS, for the first live test.
//
// The advisors in this map are real people with real Act! records. A test call
// logged against one of them writes a history entry saying we spoke to someone
// we did not speak to -- indistinguishable afterwards from a real call, on
// somebody else's contact. Setting ACT_ONLY_CRD during the trial means a
// mis-click on the wrong row writes nothing at all rather than something
// untrue. Unset it once the test passes and sync should cover everyone.
const ONLY = new Set(
  String(process.env.ACT_ONLY_CRD || "").split(",").map((s) => s.trim()).filter(Boolean));

function configured() {
  return Boolean(ENABLED && USER && PASSWORD && DATABASE);
}

/* ---------- what a disposition means to Act! ------------------------------
 * Must match Dial.OUTCOMES in webapp/dial.js; audit.py checks that it does.
 * Eight buttons collapse onto Act!'s four Call results, so three of them append
 * a sentence to the details -- without it a wrong number and an unanswered ring
 * are the same row in every Act! report.
 *
 * `null` means DO NOT WRITE. A skip is the absence of a call.
 */
const RESULTS = {
  connected:      { id: 1,  name: "Call Completed" },
  attempted:      { id: 0,  name: "Call Attempted" },
  voicemail:      { id: 17, name: "Call Left Message" },
  callback:       { id: 0,  name: "Call Attempted", note: "Call back requested." },
  received:       { id: 2,  name: "Call Received" },
  "wrong-number": { id: 0,  name: "Call Attempted", note: "Wrong number." },
  "do-not-call":  { id: 1,  name: "Call Completed", note: "Asked not to be called again." },
  skipped:        null,
  // Retired, and still mapped. Events carrying these are already in storage and
  // a phone that has not reloaded since the vocabulary changed can still send
  // one; refusing to sync it would lose the outcome in the CRM only.
  "no-answer":    { id: 0,  name: "Call Attempted" },
  gatekeeper:     { id: 0,  name: "Call Attempted", note: "Reached a gatekeeper." },
};

/* ---------- why the call was made ----------------------------------------
 * Must match Dial.PURPOSES in webapp/dial.js; audit.py checks that it does.
 *
 * These labels are the ONLY reason this field exists. They become the history
 * SUBJECT, which is what Act! renders in the grid's Title column -- so a
 * contact's history reads "Materials — Allen White" and "Meeting — Allen
 * White" instead of six rows of "Call — Allen White" that have to be opened
 * one at a time to tell apart.
 *
 * An unknown or absent purpose falls back to the original subject rather than
 * being rejected. A phone that has not reloaded since this shipped still logs
 * correctly, and a purpose is a nicety; the call is not.
 */
const PURPOSES = {
  meeting:    "Meeting",
  materials:  "Materials",
  "check-in": "Check-in",
  cold:       "Cold call",
};

/* ---------- CRD -> Act! contact ------------------------------------------ */
let CONTACTS = null;
function contacts() {
  if (CONTACTS) return CONTACTS;
  try {
    const raw = fs.readFileSync(path.join(__dirname, "act_contacts.json"), "utf8");
    CONTACTS = JSON.parse(raw).contacts || {};
  } catch {
    // An absent lookup means nothing syncs. That is a degraded state, not a
    // broken one, and it must not take the call log down with it.
    CONTACTS = {};
  }
  return CONTACTS;
}

/* ---------- auth ----------------------------------------------------------
 * Act! returns a BARE JWT with Content-Type: text/html -- not JSON, not a
 * bearer envelope. Parsing it as JSON returns null and the failure looks like a
 * credential problem, which costs an afternoon.
 *
 * Cached, because a token request per logged call would be a second round trip
 * on every button press and would burn the rate limit for no reason.
 */
let token = "";
let tokenAt = 0;
const TOKEN_TTL_MS = 20 * 60 * 1000;

async function authorize() {
  if (token && Date.now() - tokenAt < TOKEN_TTL_MS) return token;
  const basic = Buffer.from(`${USER}:${PASSWORD}`).toString("base64");
  const r = await fetch(`${BASE}/authorize`, {
    headers: { Authorization: `Basic ${basic}`, "Act-Database-Name": DATABASE },
  });
  if (!r.ok) throw new Error(`Act! authorize -> ${r.status}`);
  const t = (await r.text()).trim().replace(/^"|"$/g, "");
  if (!t || t.length < 20) throw new Error("Act! authorize returned no token");
  token = t;
  tokenAt = Date.now();
  return token;
}

async function call(method, url, body, attempt = 0) {
  const t = await authorize();
  const r = await fetch(`${BASE}/${url.replace(/^\//, "")}`, {
    method,
    headers: {
      Authorization: `Bearer ${t}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401 && attempt === 0) {
    // The cached token expired early. ONE retry -- and the counter is what makes
    // that true. The comment said "one retry" while the code recursed with no
    // guard at all, so a credential that kept being rejected looped until the
    // stack gave out instead of failing with a usable message.
    token = "";
    return call(method, url, body, attempt + 1);
  }
  if (!r.ok) {
    throw new Error(`Act! ${method} ${url} -> ${r.status} ${(await r.text()).slice(0, 200)}`);
  }
  const text = await r.text();
  try { return text ? JSON.parse(text) : null; } catch { return text; }
}

/* ---------- rep email -> Act! user id -------------------------------------
 * Act! user names ARE email addresses here, which is what makes this join
 * possible without a hand-maintained table of GUIDs that would rot. Entra gives
 * us the signed-in address; Act! gives us the id.
 *
 * Cached for the life of the process: users are added rarely, and a cold start
 * re-reads. A miss is re-checked rather than cached, so a rep who gets an Act!
 * account today starts syncing without a redeploy.
 */
let USERS = null;
async function userIdFor(email) {
  const key = String(email || "").trim().toLowerCase();
  if (!key) return null;
  if (USERS && USERS[key]) return USERS[key];
  if (!USERS) USERS = {};

  const list = await call("GET", "api/users");
  const rows = Array.isArray(list) ? list : (list && list.value) || [];
  // EVERY string property that looks like an address becomes a key, rather than
  // a named shortlist of fields.
  //
  // This was `[u.userName, u.email, u.loginName]`, which missed Act!'s actual
  // property `username` -- lower-case n, and JSON keys are case-sensitive. The
  // working Python equivalent reads `email` and `username`, and the mismatch
  // meant a rep whose address lives in one field and not the other resolved to
  // nothing and was told they had no Act! account. No error, no clue, and the
  // one field name that mattered was the one not on the list.
  //
  // Not guessing the schema is cheaper than guessing it correctly.
  for (const u of rows) {
    if (!u || !u.id) continue;
    const name = String(u.displayName || u.fullName || u.userName || "").trim();
    for (const v of Object.values(u)) {
      if (typeof v !== "string") continue;
      const s = v.trim().toLowerCase();
      // The NAME is kept alongside the id. The proven payload sent both
      // `scheduledForId` and `scheduledFor`, and the first version of this file
      // sent only the id -- so what shipped was never quite what was tested.
      if (s.includes("@") && !s.includes(" ")) USERS[s] = { id: u.id, name };
    }
  }
  USER_COUNT = rows.length;
  return USERS[key] || null;
}
let USER_COUNT = 0;

/* ---------- the write ----------------------------------------------------- */
/* Returns a short status string, always. Never throws.
 *
 *   written                the history exists in Act!, managed by the rep
 *   off                    sync is not switched on
 *   not-an-outcome         a tel: tap or an email, not a logged result
 *   no-write               the disposition deliberately writes nothing (skipped)
 *   not-in-test-allowlist  ACT_ONLY_CRD is set and this advisor is not in it
 *   no-contact             no high-confidence Act! contact for this CRD
 *   no-act-user            the rep has no Act! account, so it cannot be attributed
 *   failed: ...            Act! refused; the local log is unaffected
 */
async function logCall(who, body) {
  if (!configured()) return "off";
  if ((body.kind || "outcome") !== "outcome") return "not-an-outcome";

  const disposition = String(body.disposition || "").toLowerCase();
  if (!(disposition in RESULTS)) return "no-write";
  const result = RESULTS[disposition];
  if (!result) return "no-write";

  if (ONLY.size && !ONLY.has(String(body.crd))) return "not-in-test-allowlist";

  const contactId = contacts()[String(body.crd)];
  if (!contactId) return "no-contact";

  try {
    const rep = await userIdFor(who.name);
    if (!rep) return "no-act-user";
    const userId = rep.id;

    // A call already happened; the activity is a formality that exists so Act!
    // will produce the history. Start time is now and the window is short --
    // the browser gets no callback from a tel: link, so a real duration is
    // something we do not have and will not invent.
    const now = new Date();
    const end = new Date(now.getTime() + 60 * 1000);
    const who_ = body.name || "advisor";
    const purpose = PURPOSES[String(body.purpose || "").toLowerCase()];
    const subject = `${purpose || "Call"} — ${who_}`;

    const details = [
      result.note || "",
      String(body.note || "").trim(),
      body.phone ? `Number dialled: ${body.phone}` : "",
      "Logged from the advisor map.",
    ].filter(Boolean).join("\n");

    const task = await call("POST", `api/organizers/${userId}/tasks`, {
      subject,
      details,
      startTime: now.toISOString(),
      endTime: end.toISOString(),
      scheduledForId: userId,
      // Sent because the payload that was PROVEN sent it. This file originally
      // passed the id alone, so the thing running in production was not the
      // thing the attribution test validated -- a small difference, and exactly
      // the kind that turns "we tested this" into a false statement.
      scheduledFor: rep.name,
      activityTypeId: 0,                 // Call
      contacts: [{ id: contactId }],
      isPrivate: false,
      isTimeless: false,
    });
    const taskId = (task && task.id) || task;
    if (!taskId) return "failed: no task id returned";

    // The clear is what creates the history, and `result` -- not activityTypeId
    // -- is what decides its type. Proven on 2026-08-14: all four Call results
    // round-trip to the type they were sent as.
    await call("PUT", `api/tasks/${taskId}/clear`, {
      result: { id: result.id, name: result.name },
      history: {
        startTime: now.toISOString(),
        endTime: end.toISOString(),
        includeDetailsToHistory: true,
        subject,
        details,
        isPrivate: false,
      },
    });

    // VERIFY WHAT WAS ACTUALLY WRITTEN, rather than assuming the write did what
    // the test showed it doing.
    //
    // Returning "written" the moment the clear succeeded meant the one thing
    // this integration exists to get right -- whose call it was -- was never
    // checked. It failed the first time a rep called a COLLEAGUE: where the
    // contact is itself an Act! user, the resulting history came back managed by
    // that colleague rather than by the caller. Nothing errored. The status said
    // "written" and the CRM recorded the wrong person as having made the call,
    // which is worse than not writing at all.
    //
    // Best effort: if the read fails we do not claim the write was wrong, only
    // that we could not confirm it.
    try {
      const hist = await call("GET", `api/contacts/${contactId}/history`);
      const rowsH = Array.isArray(hist) ? hist : (hist && hist.value) || [];
      const mine = rowsH
        .filter((h) => String(h.subject || h.regarding || "") === subject)
        .sort((a, b) => String(b.created || "").localeCompare(String(a.created || "")))[0];
      if (mine) {
        const mgr = String(mine.recordManager || "");
        if (rep.name && mgr && mgr !== rep.name) {
          // WRONG NAME -> REMOVE IT. Established 2026-08-14: where the contact
          // is itself an Act! user, Act! files the resulting history under that
          // user whatever we schedule. Not overridable -- `scheduledFor`, the
          // recordManager fields on the clear, and a follow-up PATCH were all
          // tried and all ignored.
          //
          // Leaving the record would put a lie in the system of record: the CRM
          // would show a colleague making a call they did not make, plausible
          // and permanent. The rule this file opens with is that nothing is
          // written under the wrong name, and the only way to keep it once the
          // write has happened is to take it back.
          //
          // The outcome is NOT lost. It is already in Table Storage, which is
          // the record; Act! is the mirror, and a mirror missing one row is a
          // far smaller harm than one showing the wrong person.
          let undone = false;
          try {
            await call("DELETE", `api/History/${mine.id}`);
            undone = true;
          } catch { /* reported below as still present */ }
          return `not-attributable: Act! filed it under ${mgr}`
               + (undone ? ", so it was removed" : " and it could NOT be removed");
        }
      }
    } catch { /* unverified is not the same as wrong */ }
    return "written";
  } catch (e) {
    return `failed: ${String(e.message || e).slice(0, 180)}`;
  }
}

/* ---------- reading the CRM's own history ---------------------------------
 * Act! holds years of contact for anyone who is in it, written by people who
 * never touched this application -- meetings, emails, letters, calls made from
 * a desk handset in 2019. Our own log holds only what this app recorded. For
 * an advisor who exists in Act!, the CRM is simply the fuller record, and a
 * rep about to dial should see it.
 *
 * DE-DUPLICATION IS DONE ON OUR SIDE, NOT HERE.
 *
 * The obvious approach is to find our own writes inside the Act! payload and
 * filter them out, and it is the wrong one: it needs a marker in a field we do
 * not control the shape of, and if the marker misses, the rep sees every call
 * twice with no way to tell which is which.
 *
 * We already know the answer without looking. Every outcome we logged carries
 * `actStatus`, and `written` means precisely "this exact outcome now exists in
 * Act!". So the local row is the one that gets dropped, by a fact we recorded
 * ourselves at the moment we wrote it. Everything Act! returns is shown as-is.
 * The merge lives in api/log, which has both halves.
 *
 * READ ONLY, and it never throws: an Act! outage must cost a rep the CRM
 * history panel and nothing else.
 */
async function historyFor(crd) {
  if (!configured()) return { ok: false, why: "off", events: [] };
  const contactId = contacts()[String(crd)];
  if (!contactId) return { ok: false, why: "no-contact", events: [] };
  try {
    const raw = await call("GET", `api/contacts/${contactId}/history`);
    const rows = Array.isArray(raw) ? raw : (raw && raw.value) || [];
    // FIELD NAMES TAKEN FROM RECORDS THIS PROJECT HAS ACTUALLY READ BACK, not
    // from a schema: `regarding` and `created` are what act_write_test.py's
    // dump reads, `subject` and `recordManager` are what logCall's own
    // verification reads, and `historyType.activityTypeName` is how that dump
    // tells a Call from a Meeting. Each is paired with its alternate rather
    // than guessed at, because the one field name this codebase guessed --
    // `userName` for `username` -- cost an afternoon and reported a wrong
    // answer confidently while it was wrong.
    const events = rows.map((h) => {
      const t = h.historyType || {};
      return {
        id: String(h.id || ""),
        at: String(h.created || h.startTime || ""),
        subject: String(h.regarding || h.subject || ""),
        // GENEROUS, because this is still MARKUP at this point. Act! stores
        // history details as rich text, and a `<span style="...">` wrapper can
        // spend 200 characters before the first real word -- so a tight cap
        // here truncates the note down to nothing once the tags are stripped
        // out on the client. The display limit is applied there, to the text.
        details: String(h.details || h.notes || "").slice(0, 8000),
        who: String(h.recordManager || h.createUserName || ""),
        type: String((t && (t.activityTypeName || t.name)) || h.historyTypeName || ""),
      };
    });
    events.sort((a, b) => String(b.at).localeCompare(String(a.at)));
    return { ok: true, why: "", events };
  } catch (e) {
    return { ok: false, why: `failed: ${String(e.message || e).slice(0, 160)}`,
             events: [] };
  }
}

/* Why a call did or did not reach Act!, answered without making one.
 *
 * Every failure mode in this file is SILENT BY DESIGN -- a missing contact
 * match must not interrupt a rep mid-session. That is right for the rep and
 * useless for whoever is asking why the CRM is empty, so the same facts are
 * available here on request.
 *
 * Reports whether each setting is PRESENT, never what it contains.
 */
async function diagnose(who) {
  const out = {
    // The most likely answer, and the one that looks exactly like a bug: the
    // code is deployed and switched off.
    enabled: ENABLED,
    hasUser: Boolean(USER),
    hasPassword: Boolean(PASSWORD),
    hasDatabase: Boolean(DATABASE),
    base: BASE,
    onlyCrd: ONLY.size ? [...ONLY] : null,
    contactsLoaded: Object.keys(contacts()).length,
    reachable: null,
    actUser: null,
    detail: "",
  };

  if (!out.contactsLoaded) {
    out.detail = "act_contacts.json is missing or empty, so no advisor resolves "
               + "to an Act! contact. Run src/build_act_lookup.py and redeploy.";
  }
  if (!configured()) {
    out.detail = out.detail || (
      !ENABLED ? "ACT_SYNC is not set to 1, so nothing is written to Act!."
               : "Act! credentials are incomplete (ACT_USER / ACT_PASSWORD / ACT_DB).");
    return out;
  }

  // One real round trip, so "configured" is distinguishable from "working".
  try {
    const rep = await userIdFor(who.name);
    out.reachable = true;
    // The NAME is reported, not just "matched": it is the string the sync
    // compares Act!'s recordManager against, so seeing it is how you tell a
    // correct match from one that resolved to the wrong colleague.
    out.actUser = rep ? (rep.name || "matched")
                      : "no Act! user for this sign-in address";
    const id = rep && rep.id;
    out.actUserCount = USER_COUNT;
    if (!id) {
      // The addresses Act! actually returned. Without this, "your address does
      // not match" is unfalsifiable from the outside -- it looks identical
      // whether the address is genuinely absent, spelled differently, or sitting
      // in a property this code never read. All three have happened.
      //
      // These are colleagues' work addresses, already visible to any signed-in
      // user throughout this application, and only shown when the match failed.
      out.knownAddresses = Object.keys(USERS || {}).sort().slice(0, 40);
      out.signedInAs = who.name;
      out.detail = `Signed in as ${who.name}, which matches none of the `
                 + `${out.knownAddresses.length} addresses on the ${USER_COUNT} `
                 + "Act! user records. Outcomes are logged but not written to "
                 + "Act!, because they must not be filed under anyone else.";
    }
  } catch (e) {
    out.reachable = false;
    out.detail = `Act! is not reachable: ${String(e.message || e).slice(0, 200)}`;
  }
  return out;
}

// Whether one specific advisor would sync -- the other half of "why is this
// call missing". Separate because it needs a CRD and /api/health has none.
function contactStatus(crd) {
  const id = contacts()[String(crd)];
  if (!id) return "no high-confidence Act! contact for this CRD";
  if (ONLY.size && !ONLY.has(String(crd))) return "excluded by ACT_ONLY_CRD";
  return "would sync";
}


/* ---------- Mail Code -----------------------------------------------------
 * EIC's Act! database carries a "Mail Code" picklist that already encodes
 * exactly what we need:
 *
 *   1  Email and hard copy      3  Hard Copy Only        P  Prospect, no mass mailings
 *   2  Email only               C  Client                N  No mail; cannot locate
 *   NC No mail by request       U  UNSUBSCRIBE
 *
 * An opt-out through the footer link sets U.
 *
 * TWO deliberate constraints:
 *
 * 1. NEVER DOWNGRADE. The value is only written when it is strictly more
 *    restrictive than what is already on the record. A stale or out-of-order
 *    write must not be able to loosen a restriction somebody set on purpose --
 *    re-enabling mail to someone who opted out is the one outcome here that is
 *    genuinely harmful.
 *
 * 2. READ-MODIFY-WRITE, sending the WHOLE entity back. Act! Web API's PATCH
 *    semantics are unconfirmed -- partial merge or full replace -- and a partial
 *    payload interpreted as a replace would blank every field it omitted. A full
 *    entity is correct under either reading, so the ambiguity stops mattering
 *    rather than being bet on.
 *
 * The API property is customFields.email__y_n -- established by reading a real
 * export (47,466 contacts: 22,195 "2", 15,996 "P", 4,947 "N", 3,064 "U", 527
 * "NC"), not inferred from the "Mail Code" label in the Act! UI. It is still
 * supplied as ACT_MAIL_CODE_FIELD rather than hardcoded, because a field name is
 * a property of the customer's database and not of this code. Unset, nothing is
 * written and the opt-out is history only. actFields() below rediscovers it.
 *
 * Dotted paths are supported, which this field needs.
 */
const MAIL_CODE_RANK = { "1": 1, "2": 2, "C": 2, "P": 3, "3": 4, "N": 5, "NC": 6, "U": 7 };
const MAIL_CODE_OPT_OUT = "U";      // asked to unsubscribe
const MAIL_CODE_UNREACHABLE = "N";  // "No mail; cannot locate; bounce backs"

function mailCodeField() { return String(process.env.ACT_MAIL_CODE_FIELD || "").trim(); }

// Read-only. Returns the PROPERTY NAMES on a contact, plus the value of any
// property that already looks like a Mail Code -- which is what identifies the
// field without dumping somebody's record into a log.
async function actFields(crd) {
  if (!configured()) return { ok: false, reason: "act_not_configured" };
  const contactId = contacts()[String(crd || "")];
  if (!contactId) return { ok: false, reason: "no_contact", crd };
  const contact = await call("GET", `api/contacts/${encodeURIComponent(contactId)}`);
  const looksLikeMailCode = (v) => typeof v === "string"
    && Object.prototype.hasOwnProperty.call(MAIL_CODE_RANK, v.trim().toUpperCase());
  const walk = (obj, prefix = "") => Object.entries(obj || {}).flatMap(([k, v]) => {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) return walk(v, path);
    return [{ field: path, type: Array.isArray(v) ? "array" : typeof v,
              ...(looksLikeMailCode(v) ? { value: v, candidate: true } : {}) }];
  });
  const all = walk(contact);
  return { ok: true, crd, contactId,
           candidates: all.filter((f) => f.candidate),
           fields: all.map((f) => f.field).sort() };
}


/* The raw Act! contact record for one CRD.
 *
 * actFields() above deliberately returns field NAMES without values -- it exists
 * to discover a schema, and dumping a person's CRM record was not its job. This
 * one does dump it, because while assigning CRDs to Act! contacts the question
 * "what does Act! actually hold for this person" comes up constantly and the
 * alternative is switching systems for every row.
 *
 * Read-only, and admin-gated at the route. It returns exactly what Act! returns.
 */
async function actContact(crd) {
  if (!configured()) return { ok: false, reason: "act_not_configured" };
  const contactId = contacts()[String(crd || "")];
  if (!contactId) return { ok: false, reason: "no_contact_for_crd", crd };
  try {
    const contact = await call("GET", `api/contacts/${encodeURIComponent(contactId)}`);
    return { ok: true, crd: String(crd), contactId, contact };
  } catch (err) { return { ok: false, reason: "act_read_failed", detail: err.message, crd }; }
}

// Returns what it did, so the caller can log it. Never throws -- the opt-out is
// already enforced locally by the time this runs.
async function setMailCode(contactId, target) {
  const field = mailCodeField();
  if (!MAIL_CODE_RANK[target]) return { attempted: false, reason: `unknown mail code "${target}"` };
  if (!field) return { attempted: false, reason: "ACT_MAIL_CODE_FIELD not set" };
  try {
    const contact = await call("GET", `api/contacts/${encodeURIComponent(contactId)}`);
    if (!contact || typeof contact !== "object") return { attempted: false, reason: "contact_unreadable" };

    // The real field is customFields.email__y_n -- a DOTTED path, because Act!
    // nests user-defined fields under customFields. A flat contact[field] lookup
    // would have found undefined, reported "no such property", and quietly never
    // written anything.
    const path = field.split(".");
    const parentOf = (root) => path.slice(0, -1).reduce((o, k) => (o == null ? o : o[k]), root);
    const leaf = path[path.length - 1];
    const parent = parentOf(contact);
    if (!parent || typeof parent !== "object")
      return { attempted: false, reason: `contact has no container for "${field}"` };
    if (!Object.prototype.hasOwnProperty.call(parent, leaf))
      return { attempted: false, reason: `contact has no property "${field}"` };

    const current = String(parent[leaf] || "").trim().toUpperCase();
    const rankNow = MAIL_CODE_RANK[current] || 0;
    // Never downgrade. A bounce writing N must not overwrite a U somebody set
    // deliberately -- an unsubscribe is a stronger statement than a bad address,
    // and losing it would put a person who asked to be left alone back in play.
    if (rankNow >= MAIL_CODE_RANK[target])
      return { attempted: false, reason: `already ${current || "(blank)"}`, current };

    // Whole entity back, with one property changed. Cloned rather than mutated so
    // a failed request cannot leave a half-edited object behind.
    const next = JSON.parse(JSON.stringify(contact));
    parentOf(next)[leaf] = target;
    await call("PUT", `api/contacts/${encodeURIComponent(contactId)}`, next);
    return { attempted: true, ok: true, from: current || "(blank)", to: target };
  } catch (err) {
    return { attempted: true, ok: false, reason: "act_write_failed", detail: err.message };
  }
}

/* ---------- do-not-email -> Act! -----------------------------------------
 * Records an email opt-out in Act! so it is visible to whoever works the CRM.
 *
 * Two writes, in order: an ACTIVITY AND HISTORY entry via the same
 * task-then-clear path logCall() uses and that was proven against the live
 * database, then the Mail Code field via setMailCodeOptOut() above.
 *
 * History first on purpose. It is the durable record of WHY the person is
 * suppressed and it needs no schema knowledge; the Mail Code is the flag people
 * filter on and depends on ACT_MAIL_CODE_FIELD being configured. If the field
 * write is unconfigured or refused, the history still stands alone.
 *
 * An earlier draft of this instead patched an invented "doNotEmail" flag and a
 * "contactNotes" string. Neither existed. The real field turned out to be the
 * "Mail Code" picklist, which already had a U = UNSUBSCRIBE value waiting --
 * a reminder that reading the customer's schema beats inferring one.
 *
 * The contact is resolved through the CRD crosswalk, exactly as call logging
 * does -- not by querying Act! on the address, which is another syntax nobody
 * here has verified.
 */
async function markDoNotEmail(address, crd, approvedContactId = "") {
  if (!configured()) return { ok: false, reason: "act_not_configured" };
  const email = String(address || "").trim().toLowerCase();
  if (!email) return { ok: false, reason: "no_address" };
  if (!approvedContactId) return { ok: false, reason: "approved_contact_required", email };

  const contactId = contacts()[String(crd || "")];
  // Reported rather than swallowed. The advisor is already suppressed locally,
  // so they are protected either way -- but somebody should know the CRM did
  // not get the memo.
  if (!contactId) return { ok: false, reason: crd ? "no_contact" : "no_crd", email };
  if (approvedContactId && String(contactId) !== String(approvedContactId))
    return { ok: false, reason: "approved_contact_changed", email };

  try {
    // Filed under the integration's own Act! user. Attributing an opt-out to a
    // rep would be false: the advisor did this, not the rep.
    const rep = await userIdFor(process.env.ACT_USER || "");
    if (!rep) return { ok: false, reason: "no_act_user" };
    const userId = rep.id;

    const now = new Date();
    const end = new Date(now.getTime() + 60 * 1000);
    const subject = `Email opt-out — ${email}`;
    const details = [
      "This contact used the preference link in an EIC email and asked to stop receiving email.",
      `Address: ${email}`,
      "They are suppressed in the advisor map and will be excluded from future batches automatically.",
      "Recorded from the advisor map.",
    ].join(String.fromCharCode(10));

    const task = await call("POST", `api/organizers/${userId}/tasks`, {
      subject, details,
      startTime: now.toISOString(), endTime: end.toISOString(),
      scheduledForId: userId, scheduledFor: rep.name,
      activityTypeId: 0,
      contacts: [{ id: contactId }],
      isPrivate: false, isTimeless: false,
    });
    const taskId = (task && task.id) || task;
    if (!taskId) return { ok: false, reason: "no_task_id", email };

    await call("PUT", `api/tasks/${taskId}/clear`, {
      result: { id: RESULTS["do-not-call"] ? RESULTS["do-not-call"].id : 0,
                name: "Email opt-out" },
      history: {
        startTime: now.toISOString(), endTime: end.toISOString(),
        includeDetailsToHistory: true, subject, details, isPrivate: false,
      },
    });
    // History first, Mail Code second. The history entry is the durable record
    // of WHY; the field is the flag people filter on. If the field write is
    // unconfigured or refused, the history still stands on its own.
    const mailCode = await setMailCode(contactId, MAIL_CODE_OPT_OUT);
    return { ok: true, email, contactId, mailCode };
  } catch (err) { return { ok: false, reason: "act_write_failed", detail: err.message, email }; }
}


/* A hard bounce, recorded on the contact.
 *
 * Writes Mail Code N -- "No mail; cannot locate; bounce backs" -- which is the
 * value EIC already uses for exactly this, by hand, on 4,947 contacts. Same
 * never-downgrade rule as the opt-out path, so a contact already marked U stays
 * U: someone asking not to be emailed outranks their address being broken.
 */
async function markHardBounce(crd, address, detail, approvedContactId = "") {
  if (!configured()) return { ok: false, reason: "act_not_configured" };
  if (!crd) return { ok: false, reason: "no_crd" };
  if (!approvedContactId) return { ok: false, reason: "approved_contact_required" };
  const contactId = contacts()[String(crd)];
  if (!contactId) return { ok: false, reason: "no_contact" };
  if (approvedContactId && String(contactId) !== String(approvedContactId))
    return { ok: false, reason: "approved_contact_changed" };
  const mailCode = await setMailCode(contactId, MAIL_CODE_UNREACHABLE);

  // History as well, for the same reason opt-outs get one: the code says WHAT,
  // and only the history says why and when.
  try {
    const rep = await userIdFor(process.env.ACT_USER || "");
    if (rep) {
      const now = new Date(), end = new Date(now.getTime() + 60000);
      const subject = `Email hard bounce — ${address}`;
      const details = [
        "An email from the advisor map was permanently rejected by the recipient's mail server.",
        `Address: ${address}`,
        detail ? `Reported reason: ${detail}` : "",
        "The address is suppressed in the advisor map and excluded from future batches.",
      ].filter(Boolean).join(String.fromCharCode(10));
      const task = await call("POST", `api/organizers/${rep.id}/tasks`, {
        subject, details, startTime: now.toISOString(), endTime: end.toISOString(),
        scheduledForId: rep.id, scheduledFor: rep.name, activityTypeId: 0,
        contacts: [{ id: contactId }], isPrivate: false, isTimeless: false,
      });
      const taskId = (task && task.id) || task;
      if (taskId) await call("PUT", `api/tasks/${taskId}/clear`, {
        result: { id: 0, name: "Email bounce" },
        history: { startTime: now.toISOString(), endTime: end.toISOString(),
                   includeDetailsToHistory: true, subject, details, isPrivate: false },
      });
    }
  } catch (err) { return { ok: !!mailCode.ok, mailCode, historyError: err.message }; }
  return { ok: true, mailCode };
}

module.exports = { configured, logCall, historyFor, diagnose, contactStatus,
  markDoNotEmail, markHardBounce, actFields, actContact, setMailCode, MAIL_CODE_RANK,
                   RESULTS, PURPOSES };
