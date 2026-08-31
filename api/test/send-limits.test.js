"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../shared/email-core");

function cfgWith(env) {
  const saved = { ...process.env };
  Object.assign(process.env, env);
  const c = core.config();
  process.env = saved;
  return c;
}

test("a batch over the per-send limit is refused for sending but not for drafts", () => {
  // The exact case that reached a confirmation dialog before failing: 48
  // recipients with the limit at 25.
  const cfg = cfgWith({ EMAIL_DIRECT_BATCH_MAX: "25", EMAIL_EXTERNAL_24H_LIMIT: "25" });
  const drafts = core.guardrail(48, "drafts", cfg);
  const send = core.guardrail(48, "send", cfg);
  assert.equal(drafts.blocked, false, "drafts are not limited by the send cap");
  assert.equal(send.blocked, true);
  assert.match(send.message, /limited to 25 recipients per batch/);
});

test("the per-send limit is a hard edge, not a soft one", () => {
  // Nothing splits a batch across days. It is refused whole, so the boundary
  // has to be exact.
  const cfg = cfgWith({ EMAIL_DIRECT_BATCH_MAX: "25" });
  assert.equal(core.guardrail(25, "send", cfg).blocked, false, "25 is allowed");
  assert.equal(core.guardrail(26, "send", cfg).blocked, true, "26 is not");
});

test("the client blocks from the server plan rather than reimplementing capacity", () => {
  // A daily allowance can produce a valid multi-day batch, so recipient count
  // and firm-domain arithmetic belong to the server-authored plan. The client
  // keeps only the global kill switch and the plan's fit decision.
  const src = require("fs").readFileSync(
    require.resolve("../../webapp/email.js"), "utf8");
  const fn = src.split("function sendBlockedReason")[1].split(String.fromCharCode(10) + "  }")[0];
  assert.match(fn, /planRemediation\(deliveryPlan\)/, "uses the server plan's fit decision");
  assert.match(fn, /directSendAvailable/, "respects the kill switch");
  assert.doesNotMatch(fn, /directBatchMax|rolling|externalCount|INTERNAL/,
    "does not duplicate batch, calendar, or domain policy in the browser");
});

test("internal recipients do not consume daily capacity", () => {
  const cfg = cfgWith({ EMAIL_INTERNAL_DOMAINS: "eicatlanta.com" });
  assert.equal(core.isExternal("colleague@eicatlanta.com", cfg), false);
  assert.equal(core.isExternal("advisor@morganstanley.com", cfg), true);
});
