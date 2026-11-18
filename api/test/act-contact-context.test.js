"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const crypto = require("node:crypto");
const context = require("../shared/act-contact-context");

const actor = { id: "rep-1" };
const crd = "5551234";
const name = "Jane Advisor";
const email = "jane.advisor@firm.example";
function fixture({ claimedCrds = [], emailHashes = [], duplicate = false, uncertain = false } = {}) {
  const calls = [];
  const history = new Map();
  const act = {
    configured: () => true,
    findContactDuplicates: async () => { calls.push("check"); return duplicate; },
    addContact: async (_crd, draft) => {
      calls.push("create");
      assert.equal(draft.customFields.crd, crd);
      assert.equal(draft.fullName, name);
      assert.equal(draft.emailAddress, email);
      if (uncertain) throw new Error("network timeout");
      return { ...draft, id: "verified-guid" };
    },
  };
  const addressService = {
    preview: async (id) => ({ crd: id, primary: { city: "New York", state: "NY" } }),
    update: async (_actor, payload) => { calls.push(payload.action); return { verified: true }; },
  };
  const journal = {
    lock: async (_key, apply) => apply(),
    creationStatus: async (id) => history.get(id) || null,
    reserveCreation: async (id) => { calls.push("reserve"); history.set(id, { state: "pending" }); },
    finishCreation: async (id) => { calls.push("verify"); history.set(id, { state: "verified" }); },
  };
  const svc = context.createService({
    lookup: { contacts: { "2066775": "linked-guid" }, address_review: { "2066775": {} },
      identity_manifest_hash: "one", activity_contacts: { "123456": { id: "activity-only" } },
      crm_directory: { claimedCrds, emailHashes, contacts: [
        { name: "Found in ACT", email: "crm@firm.example", company: "Firm", primary: { city: "Irvine" } },
      ] } },
    resolve: async () => ({ name, email, firm: "Firm", lastName: "Advisor" }),
    addressService, act, journal,
  });
  return { svc, calls };
}

test("CRM-only search returns a distinct contact, and activity-only links do not get write authority", async () => {
  const { svc, calls } = fixture();
  assert.equal(svc.search("crm@firm.example").count, 1);
  assert.equal(svc.search("found irvine").contacts[0].name, "Found in ACT");
  assert.equal(svc.search("no such contact").count, 0);
  assert.deepEqual(await svc.preview("123456"), { status: "review" });
  assert.equal((await svc.preview("2066775")).primary.city, "New York");
  await assert.rejects(svc.change(actor, { crd: "2066775", action: "replace_primary" }), /confirm/);
  assert.deepEqual(calls, []);
  await svc.change(actor, { crd: "2066775", action: "replace_primary", confirmed: true });
  assert.deepEqual(calls, ["replace_primary"]);
});

test("confirmed creation uses server fields and refuses a second write", async () => {
  const { svc, calls } = fixture();
  const state = await svc.preview(crd);
  assert.equal(state.status, "unlinked");
  const input = { crd, action: "add_contact", revision: state.revision,
    confirmed: true, emailAddress: "different@example.com" };
  await assert.rejects(svc.change(actor, { ...input, confirmed: false }), /confirm/);
  await assert.rejects(svc.change(actor, { ...input, revision: "old" }), /changed/);
  assert.deepEqual(calls, []);
  assert.deepEqual(await svc.change(actor, input), { status: "added", verified: true });
  assert.equal((await svc.preview(crd)).status, "added");
  await assert.rejects(svc.change(actor, input), /changed/);
  assert.deepEqual(calls, ["check", "reserve", "create", "verify"]);
});

test("snapshot conflicts, live duplicates and uncertain creates never authorize another create", async () => {
  const fixtures = [
    fixture({ claimedCrds: [crd] }),
    fixture({ emailHashes: [crypto.createHash("sha256").update(email).digest("hex")] }),
  ];
  for (const { svc, calls } of fixtures) {
    assert.deepEqual(await svc.preview(crd), { status: "review" });
    assert.deepEqual(calls, []);
  }
  for (const options of [{ duplicate: true }, { uncertain: true }]) {
    const { svc, calls } = fixture(options);
    const { revision } = await svc.preview(crd);
    const input = { crd, action: "add_contact", revision, confirmed: true };
    if (options.duplicate) {
      await assert.rejects(svc.change(actor, input), /existing record/);
      assert.deepEqual(calls, ["check"]);
    } else {
      await assert.rejects(svc.change(actor, input), /did not confirm/);
      assert.equal((await svc.preview(crd)).status, "pending");
      await assert.rejects(svc.change(actor, input), /changed/);
      assert.deepEqual(calls, ["check", "reserve", "create"]);
    }
  }
});
