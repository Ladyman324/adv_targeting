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

test("domain health combines counts across senders instead of averaging their rates", () => {
  const rows = health.summariseByDomain([
    ...sends(40, "u1", "ubs.com"), ...sends(160, "u2", "ubs.com"),
    ...sends(300, "u2", "ml.com"),
  ], events(4, "u1", "hard", "ubs.com", "5.1.1"),
  [{ userId: "u2", domain: "ubs.com" }]);
  const ubs = rows.find((row) => row.domain === "ubs.com");
  assert.equal(ubs.sent, 200);
  assert.equal(ubs.hard, 4);
  assert.equal(ubs.rates.hard, 2);
  assert.equal(ubs.unsubscribed, 1);
  assert.equal(ubs.rates.unsubscribe, 0.5);
  assert.equal(ubs.senders.length, 2);
  assert.equal(ubs.senders.find((row) => row.userId === "u1").hard, 4);
  assert.equal(ubs.senders.find((row) => row.userId === "u2").hard, 0);
  assert.equal(rows[0].domain, "ml.com");
  assert.equal(rows[0].hard, 0);
  assert.deepEqual(ubs.codes, [{ code: "5.1.1", n: 4 }]);
  assert.equal(rows.reduce((total, row) => total + row.sent, 0), 500);
});

test("domain health normalises domains, retains event-only domains, and labels small samples honestly", () => {
  const rows = health.summariseByDomain([
    ...sends(6, "u1", " UBS.COM "), ...sends(4, "u2", "ubs.com"),
    ...sends(1, "u1", ""),
  ], [
    ...events(2, "u1", "hard", "ubs.com", "5.1.1"),
    ...events(1, "u2", "policy", "new.com", "5.7.1"),
  ]);
  const ubs = rows.find((row) => row.domain === "ubs.com");
  assert.equal(ubs.sent, 10);
  assert.equal(ubs.levels.hard, "unknown");
  assert.equal(ubs.advice.length, 1);
  assert.match(ubs.advice[0].text, /Only 10/);
  assert.equal(rows.find((row) => row.domain === "(unknown)").sent, 1);
  assert.equal(rows.find((row) => row.domain === "new.com").policy, 1);
  assert.equal(rows.find((row) => row.domain === "new.com").rates.policy, 0);
  assert.deepEqual(health.summariseByDomain([], []), []);
});

test("domain advice distinguishes list quality, policy, and sending pace without repeating the same domain warning", () => {
  const [row] = health.summariseByDomain(sends(100, "u1", "ubs.com"), [
    ...events(5, "u1", "hard", "ubs.com", "5.1.1"),
    ...events(3, "u1", "policy", "ubs.com", "5.7.1"),
    ...events(10, "u1", "soft", "ubs.com", "4.7.0"),
  ]);
  assert.equal(row.advice.length, 3);
  assert.match(row.advice[0].text, /list-quality problem/);
  assert.match(row.advice[1].text, /reputation/);
  assert.match(row.advice[2].text, /space sends/);
});

test("Sender Health toggle renders domain metrics and sender drilldown without a second API call", async () => {
  const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
  const source = fs.readFileSync(path.join(__dirname, "../../webapp/email.js"), "utf8");
  const nodes = { emailTitle: {}, emailBody: {} };
  let apiCalls = 0;
  const sendsData = [...sends(50, "u1", "ubs.com"), ...sends(50, "u2", "ubs.com")];
  const data = { reps: health.summarise(sendsData, []),
    domains: health.summariseByDomain(sendsData, []), totals: { sends: 100, events: 0 } };
  const ctx = vm.createContext({ document: { getElementById: (id) => nodes[id] },
    esc: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    api: async () => { apiCalls++; return data; } });
  vm.runInContext(source.slice(source.indexOf("  let healthDays = 90;"),
    source.indexOf("  async function openDailyCaps(")), ctx);
  await vm.runInContext("openHealth()", ctx);
  assert.match(nodes.emailBody.innerHTML, /By sender/);
  assert.match(nodes.emailBody.innerHTML, /By domain/);
  assert.match(nodes.emailBody.innerHTML, /<h3>Rep u1<\/h3>/);
  const start = source.indexOf('    if (action === "health-group") {');
  const end = source.indexOf("\n    }", start) + "\n    }".length;
  ctx.action = "health-group";
  ctx.button = { dataset: { group: "domain" } };
  vm.runInContext("(function(){" + source.slice(start, end) + "})()", ctx);
  assert.equal(apiCalls, 1);
  assert.match(nodes.emailBody.innerHTML, /<h3>ubs.com<\/h3>/);
  assert.match(nodes.emailBody.innerHTML, /Recipient domains across all senders/);
  assert.match(nodes.emailBody.innerHTML, /aria-pressed="true"/);
  assert.match(nodes.emailBody.innerHTML, /<th>Sender<\/th>/);
  assert.match(nodes.emailBody.innerHTML, /Rep u1/);
  assert.match(nodes.emailBody.innerHTML, /Rep u2/);
  ctx.button.dataset.group = "sender";
  vm.runInContext("(function(){" + source.slice(start, end) + "})()", ctx);
  assert.match(nodes.emailBody.innerHTML, /<h3>Rep u1<\/h3>/);
  assert.equal(apiCalls, 1);
});
