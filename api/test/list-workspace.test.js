"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const dial = fs.readFileSync(path.join(__dirname, "../../webapp/dial.js"), "utf8");
const lists = {
  active: {
    id:"active", name:"Active rotation", cursor:1, cycle:1,
    cycleStartedUtc:"2026-11-01T00:00:00Z", etag:"one",
    items:[{ crd:"1" }, { crd:"2" }, { crd:"3" }],
  },
  prospects: {
    id:"prospects", name:"Prospects", cursor:0, cycle:2,
    cycleStartedUtc:"2026-11-21T00:00:00Z", etag:"two",
    items:[{ crd:"4" }, { crd:"5" }],
  },
};

function browserDial(storage){
  const calls = [];
  const context = {
    window:{}, URLSearchParams,
    localStorage:{
      getItem:key => storage.get(key),
      setItem:(key, value) => storage.set(key, value),
      removeItem:key => storage.delete(key),
    },
    fetch:async (url, options) => {
      calls.push({ url, method:options.method || "GET" });
      const id = new URL(url, "https://example.test").searchParams.get("id");
      const send = body => ({ ok:true, headers:{ get:() => "application/json" }, json:async () => body });
      if (options.method === "PUT") {
        const body = JSON.parse(options.body);
        assert.equal(body.etag, lists[id].etag);
        return send({ ...body, count:body.items.length, etag:"changed" });
      }
      if (url.startsWith("/api/queue?")) return send(lists[id]);
      if (url === "/api/queue") return send({ lists:Object.values(lists) });
      if (url === "/api/health") return send({ configured:true, storageOk:true, user:{ id:"rep" } });
      if (url === "/api/settings") return send({ settings:{ defaultListId:"active" } });
      if (url.startsWith("/api/log?")) return send({ events:[
        { kind:"outcome", crd:"1", disposition:"talked" },
        { kind:"outcome", crd:"4", disposition:"talked" },
      ] });
      return send({});
    },
  };
  vm.runInNewContext(dial, context);
  return { Dial:context.window.Dial, calls };
}

test("reading and editing a separate saved list preserves the active calling session", async () => {
  const { Dial, calls } = browserDial(new Map());
  await Dial.init();
  assert.equal(Dial.start(), true);
  const prior = Dial.state.items;
  const saved = await Dial.readList("prospects");
  await Dial.updateSavedList(saved, { name:"City prospects", items:[saved.items[1]] });
  assert.equal(Dial.state.listId, "active");
  assert.equal(Dial.state.items, prior);
  assert.equal(Dial.current().crd, "2");
  assert.equal(Dial.progress().done, 1);
  assert.equal(calls.filter(call => call.url === "/api/queue?id=active").length, 1);
});

test("the active work list and remaining contact survive reload with a different default list", async () => {
  const storage = new Map();
  const { Dial } = browserDial(storage);
  await Dial.init();
  await Dial.openList("prospects", { resume:false });
  assert.equal(Dial.start(), true);
  assert.equal(Dial.current().crd, "5");
  Dial.pause();

  const { Dial:reloaded } = browserDial(storage);
  await reloaded.init();
  assert.equal(reloaded.state.listId, "prospects");
  assert.equal(reloaded.state.running, false);
  assert.equal(reloaded.progress().done, 1);
  const { Dial:reloadedAgain } = browserDial(storage);
  await reloadedAgain.init();
  assert.equal(reloadedAgain.state.listId, "prospects");
  assert.equal(reloaded.start(), true);
  assert.equal(reloaded.current().crd, "5");

  reloaded.end();
  assert.equal(reloaded.preferredListId(), "active");
});

test("a queue paused before the first completed contact is still in progress", async () => {
  const storage = new Map();
  const { Dial } = browserDial(storage);
  await Dial.init();
  Dial.state.done.clear();
  assert.equal(Dial.queueInProgress(), false);
  assert.equal(Dial.start(), true);
  Dial.pause();
  assert.equal(Dial.progress().done, 0);
  assert.equal(Dial.queueInProgress(), true);
  assert.equal(Dial.state.running, false);
});

test("map and call badges are independent and use the correct owner's list", () => {
  const app = fs.readFileSync(path.join(__dirname, "../../webapp/app.js"), "utf8");
  const code = app.slice(app.indexOf("function listUsage("), app.indexOf("function listUsageBadges("));
  const context = {
    listWorkspace:null, scope:"GA", mapListFilter:{ id:"map-list" },
    Dial:{ state:{ listId:"queue-list" } },
    ROLE_LIST_IDS:{ key:"role-key" },
    standingKindOf:id => id === "role-key" ? "key" : "",
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  const usage = (type, id) => JSON.parse(JSON.stringify(context.listUsage(type, id)));
  assert.deepEqual(usage("list", "browsed-list"), { onMap:false, inQueue:false });
  assert.deepEqual(usage("list", "map-list"), { onMap:true, inQueue:false });
  assert.deepEqual(usage("list", "queue-list"), { onMap:false, inQueue:true });

  context.mapListFilter = { id:"queue-list" };
  assert.deepEqual(usage("list", "queue-list"), { onMap:true, inQueue:true });
  context.listWorkspace = { owner:{ id:"other-rep" } };
  assert.deepEqual(usage("list", "queue-list"), { onMap:false, inQueue:false });
  context.mapListFilter.ownerId = "other-rep";
  assert.deepEqual(usage("list", "queue-list"), { onMap:true, inQueue:false });
  context.scope = "US";
  assert.deepEqual(usage("list", "queue-list"), { onMap:false, inQueue:false });

  context.listWorkspace = null;
  context.scope = "GA";
  context.Dial.state.listId = "role-key";
  context.mapListFilter = { id:"role-key" };
  assert.deepEqual(usage("smart", "key"), { onMap:true, inQueue:true });
  assert.deepEqual(usage("filter", "role-key"), { onMap:false, inQueue:false });
});
