"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const email = fs.readFileSync(path.join(ROOT, "webapp/email.js"), "utf8");
const css = fs.readFileSync(path.join(ROOT, "webapp/email.css"), "utf8");
const app = fs.readFileSync(path.join(ROOT, "webapp/app.js"), "utf8");
const field = fs.readFileSync(path.join(ROOT, "webapp/field.html"), "utf8");

function bodyOf(name) {
  const start = email.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} exists`);
  const next = email.indexOf("\n  function ", start + 10);
  return email.slice(start, next < 0 ? email.length : next);
}

test("daily capacity is visible and described as an Eastern calendar day", () => {
  assert.match(email, /Daily email capacity/);
  assert.match(email, /available today/);
  assert.match(email, /Resets at midnight Eastern/);
  assert.match(email, /Approved and scheduled emails reserve capacity\. Outlook drafts do not\./);
  assert.doesNotMatch(bodyOf("sendBlockedReason"), /rolling|last 24|24-hour|directBatchMax|INTERNAL/);
});

test("the composer uses a server-authored plan and binds approval to its hash", () => {
  const request = bodyOf("requestCapacityPlan");
  assert.match(request, /api\("capacity_plan", \{ batchId: detail\.batch\.id, scheduledForUtc \}\)/);
  assert.match(email, /capacityPlanHash: mode === "send" \? deliveryPlan\.hash : ""/);
  assert.match(email, /action === "mate-toggle"[\s\S]*?deliveryPlan = null; deliveryPlanKey = ""; deliveryPlanError = "";[\s\S]*?await requestCapacityPlan\(\)/,
    "advisor Cc changes must invalidate the visible plan even when the primary count is unchanged");
  assert.match(email, /The delivery plan was refreshed; review it and approve again\./);
  assert.match(email, /Approve multi-day send/);
  assert.match(email, /Approve &amp; Schedule/);
  assert.match(email, /Approve &amp; Send/);
});

test("schedule edits refresh capacity without replacing the active native inputs", () => {
  const sync = bodyOf("syncScheduleInputs");
  assert.match(sync, /scheduleCapacityPlanRefresh\(300, true\)/);
  assert.doesNotMatch(sync, /composerView\(\)/);
  const request = bodyOf("requestCapacityPlan");
  assert.match(request, /paintCapacityPlanNodes\(\)/);
  assert.match(email, /Your weekend start moves to/);
  assert.match(email, /weekends are skipped/i);
});

test("the plan gives fit remediation and repeats daily tranches at confirmation", () => {
  assert.match(email, /Remove at least \$\{excess\} recipient/);
  assert.match(email, /schedule a smaller batch, or use Outlook drafts/);
  assert.match(email, /planLines = mode === "send"/);
  assert.match(email, /\$\{planDayLabel\(day\)\} — \$\{Number\(day\.messageCount\) \|\| 0\} emails · \$\{Number\(day\.units\) \|\| 0\} capacity/);
  assert.match(email, /count toward daily capacity/);
  assert.match(email, /Number\(minParts\.day\) \+ 7/,
    "the picker maximum follows Eastern calendar dates rather than 168 elapsed hours");
});

test("capacity details stay in scrollable content and the sticky footer stays compact", () => {
  assert.match(email, /id="emailDeliveryPlan" class="email-plan/);
  assert.match(email, /id="emailCapacityCompact"/);
  assert.match(css, /complete capacity plan lives in the scrollable editor/);
  assert.match(css, /\.email-footer-status/);
  assert.doesNotMatch(css, /\.email-footer-status\{[^}]*position:\s*sticky/s);
  assert.match(css, /\.email-list \.email-jump\{[^}]*overflow-y:auto;overflow-x:hidden/s);
});

test("desktop Lists shows one compact capacity line and Field inherits the shared emailer", () => {
  assert.match(app, /data-email-capacity-status/);
  assert.match(app, /emailcapacityrequest/);
  assert.equal((app.match(/data-email-capacity-status/g) || []).length, 1);
  assert.match(field, /<script src="email\.js\?/);
  assert.match(field, /<link rel="stylesheet" href="email\.css\?/);
  assert.doesNotMatch(field, /data-email-capacity-status/);
});

test("Email Activity reads the stored capacity plan for progress and next send", () => {
  const schedule = bodyOf("batchScheduleText");
  assert.match(schedule, /storedCapacityPlan\(batch\)/);
  assert.match(schedule, /of \$\{total\} sent · \$\{scheduled\} scheduled/);
  assert.match(schedule, /Next: \$\{shortEasternDateTime\(next\)\}/);
  assert.match(schedule, /unsent · held for review/);
});
