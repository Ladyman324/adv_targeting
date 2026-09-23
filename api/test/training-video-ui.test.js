"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const read = (name) => fs.readFileSync(path.join(ROOT, name), "utf8");

test("both applications load the shared training module before their view", () => {
  const desktop = read("webapp/index.html");
  const field = read("webapp/field.html");
  const scripts = (html) => [...html.matchAll(/<script src="([^"?]+)/g)]
    .map((match) => path.basename(match[1]));
  const desktopScripts = scripts(desktop);
  const fieldScripts = scripts(field);
  assert.ok(desktopScripts.indexOf("training.js") < desktopScripts.indexOf("app.js"));
  assert.ok(fieldScripts.indexOf("training.js") < fieldScripts.indexOf("field.js"));
  for (const html of [desktop, field]) {
    assert.match(html, /media-src[^;]*eicadvisorlog\.blob\.core\.windows\.net/);
    assert.doesNotMatch(html, /Advisor Map - Introduction\.mp4/);
  }
});

test("the player is opt-in, metadata-only, and records a versioned response", () => {
  const source = read("webapp/training.js");
  assert.match(source, /preload = "metadata"/);
  assert.match(source, /\/api\/training-video/);
  assert.match(source, /introVideoSeenVersion/);
  assert.match(source, /Dial\.saveSettings/);
  assert.doesNotMatch(source, /autoplay/i);
  assert.doesNotMatch(source, /\.mp4/);
});
