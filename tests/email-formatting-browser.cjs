// Local browser checks only. All API calls are mocked; never sends mail.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const core = require("../api/shared/email-core");
const root = path.resolve(__dirname, "..");
const clone = value => JSON.parse(JSON.stringify(value));
const batchId = "11111111-1111-4111-8111-111111111111";
const recipient = { firstName: "Dana", lastName: "Whitfield", firm: "UBS", nameFallback: false };
async function select(page, id, phrase) {
  await page.locator("#" + id + "Editor").scrollIntoViewIfNeeded();
  await page.locator('#' + id + 'Editor').evaluate((editor, phrase) => {
    editor.focus();
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const start = node.textContent.indexOf(phrase);
      if (start < 0) continue;
      const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + phrase.length);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      editor.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })); return;
    }
    throw new Error("Phrase missing: " + phrase);
  }, phrase);
}
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE });
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage(), errors = [], requests = [];
      page.on("pageerror", e => errors.push(e.message));
      let template = { id: "formatting", name: "Formatting", subject: "Example", bodyText: "Hi {{first_name}},\n\nThe common phrase stays here.",
        bodyRichHtml: "", images: [], requiredDocumentIds: [], requiredMaterialFamilyIds: [], published: true, version: 1 };
      let data = { batch: { id: batchId, name: "Formatting check", templateId: template.id, status: "editing", mode: "", commonSubject: "Example",
        commonBodyText: template.bodyText, commonBodyRichHtml: "", attachmentIds: [], recipientCount: 1, commonRevision: 1, graphMailbox: "rep@example.test" },
        messages: [{ id: "m1", recipientName: "Dana Whitfield", recipientEmail: "dana@example.test", greetingName: "Dana", state: "editing", subject: "Example",
          bodyText: "Hello Dana. The selected phrase stays here. Plain ending.", bodyHtml: "<p>Hello Dana. The selected phrase stays here. Plain ending.</p>",
          bodyRichHtml: "", attachments: [], validation: { errors: [], warnings: [] }, reviewed: true }], approvedText: template, templateImages: [] };
      await page.route("**/*", async route => {
        const url = new URL(route.request().url());
        assert.equal(url.origin, "https://email-test.invalid");
        if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/' + (viewport.width < 500 ? 'field.css' : 'style.css') + '"><link rel="stylesheet" href="/email.css"></head><body><script src="/email.js"></script></body></html>' });
        if (["/style.css", "/field.css", "/email.css", "/email.js"].includes(url.pathname)) return route.fulfill({ contentType: url.pathname.endsWith(".js") ? "text/javascript" : "text/css", body: fs.readFileSync(path.join(root, "webapp", url.pathname.slice(1)), "utf8") });
        assert.equal(url.pathname, "/api/email");
        const op = url.searchParams.get("op"), body = route.request().postDataJSON() || {};
        requests.push({ op, body });
        if (op === "catalog") return route.fulfill({ json: { templates: [template], documents: [], isAdmin: true, config: {}, limits: {}, internalRecipients: [],
          policy: { directSendAvailable: false }, connection: { connected: true, profile: { displayName: "Example Sender", jobTitle: "Director" } } } });
        if (op === "lint_template") return route.fulfill({ json: { ok: true, errors: [], warnings: [] } });
        if (op === "capacity_plan") return route.fulfill({ json: { deliveryPlan: { fit: true, hash: "mock", days: [], externalUnits: 1 } } });
        if (op === "put_template") {
          template = { ...template, ...body, ...core.normalizeRichBody(body.bodyRichHtml), version: 2 };
          return route.fulfill({ json: { id: template.id, version: 2 } });
        }
        if (op === "update_message") {
          const m = data.messages[0];
          if (body.bodyText != null || body.bodyRichHtml != null) Object.assign(m, core.renderBody(body.bodyText, body.bodyRichHtml, null, null), { bodyOverridden: true });
          if (body.resetBody) Object.assign(m, core.renderBody(data.batch.commonBodyText, data.batch.commonBodyRichHtml, recipient, null), { bodyOverridden: false });
        }
        if (op === "update_common") {
          const saved = core.normalizeRichBody(body.bodyRichHtml);
          Object.assign(data.batch, { commonBodyText: saved.bodyText, commonBodyRichHtml: saved.bodyRichHtml });
          if (!data.messages[0].bodyOverridden) Object.assign(data.messages[0], core.renderBody(saved.bodyText, saved.bodyRichHtml, recipient, null));
        }
        return route.fulfill({ json: clone(data) });
      });
      await page.goto("https://email-test.invalid/");
      await page.evaluate(id => window.EmailComposer.openBatch(id), batchId);
      await page.locator("#emailOneBodyEditor").waitFor();
      const tools = page.locator("#emailOneBody").locator("..");
      await select(page, "emailOneBody", "selected phrase");
      for (const label of ["Bold", "Italic", "Underline"]) await tools.getByRole("button", { name: label, exact: true }).click();
      await tools.getByLabel("Text color", { exact: true }).evaluate(input => { input.value = "#8b0000"; input.dispatchEvent(new Event("input", { bubbles: true })); });
      const rich = await page.locator("#emailOneBody").evaluate(box => box._richHtml);
      assert.match(rich, /font-weight: bold|font-weight:bold/); assert.match(rich, /font-style: italic|font-style:italic/);
      assert.match(rich, /text-decoration: underline|text-decoration:underline/); assert.match(rich, /139, 0, 0/);
      assert.match(rich, /<\/span> stays here\. Plain ending\./);
      await page.locator("#emailOneSubject").click();
      await page.waitForFunction(() => document.querySelector(".email-rendered").innerHTML.includes("139, 0, 0"));
      assert.ok(requests.some(r => r.op === "update_message" && r.body.bodyRichHtml));
      await page.evaluate(id => window.EmailComposer.openBatch(id), batchId);
      assert.match(await page.locator("#emailOneBody").evaluate(box => box._richHtml), /139, 0, 0/);
      await select(page, "emailOneBody", "selected phrase");
      await page.locator("#emailOneBody").locator("..").getByRole("button", { name: "Clear formatting", exact: true }).click();
      assert.doesNotMatch(await page.locator("#emailOneBody").evaluate(box => box._richHtml), /font-weight|font-style|text-decoration|139, 0, 0/);
      await page.locator("#emailOneBodyEditor").press("Control+z");
      assert.match(await page.locator("#emailOneBody").evaluate(box => box._richHtml), /139, 0, 0/, "native Undo restores the selected formatting");
      await page.locator("#emailOneSubject").click();
      await page.waitForFunction(() => document.querySelector(".email-rendered").innerHTML.includes("139, 0, 0"));
      await select(page, "emailCommonBody", "common phrase");
      await page.locator("#emailCommonBody").locator("..").getByRole("button", { name: "Bold", exact: true }).click();
      assert.equal(await page.locator("#emailApply").isEnabled(), true, "format-only common changes must enable Apply");
      await page.locator("#emailApply").click();
      await page.waitForFunction(() => document.querySelector("#emailCommonBody")._richHtml.includes("font-weight"));
      assert.ok(requests.some(r => r.op === "update_common" && r.body.bodyRichHtml));
      assert.match(data.messages[0].bodyHtml, /139, 0, 0/, "common edits must preserve individual override");
      // An administrator can save formatting-only template changes, too.
      await page.evaluate(() => window.EmailComposer.openAdmin("templates"));
      await page.locator('[data-email="tpl-edit"]').first().click();
      await page.locator("#tplBodyEditor").waitFor();
      await select(page, "tplBody", "first_name");
      await page.locator("#tplBody").locator("..").getByRole("button", { name: "Bold", exact: true }).click();
      const merged = await page.locator("#tplBody").evaluate(box => box._richHtml);
      assert.match(merged, /\{\{first_name\}\}/, "a partial token selection must stay intact");
      core.normalizeRichBody(merged);
      assert.equal(await page.locator("#tplSave").isEnabled(), true);
      await page.locator("#tplSave").click();
      await page.waitForFunction(() => document.querySelector("#tplSave")?.textContent === "Saved");
      assert.ok(requests.some(r => r.op === "put_template" && r.body.bodyRichHtml));
      assert.equal(requests.filter(r => ["approve", "send", "direct_send"].includes(r.op)).length, 0);
      assert.deepEqual(errors, []);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      console.log(`PASS ${viewport.width}px: selected-text B/I/U/color, save/reopen, common formatting-only edit, override preservation, template save, whole merge token, no sends`);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
