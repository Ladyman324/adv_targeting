"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const probe = require("../refresh-release");
const now = Date.parse("2026-11-07T08:00:00Z");
const future = new Date(now + 30 * 60000).toISOString();
test("machine release API requires Azure Functions authentication", () => {
  const cfg = require("../refresh-release/function.json");
  const trigger = cfg.bindings.find(x => x.type === "httpTrigger");
  assert.equal(trigger.authLevel, "function");
  assert.deepEqual(trigger.methods, ["get"]);
});
test("business data and API routes retain Microsoft sign-in", () => {
  const cfg = JSON.parse(fs.readFileSync(path.join(__dirname,"../../webapp/staticwebapp.config.json"),"utf8"));
  const publicData = cfg.routes.filter(r => r.route.startsWith("/data/") && r.allowedRoles.includes("anonymous"));
  assert.deepEqual(publicData.map(r => r.route), ["/data/refresh_release.json"]);
  assert.deepEqual(cfg.routes.find(r => r.route === "/api/*").allowedRoles,["authenticated"]);
  assert.deepEqual(cfg.routes.find(r => r.route === "/data/*").allowedRoles,["authenticated"]);
});
test("quiet check reads scheduling metadata, never message content", () => {
  for (const field of ["subject","bodyText","bodyHtml","recipientEmail","recipientName"])
    assert.equal(probe.SELECT.includes(field),false);
});
test("editing and terminal messages do not block deployment", () => {
  assert.equal(probe.quiet(["editing","ready","sent","failed","canceled","auth_required","draft_ready"].map(state=>({state})),now).quiet,true);
});
test("a live worker lease always blocks deployment", () => {
  assert.equal(probe.quiet([{state:"send_scheduled",capacityDay:"2026-11-07",leaseUntilUtc:future}],now,15,()=>false).quiet,false);
});
test("planned future work leaves a bounded quiet window", () => {
  assert.equal(probe.quiet([{state:"send_scheduled",plannedSendUtc:future}],now).quiet,true);
});
test("overdue legacy or unknown work fails closed", () => {
  for (const state of ["send_scheduled","drafting","unknown_state","send_ambiguous"])
    assert.equal(probe.quiet([{state}],now).quiet,false);
});
test("calendar-reserved sends outside the sending window may wait safely", () => {
  assert.equal(probe.quiet([{state:"send_scheduled",capacityDay:"2026-11-06"}],now,15,()=>false).quiet,true);
  assert.equal(probe.quiet([{state:"send_scheduled",capacityDay:"2026-11-06"}],now,15,t=>t>=now+10*60000).quiet,false);
});
test("scheduled batch preflight due soon blocks publication", () => {
  assert.equal(probe.quiet([{state:"scheduled_preflight",plannedSendUtc:new Date(now+60000).toISOString()}],now).quiet,false);
});
test("machine probe refuses non-read methods", async () => {
  const context={};await probe(context,{method:"POST"});assert.equal(context.res.status,405);
  assert.deepEqual(context.res.body,{available:false});
});
