"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHandler, config } = require("../training-video");

function fixture(overrides = {}) {
  const requested = [];
  const generated = [];
  const missing = new Set(overrides.missing || []);
  const container = {
    getBlobClient(name) {
      requested.push(name);
      return {
        async getProperties() {
          if (missing.has(name)) {
            const err = new Error("missing");
            err.statusCode = 404;
            throw err;
          }
          return {};
        },
        async generateSasUrl(options) {
          generated.push({ name, options });
          return `https://storage.invalid/training/${name}?sig=short-lived`;
        },
      };
    },
  };
  const service = {
    getContainerClient(name) {
      assert.equal(name, "training");
      return container;
    },
  };
  const deps = {
    env: { AZURE_STORAGE_CONNECTION_STRING: "secret" },
    identity: overrides.identity || (() => ({ id: "rep" })),
    serviceFactory: () => service,
    now: () => new Date("2026-09-23T16:00:00Z"),
    ok(context, body) { context.res = { status: 200, body }; },
    fail(context, err) {
      context.res = { status: err.statusCode || 500, body: { error: err.message } };
    },
  };
  return { handler: createHandler(deps), requested, generated };
}

test("returns read-only, expiring URLs for an authenticated employee", async () => {
  const f = fixture();
  const context = {};
  await f.handler(context, { headers: {} });

  assert.equal(context.res.status, 200);
  assert.equal(context.res.body.version, "1");
  assert.match(context.res.body.videoUrl, /advisor-map-introduction-v1\.mp4/);
  assert.match(context.res.body.posterUrl, /\.jpg/);
  assert.match(context.res.body.captionsUrl, /\.vtt/);
  assert.equal(context.res.body.expiresUtc, "2026-09-23T17:00:00.000Z");
  assert.deepEqual(f.requested, [
    "advisor-map-introduction-v1.mp4",
    "advisor-map-introduction-v1.jpg",
    "advisor-map-introduction-v1.vtt",
  ]);
  for (const row of f.generated) {
    assert.equal(row.options.permissions.toString(), "r");
    assert.equal(row.options.startsOn.toISOString(), "2026-09-23T15:55:00.000Z");
    assert.equal(row.options.expiresOn.toISOString(), "2026-09-23T17:00:00.000Z");
  }
  assert.doesNotMatch(JSON.stringify(context.res.body), /secret/);
});

test("optional poster and captions may be absent", async () => {
  const f = fixture({ missing: [
    "advisor-map-introduction-v1.jpg",
    "advisor-map-introduction-v1.vtt",
  ] });
  const context = {};
  await f.handler(context, { headers: {} });
  assert.equal(context.res.status, 200);
  assert.equal(context.res.body.posterUrl, undefined);
  assert.equal(context.res.body.captionsUrl, undefined);
  assert.equal(f.generated.length, 1);
});

test("the recording itself is required", async () => {
  const f = fixture({ missing: ["advisor-map-introduction-v1.mp4"] });
  const context = {};
  await f.handler(context, { headers: {} });
  assert.equal(context.res.status, 503);
  assert.match(context.res.body.error, /temporarily unavailable/i);
});

test("authentication is checked before storage is touched", async () => {
  let touched = false;
  const handler = createHandler({
    env: { AZURE_STORAGE_CONNECTION_STRING: "secret" },
    identity() {
      const err = new Error("Not signed in.");
      err.statusCode = 401;
      throw err;
    },
    serviceFactory() { touched = true; throw new Error("must not happen"); },
    ok() {},
    fail(context, err) {
      context.res = { status: err.statusCode, body: { error: err.message } };
    },
  });
  const context = {};
  await handler(context, { headers: {} });
  assert.equal(context.res.status, 401);
  assert.equal(touched, false);
});

test("unsafe configured blob paths fail closed", () => {
  assert.throws(() => config({ TRAINING_VIDEO_BLOB: "../private.txt" }), /invalid/i);
});
