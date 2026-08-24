/* Storage and identity for the call log.
 *
 * WHY TABLE STORAGE
 * -----------------
 * The volume here is a few thousand rows a year from a sales team of six. That
 * is far below the point where a relational database earns its cost or its
 * administration. Table Storage is pennies a month, needs no server, and gives
 * point lookups on the two access patterns that actually matter: "my queue" and
 * "is this CRD on the do-not-call list".
 *
 * WHY THE DATA MODEL IS CRM-NEUTRAL
 * ---------------------------------
 * Which CRM this firm ends up on is undecided. A disposition is stored as our
 * own record first and shipped outward second, so the record survives changing
 * CRM. Nothing here builds an Act! payload or knows an Act! id; the one
 * concession is `actStatus`, a single column recording whether the outward push
 * succeeded. That belongs on the row -- "which calls never reached the CRM" is
 * a question about these rows -- and it is named after the current CRM only
 * because pretending otherwise would make it harder to read.
 *
 * WHY IT REFUSES TO RUN UNCONFIGURED
 * ----------------------------------
 * The whole reason this endpoint exists is that dispositions in localStorage
 * are one browser's private scratchpad -- firm knowledge with no backup that
 * dies on a cache clear. An endpoint that silently accepted writes and dropped
 * them would be strictly worse than that, because it would look like it worked.
 * With no connection string configured every write returns 503 and says so.
 *
 * IDENTITY IS NEVER TAKEN FROM THE CLIENT
 * ---------------------------------------
 * Attribution comes from x-ms-client-principal, which Static Web Apps injects
 * at the edge from the session cookie. A body field claiming to be someone is
 * ignored. This is the whole reason the Azure move was worth doing.
 */
"use strict";

const { TableClient, odata } = require("@azure/data-tables");

const CONN = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
// A LOOKUP, not a pass-through. table("whatever") with no entry here resolves
// to undefined and every call against it throws -- which is exactly how the
// contact flags shipped broken: the store function, the API route and the
// client were all correct and the table simply had no name.
const TABLES = { log: "CallLog", queue: "DialQueue", dnc: "DoNotCall",
                 settings: "RepSettings", contactflags: "ContactFlags" };

// A queue entry is a SNAPSHOT, not a CRD reference. The desktop resolves an
// advisor from contacts.json and the field view resolves them from whichever
// geographic tile they sit in -- so a queue of bare ids built at a desk would
// be unopenable on a phone standing somewhere else. Carrying name, firm and
// number means the list is dialable on any device with nothing else loaded.
//
// Table Storage caps a string property at 64 KB. 250 snapshots is roughly a
// third of that; the serialized length is checked on write regardless, because
// a cap that is only true in theory is not a cap.
const MAX_QUEUE = 250;
const MAX_QUEUE_BYTES = 60 * 1024;

const clients = new Map();
let ensured = new Set();

function configured() {
  return Boolean(CONN);
}

async function table(which) {
  if (!configured()) {
    const err = new Error(
      "Call logging is not configured: AZURE_STORAGE_CONNECTION_STRING is " +
      "unset on the Static Web App. Nothing was saved."
    );
    err.statusCode = 503;
    throw err;
  }
  const name = TABLES[which];
  if (!clients.has(name)) {
    clients.set(name, TableClient.fromConnectionString(CONN, name, {
      allowInsecureConnection: false,
    }));
  }
  const client = clients.get(name);
  // Created on demand rather than by a deployment step, because a deployment
  // step is a thing someone can forget on the day they rotate the account.
  if (!ensured.has(name)) {
    await client.createTable().catch((e) => {
      if (e.statusCode !== 409) throw e;   // 409 = already there, which is fine
    });
    ensured.add(name);
  }
  return client;
}

/* ---------- identity ---------------------------------------------------- */
/* Shape, from Static Web Apps:
 *   { identityProvider, userId, userDetails, userRoles: [...], claims: [...] }
 * userId is the stable per-tenant object id; userDetails is the UPN, which is
 * what a human wants to read in a log. Both are stored: the id is the key, the
 * name is for display and survives the person leaving. */
function identity(req) {
  const raw = (req.headers && (req.headers["x-ms-client-principal"] ||
                               req.headers["X-MS-CLIENT-PRINCIPAL"])) || "";
  if (raw) {
    try {
      const p = JSON.parse(Buffer.from(raw, "base64").toString("utf8"));
      if (p && p.userId) {
        const roles = new Set(p.userRoles || []);
        for (const claim of (p.claims || [])) {
          const type = String(claim.typ || claim.type || "").toLowerCase();
          if (type === "roles" || type.endsWith("/role") || type.endsWith("/roles"))
            for (const role of String(claim.val || claim.value || "").split(","))
              if (role.trim()) roles.add(role.trim());
        }
        return { id: String(p.userId), name: String(p.userDetails || ""),
                 roles: [...roles], dev: false };
      }
    } catch { /* fall through to the refusal below */ }
  }
  // Local development only, and only when explicitly opted in. Without this
  // gate a misconfigured production route would file every rep's calls under
  // one shared pseudo-user and nobody would notice until an audit.
  if (process.env.ALLOW_DEV_IDENTITY === "1") {
    return { id: "dev-local", name: "local developer", roles: [], dev: true };
  }
  const err = new Error("Not signed in.");
  err.statusCode = 401;
  throw err;
}

/* ---------- keys -------------------------------------------------------- */
// Descending time: Table Storage sorts RowKey ascending, and every query we run
// wants newest first. Subtracting from a fixed ceiling gets that for free
// rather than by fetching everything and sorting in memory.
const CEILING = 9999999999999;
function descendingKey(when) {
  const inv = String(CEILING - when.getTime()).padStart(13, "0");
  return `${inv}-${Math.random().toString(36).slice(2, 8)}`;
}

const clean = (v, max) => (v === undefined || v === null)
  ? "" : String(v).slice(0, max === undefined ? 256 : max);

/* ---------- the log ------------------------------------------------------ */
async function appendEvent(who, body) {
  const client = await table("log");
  const at = new Date();
  const entity = {
    partitionKey: who.id,
    rowKey: descendingKey(at),
    atUtc: at.toISOString(),
    userName: clean(who.name),
    crd: clean(body.crd, 32),
    advisorName: clean(body.name),
    firm: clean(body.firm),
    phone: clean(body.phone, 40),
    phoneKind: clean(body.phoneKind, 24),
    kind: clean(body.kind, 24) || "outcome",
    disposition: clean(body.disposition, 32),
    // Why the call was made, from a closed list. Distinct from the note because
    // it is the field that becomes the Act! history SUBJECT -- the CRM's Title
    // column -- which makes it the only part of a logged call that is legible
    // without opening the record. Empty on every call logged before it existed.
    purpose: clean(body.purpose, 32),
    // The one free-text field, offered by both views. It reaches Act! in the
    // history details, so it is the rep's own words in front of the whole firm.
    note: clean(body.note, 4000),
    sessionId: clean(body.sessionId, 64),
    // Whether this outcome reached Act!, filled in immediately after. Stored on
    // the row rather than in a separate table so that "which calls are missing
    // from the CRM" is a query over the same rows the calls live in.
    actStatus: "",
  };
  await client.createEntity(entity);
  return { id: entity.rowKey, at: entity.atUtc };
}

// A MERGE, not a replace: this runs after the row exists and must touch exactly
// one column. `updateEntity(..., "Replace")` here would drop every field the
// caller did not resend -- the whole call outcome -- to record a status about
// it. Unconditional by design; there is no concurrent writer for this column,
// and an etag round trip to store a status string is not worth the latency on
// the rep's button press.
async function setActStatus(who, rowKey, status) {
  const client = await table("log");
  await client.updateEntity(
    { partitionKey: who.id, rowKey, actStatus: clean(status, 200) }, "Merge");
}

// History for ONE advisor, across every user -- "has anyone here called them
// already?" is a question a rep needs answered before dialling, and it is the
// question a private per-user log can never answer.
//
// Cross-partition, so it scans. At this firm's volume that is the right trade
// against maintaining a second copy of every row keyed the other way; if the
// log ever reaches six figures, add that copy rather than an index.
//
// IT READS EVERY MATCH BEFORE SORTING. It used to stop at limit * 4 rows and
// sort afterwards -- but the scan arrives in PartitionKey order, which is user
// id order, so the truncation kept whichever colleagues sorted first and could
// silently drop the newest call in the firm. That is the exact question this
// function exists to answer. The filter already narrows to one advisor, so the
// unbounded read is a handful of rows; a cap is a safety valve, not the plan.
const CRD_SCAN_CAP = 5000;

async function eventsForCrd(crd, limit) {
  const client = await table("log");
  const out = [];
  const iter = client.listEntities({
    queryOptions: { filter: odata`crd eq ${String(crd)}` },
  });
  for await (const e of iter) {
    out.push(e);
    if (out.length >= CRD_SCAN_CAP) break;
  }
  // RowKey is an inverted timestamp, so ascending IS newest-first.
  out.sort((a, b) => String(a.rowKey).localeCompare(String(b.rowKey)));
  return out.slice(0, limit);
}

// `since` is what makes cycle progress cheap. RowKey is an inverted timestamp,
// so "everything newer than T" is a contiguous range at the START of the
// partition -- the scan stops as soon as it passes T rather than reading a
// year of history to count this week's calls.
async function recentForUser(who, limit, since) {
  const client = await table("log");
  const out = [];
  const iter = client.listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${who.id}` },
  });
  const cutoff = since ? new Date(since).getTime() : null;
  for await (const e of iter) {          // already newest-first by RowKey
    if (cutoff && new Date(e.atUtc).getTime() < cutoff) break;
    out.push(e);
    if (out.length >= limit) break;
  }
  return out;
}

/* ---------- the queue ---------------------------------------------------- */
// A list is one row; the row key IS the list id. This was hardcoded to
// "current", which is why there could only ever be one -- everything else about
// the shape already supported many. "current" is kept as the id of the working
// list so queues that exist today survive untouched.
const DEFAULT_LIST = "current";

function listId(v) {
  const s = String(v || DEFAULT_LIST).toLowerCase().replace(/[^a-z0-9-]/g, "").slice(0, 40);
  return s || DEFAULT_LIST;
}

function summarise(e) {
  let n = 0;
  try { n = JSON.parse(e.items || "[]").length; } catch { n = 0; }
  return {
    id: e.rowKey,
    name: e.name || (e.rowKey === DEFAULT_LIST ? "Call list" : e.rowKey),
    count: n,
    cursor: Number(e.cursor) || 0,
    cycle: Number(e.cycle) || 1,
    cycleStartedUtc: e.cycleStartedUtc || "",
    updatedUtc: e.updatedUtc || "",
    // The row's version, so a client that read it can prove it is writing on
    // top of what it read. A queue save replaces the WHOLE row, and the same
    // rep works from a desk and a phone -- without this, the second device to
    // save silently erases whatever the first one added.
    etag: e.etag || "",
  };
}

async function getQueue(who, id) {
  const client = await table("queue");
  const key = listId(id);
  try {
    const e = await client.getEntity(who.id, key);
    return { ...summarise(e), items: JSON.parse(e.items || "[]") };
  } catch (e) {
    if (e.statusCode === 404) {
      return { id: key, name: key === DEFAULT_LIST ? "Call list" : key,
               items: [], count: 0, cursor: 0, cycle: 1,
               cycleStartedUtc: "", updatedUtc: "" };
    }
    throw e;
  }
}

// Summaries only -- the items are the bulk, and a picker needs a name and a
// count, not 250 snapshots per list.
async function listQueues(who) {
  const client = await table("queue");
  const out = [];
  const iter = client.listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${who.id}` },
  });
  for await (const e of iter) out.push(summarise(e));
  out.sort((a, b) => String(b.updatedUtc).localeCompare(String(a.updatedUtc)));
  return out;
}

async function deleteQueue(who, id) {
  const client = await table("queue");
  await client.deleteEntity(who.id, listId(id)).catch((e) => {
    if (e.statusCode !== 404) throw e;
  });
  return { id: listId(id), deleted: true };
}

// Only these fields, whatever the client sends. A queue row is not a place to
// accumulate whatever the calling page happened to have in scope.
function snapshot(it) {
  return {
    crd: clean(it && it.crd, 32),
    name: clean(it && it.name, 120),
    firm: clean(it && it.firm, 120),
    phone: clean(it && it.phone, 40),
    phoneKind: clean(it && it.phoneKind, 24),
    city: clean(it && it.city, 60),
    state: clean(it && it.state, 8),
    email: clean(it && it.email, 160),
  };
}

async function putQueue(who, opts) {
  const client = await table("queue");
  const { id, name, items, cursor, cycle, cycleStartedUtc, etag } = opts || {};
  let trimmed = (items || []).filter((it) => it && it.crd)
                             .slice(0, MAX_QUEUE).map(snapshot);
  // Shed from the tail until it fits, rather than failing the whole write and
  // losing a queue someone spent ten minutes assembling.
  let payload = JSON.stringify(trimmed);
  while (payload.length > MAX_QUEUE_BYTES && trimmed.length) {
    trimmed = trimmed.slice(0, -1);
    payload = JSON.stringify(trimmed);
  }
  const key = listId(id);
  const entity = {
    partitionKey: who.id,
    rowKey: key,
    name: clean(name, 60) || (key === DEFAULT_LIST ? "Call list" : key),
    items: payload,
    cursor: Math.max(0, Math.min(Number(cursor) || 0, trimmed.length)),
    // A cycle is one pass through a saved list. Progress is NOT stored here --
    // it is derived from the call log since cycleStartedUtc, so it stays right
    // when the list is reordered, added to, or worked from two devices.
    cycle: Math.max(1, Number(cycle) || 1),
    cycleStartedUtc: cycleStartedUtc || new Date().toISOString(),
    updatedUtc: new Date().toISOString(),
    userName: clean(who.name),
  };
  // With an etag this is a guarded replace: if the row moved since the client
  // read it, the write is REFUSED rather than silently winning. Without one it
  // is an ordinary upsert, so a first write (no row yet) and any older client
  // still work.
  let written;
  if (etag) {
    try {
      const r = await client.updateEntity(entity, "Replace", { etag });
      written = { ...entity, etag: r.etag };
    } catch (e) {
      if (e.statusCode === 412 || e.statusCode === 404) {
        const err = new Error(
          "This list changed on another device. Reload to pick up the newer "
          + "version — nothing was overwritten.");
        err.statusCode = 409;
        throw err;
      }
      throw e;
    }
  } else {
    const r = await client.upsertEntity(entity, "Replace");
    written = { ...entity, etag: (r && r.etag) || "" };
  }
  return { ...summarise(written), items: trimmed,
           dropped: Math.max(0, (items || []).length - trimmed.length) };
}

/* ---------- do not call --------------------------------------------------- */
// Firm-wide, not per-user. A rep who is told "take me off your list" has been
// told on behalf of the firm, and the next rep dialling the same person three
// weeks later is the exact failure this prevents. One partition, so listing it
// is a single cheap scan and the whole set ships to the client.
async function listDnc() {
  const client = await table("dnc");
  const out = [];
  const iter = client.listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${"dnc"}` },
  });
  for await (const e of iter) {
    out.push({ crd: e.rowKey, by: e.userName || "", at: e.atUtc || "",
               reason: e.reason || "" });
  }
  return out;
}

/* KEY CONTACT and DUE DILIGENCE, per advisor.
 *
 * FIRM-WIDE, like the do-not-call list and unlike a call queue. Which person at
 * a firm runs manager due diligence is a fact about that firm, not a private
 * note: if Kate works it out, Will should not have to work it out again. Each
 * flag records who set it and when, so the knowledge has a source.
 *
 * Two independent booleans rather than one "role" field. They are usually the
 * same person and sometimes not, and two flags make "both" render as two icons
 * with no third state to invent.
 *
 * Deletable, unlike a do-not-call entry: this is sales knowledge that goes out
 * of date, not a compliance suppression.
 */
const FLAG_KINDS = new Set(["key", "dd"]);

/* A FLAG IS A SET OF REPS, not a boolean.
 *
 * It was one shared true/false with a single name attached, and that model
 * broke the moment the standing lists became personal: the star on a card read
 * "somebody marked this person", so a second rep saw it already lit, and
 * clicking it CLEARED the first rep's flag instead of adding their own. One
 * rep could silently delete another's, and there was no way to join.
 *
 * So `keyBy`/`ddBy` carry every rep who marked them, newline-separated. The
 * flag is on when the set is non-empty; a rep's own list is the rows their
 * name appears in; and a click adds or removes exactly one member.
 *
 * Bounded in practice -- a handful of reps per advisor -- and capped anyway,
 * because a Table Storage string property is not a place to discover a limit.
 */
const FLAG_MEMBER_CAP = 40;

function flagMembers(raw, fallback) {
  const text = String(raw == null ? "" : raw).trim();
  const source = text || String(fallback || "").trim();
  const seen = new Map();
  for (const part of source.split(/[\n;,]/)) {
    const name = part.trim();
    if (!name) continue;
    // Case-insensitive identity, first spelling wins -- a UPN is not
    // case-sensitive and two casings of one rep are one rep.
    const key = name.toLowerCase();
    if (!seen.has(key)) seen.set(key, name);
  }
  return [...seen.values()].slice(0, FLAG_MEMBER_CAP);
}

const flagMembersText = (list) => list.join(String.fromCharCode(10));

async function listFlags() {
  const client = await table("contactflags");
  const out = [];
  for await (const e of client.listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${"flags"}` } })) {
    // Rows written before the set existed carry one `userName`. Treated as a
    // set of one, which is the best attribution those rows ever had.
    const last = e.userName || "";
    const keyBy = e.keyContact === true ? flagMembers(e.keyBy, last) : [];
    const ddBy = e.dueDiligence === true ? flagMembers(e.ddBy, last) : [];
    out.push({ crd: e.rowKey, key: keyBy.length > 0, dd: ddBy.length > 0,
               name: e.advisorName || "", firmCrd: e.firmCrd || "",
               by: last, at: e.atUtc || "",
               keyBy, ddBy });
  }
  return out;
}

async function setFlag(who, crd, kind, on, name, firmCrd) {
  if (!FLAG_KINDS.has(String(kind))) {
    const err = new Error("Unknown flag."); err.statusCode = 400; throw err;
  }
  const rowKey = String(crd).slice(0, 32);
  const client = await table("contactflags");
  // A 404 means "no flags yet", which is the normal case and not an error.
  let existing = null;
  try { existing = await client.getEntity("flags", rowKey); }
  catch (e) { if (e.statusCode !== 404) throw e; }

  const last = (existing && existing.userName) || "";
  const members = {
    key: existing && existing.keyContact === true ? flagMembers(existing.keyBy, last) : [],
    dd: existing && existing.dueDiligence === true ? flagMembers(existing.ddBy, last) : [],
  };
  // ONE MEMBER CHANGES, and only ever the caller's own. Another rep's mark is
  // not this rep's to remove.
  const me = clean(who.name);
  const target = kind === "key" ? "key" : "dd";
  const without = members[target].filter((n) => n.toLowerCase() !== me.toLowerCase());
  members[target] = on
    ? [...without, me].slice(0, FLAG_MEMBER_CAP)
    : without;

  // Nobody left on either flag: remove the row rather than keeping a tombstone
  // that says "this person is nothing", which would ship to every client
  // forever. Note this now means nobody AT ALL, not merely nobody recently.
  if (!members.key.length && !members.dd.length) {
    if (existing) await client.deleteEntity("flags", rowKey);
    return { crd: rowKey, key: false, dd: false, keyBy: [], ddBy: [], removed: true };
  }
  const at = new Date().toISOString();
  await client.upsertEntity({
    partitionKey: "flags", rowKey,
    keyContact: members.key.length > 0, dueDiligence: members.dd.length > 0,
    advisorName: clean(name || (existing && existing.advisorName), 256),
    firmCrd: clean(firmCrd || (existing && existing.firmCrd), 32),
    keyBy: flagMembersText(members.key), ddBy: flagMembersText(members.dd),
    userName: me, userId: who.id, atUtc: at,
  }, "Replace");
  return { crd: rowKey, key: members.key.length > 0, dd: members.dd.length > 0,
           keyBy: members.key, ddBy: members.dd,
           name: clean(name || (existing && existing.advisorName), 256),
           firmCrd: clean(firmCrd || (existing && existing.firmCrd), 32),
           by: me, at };
}

async function addDnc(who, crd, reason, name) {
  const client = await table("dnc");
  const at = new Date().toISOString();
  await client.upsertEntity({
    partitionKey: "dnc",
    rowKey: String(crd).slice(0, 32),
    advisorName: clean(name),
    userName: clean(who.name),
    userId: who.id,
    atUtc: at,
    reason: clean(reason, 500),
  }, "Replace");
  // The SAME shape listDnc returns. The client stores whichever it received
  // last, so a POST answering with entity-shaped fields and a GET answering
  // with these would make "who added this" appear and vanish depending on the
  // order of two unrelated requests.
  return { crd: String(crd), by: clean(who.name), at, reason: clean(reason, 500) };
}

/* ---------- per-rep settings ----------------------------------------------
 * WHY THIS IS NOT localStorage.
 *
 * Every preference here was going to be a browser key, and this application
 * has already learned what that costs: the active-list preference started in
 * localStorage and the phone kept forgetting where the desk had got to. A rep
 * works the same territory from two devices, and a "default" that is different
 * on each is not a default -- it is two settings with one name.
 *
 * So one row per user, keyed on the Entra object id like everything else. It
 * is a handful of bytes read once at load.
 *
 * A CLOSED SET OF KEYS, and unknown ones are DROPPED rather than stored. A
 * settings row that accepts whatever the page had in scope becomes a junk
 * drawer nobody dares delete from, and it is the one row read on every single
 * page load.
 */
const SETTING_KEYS = {
  // The map's opening scope -- a state code or a territory key. Not validated
  // against a list here: the options come from territories.yaml and the SEC
  // feed, and a server that had its own copy of that list would be a second
  // copy to keep in step. A bad value costs one wrong opening view.
  defaultScope: 24,
  // Which call list opens first, on both devices. This is the one that was
  // living in localStorage and disagreeing with itself.
  defaultListId: 64,
  /* Who to copy on every email this rep sends. Off unless set.
   *
   *   copySelf       ""  |  "cc"  |  "bcc"
   *   copyInternal   ""  |  "cc"  |  "bcc"
   *   copyInternalTo an address, which the SERVER re-checks against the
   *                  EMAIL_INTERNAL_RECIPIENTS allowlist on every send -- a
   *                  value saved here is a preference, never a permission.
   *
   * Stored as strings like every other setting; the length caps are the only
   * validation this layer does.
   */
  copySelf: 8,
  copyInternal: 8,
  copyInternalTo: 254,
  // The field view's starting point when "Near me" is not what is wanted --
  // a rep planning a trip to a city they are not standing in.
  homeLabel: 80,
  homeLat: 24,
  homeLon: 24,
  // Signs the email drafts. Free text on purpose: it goes into the rep's own
  // outgoing mail, not into the CRM or anyone else's screen.
  //
  // 1500, NOT 600. A real signature from Outlook -- name, title, address, two
  // numbers, a web address and the SEC registration disclaimer -- measures 603
  // characters, so the original cap cut it mid-sentence. Silently, and in the
  // disclaimer, which is the one part of an email where a truncated version is
  // worse than none at all. 1500 clears that with room for a longer legal
  // block; beyond it the mailto: URL itself becomes the binding limit, which
  // the settings screen warns about rather than storing and hoping.
  emailSignature: 1500,
  // Auto-dial, which was per-device for no reason anyone chose.
  autoDialOn: 8,
  autoDialDelay: 8,
  autoDialAnnounce: 8,
  // The field view's opening radius, by index into its own RADII table.
  fieldRadius: 8,
};

async function getSettings(who) {
  const client = await table("settings");
  try {
    const e = await client.getEntity(who.id, "settings");
    const out = {};
    for (const k of Object.keys(SETTING_KEYS)) {
      if (e[k] !== undefined && e[k] !== null) out[k] = String(e[k]);
    }
    return out;
  } catch (e) {
    if (e.statusCode === 404) return {};
    throw e;
  }
}

// A MERGE of the keys actually sent, not a replace. The field view saves a
// radius and the desktop saves a scope, and a replace would mean whichever
// page saved last silently cleared the other's preference.
async function putSettings(who, patch) {
  const client = await table("settings");
  const entity = { partitionKey: who.id, rowKey: "settings" };
  let touched = 0;
  for (const [k, max] of Object.entries(SETTING_KEYS)) {
    if (!(k in (patch || {}))) continue;
    entity[k] = clean(patch[k], max);
    touched++;
  }
  if (!touched) return getSettings(who);
  // upsert: the row does not exist until the first preference is set, and
  // creating it on read would write on every page load.
  await client.upsertEntity(entity, "Merge");
  return getSettings(who);
}

/* ---------- http helpers -------------------------------------------------- */
function ok(context, body) {
  context.res = {
    status: 200,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    body: JSON.stringify(body),
  };
}

function fail(context, err) {
  const status = err && err.statusCode ? err.statusCode : 500;
  context.res = {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    body: JSON.stringify({
      error: (err && err.message) || "Unexpected error",
      configured: configured(),
    }),
  };
  if (status >= 500) context.log.error(err);
}

module.exports = {
  configured, identity, appendEvent, setActStatus, eventsForCrd, recentForUser,
  getQueue, putQueue, listQueues, deleteQueue, listDnc, addDnc,
  listFlags, setFlag,
  getSettings, putSettings, SETTING_KEYS,
  ok, fail, MAX_QUEUE, DEFAULT_LIST,
};
