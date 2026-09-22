"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),Module=require("module");
const {FakeTableService}=require("./helpers/fake-table"),defs=require("../shared/audience-definition");
const WHO={id:"rep-one",name:"rep@example.com"};
const raw=()=>({version:1,scope:{kind:"territory",value:"T:Northeast",states:["CT","MA","ME","NH","NY","RI","VT"],label:"Northeast"},filters:{selectedFirms:["123"],assetsOnly:true,roles:["dd","key","dd"]}});
function env(){const service=new FakeTableService(),p=require.resolve("../shared/store.js");delete require.cache[p];const real=Module._load;Module._load=function(r,parent){if(parent&&parent.filename===p&&r==="@azure/data-tables")return{TableClient:{fromConnectionString:(_c,n)=>service.table(n)},odata:(s,...v)=>s.reduce((o,x,i)=>o+x+(i<v.length?"'"+v[i]+"'":""),"")};return real.apply(this,arguments)};process.env.AZURE_STORAGE_CONNECTION_STRING="x";try{const store=require(p);store.__testService=service;return store}finally{Module._load=real}}
test("definition v1 canonicalizes and rejects unsupported or national criteria",()=>{const n=defs.normalizeDefinition(raw()).definition;assert.equal(n.filters.assetsOnly,true);assert.deepEqual(n.filters.roles,["dd","key"]);assert.deepEqual(n.filters.excluded,[]);assert.throws(()=>defs.normalizeDefinition({...raw(),mystery:true}),/not supported/);assert.throws(()=>defs.normalizeDefinition({version:1,scope:{kind:"state",value:"US",states:["NY"]},filters:{}}),/national/);assert.throws(()=>defs.normalizeDefinition({...raw(),filters:{query:"x"}}),/not supported/)});

test("definition rejects invalid filter enums and noncanonical territories", () => {
  for (const [key, value] of [["aum", ["bogus"]], ["roles", ["bogus"]], ["lastEmailed", "weekly"],
    ["lastCalled", "recent"], ["joinedFirm", "d30"]])
    assert.throws(() => defs.normalizeDefinition({
      ...raw(), filters: { [key]: value },
    }), new RegExp("filters." + key));
  assert.throws(() => defs.normalizeDefinition({
    ...raw(), scope: { ...raw().scope, value: "Northeast" },
  }), /canonical/);
  assert.throws(() => defs.normalizeDefinition({
    ...raw(), scope: { ...raw().scope, states: ["NY"] },
  }), /exact states/);
});
test("audiences are personal and use etag concurrency",async()=>{const s=env(),n=defs.normalizeDefinition(raw()),fields={definition:n.definition,definitionJson:n.json},made=await s.putAudience(WHO,{id:"a-12345678",name:"UBS assets",description:"NY",...fields});assert.equal(made.type,"dynamic");assert.equal((await s.listAudiences(WHO)).length,1);assert.equal((await s.getAudience(WHO,made.id)).definition.version,1);await assert.rejects(()=>s.getAudience({id:"other",name:"x"},made.id),e=>e.statusCode===404);await assert.rejects(()=>s.putAudience(WHO,{id:made.id,name:"x",...fields}),e=>e.statusCode===409);const changed=await s.putAudience(WHO,{id:made.id,name:"Changed",etag:made.etag,...fields});assert.equal(changed.name,"Changed");await assert.rejects(()=>s.putAudience(WHO,{id:made.id,name:"stale",etag:made.etag,...fields}),e=>e.statusCode===409);await s.deleteAudience(WHO,made.id);await assert.rejects(()=>s.getAudience(WHO,made.id),e=>e.statusCode===404)});

test("a corrupt saved definition is returned safely as invalid", async () => {
  const store = env();
  const made = await store.putAudience(WHO, { id: "a-corrupt01", name: "Old",
    definition: null, definitionJson: "not-json" });
  const loaded = await store.getAudience(WHO, made.id);
  assert.equal(loaded.definition, null);
  assert.equal(loaded.invalidDefinition, true);
  assert.equal(loaded.name, "Old");
});

test("last-called summary is per rep, outbound-only, and uses the latest event time", async () => {
  const store = env();
  const log = store.__testService.table("CallLog");
  const add = (partitionKey, rowKey, crd, atUtc, disposition) =>
    log.createEntity({ partitionKey, rowKey, crd, atUtc, disposition });
  await add(WHO.id, "001", "111", "2026-08-01T12:00:00Z", "connected");
  await add(WHO.id, "002", "111", "2026-08-12T12:00:00Z", "voicemail");
  await add(WHO.id, "003", "222", "2026-08-20T12:00:00Z", "received");
  await add(WHO.id, "004", "333", "2026-08-21T12:00:00Z", "skipped");
  await add("other-rep", "001", "444", "2026-08-30T12:00:00Z", "connected");
  assert.deepEqual(await store.latestCallsForUser(WHO), [
    ["111", "2026-08-12T12:00:00Z"],
  ]);
});

test("team last-called summary spans reps, keeps the caller, and applies the same exclusions", async () => {
  const store = env();
  const log = store.__testService.table("CallLog");
  const add = (partitionKey, rowKey, crd, atUtc, disposition, userName) =>
    log.createEntity({ partitionKey, rowKey, crd, atUtc, disposition, userName, note: "private" });
  await add(WHO.id, "001", "111", "2026-08-01T12:00:00Z", "connected", WHO.name);
  await add("other-rep", "001", "111", "2026-08-12T12:00:00Z", "voicemail", "other@example.com");
  await add("other-rep", "002", "222", "2026-08-20T12:00:00Z", "received", "other@example.com");
  await add(WHO.id, "002", "333", "2026-08-21T12:00:00Z", "skipped", WHO.name);
  await add(WHO.id, "003", "444", "2026-08-22T12:00:00Z", "attempted", WHO.name);
  assert.deepEqual(await store.latestCallsForTeam(), [
    ["111", "2026-08-12T12:00:00Z", "other@example.com"],
    ["444", "2026-08-22T12:00:00Z", WHO.name],
  ]);
});

test("definition activity scopes default to me and accept only me or team", () => {
  const plain = defs.normalizeDefinition(raw()).definition.filters;
  assert.equal(plain.emailScope, "me");
  assert.equal(plain.callScope, "me");
  const team = defs.normalizeDefinition({ ...raw(),
    filters: { lastEmailed: "d30", emailScope: "team", callScope: "me" } }).definition.filters;
  assert.equal(team.emailScope, "team");
  assert.equal(team.callScope, "me");
  for (const key of ["emailScope", "callScope"])
    assert.throws(() => defs.normalizeDefinition({ ...raw(), filters: { [key]: "firm" } }),
      new RegExp("filters." + key));
});

test("call log summary route serves the team only when asked, without our own people", async () => {
  const target = require.resolve("../log/index.js");
  delete require.cache[target];
  const real = Module._load;
  const calls = [];
  const fakeStore = {
    identity: () => WHO,
    latestCallsForUser: async (who) => { calls.push(["mine", who.id]); return [["111", "2026-08-01T12:00:00Z"]]; },
    latestCallsForTeam: async () => { calls.push(["team"]); return [
      ["111", "2026-08-02T12:00:00Z", "other@example.com"],
      ["999", "2026-08-03T12:00:00Z", "other@example.com"]]; },
    ok: (context, body) => { context.res = { status: 200, body }; return context.res; },
    fail: (context, error) => { context.res = { status: error.statusCode || 500, body: { error: error.message } }; return context.res; },
  };
  const fakeAdvisors = { load: async () => ({ internalCrds: new Set(["999"]) }) };
  Module._load = function (request, parent) {
    if (parent && parent.filename === target) {
      if (request === "../shared/store") return fakeStore;
      if (request === "../shared/act") return {};
      if (request === "../shared/advisor-lookup") return fakeAdvisors;
    }
    return real.apply(this, arguments);
  };
  let handler;
  try { handler = require(target); } finally { Module._load = real; delete require.cache[target]; }

  const mine = {};
  await handler(mine, { method: "GET", query: { summary: "1" } });
  assert.deepEqual(mine.res.body.entries, [["111", "2026-08-01T12:00:00Z"]]);
  const team = {};
  await handler(team, { method: "GET", query: { summary: "1", scope: "team" } });
  assert.deepEqual(team.res.body.entries, [["111", "2026-08-02T12:00:00Z", "other@example.com"]]);
  assert.deepEqual(calls, [["mine", WHO.id], ["team"]]);
});

test("audience HTTP endpoint authenticates and routes GET, PUT, and DELETE", async () => {
  const target = require.resolve("../audiences/index.js");
  delete require.cache[target];
  const real = Module._load;
  const calls = [];
  const fakeStore = {
    identity: (req) => {
      if (!req.authenticated) {
        const error = new Error("Not signed in."); error.statusCode = 401; throw error;
      }
      return WHO;
    },
    listAudiences: async (who) => { calls.push(["list", who]); return []; },
    getAudience: async (who, id) => { calls.push(["get", who, id]); return { id }; },
    putAudience: async (who, value) => { calls.push(["put", who, value]); return value; },
    deleteAudience: async (who, id) => { calls.push(["delete", who, id]); return { id, deleted: true }; },
    ok: (context, body) => { context.res = { status: 200, body }; return context.res; },
    fail: (context, error) => {
      context.res = { status: error.statusCode || 500, body: { error: error.message } };
      return context.res;
    },
  };
  Module._load = function (request, parent) {
    if (parent && parent.filename === target && request === "../shared/store") return fakeStore;
    return real.apply(this, arguments);
  };
  let handler;
  try { handler = require(target); } finally { Module._load = real; }

  const unauthorized = {};
  await handler(unauthorized, { method: "GET", query: {}, authenticated: false });
  assert.equal(unauthorized.res.status, 401);

  const listed = {};
  await handler(listed, { method: "GET", query: {}, authenticated: true });
  assert.equal(listed.res.status, 200);
  assert.equal(calls.at(-1)[0], "list");

  const invalid = {};
  await handler(invalid, { method: "PUT", query: {}, authenticated: true,
    body: { definition: { version: 1, scope: { kind: "state", value: "US",
      states: ["NY"] }, filters: {} } } });
  assert.equal(invalid.res.status, 400);

  const created = {};
  await handler(created, { method: "PUT", query: {}, authenticated: true,
    body: { name: "Northeast", definition: raw() } });
  assert.equal(created.res.status, 200);
  assert.equal(calls.at(-1)[0], "put");

  const deleted = {};
  await handler(deleted, { method: "DELETE", query: { id: "a-12345678" },
    authenticated: true });
  assert.equal(deleted.res.status, 200);
  assert.equal(calls.at(-1)[0], "delete");
});
