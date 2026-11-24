"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const crypto = require("node:crypto");
const context = require("../shared/act-contact-context");

const actor = { id: "rep-1" };
const crd = "5551234";
const name = "Jane Advisor";
const email = "jane.advisor@firm.example";
function fixture({ claimedCrds = [], emailHashes = [], duplicate = false, uncertain = false,
  profile = {}, mailCode = "2", omitMailCode = false } = {}) {
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
      return { ...draft, id: "verified-guid",
        ...(omitMailCode ? { customFields: { crd } } : {}) };
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
      contact_create_profiles: { [crd]: profile },
      crm_directory: { claimedCrds, emailHashes, contacts: [
        { name: "Found in ACT", email: "crm@firm.example", company: "Firm", primary: { city: "Irvine" } },
      ] } },
    resolve: async () => ({ name, email, firm: "Firm", lastName: "Advisor" }),
    addressService, act, journal, mailCodeFor: async () => mailCode,
  });
  return { svc, calls };
}

test("CRM-only search returns a distinct contact, and activity-only links do not get write authority", async () => {
  const { svc, calls } = fixture();
  assert.equal(svc.search("crm@firm.example").count, 1);
  assert.equal(svc.search("found irvine").contacts[0].name, "Found in ACT");
  assert.equal(svc.search("found irvine", true).count, 0);
  assert.equal(svc.search("found act", true).count, 1);
  assert.equal(svc.search("no such contact").count, 0);
  assert.deepEqual(await svc.preview("123456"), { status: "review" });
  assert.equal((await svc.preview("2066775")).primary.city, "New York");
  await assert.rejects(svc.change(actor, { crd: "2066775", action: "replace_primary" }), /confirm/);
  assert.deepEqual(calls, []);
  await svc.change(actor, { crd: "2066775", action: "replace_primary", confirmed: true });
  assert.deepEqual(calls, ["replace_primary"]);
});

test("the roster can look up only batched ACT! link statuses", () => {
  const { svc, calls } = fixture({ claimedCrds:["6000000"], emailHashes:["duplicate"], profile:{ emailHash:"duplicate" } });
  assert.deepEqual(svc.advisorStatuses(["2066775", "123456", "6000000", crd, "7000000", "2066775"]), [
    ["2066775", "linked"], ["123456", "review"], ["6000000", "review"], [crd, "review"], ["7000000", "unlinked"],
  ]);
  assert.throws(() => svc.advisorStatuses(["invalid"]), /valid advisor CRDs/);
  assert.throws(() => svc.advisorStatuses(Array(101).fill(crd)), /at most 100/);
  assert.deepEqual(calls, []);
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

test("creation verifies the approved contact profile, defaults to email only, and reports missing CRM fields", async () => {
  const businessAddress = { line1: "15 Main Street", city: "Atlanta", state: "GA", postalCode: "30303" };
  const profile = { emailHash: crypto.createHash("sha256").update(email).digest("hex"),
    department: "Advisory team", jobTitle: "Advisor", website: "https://firm.example/jane",
    businessPhone: "+14045551234", businessExtension: "22", businessAddress };
  const { svc } = fixture({ profile, omitMailCode: true });
  const state = await svc.preview(crd);
  assert.equal(state.draft.department, profile.department);
  assert.equal(state.draft.jobTitle, profile.jobTitle);
  assert.equal(state.draft.website, profile.website);
  assert.equal(state.draft.businessPhone, "4045551234");
  assert.equal(state.draft.businessExtension, "22");
  assert.equal(state.draft.businessAddress.line1, businessAddress.line1);
  assert.equal(state.draft.customFields.email__y_n, "2");
  assert.equal(state.draft.referredBy, undefined);
  const result = await svc.change(actor, { crd, action: "add_contact", confirmed: true, revision: state.revision });
  assert.equal(result.primary.line1, businessAddress.line1);
  assert.deepEqual(result.unverifiedFields, ["Mail Code"]);
  assert.equal((await svc.preview(crd)).status, "added");
  const different = fixture({ profile: { ...profile, emailHash: "mismatch" }, mailCode: "U" });
  const draft = (await different.svc.preview(crd)).draft;
  assert.equal(draft.department, undefined);
  assert.equal(draft.businessAddress, undefined);
  assert.equal(draft.customFields.email__y_n, "U");
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
