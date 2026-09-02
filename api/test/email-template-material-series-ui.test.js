"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const email = fs.readFileSync(path.join(ROOT, "webapp/email.js"), "utf8");
const service = fs.readFileSync(path.join(ROOT, "api/shared/email-service.js"), "utf8");
const store = fs.readFileSync(path.join(ROOT, "api/shared/email-store.js"), "utf8");

test("template authoring requires material series rather than version PDFs", () => {
  assert.match(email, /<legend>Required material series<\/legend>/);
  assert.match(email, /class="tpl-family-req"/);
  assert.match(email, /requiredMaterialFamilyIds: \[\.\.\.document\.querySelectorAll\("\.tpl-family-req:checked"\)/);
  assert.match(email, /current UBS, Morgan Stanley, Merrill Lynch, Raymond James/);
  assert.doesNotMatch(email, /<legend>Required attachments<\/legend>\$\{docs\.length/);
});

test("the composer locks template-required series and explains automatic routing", () => {
  assert.match(email, /tag\.textContent = "required series"/);
  assert.match(email, /box\.checked = true; box\.disabled = true; box\.dataset\.templateRequired = "1"/);
  assert.match(email, /current approved client-group version is selected per recipient/);
});

test("the server merges template-required series independently of client input", () => {
  assert.match(service, /templateRequired = materials\.templateRequirements\(template, catalogDocuments\)/);
  assert.match(service, /\.\.\.templateRequired\.familyIds/);
  assert.match(service, /materials\.resolveFamilies\(allDocuments, materialFamilyIds, recipient\.email, routePolicy\)/);
  assert.match(store, /requiredMaterialFamilyIdsJson/);
  assert.match(store, /normalizedRequirements = materials\.templateRequirements\(input, await listDocuments\(\)\)/);
});
