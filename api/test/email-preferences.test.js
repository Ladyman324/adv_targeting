"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("module");

function load(options = {}) {
  const calls = { suppress: [], act: [], synced: [] };
  const claim = Object.hasOwn(options, "claim") ? options.claim
    : { email: "Advisor@Example.com", crd: "123456" };
  const routePath = require.resolve("../email-preferences/index.js");
  const suppress = {
    readToken: () => claim,
    norm: (value) => String(value || "").trim().toLowerCase(),
    suppress: async (...args) => { calls.suppress.push(args); return { added: true }; },
    markActSynced: async (email) => { calls.synced.push(email); },
  };
  const act = {
    markDoNotEmail: async (...args) => {
      calls.act.push(args);
      if (options.actError) throw options.actError;
      return options.actResult || { ok: true };
    },
  };
  delete require.cache[routePath];
  const realLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (parent && parent.filename === routePath) {
      if (request === "../shared/email-suppress") return suppress;
      if (request === "../shared/act") return act;
    }
    return realLoad.call(this, request, parent, isMain);
  };
  try { return { route: require(routePath), calls }; }
  finally { Module._load = realLoad; delete require.cache[routePath]; }
}

function context(logs) {
  return { log: Object.assign((value) => logs.push(String(value)),
    { warn: (value) => logs.push(String(value)), error: (value) => logs.push(String(value)) }) };
}

async function request(route, method, { query = {}, body = {}, headers = {} } = {}) {
  const logs = [];
  const ctx = context(logs);
  await route(ctx, { method, query, body, headers });
  return { status: ctx.res.status, html: ctx.res.body, logs };
}

test("GET renders an address-confirmation form and never suppresses", async () => {
  const { route, calls } = load();
  const response = await request(route, "GET", { query: { t: "signed-token" } });
  assert.equal(response.status, 200);
  assert.match(response.html, /<form method="post"/);
  assert.match(response.html, /name="email" type="email"/);
  assert.match(response.html, /name="t" value="signed-token"/);
  assert.doesNotMatch(response.html, /Advisor@Example\.com/i,
    "the page must not hand a form-filling scanner the answer");
  assert.equal(calls.suppress.length, 0);
  assert.equal(calls.act.length, 0);
});

test("an empty or wrong submitted address writes nothing", async () => {
  for (const email of ["", "someone-else@example.com"]) {
    const { route, calls } = load();
    const response = await request(route, "POST", {
      body: { t: "signed-token", email },
    });
    assert.equal(response.status, 200);
    assert.match(response.html, /does not match/);
    assert.doesNotMatch(response.html, /Advisor@Example\.com/i);
    assert.equal(calls.suppress.length, 0);
    assert.equal(calls.act.length, 0);
  }
});

test("a normalized match suppresses only the signed token address", async () => {
  const { route, calls } = load();
  const response = await request(route, "POST", {
    body: "t=signed-token&email=%20advisor%40example.com%20",
  });
  assert.equal(response.status, 200);
  assert.match(response.html, /You have been unsubscribed/);
  assert.doesNotMatch(response.html, /Advisor@Example\.com/i);
  assert.deepEqual(calls.suppress, [["Advisor@Example.com", { source: "unsubscribe-form" }]]);
  assert.deepEqual(calls.act, [["Advisor@Example.com", "123456"]]);
  assert.deepEqual(calls.synced, ["Advisor@Example.com"]);
});

test("an Act failure cannot undo the local suppression", async () => {
  const { route, calls } = load({ actError: new Error("CRM unavailable") });
  const response = await request(route, "POST", {
    body: { t: "signed-token", email: "advisor@example.COM" },
  });
  assert.equal(response.status, 200);
  assert.match(response.html, /You have been unsubscribed/);
  assert.equal(calls.suppress.length, 1);
  assert.equal(calls.act.length, 1);
  assert.equal(calls.synced.length, 0);
});

test("an invalid signed token discloses nothing and writes nothing", async () => {
  const { route, calls } = load({ claim: null });
  const response = await request(route, "POST", {
    body: { t: "bad-token", email: "advisor@example.com" },
  });
  assert.equal(response.status, 400);
  assert.doesNotMatch(response.html, /advisor@example\.com/i);
  assert.equal(calls.suppress.length, 0);
  assert.equal(calls.act.length, 0);
});

test("request telemetry never logs the bearer token from the referrer", async () => {
  const { route } = load();
  const response = await request(route, "POST", {
    body: { t: "signed-token", email: "" },
    headers: {
      "user-agent": "MailScanner/1.0",
      "x-forwarded-for": "[2001:db8::1234]:443, 10.0.0.1",
      referer: "https://advisors.example/api/email-preferences?t=sealed-bearer#form",
    },
  });
  const log = response.logs.join("\n");
  assert.match(log, /MailScanner\/1\.0/);
  assert.match(log, /2001:db8::1234/);
  assert.match(log, /https:\/\/advisors\.example\/api\/email-preferences/);
  assert.doesNotMatch(log, /sealed-bearer|\?t=|#form/);
});
