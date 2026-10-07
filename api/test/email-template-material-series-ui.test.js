"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const email = fs.readFileSync(path.join(ROOT, "webapp/email.js"), "utf8");
const service = fs.readFileSync(path.join(ROOT, "api/shared/email-service.js"), "utf8");
const store = fs.readFileSync(path.join(ROOT, "api/shared/email-store.js"), "utf8");
const handler = fs.readFileSync(path.join(ROOT, "api/email/index.js"), "utf8");
const css = fs.readFileSync(path.join(ROOT, "webapp/email.css"), "utf8");
const desktopHtml = fs.readFileSync(path.join(ROOT, "webapp/index.html"), "utf8");
const fieldHtml = fs.readFileSync(path.join(ROOT, "webapp/field.html"), "utf8");

test("Fact Sheet is available in document metadata and recognized during batch upload", () => {
  assert.match(email, /MATERIAL_CATEGORIES = \["Presentation", "Fact Sheet"/);
  assert.match(email, /fact\\s\*sheet/);
  assert.match(email, /category = "Fact Sheet"/);
});

test("collapsed library rows expose the confirmed Withdraw action", () => {
  const start = email.indexOf("function libraryHtml(docs) {");
  const end = email.indexOf("function routesHtml()", start);
  const library = email.slice(start, end);
  assert.match(library, /<summary>[\s\S]*data-email="doc-delete"[\s\S]*<\/summary>/);
  assert.match(library, /aria-label="Withdraw /);
  assert.match(email, /Withdraw "\$\{name\}" from the approved catalog/);
  assert.match(email, /if \(action === "doc-delete"\)[\s\S]*confirm\(/);
  assert.match(email, /if \(!toggle\) event\.preventDefault\(\)/,
    "the shared button dispatcher prevents the Withdraw click from opening the disclosure");
  assert.equal((library.match(/data-email="doc-delete"/g) || []).length, 1,
    "one Withdraw control per document, visible even when collapsed");
});

test("monthly library labels distinguish older approved editions without changing send eligibility", () => {
  const start = email.indexOf("function libraryHtml(docs) {");
  const end = email.indexOf("function routesHtml()", start);
  const library = email.slice(start, end);
  const ctx = vm.createContext({
    materialSearch: "", MATERIAL_CHANNELS: [["generic", "Generic"], ["ubs", "UBS"]],
    materialStatus: (d) => d.freshness || "current",
    materialSendable: (d) => ["current", "stale"].includes(d.freshness || "current"),
    esc: (v) => String(v), channelLabel: (v) => v, audienceLabel: (v) => v,
    strategyLabel: (v) => v, materialEditFields: () => "", bytes: () => "1 KB",
  });
  vm.runInContext(library, ctx);
  const make = (id, channel, periodKey, audience = "client", freshness = "current") => ({
    id, name: id, familyId: "case", familyName: "Case for Value", category: "Case for Value vs Growth",
    channel, strategy: "general", periodKind: "month", periodKey, audience, freshness, version: 1,
  });
  const html = vm.runInContext("libraryHtml(docs)", Object.assign(ctx, { docs: [
    make("july", "generic", "2026-07"),
    make("september", "generic", "2026-09"),
    make("ubs-july", "ubs", "2026-07"),
    make("ubs-september-advisor", "ubs", "2026-09", "advisor_only"),
  ] }));
  assert.match(html, /2026-07<\/span><span class="email-material-version-actions"><span class="email-fresh older">older approved/);
  assert.match(html, /2026-09<\/span><span class="email-material-version-actions"><span class="email-fresh current">current/);
  assert.equal((html.match(/older approved/g) || []).length, 2,
    "a newer edition is visible regardless of the older edition's audience designation");
  assert.doesNotMatch(library, /currentDocument\(/,
    "display labels must not change the server's attachment-permission rule");
});

test("template authoring recommends material series rather than version PDFs", () => {
  assert.match(email, /<legend>Recommended material series<\/legend>/);
  assert.match(email, /class="tpl-family-req"/);
  assert.match(email, /requiredMaterialFamilyIds: \[\.\.\.document\.querySelectorAll\("\.tpl-family-req:checked"\)/);
  assert.match(email, /current UBS, Morgan Stanley, Merrill Lynch, Raymond James/);
  assert.doesNotMatch(email, /<legend>Required attachments<\/legend>\$\{docs\.length/);
  assert.match(email, /<legend>Unavailable recommendations<\/legend>/);
  assert.match(email, /Saving removes these obsolete IDs/);
});

test("the composer preselects removable materials and previews wording before generation", () => {
  assert.match(email, /tag\.textContent = "recommended"/);
  assert.match(email, /box\.checked = isSuggested && !box\.disabled/);
  assert.match(email, /id="emailSetupPreview"/);
  assert.match(email, /previewHtml\(chosen, SAMPLE\)/);
  assert.match(email, /You removed a recommended attachment/);
  assert.match(email, /latest approved client-group version is selected per recipient/);
});

test("mixed material periods require an explicit pre-generation choice and save held contacts", () => {
  assert.match(email, /data-material-choice="latest"/);
  assert.match(email, /data-material-choice="newer"/);
  assert.match(email, /api\("preview_materials", \{ recipients: kept, materialFamilyIds \}\)/);
  assert.match(email, /saveMaterialHoldList\(held, kept\)/);
  assert.match(email, /expectedMaterialSignatures/);
  assert.match(service, /material_review_changed/);
  assert.match(handler, /op === "preview_materials"/);
});

test("held recipients are saved as a separate, address-preserving working list", async () => {
  const start = email.indexOf("  function materialReviewKey(");
  const end = email.indexOf("  function setupView()", start);
  const saved = [];
  const ctx = vm.createContext({
    materialReviewSnapshot: "", materialHoldListId: "", sourceList: { name: "Edward Jones leaders" },
    global: { crypto: { randomUUID: () => "test-id" }, Dial: { loadLists: async () => {} } },
    fetch: async (_url, options) => {
      const body = JSON.parse(options.body);
      saved.push(body);
      return { ok: true, json: async () => ({ items: body.items, dropped: 0 }) };
    },
    Date, JSON, Map, Set, Error,
  });
  vm.runInContext(email.slice(start, end), ctx);
  const result = await vm.runInContext(`saveMaterialHoldList(
    [{ contactId: "42", name: "Advisor", email: "advisor@example.com" }],
    [{ contactId: "42", name: "Advisor", email: "advisor@example.com", firm: "Edward Jones" }])`, ctx);
  assert.equal(result, "Awaiting material - Edward Jones leaders");
  assert.equal(saved[0].id, "material-hold-test-id");
  assert.deepEqual(saved[0].items, [{ crd: "42", name: "Advisor",
    firm: "Edward Jones", email: "advisor@example.com" }]);
});

test("the server freezes only the rep's selected material, not the template suggestion", () => {
  assert.match(service, /const selected = materials\.templateRequirements\(\{/);
  assert.match(service, /requiredDocumentIds: \(input\.attachmentIds \|\| \[\]\)\.map\(String\)/);
  assert.doesNotMatch(service, /\.\.\.templateRequired\.familyIds/);
  assert.match(service, /materials\.resolveFamilies\(allDocuments, materialFamilyIds, recipient\.email, routePolicy,[\s\S]*strategies: recipient\.materialStrategies/);
  assert.match(store, /requiredMaterialFamilyIdsJson/);
  assert.match(store, /normalizedRequirements = materials\.templateRequirements\(input, await listDocuments\(\)\)/);
  assert.match(store, /templatesRequiringDocument\(await listTemplates\(\), docId\)/);
  assert.match(store, /document_required_by_template/);
});

test("the composer warns about unavailable suggestions without blocking generation", () => {
  assert.match(email, /id="emailRequirementWarning"/);
  assert.match(email, /suggestions\.missingDocumentIds/);
  assert.match(email, /emailCreateButton/);
  assert.doesNotMatch(email, /missing\.length > 0/);
});

test("materials administration exposes audience, strategy and scrollable PDF preview controls", () => {
  assert.match(email, /<label>Approved for<select data-upload-field="audience">/);
  assert.match(email, /Family name<input data-upload-field="familyName"/);
  assert.match(email, /Family ID<input data-upload-field="familyId"/);
  assert.match(email, /Family name<input data-material-field="familyName"/);
  assert.match(email, /familyName: row\.familyName/);
  assert.match(email, /familyName: value\("familyName"\)/);
  assert.match(email, /name: d\.familyName \|\| d\.name/);
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
  assert.match(store, /familyName: e\.familyName \|\| ""/);
  assert.match(store, /familyName: meta\.familyName/);
  assert.match(store, /propagateFamilyName/);
  assert.match(store, /audience: e\.materialAudience \|\| "advisor_only"/);
  assert.match(store, /strategy: e\.materialStrategy \|\| "general"/);
  assert.match(store, /materialAudience: meta\.audience, materialStrategy: meta\.strategy/);
  assert.match(store, /materialSlotKey\(doc\) === slot/);
  assert.match(store, /material_client_preferred/);
});
