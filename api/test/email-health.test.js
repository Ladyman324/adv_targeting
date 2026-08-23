"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const health = require("../shared/email-health");

const sends = (n, userId, domain) => Array.from({ length: n }, () =>
  ({ userId, userName: "Rep " + userId, domain, address: `x${Math.random()}@${domain}`, sentUtc: "2026-08-19" }));
const events = (n, userId, kind, domain, code) => Array.from({ length: n }, () =>
  ({ userId, kind, domain, code }));

test("a small sample yields no verdict rather than a wrong one", () => {
  // Two bounces out of six is 33% and means nothing. Reporting it as a crisis
  // would train people to ignore the dashboard.
  const [r] = health.summarise(sends(6, "u1", "ml.com"), events(2, "u1", "hard", "ml.com", "5.1.1"));
  assert.equal(r.levels.hard, "unknown");
  assert.match(r.advice[0].text, /Only 6 message/);
  assert.equal(r.advice.length, 1, "and nothing else, because nothing else is knowable");
});

test("a clean sender is told so plainly", () => {
  const [r] = health.summarise(sends(200, "u1", "ml.com"), []);
  assert.equal(r.levels.hard, "ok");
  assert.equal(r.advice.length, 1);
  assert.equal(r.advice[0].level, "ok");
  assert.match(r.advice[0].text, /Nothing to act on/);
});

test("each signal produces its own, different remedy", () => {
  const [r] = health.summarise(sends(200, "u1", "ml.com"), [
    ...events(12, "u1", "hard", "ml.com", "5.1.1"),
    ...events(30, "u1", "soft", "ml.com", "4.7.0"),
    ...events(8, "u1", "policy", "ml.com", "5.7.1"),
  ]);
  const text = r.advice.map((a) => a.text).join(" ");
  assert.match(text, /list-quality problem/, "hard bounces are about the list");
  assert.match(text, /earliest warning/, "deferrals are about pace");
  assert.match(text, /reputation\s+or content/, "policy refusals are about content and reputation");
  assert.equal(r.levels.hard, "bad");
});

test("a problem confined to one firm is named as such", () => {
  // The whole reason for a per-domain breakdown: a firm-wide average hides the
  // one wirehouse that has started refusing.
  const [r] = health.summarise(
    [...sends(150, "u1", "ml.com"), ...sends(60, "u1", "ubs.com")],
    events(9, "u1", "hard", "ubs.com", "5.1.1"));
  const text = r.advice.map((a) => a.text).join(" ");
  assert.match(text, /ubs\.com/);
  assert.match(text, /throttles per domain/);
  const ubs = r.domains.find((d) => d.domain === "ubs.com");
  const ml = r.domains.find((d) => d.domain === "ml.com");
  assert.equal(ubs.levels.hard, "bad");
  assert.equal(ml.levels.hard, "ok", "the healthy firm is not implicated");
});

test("only delivery attempts count toward the denominator", () => {
  // Rates are per message that actually reached Exchange. Counting drafts would
  // flatter every number.
  const [r] = health.summarise(sends(100, "u1", "ml.com"), events(5, "u1", "hard", "ml.com", "5.1.1"));
  assert.equal(r.sent, 100);
  assert.equal(r.rates.hard, 5);
});

test("reps are kept separate and sorted by volume", () => {
  const out = health.summarise(
    [...sends(50, "u1", "ml.com"), ...sends(300, "u2", "ml.com")],
    events(20, "u1", "hard", "ml.com", "5.1.1"));
  assert.equal(out.length, 2);
  assert.equal(out[0].userId, "u2", "busiest first");
  assert.equal(out.find((x) => x.userId === "u2").hard, 0,
    "one rep's bounces never land on another");
});

test("unsubscribes are counted where they are attributable", () => {
  const s = sends(100, "u1", "ml.com");
  const [r] = health.summarise(s, [], [{ userId: "u1", domain: "ml.com" }, { userId: "u1", domain: "ml.com" }]);
  assert.equal(r.unsubscribed, 2);
  assert.equal(r.rates.unsubscribe, 2);
});
