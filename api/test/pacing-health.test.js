"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../shared/email-core");

const mk = (n, domain, ordinal) => ({ id: `${domain}-${n}`, recipientEmail: `p${n}@${domain}`, ordinal });

function longestRun(list) {
  let best = 1, run = 1;
  for (let i = 1; i < list.length; i++) {
    run = list[i] === list[i - 1] ? run + 1 : 1;
    if (run > best) best = run;
  }
  return list.length ? best : 0;
}
const domains = (msgs) => msgs.map((m) => m.recipientEmail.split("@")[1]);

test("a firm-grouped list is broken up before sending", () => {
  // The case this exists for: 130 consecutive sends to one wirehouse is exactly
  // what gets a sending domain throttled, and the cost lands on eicatlanta.com
  // for all mail, not just this batch.
  const batch = [
    ...Array.from({ length: 130 }, (_, i) => mk(i, "morganstanley.com", i)),
    ...Array.from({ length: 100 }, (_, i) => mk(i, "ml.com", 130 + i)),
    ...Array.from({ length: 70 }, (_, i) => mk(i, "ubs.com", 230 + i)),
    ...Array.from({ length: 50 }, (_, i) => mk(i, "wellsfargo.com", 300 + i)),
  ];
  assert.equal(longestRun(domains(batch)), 130, "the input really is grouped");
  const order = core.interleaveByDomain(batch);
  assert.equal(order.length, batch.length, "nobody is dropped");
  assert.equal(new Set(order.map((m) => m.id)).size, batch.length, "nobody is duplicated");
  // Once the smaller firms run out the largest necessarily runs consecutively,
  // but the front of the batch -- where a gateway forms its impression -- is mixed.
  assert.ok(longestRun(domains(order).slice(0, 200)) <= 2,
    "the first 200 sends should alternate between firms");
});

test("a single-domain batch is left alone", () => {
  const batch = Array.from({ length: 20 }, (_, i) => mk(i, "morganstanley.com", i));
  const order = core.interleaveByDomain(batch);
  assert.deepEqual(order.map((m) => m.id), batch.map((m) => m.id), "nothing to interleave");
});

test("the order is deterministic and independent of input order", () => {
  const a = [mk(1, "b.com", 0), mk(2, "a.com", 1), mk(3, "b.com", 2)];
  const b = [mk(3, "b.com", 2), mk(2, "a.com", 1), mk(1, "b.com", 0)];
  assert.deepEqual(core.interleaveByDomain(a).map((m) => m.id),
                   core.interleaveByDomain(b).map((m) => m.id),
                   "a retry must not reshuffle a queue that is already scheduled");
});

test("recipients with no usable domain are still sent", () => {
  const batch = [mk(1, "a.com", 0), { id: "odd", recipientEmail: "", ordinal: 1 }];
  assert.equal(core.interleaveByDomain(batch).length, 2);
});

test("campaign health pauses only on a real signal", () => {
  // A percentage over three messages is noise, not a signal.
  assert.equal(core.campaignHealth(10, 5).pause, false, "sample too small to judge");
  assert.equal(core.campaignHealth(100, 3).pause, false, "3% is at the limit, not over it");
  assert.equal(core.campaignHealth(100, 4).pause, true);
  assert.equal(core.campaignHealth(250, 10).pause, true);
  assert.equal(core.campaignHealth(0, 0).pause, false);
});

test("the health thresholds are configurable", () => {
  process.env.EMAIL_BOUNCE_PAUSE_PERCENT = "10";
  process.env.EMAIL_BOUNCE_MIN_SAMPLE = "50";
  const cfg = core.config();
  assert.equal(core.campaignHealth(100, 4, cfg).pause, false, "4% is under a 10% limit");
  assert.equal(core.campaignHealth(100, 11, cfg).pause, true);
  assert.equal(core.campaignHealth(40, 40, cfg).pause, false, "under the larger sample floor");
  delete process.env.EMAIL_BOUNCE_PAUSE_PERCENT;
  delete process.env.EMAIL_BOUNCE_MIN_SAMPLE;
});

test("the brake pauses and never cancels", () => {
  // Pausing is reversible by a human who can look at the bounces. Cancelling
  // would destroy a half-sent campaign on an automated percentage.
  const src = require("fs").readFileSync(require.resolve("../email-worker/index.js"), "utf8");
  const fn = src.split("async function refreshBatch")[1].split(String.fromCharCode(10) + "}")[0];
  assert.match(fn, /status = "paused"/);
  assert.doesNotMatch(fn, /status = "canceled"/);
  assert.match(fn, /batch_paused_bounce_rate/);
});

test("sending is paced on the interleaved position, not list order", () => {
  const src = require("fs").readFileSync(require.resolve("../email-worker/index.js"), "utf8");
  assert.match(src, /claimed\.sendPosition >= 0 \? claimed\.sendPosition : claimed\.ordinal/,
    "with a fallback for batches approved before positions existed");
});
