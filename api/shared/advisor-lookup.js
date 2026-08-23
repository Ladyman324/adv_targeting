"use strict";

/* Is this address one of our advisors?
 *
 * The reply sweep asks that about every message it sees, and the answer is
 * almost always NO -- internal mail, newsletters, IT notices, the rep's own
 * correspondence. Only a YES may be persisted at all, so this lookup is the
 * privacy boundary as much as it is a filter.
 *
 * WHY A BLOB IN MEMORY AND NOT A TABLE QUERY
 * ------------------------------------------
 * The access pattern is many lookups, mostly misses, against a set that changes
 * only when the pipeline runs. A per-message Table Storage round trip would
 * spend its entire budget proving that the CFO's email is not from an advisor.
 * One ~1.4 MB blob read per cold start, held in a Map, makes a miss free.
 *
 * Built by src/export_advisor_emails.py and uploaded separately -- see that
 * module's docstring for why the ambiguous addresses are published rather than
 * resolved.
 *
 * FAILS CLOSED
 * ------------
 * If the blob cannot be read, `ready` is false and `lookup()` throws rather
 * than returning "not an advisor" for everybody. A sweep that cannot tell
 * advisors from strangers must stop, not quietly conclude that nobody wrote to
 * us -- that failure would look exactly like a quiet week.
 */

const zlib = require("zlib");
const { BlobServiceClient } = require("@azure/storage-blob");
const core = require("./email-core");

const CONTAINER = process.env.ADVISOR_LOOKUP_CONTAINER || "lookups";
const BLOB = process.env.ADVISOR_LOOKUP_BLOB || "advisor_emails.json.gz";

/* Cached, but NOT forever.
 *
 * A warm Function instance lives for hours. Caching for the life of the process
 * meant a freshly uploaded universe was ignored until something happened to
 * recycle it -- so an advisor added this morning could go unrecognised all day,
 * and their reply would be discarded as not-ours. Silently, because that is
 * exactly what the filter is supposed to do to strangers.
 *
 * Re-read on a timer rather than on every call: the file is ~1.4 MB and the
 * universe changes when the pipeline runs, which is daily at most.
 */
const TTL_MS = Number(process.env.ADVISOR_LOOKUP_TTL_MS || 15 * 60 * 1000);
let cache = null;
let loadedAt = 0;

function normalise(address) {
  return String(address || "").trim().toLowerCase();
}

function domainOf(address) {
  const at = normalise(address).lastIndexOf("@");
  return at < 0 ? "" : normalise(address).slice(at + 1);
}

async function download() {
  const conn = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!conn) throw new Error("AZURE_STORAGE_CONNECTION_STRING is not set.");
  const client = BlobServiceClient.fromConnectionString(conn);
  const blob = client.getContainerClient(CONTAINER).getBlobClient(BLOB);
  const buffer = await blob.downloadToBuffer();
  // Stored gzipped. Written with mtime 0 so the bytes are stable for a given
  // universe; identity comes from contentHash inside, never the file itself.
  return JSON.parse(zlib.gunzipSync(buffer).toString("utf8"));
}

async function load(force = false) {
  const fresh = cache && !force && (Date.now() - loadedAt) < TTL_MS;
  if (fresh) return cache;
  let payload;
  try {
    payload = await download();
  } catch (err) {
    /* A refresh failure must not throw away a universe we already hold.
     *
     * Failing closed is right when there is NOTHING -- a sweep that cannot tell
     * advisors from strangers must stop. It is wrong when we have a perfectly
     * good copy from ten minutes ago: discarding it would turn a transient blob
     * error into "nobody wrote to us today".
     */
    if (cache) return cache;
    throw err;
  }
  cache = {
    byEmail: new Map(Object.entries(payload.byEmail || {})),
    ambiguous: new Set(payload.ambiguous || []),
    byDomain: new Map(Object.entries(payload.byDomain || {})),
    byCrd: new Map(Object.entries(payload.byCrd || {})),
    internalCrds: new Set(payload.internalCrds || []),
    contentHash: payload.contentHash || "",
    generated: payload.generated || "",
    ready: true,
  };
  loadedAt = Date.now();
  return cache;
}

/* What we know about an address.
 *
 *   { kind: "advisor",   crd }        exactly one advisor holds it
 *   { kind: "ambiguous" }             several do -- real advisor traffic, but
 *                                     naming one of them would be a guess
 *   { kind: "firm", firmCrd }         unknown address at a domain that belongs
 *                                     to one firm: worth recording as advisor
 *                                     traffic, cannot name the person
 *   { kind: "unknown" }               not ours. Discard, persist nothing.
 */
function classifyAddress(index, address) {
  const email = normalise(address);
  if (!email) return { kind: "unknown" };

  /* OUR OWN PEOPLE, decided at RUNTIME from current configuration.
   *
   * The export already leaves internal addresses out of the blob, so this looks
   * redundant. It is not, and the difference matters:
   *
   *   the export ran at some point in the past, with whatever
   *   EMAIL_INTERNAL_DOMAINS was set to THEN
   *
   *   this reads the setting as it is NOW
   *
   * So adding a domain -- a new office, an acquisition, a dba -- takes effect
   * on the next sweep rather than on the next pipeline run, and a rep hired
   * this morning is never tracked even if the blob predates them. The blob's
   * exclusion becomes an optimisation; THIS is the boundary.
   *
   * Returned as its own kind rather than as "unknown" so a caller can tell the
   * difference between "we do not know this person" and "we deliberately do not
   * track this person".
   */
  const internalDomains = core.config().internalDomains;
  if (internalDomains && internalDomains.has(domainOf(email))) {
    return { kind: "internal", email };
  }

  const crd = index.byEmail.get(email);
  if (crd) return { kind: "advisor", crd };
  if (index.ambiguous.has(email)) return { kind: "ambiguous", email };
  const firmCrd = index.byDomain.get(domainOf(email));
  if (firmCrd) return { kind: "firm", firmCrd, email };
  return { kind: "unknown" };
}

async function lookup(address) {
  const index = await load();
  return classifyAddress(index, address);
}

/* The address on file for one advisor, for a follow-up.
 *
 * The reverse of classifyAddress(), and the reason a follow-up can be sent at
 * all. It used to take its recipient from the ACTIVITY LOG, which is empty
 * until the sweep has observed somebody -- so on day one a follow-up could
 * reach nobody, and the error blamed the advisor for having no observed
 * address rather than the design for having nowhere else to look.
 *
 * Server-side by construction: the client names a CRD, never an address, so
 * there is nothing here to verify and no way to turn this endpoint into a
 * relay. Returns "" when we hold no address, which the caller must treat as a
 * refusal.
 */
async function emailForCrd(crd) {
  const index = await load();
  return index.byCrd.get(String(crd || "").trim()) || "";
}

/* Is this "advisor" one of our own people?
 *
 * 18 of EIC's own registered reps appear in the SEC feed and therefore on the
 * map. Their addresses are excluded from every lookup above, so the sweep
 * cannot recognise them and nothing about their mail is ever recorded -- which
 * matters because the activity timeline is FIRM-WIDE, and internal
 * correspondence between colleagues is both more sensitive than advisor mail
 * and nobody else's business.
 *
 * Their CRDs are published so the applications can SAY that. An empty timeline
 * with no explanation looks like a bug, and somebody would eventually "fix" it.
 */
async function isInternalCrd(crd) {
  const index = await load();
  return index.internalCrds.has(String(crd || "").trim());
}

// Test seam. Lets the classification be exercised without Azure, and lets a
// caller inject a known universe rather than mocking the SDK.
function useIndex(payload) {
  cache = {
    byEmail: new Map(Object.entries(payload.byEmail || {})),
    ambiguous: new Set(payload.ambiguous || []),
    byDomain: new Map(Object.entries(payload.byDomain || {})),
    byCrd: new Map(Object.entries(payload.byCrd || {})),
    internalCrds: new Set(payload.internalCrds || []),
    contentHash: payload.contentHash || "test",
    generated: payload.generated || "",
    ready: true,
  };
  loadedAt = Date.now();
  return cache;
}

function reset() { cache = null; loadedAt = 0; }

module.exports = { load, lookup, classifyAddress, emailForCrd, isInternalCrd,
                   useIndex, reset,
                   normalise, domainOf };
