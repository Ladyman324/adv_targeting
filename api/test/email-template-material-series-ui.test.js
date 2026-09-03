"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const email = fs.readFileSync(path.join(ROOT, "webapp/email.js"), "utf8");
const service = fs.readFileSync(path.join(ROOT, "api/shared/email-service.js"), "utf8");
const store = fs.readFileSync(path.join(ROOT, "api/shared/email-store.js"), "utf8");
const handler = fs.readFileSync(path.join(ROOT, "api/email/index.js"), "utf8");
const css = fs.readFileSync(path.join(ROOT, "webapp/email.css"), "utf8");
const desktopHtml = fs.readFileSync(path.join(ROOT, "webapp/index.html"), "utf8");
const fieldHtml = fs.readFileSync(path.join(ROOT, "webapp/field.html"), "utf8");

test("template authoring requires material series rather than version PDFs", () => {
  assert.match(email, /<legend>Required material series<\/legend>/);
  assert.match(email, /class="tpl-family-req"/);
  assert.match(email, /requiredMaterialFamilyIds: \[\.\.\.document\.querySelectorAll\("\.tpl-family-req:checked"\)/);
  assert.match(email, /current UBS, Morgan Stanley, Merrill Lynch, Raymond James/);
  assert.doesNotMatch(email, /<legend>Required attachments<\/legend>\$\{docs\.length/);
  assert.match(email, /<legend>Obsolete required attachments<\/legend>/);
  assert.match(email, /Saving removes these obsolete IDs/);
});

test("the composer locks template-required series and explains automatic routing", () => {
  assert.match(email, /tag\.textContent = "required series"/);
  assert.match(email, /box\.checked = true; box\.disabled = true; box\.dataset\.templateRequired = "1"/);
  assert.match(email, /current approved client-group version is selected per recipient/);
});

test("the server merges template-required series independently of client input", () => {
  assert.match(service, /templateRequired = materials\.templateRequirements\(template, catalogDocuments\)/);
  assert.match(service, /\.\.\.templateRequired\.familyIds/);
  assert.match(service, /materials\.resolveFamilies\(allDocuments, materialFamilyIds, recipient\.email, routePolicy,[\s\S]*strategies: recipient\.materialStrategies/);
  assert.match(store, /requiredMaterialFamilyIdsJson/);
  assert.match(store, /normalizedRequirements = materials\.templateRequirements\(input, await listDocuments\(\)\)/);
  assert.match(store, /templatesRequiringDocument\(await listTemplates\(\), docId\)/);
  assert.match(store, /document_required_by_template/);
});

test("the composer exposes hidden orphan requirements before batch creation", () => {
  assert.match(email, /id="emailRequirementWarning"/);
  assert.match(email, /chosenRequirements\.missingDocumentIds/);
  assert.match(email, /emailCreateButton/);
  assert.match(email, /missing\.length > 0/);
});

test("materials administration exposes audience, strategy and scrollable PDF preview controls", () => {
  assert.match(email, /<label>Approved for<select data-upload-field="audience">/);
  assert.match(email, /\["client", "Client"\]/);
  assert.match(email, /\["advisor_only", "Advisor Only"\]/);
  assert.match(email, /data-email="material-preview-queue"/);
  assert.match(email, /data-email="material-preview-doc"/);
  assert.match(email, /op=document_preview/);
  assert.match(email, /<iframe[\s\S]*materialPreview\.url/);
  assert.doesNotMatch(email, /materialPreview\.title\)}" sandbox/);
  assert.match(email, /fetch\(`\/api\/email\?op=document_preview/);
  assert.match(email, /const blob = await response\.blob\(\)/);
  assert.match(email, /URL\.createObjectURL\(blob\)/);
  assert.match(desktopHtml, /frame-src 'self' blob:/);
  assert.match(fieldHtml, /frame-src 'self' blob:/);
  assert.match(css, /email-material-preview-dialog[\s\S]*height:min\(880px,92vh\)/);
  assert.match(css, /email-material-preview-dialog iframe[\s\S]*height:100%/);
});

test("commentary upload suggestions understand EICIX and separator-heavy filenames", () => {
  assert.match(email, /replace\(\/_\+\/g, " "\)/);
  assert.match(email, /if \(\/\\bcommentary\\b\/i\.test\(raw\)\)/);
  assert.match(email, /ACV\|All\[- \]Cap\|EICIX\|Mutual Fund/);
  assert.match(email, /category === "Quarterly Commentary" \? "eic-commentary"/);
});
test("document preview is an admin-only, integrity-checked PDF response", () => {
  assert.match(handler, /op === "document_preview"[\s\S]*if \(!isAdmin\(who\)\)/);
  assert.match(handler, /Content-Type": "application\/pdf"/);
  assert.match(handler, /Content-Disposition": `inline/);
  assert.match(store, /async function documentPreview/);
  assert.match(store, /Number\(properties\.contentLength\) !== Number\(doc\.size\)/);
  assert.match(store, /sha256 !== doc\.sha256/);
});

test("the PDF preview endpoint executes only for an email administrator", async () => {
  const baseStore = require("../shared/store");
  const emailStore = require("../shared/email-store");
  const handlerPath = require.resolve("../email/index.js");
  const originalIdentity = baseStore.identity;
  const originalPreview = emailStore.documentPreview;
  let roles = ["EmailAdministrator"], previewCalls = 0;
  baseStore.identity = () => ({ id: "admin", name: "Admin", roles });
  emailStore.documentPreview = async (id) => {
    previewCalls++;
    assert.equal(id, "quarterly");
    return { doc: { name: "Quarterly Commentary", fileName: "Quarterly.PDF" },
      bytes: Buffer.from("%PDF-test") };
  };
  delete require.cache[handlerPath];
  const handler = require(handlerPath);
  try {
    const context = { log: { error() {} } };
    await handler(context, { method: "GET", query: { op: "document_preview", id: "quarterly" } });
    assert.equal(context.res.status, 200);
    assert.equal(context.res.isRaw, true);
    assert.equal(context.res.headers["Content-Type"], "application/pdf");
    assert.match(context.res.headers["Content-Disposition"], /^inline; filename="Quarterly\.PDF"$/);
    assert.equal(context.res.body.toString(), "%PDF-test");

    roles = [];
    const denied = { log: { error() {} } };
    await handler(denied, { method: "GET", query: { op: "document_preview", id: "quarterly" } });
    assert.equal(denied.res.status, 403);
    assert.equal(previewCalls, 1, "a non-admin request must be rejected before blob access");
  } finally {
    baseStore.identity = originalIdentity;
    emailStore.documentPreview = originalPreview;
    delete require.cache[handlerPath];
  }
});
test("the catalog keeps audience and commentary strategy as server metadata", () => {
  assert.match(store, /audience: e\.materialAudience \|\| "advisor_only"/);
  assert.match(store, /strategy: e\.materialStrategy \|\| "general"/);
  assert.match(store, /materialAudience: meta\.audience, materialStrategy: meta\.strategy/);
  assert.match(store, /materialSlotKey\(doc\) === slot/);
  assert.match(store, /material_client_preferred/);
});
