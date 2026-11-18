"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createService, sameAddress } = require("../shared/act-address-review");
const clone = value => JSON.parse(JSON.stringify(value));
function fixture(options = {}) {
  const primary = { line1: "1285 Avenue of the Americas", line2: "Floor 15", line3: "", city: "New York", state: "NY", postalCode: "10019" };
  const alternate = { line1: "401 West 4th Street", line2: "Suite 3100", line3: "", city: "Austin", state: "TX", postalCode: "78701" };
  const record = { id: "act-chris", firstName: "Christopher", middleName: "Wayne", lastName: "Tolman", emailAddress: "c.tolman@example.com",
    edited: "2026-11-07T00:00:00Z", businessAddress: { ...primary, country: "USA", latitude: 40.76, longitude: -73.98 },
    homeAddress: { country: "John/Chris/Regina/Alec" }, customFields: { team: "Advocate Partners", mailCode: "2" }, mobilePhone: "2125551234" };
  const lookup = { identity_manifest_hash: "approved-release", contacts: { "2066775": "act-chris" },
    activity_contacts: { "9999999": { id: "other-act-contact", email: "assistant@example.com" } },
    address_review: { "2066775": { contactId: "act-chris", identity: { firstName: "Christopher", middleName: "Wayne", lastName: "Tolman", emailAddress: "c.tolman@example.com" },
      candidates: [{ id: "austin", address: alternate, source: "UBS", sourceFile: "ubs_2026-10-25.csv", url: "https://example.com/chris" }] } } };
  let live = record, reads = 0, writes = [], audit = [];
  const journal = { lock: async (id, op) => op(), begin: async change => { audit.push(clone(change)); return "audit-1"; }, finish: async (id, state) => { audit.push(state); } };
  const service = createService({ lookup, secondaryField: "homeAddress", journal,
    read: async (crd, id) => { reads++; if (options.onRead) options.onRead(live, reads); return clone(live); },
    write: async (crd, id, next) => { writes.push({ crd, id, next: clone(next) }); if (options.writeError) throw new Error("provider secret response"); if (!options.ignoreWrite) live = clone(next); },
    ...options.dependencies });
  return { service, lookup, record, alternate, primary, writes, audit, journal, live: () => live };
}
const who = { id: "eic-user", name: "rep@example.com", dev: false };
async function body(f, action = "add_secondary") {
  const preview = await f.service.preview("2066775");
  return { crd: "2066775", candidateId: "austin", action, revision: preview.revision };
}

test("preview resolves the approved ACT id and displays the precise source address", async () => {
  const f = fixture(), preview = await f.service.preview("2066775");
  assert.equal(preview.contactId, "act-chris");
  assert.equal(preview.primary.state, "NY");
  assert.equal(preview.candidates[0].address.line2, "Suite 3100");
  assert.equal(f.writes.length, 0);
});
test("activity-only email routing cannot authorize an address edit", async () => {
  const f = fixture();
  await assert.rejects(f.service.preview("9999999"), /No unique approved/);
  assert.equal(f.writes.length, 0);
});
test("ambiguous ACT ids, stale artifact ids, and wrong live contact are refused", async () => {
  for (const mutate of [f => { f.lookup.contacts["1111111"] = "ACT-CHRIS"; },
    f => { f.lookup.address_review["2066775"].contactId = "someone-else"; },
    f => { f.record.id = "someone-else"; }]) {
    const f = fixture(); mutate(f);
    await assert.rejects(f.service.preview("2066775"));
    assert.equal(f.writes.length, 0);
  }
});
test("live name or email changes refuse stale links before any write", async () => {
  for (const key of ["firstName", "middleName", "lastName", "emailAddress"]) {
    const f = fixture(), b = await body(f);
    f.record[key] = "different-person";
    await assert.rejects(f.service.update(who, b), /identity has changed/);
    assert.equal(f.writes.length, 0);
  }
});
test("adding secondary preserves primary, Partners, and all unrelated fields", async () => {
  const f = fixture(), before = clone(f.record), result = await f.service.update(who, await body(f));
  assert.equal(result.verified, true);
  assert.equal(result.candidates.length, 0);
  assert.deepEqual(f.live().businessAddress, before.businessAddress);
  assert.equal(f.live().homeAddress.country, before.homeAddress.country);
  assert.deepEqual(f.live().customFields, before.customFields);
  assert.equal(f.live().mobilePhone, before.mobilePhone);
  assert.equal(f.writes[0].id, "act-chris");
  assert.equal(f.audit.at(-1), "verified");
});
test("replacing primary changes only its address and clears old coordinates", async () => {
  const f = fixture(), before = clone(f.record), result = await f.service.update(who, await body(f, "replace_primary"));
  assert.equal(result.candidates.length, 0);
  assert.equal(f.live().businessAddress.city, "Austin");
  assert.equal(f.live().businessAddress.latitude, null);
  assert.deepEqual(f.live().homeAddress, before.homeAddress);
  assert.deepEqual(f.live().customFields, before.customFields);
});
test("secondary matching roster suppresses conflict while primary still wins", async () => {
  const f = fixture(); Object.assign(f.record.homeAddress, f.alternate);
  const preview = await f.service.preview("2066775");
  assert.equal(preview.candidates.length, 0);
  assert.equal(preview.primary.city, "New York");
});
test("address comparison accepts casing, street abbreviations, ZIP4; rejects different suites", () => {
  const f = fixture();
  assert.ok(sameAddress(f.alternate, { ...f.alternate, line1: "401 west 4th st.", line2: "STE 3100", postalCode: "78701-1234" }));
  assert.ok(!sameAddress(f.alternate, { ...f.alternate, line2: "Suite 2000" }));
  assert.ok(!sameAddress({}, {}));
});
test("existing complete or partial secondary address is never overwritten", async () => {
  for (const secondary of [{ line1: "200 Other Street", city: "Dallas", state: "TX", postalCode: "75201" }, { line1: "200 Other Street" }]) {
    const f = fixture(); Object.assign(f.record.homeAddress, secondary);
    const b = await body(f);
    await assert.rejects(f.service.update(who, b), /already has a secondary/);
    assert.equal(f.writes.length, 0);
  }
});
test("stale previews and a record changed just before PUT are rejected", async () => {
  const f = fixture(), b = await body(f);
  f.record.edited = "new-edit";
  await assert.rejects(f.service.update(who, b), /changed/);
  assert.equal(f.writes.length, 0);
  const racing = fixture({ onRead: (live, n) => { if (n === 3) live.mobilePhone = "changed-in-ACT"; } });
  await assert.rejects(racing.service.update(who, await body(racing)), /changed while/);
  assert.equal(racing.writes.length, 0);
});
test("tampered client contact id and address never reach ACT", async () => {
  const f = fixture(), b = await body(f);
  await f.service.update(who, { ...b, contactId: "victim", address: { city: "Miami" } });
  assert.equal(f.writes[0].id, "act-chris");
  assert.equal(f.writes[0].next.homeAddress.city, "Austin");
  const g = fixture();
  await assert.rejects(g.service.update(who, { ...await body(g), candidateId: "invented" }));
  assert.equal(g.writes.length, 0);
});
test("anonymous and dev actors cannot write ACT", async () => {
  for (const actor of [null, { id: "dev", dev: true }]) {
    const f = fixture();
    await assert.rejects(f.service.update(actor, await body(f)), /Sign in/);
    assert.equal(f.writes.length, 0);
  }
});
test("readback must confirm actual fields, not just a successful PUT", async () => {
  const f = fixture({ ignoreWrite: true });
  await assert.rejects(f.service.update(who, await body(f)), /did not confirm/);
  assert.equal(f.audit.at(-1), "verification_required");
});
test("provider failures never expose raw provider data or claim success", async () => {
  const f = fixture({ writeError: true });
  await assert.rejects(f.service.update(who, await body(f)), error => !error.message.includes("secret") && error.statusCode === 502);
});
test("failure to record durable intent prevents the CRM write", async () => {
  const f = fixture(); f.journal.begin = async () => { throw new Error("storage unavailable"); };
  await assert.rejects(f.service.update(who, await body(f)));
  assert.equal(f.writes.length, 0);
});
test("a verified save remains truthful if finishing its audit is delayed", async () => {
  const f = fixture(); f.journal.finish = async () => { throw new Error("storage delayed"); };
  const result = await f.service.update(who, await body(f));
  assert.equal(result.verified, true);
  assert.equal(result.auditPending, true);
});
