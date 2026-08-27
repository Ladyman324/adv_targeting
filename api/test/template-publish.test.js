"use strict";

/* A template may exist without being sendable.
 *
 * `retired` already existed and hides a template from EVERYONE, admins
 * included -- a soft delete with no way back. What was missing is the state in
 * between: written, in the library, not yet cleared for the sales team.
 *
 * Held templates are REMOVED for a rep rather than disabled, and the removal
 * happens on the server. A greyed-out row still ships the wording to anyone who
 * opens developer tools, and unapproved wording reaching the sales team is the
 * whole thing this is meant to prevent.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("module");

process.env.AZURE_STORAGE_CONNECTION_STRING =
  "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net";

const TEMPLATES = [
  { id: "live", name: "Case for Value", subject: "s", bodyText: "b", version: 3,
    published: true, requiredDocumentIds: [] },
  { id: "held", name: "Draft wording", subject: "s", bodyText: "b", version: 1,
    published: false, requiredDocumentIds: [] },
  { id: "legacy", name: "Written before this existed", subject: "s", bodyText: "b",
    version: 9, requiredDocumentIds: [] },
];

function load() {
  const servicePath = require.resolve("../shared/email-service.js");
  const storeStub = {
    listTemplates: async () => TEMPLATES.map((t) => ({ ...t })),
    getTemplate: async (id) => TEMPLATES.map((t) => ({ ...t })).find((t) => t.id === id) || null,
    listDocuments: async () => [],
    policy: async () => ({ killed: false }),
    rollingExternalCount: async () => 0,
  };
  const authStub = { status: async () => ({ connected: true, mail: "rep@eicatlanta.com" }) };
  delete require.cache[servicePath];
  const realLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (parent && parent.filename === servicePath) {
      if (request === "./email-store") return storeStub;
      if (request === "./email-auth") return authStub;
    }
    return realLoad.call(this, request, parent, isMain);
  };
  try { return require(servicePath); }
  finally { Module._load = realLoad; delete require.cache[servicePath]; }
}

test("a rep never receives a held template, and an admin sees all of them", async () => {
  const service = load();
  const rep = await service.catalog({ id: "u1", name: "Rep" }, { isAdmin: false });
  const admin = await service.catalog({ id: "u2", name: "Admin" }, { isAdmin: true });

  assert.deepEqual(rep.templates.map((t) => t.id), ["live", "legacy"],
    "the held template is absent from the payload, not merely flagged in it");
  assert.deepEqual(admin.templates.map((t) => t.id), ["live", "held", "legacy"]);
});

test("a template written before this existed stays live", async () => {
  // Absent means published. Anything else would silently withdraw every
  // template in the library the moment this shipped.
  const service = load();
  const rep = await service.catalog({ id: "u1", name: "Rep" }, { isAdmin: false });
  assert.ok(rep.templates.some((t) => t.id === "legacy"),
    "no published field means published; an existing library must not go dark");
});

test("catalog with no options is treated as a rep, not an admin", async () => {
  // Fail closed: a caller that forgets to say who is asking must not be handed
  // unapproved wording.
  const service = load();
  const anon = await service.catalog({ id: "u1", name: "Rep" });
  assert.equal(anon.templates.some((t) => t.id === "held"), false);
});
