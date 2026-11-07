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

test("invalid rows and canceled or paused batches cannot start work", () => {
  assert.equal(probe.quiet([{state:"invalid"}],now).quiet,true);
  for (const parentBatchStatus of ["canceled","paused"])
    assert.equal(probe.quiet([{state:"draft_pending",parentBatchStatus}],now).quiet,true);
  assert.equal(probe.quiet([{state:"draft_pending",parentBatchStatus:"sending"}],now).quiet,false);
  assert.equal(probe.quiet([{state:"draft_pending"}],now).quiet,false);
});
test("manual handling and retry copies follow the existing worker skip rules", () => {
  assert.equal(probe.quiet([{state:"draft_pending",handledManuallyUtc:"2026-11-01T12:00:00Z"}],now).quiet,true);
  assert.equal(probe.quiet([{state:"draft_pending",retryBatchId:"replacement"}],now).quiet,true);
});
test("an active lease still blocks canceled or manually held work", () => {
  for (const hold of [{parentBatchStatus:"canceled"},{parentBatchStatus:"paused"},{handledManuallyUtc:"2026-11-01T12:00:00Z"},{retryBatchId:"replacement"}])
    assert.equal(probe.quiet([{state:"draft_pending",leaseUntilUtc:future,...hold}],now).quiet,false);
});

test("metadata joins parent batch status without fetching message content", async t => {
  const {TableClient}=require("@azure/data-tables");
  const previous=process.env.AZURE_STORAGE_CONNECTION_STRING;
  process.env.AZURE_STORAGE_CONNECTION_STRING="metadata-test-fixture";
  t.after(()=>{if(previous===undefined)delete process.env.AZURE_STORAGE_CONNECTION_STRING;else process.env.AZURE_STORAGE_CONNECTION_STRING=previous;});
  t.mock.method(TableClient,"fromConnectionString",(connection,table)=>({
    async *listEntities(options) {
      for(const field of ["subject","bodyText","bodyHtml","recipientEmail","recipientName"])
        assert.equal(options.queryOptions.select.includes(field),false);
      if(table==="EmailBatches")yield {partitionKey:"user",rowKey:"old-batch",status:"canceled"};
      else {
        assert.match(options.queryOptions.filter,/state ne 'invalid'/);
        yield {partitionKey:"user_old-batch",state:"draft_pending"};
      }
    }
  }));
  const rows=await probe.workMetadata();
  assert.equal(rows[0].parentBatchStatus,"canceled");
  assert.equal(probe.quiet(rows,now).quiet,true);
});
