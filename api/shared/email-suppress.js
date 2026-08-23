"use strict";

/* The do-not-email list.
 *
 * Deliberately keyed on the EMAIL ADDRESS, not on CRD. A person who asks not to
 * be emailed is asking about the address they received the mail at. Keying on
 * CRD would suppress an advisor's every address on the strength of one opt-out,
 * and -- worse in the other direction -- would silently fail to suppress anyone
 * whose address we hold without a CRD match. The address is what was mailed, so
 * the address is what is suppressed.
 *
 * Entries are additive and there is no delete here, for the same reason the
 * do-not-call list has none: removing a suppression is a compliance decision
 * taken deliberately in the storage account, not a click.
 */

const crypto = require("crypto");
const { TableClient } = require("@azure/data-tables");

const TABLE = "EmailSuppression";
let client;

async function table() {
  const conn = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!conn) throw new Error("Email storage is not configured.");
  client ||= TableClient.fromConnectionString(conn, TABLE, { allowInsecureConnection: false });
  await client.createTable().catch((e) => { if (e.statusCode !== 409) throw e; });
  return client;
}

const norm = (email) => String(email || "").trim().toLowerCase();

// Addresses are the row key, and Azure Table keys forbid / \ # ? and control
// characters. None are legal in an address anyway, but a hash keeps a malformed
// input from becoming a 400 from storage rather than a clean rejection here.
const keyFor = (email) => crypto.createHash("sha256").update(norm(email)).digest("hex");

/* ---------- the signed token -------------------------------------------
 * The preference link has to work for someone who is not signed in and never
 * will be, so the URL itself carries the authority. It names one address and is
 * signed with a server-only key: without the signature anyone could unsubscribe
 * anyone by editing a query string, which is both a nuisance and a way to
 * silence a competitor's inbox.
 *
 * No expiry. A footer link should still work when someone digs the email out of
 * their archive a year later, and the token grants exactly one narrow power --
 * suppressing the address already written inside it.
 */
function secret() {
  const raw = process.env.EMAIL_UNSUBSCRIBE_SECRET || "";
  if (!raw) return null;                    // unconfigured -> no link is emitted
  return crypto.createHash("sha256").update(raw).digest();
}

const b64url = (buf) => buf.toString("base64")
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64url = (s) => Buffer.from(String(s || "").replace(/-/g, "+").replace(/_/g, "/"), "base64");

// The CRD rides along inside the SIGNED payload so the opt-out can be written
// back to Act! through the proven CRD -> contact crosswalk. It is signed, not
// merely appended, so it cannot be swapped to point at another contact. The
// suppression itself is still keyed on the address alone -- the CRD is only
// how the CRM is told, never what decides who is suppressed.
function signToken(email, crd = "") {
  const key = secret();
  if (!key) return "";
  const claim = `${norm(email)}|${String(crd || "").replace(/[^0-9]/g, "").slice(0, 12)}`;
  const payload = b64url(Buffer.from(claim, "utf8"));
  const mac = b64url(crypto.createHmac("sha256", key).update(payload).digest()).slice(0, 27);
  return `${payload}.${mac}`;
}

// Returns the address the token names, or null. Timing-safe, and it verifies
// before it decodes -- an unverified payload is attacker-controlled input and
// nothing downstream should see it.
function readToken(token) {
  const key = secret();
  if (!key) return null;
  const [payload, mac] = String(token || "").split(".");
  if (!payload || !mac) return null;
  const want = b64url(crypto.createHmac("sha256", key).update(payload).digest()).slice(0, 27);
  const a = Buffer.from(mac, "utf8"), b = Buffer.from(want, "utf8");
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  const claim = unb64url(payload).toString("utf8");
  // Tokens minted before the CRD was added carry the bare address. Still valid:
  // those emails are already in inboxes and their links must keep working.
  const [email, crd = ""] = claim.split("|");
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254) return null;
  return { email, crd };
}

// The URL that goes in the footer. Empty when either half is unconfigured, which
// leaves the sentence intact but unlinked rather than shipping a dead link.
function manageUrl(email, crd = "") {
  const base = String(process.env.EMAIL_PUBLIC_BASE_URL || "").replace(/\/+$/, "");
  const token = signToken(email, crd);
  return base && token ? `${base}/api/email-preferences?t=${encodeURIComponent(token)}` : "";
}


/* ---------- the Act! Mail Code floor ------------------------------------
 * Years of opt-outs were already recorded in Act! before this application had a
 * suppression list of its own. On the 2026-08-13 export, 1,560 addresses that
 * Act! marks do-not-email were still selectable and sendable here -- 701 of them
 * explicit UNSUBSCRIBEs. That is not a gap worth carrying into a first real send.
 *
 * Shipped as static data rather than imported into the table, for two reasons:
 * it applies the moment it deploys with no migration step to forget, and it
 * cannot be edited away from inside the app. The live table is still consulted
 * as well and covers everything since the export.
 *
 * Rebuild with src/build_act_mail_codes.py when a fresh export lands.
 */
let FLOOR = null;
function mailCodeFloor() {
  if (FLOOR) return FLOOR;
  try {
    // eslint-disable-next-line global-require
    const raw = require("./act_mail_codes.json");
    FLOOR = {
      byAddress: new Map(Object.entries(raw.addresses || {})
        .map(([a, code]) => [String(a).toLowerCase(), code])),
      // Keyed on the contact as well as the address. Act! users overwrite the
      // email field with a note -- "unsubscribed 3/27/26", "retired" -- which
      // destroys the address but not the opt-out. On the 2026-08-13 export that
      // hid 823 people who remained reachable through the address WE hold from
      // SEC data.
      byCrd: new Map(Object.entries(raw.crds || {})
        .map(([crd, code]) => [String(crd), code])),
      builtUtc: raw.built_utc || "",
    };
  } catch {
    // Absent file means the floor is not applied. Not thrown: a missing baseline
    // must not take email down, and the live table still works.
    FLOOR = { byAddress: new Map(), byCrd: new Map(), builtUtc: "" };
  }
  return FLOOR;
}

const MAIL_CODE_REASON = {
  U: "asked to unsubscribe",
  NC: "asked not to receive mail",
  N: "recorded in the CRM as unreachable or bouncing",
  BB: "recorded in the CRM as bouncing",
};
const mailCodeReason = (code) => MAIL_CODE_REASON[code] || "marked do-not-email in the CRM";

// The Mail Code standing against one recipient, by address OR by contact id.
function floorCodeFor(recipient) {
  const f = mailCodeFloor();
  const address = norm(recipient && recipient.email);
  const crd = String((recipient && (recipient.contactId || recipient.crd)) || "").trim();
  return (address && f.byAddress.get(address)) || (crd && f.byCrd.get(crd)) || null;
}

/* ---------- the list ---------------------------------------------------- */

async function suppress(email, { source = "unsubscribe-link", note = "" } = {}) {
  const address = norm(email);
  if (!address) throw new Error("No address supplied.");
  const t = await table();
  const row = { partitionKey: "email", rowKey: keyFor(address), address, source,
                note: String(note || "").slice(0, 500), addedUtc: new Date().toISOString(),
                actSynced: false };
  // Upsert, not create: a second click is a no-op that must not 409. It keeps
  // the ORIGINAL timestamp though -- when they first asked is the fact that
  // matters, and re-stamping it every click would erase it.
  try { await t.createEntity(row); return { address, added: true }; }
  catch (e) {
    if (e.statusCode !== 409) throw e;
    return { address, added: false, alreadySuppressed: true };
  }
}

async function isSuppressed(email) {
  const address = norm(email);
  if (!address) return false;
  if (mailCodeFloor().byAddress.has(address)) return true;
  const t = await table();
  try { await t.getEntity("email", keyFor(address)); return true; }
  catch (e) { if (e.statusCode === 404) return false; throw e; }
}

// One round trip for a whole batch rather than one per recipient. A 400-person
// batch checked serially is 400 sequential storage calls on the approval path.
async function suppressedAmong(emails) {
  const found = await tableAddresses();
  const f = mailCodeFloor();
  return new Set((emails || []).map(norm)
    .filter((e) => found.has(e) || f.byAddress.has(e)));
}

async function tableAddresses() {
  const found = new Set();
  try {
    const t = await table();
    for await (const row of t.listEntities({ queryOptions: { filter: "PartitionKey eq 'email'" } }))
      found.add(String(row.address || "").toLowerCase());
  } catch (err) {
    // Deliberately fatal. Failing open would mean mailing known opt-outs during
    // a storage blip, which is the one outcome this whole file exists to prevent.
    err.message = `Suppression list unavailable, so no batch can be built: ${err.message}`;
    throw err;
  }
  return found;
}

/* The check the send path actually uses.
 *
 * Takes recipients rather than addresses so the CRM contact id participates, and
 * returns WHY each one is blocked -- a rep told "3 removed" learns nothing, while
 * "3 removed: two asked to unsubscribe, one is bouncing" is actionable.
 */
async function blockedAmong(recipients) {
  const found = await tableAddresses();
  const out = new Map();
  for (const r of recipients || []) {
    const address = norm(r && r.email);
    if (!address) continue;
    if (found.has(address)) { out.set(address, "asked to unsubscribe"); continue; }
    const code = floorCodeFor(r);
    if (code) out.set(address, mailCodeReason(code));
  }
  return out;
}

async function list() {
  const t = await table();
  const out = [];
  for await (const row of t.listEntities({ queryOptions: { filter: "PartitionKey eq 'email'" } }))
    out.push({ address: row.address, source: row.source, note: row.note,
               addedUtc: row.addedUtc, actSynced: !!row.actSynced });
  out.sort((a, b) => String(b.addedUtc).localeCompare(String(a.addedUtc)));
  return out;
}

// Marked separately from the write, so a failed Act! push leaves the suppression
// in force locally and retryable rather than losing it.
async function markActSynced(address) {
  const t = await table();
  await t.updateEntity({ partitionKey: "email", rowKey: keyFor(address),
                         actSynced: true, actSyncedUtc: new Date().toISOString() }, "Merge");
}

async function pendingActSync(limit = 100) {
  return (await list()).filter((r) => !r.actSynced).slice(0, limit);
}

module.exports = { suppress, isSuppressed, suppressedAmong, blockedAmong,
                   mailCodeFloor, floorCodeFor, list, markActSynced,
                   pendingActSync, signToken, readToken, manageUrl, norm };
