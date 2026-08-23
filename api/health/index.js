/* GET /api/health — is logging actually going to work, and who am I?
 *
 * Exists so the front end can find out at load time rather than at the moment a
 * rep presses a disposition button. If storage is unconfigured, the dialer says
 * so up front and refuses to start a session -- far better than accepting an
 * afternoon of calls and failing on every save.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const store = require("../shared/store");
const actSync = require("../shared/act");

const RELEASE_PATH = path.join(__dirname, "..", "release.json");

/*
 * release.json is generated in a fresh staging directory by build_api.sh.  It
 * contains only build provenance; never environment variables, Azure settings,
 * contact data, or credentials.  A whitelist here makes that guarantee hold
 * even if somebody accidentally adds a sensitive field to the build file.
 */
function releaseMetadata(file = RELEASE_PATH) {
  const unavailable = {
    available: false, id: "development", commit: "", builtUtc: "",
    workflowRun: "", dirty: false,
  };
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    const id = String(raw.id || "");
    const commit = String(raw.commit || "");
    const builtUtc = String(raw.builtUtc || "");
    const workflowRun = String(raw.workflowRun || "");
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(id)) return unavailable;
    if (!/^[0-9a-f]{7,40}$/i.test(commit)) return unavailable;
    if (!/^\d{4}-\d{2}-\d{2}T/.test(builtUtc)
        || !Number.isFinite(Date.parse(builtUtc))) return unavailable;
    if (workflowRun && !/^[A-Za-z0-9._-]{1,64}$/.test(workflowRun))
      return unavailable;
    return {
      available: true,
      id,
      commit: commit.toLowerCase(),
      builtUtc,
      workflowRun,
      dirty: raw.dirty === true,
    };
  } catch {
    return unavailable;
  }
}

const release = releaseMetadata();

async function health(context, req) {
  try {
    const who = store.identity(req);
    let storageOk = false;
    let detail = "";
    if (store.configured()) {
      try {
        await store.getQueue(who);          // cheapest real round trip
        storageOk = true;
      } catch (e) {
        detail = e.message || String(e);
      }
    } else {
      detail = "AZURE_STORAGE_CONNECTION_STRING is not set.";
    }
    return store.ok(context, {
      user: { id: who.id, name: who.name, dev: who.dev },
      configured: store.configured(),
      storageOk,
      detail,
      release,
      // Diagnostic only, and deliberately says nothing the caller could not
      // already infer. "Why did my call not appear in Act!" has several
      // answers that all look identical from the outside -- not deployed, not
      // switched on, no Act! account for this person, no contact match for that
      // advisor -- and guessing between them costs an afternoon each time.
      //
      // No secret is exposed: whether a setting is present, never its value.
      act: await actSync.diagnose(who),
    });
  } catch (err) {
    return store.fail(context, err);
  }
}

module.exports = health;
module.exports.releaseMetadata = releaseMetadata;
