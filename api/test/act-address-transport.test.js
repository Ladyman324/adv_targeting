"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");
const realFs = require("node:fs");
const path = require("node:path");
const actPath = require.resolve("../shared/act");

async function harness(run, overrides = {}, respond) {
  const oldLoad = Module._load, oldFetch = global.fetch;
  const keys = ["ACT_SYNC", "ACT_USER", "ACT_PASSWORD", "ACT_DB", "ACT_BASE", "ACT_ONLY_CRD"];
  const oldEnv = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  Object.assign(process.env, { ACT_SYNC: "1", ACT_USER: "test", ACT_PASSWORD: "test", ACT_DB: "test", ACT_BASE: "https://act.test", ACT_ONLY_CRD: "", ...overrides });
  const lookup = { contacts: { "2066775": "855992fd-e032-4454-947b-3dcd5515dfe3" }, activity_contacts: { "9999999": { id: "assistant-route" } } };
  Module._load = function(name, parent, main) {
    if (parent && parent.filename === actPath && name === "fs") return { ...realFs, readFileSync: () => JSON.stringify(lookup) };
    if (parent && parent.filename === actPath && name === "./store") return {};
    return oldLoad.apply(this, arguments);
  };
  const requests = [];
  global.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options });
    if (String(url).endsWith("/authorize")) return new Response("test-token-long-enough-for-auth-1234");
    if (respond) return respond(new URL(url), options);
    assert.equal(String(url), "https://act.test/api/contacts/" + lookup.contacts["2066775"]);
    return new Response(JSON.stringify({ id: lookup.contacts["2066775"], firstName: "Christopher" }), { headers: { "Content-Type": "application/json" } });
  };
  delete require.cache[actPath];
  try { await run(require(actPath), requests, lookup); }
  finally {
    delete require.cache[actPath]; Module._load = oldLoad; global.fetch = oldFetch;
    for (const key of keys) { if (oldEnv[key] === undefined) delete process.env[key]; else process.env[key] = oldEnv[key]; }
  }
}

test("address transport resolves approved id and uses existing ACT auth with a bounded request", () => harness(async (act, requests, lookup) => {
  const result = await act.addressRecord("2066775", lookup.contacts["2066775"]);
  assert.equal(result.id, lookup.contacts["2066775"]);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].options.method, "GET");
  assert.match(requests[1].options.headers.Authorization, /^Bearer /);
  assert.ok(requests[1].options.signal);
}));
test("address transport refuses activity-only, stale, and duplicate approved links without contacting ACT", async () => {
  await harness(async (act, requests) => {
    await assert.rejects(act.addressRecord("9999999", "assistant-route"), /approved ACT! link/);
    await assert.rejects(act.addressRecord("2066775", "different-contact"), /approved ACT! link/);
    assert.equal(requests.length, 0);
  });
  await harness(async (act, requests, lookup) => {
    lookup.contacts["1234567"] = lookup.contacts["2066775"];
    await assert.rejects(act.addressRecord("2066775", lookup.contacts["2066775"]), /approved ACT! link/);
    assert.equal(requests.length, 0);
  });
});
test("existing ACT_SYNC switch and trial CRD allow-list also govern address writes", async () => {
  for (const setting of [{ ACT_SYNC: "0" }, { ACT_ONLY_CRD: "1234567" }])
    await harness(async (act, requests, lookup) => {
      await assert.rejects(act.addressRecord("2066775", lookup.contacts["2066775"], { id: "attempt" }), /unavailable/);
      assert.equal(requests.length, 0);
    }, setting);
});
test("address PUT goes to the approved contact endpoint with the server-constructed record", () => harness(async (act, requests, lookup) => {
  const next = { id: lookup.contacts["2066775"], businessAddress: { city: "Austin" }, customFields: { preserved: "yes" } };
  await act.addressRecord("2066775", next.id, next);
  assert.equal(requests[1].options.method, "PUT");
  assert.deepEqual(JSON.parse(requests[1].options.body), next);
}));


test("creation checks existing addresses and names, then verifies the created contact by ID", () => {
  const id = "855992fd-e032-4454-947b-3dcd5515dfe3";
  const draft = { firstName: "Avery", lastName: "O'Neil", emailAddress: "a+test@example.com",
    customFields: { crd: "7654321" } };
  return harness(async (act, requests) => {
    assert.equal(await act.findContactDuplicates("7654321", draft), false);
    assert.deepEqual(await act.addContact("7654321", draft), { ...draft, id });
    assert.deepEqual(requests.slice(1).map((row) => row.options.method), ["GET", "POST", "GET"]);
    assert.ok(requests.slice(1).every((row) => row.options.signal));
  }, {}, (url, options) => {
    if (options.method === "GET" && url.pathname === "/api/contacts") {
      const clause = url.searchParams.get("$filter");
      assert.match(clause, /EmailAddress eq 'a\+test@example\.com'/);
      assert.match(clause, /AltEmailAddress eq/);
      assert.match(clause, /LastName eq 'O''Neil'/);
      assert.equal(url.searchParams.get("$top"), "1");
      return Response.json({ value: [] });
    }
    if (options.method === "POST") {
      assert.equal(url.pathname, "/api/contacts");
      assert.deepEqual(JSON.parse(options.body), draft);
      return Response.json({ id });
    }
    assert.equal(url.pathname, "/api/contacts/" + id);
    return Response.json({ ...draft, id });
  });
});
