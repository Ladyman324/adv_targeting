// Local-only browser smoke test. All APIs are mocked; never contacts Azure/Graph.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const batchId = "11111111-1111-4111-8111-111111111111";
const childId = "22222222-2222-4222-8222-222222222222";
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE });
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage(), errors = [], requests = [];
      const data = { batch: { id: batchId, name: "Local safety test", status: "partial_failure", mode: "send", recipientCount: 2 },
        messages: [1, 2].map(i => ({ id: "m" + i, recipientName: "Example " + i, recipientEmail: `example${i}@example.test`,
          state: "failed", retryEligibility: { reason: "Check Outlook status first." } })) };
      let child;
      page.on("pageerror", e => errors.push(e.message));
      await page.route("**/*", async route => {
        const url = new URL(route.request().url());
        if (url.origin !== "https://email-test.invalid") throw new Error("Unexpected external request");
        if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/' + (viewport.width < 500 ? 'field.css' : 'style.css') + '"><link rel="stylesheet" href="/email.css"></head><body><script src="/email.js"></script></body></html>' });
        if (["/style.css", "/field.css", "/email.css", "/email.js"].includes(url.pathname)) return route.fulfill({
          contentType: url.pathname.endsWith("js") ? "text/javascript" : "text/css",
          body: fs.readFileSync(path.join(root, "webapp", url.pathname.slice(1)), "utf8") });
        if (url.pathname !== "/api/email") throw new Error("Unexpected path " + url.pathname);
        const op = url.searchParams.get("op"), body = route.request().postDataJSON();
        requests.push({ op, body });
        if (op === "catalog") return route.fulfill({ json: { templates: [], documents: [], config: {},
          policy: { directSendAvailable: true }, limits: {}, connection: { connected: true, profile: {} } } });
        if (op === "batch" && url.searchParams.get("id") === childId) return route.fulfill({ json: child });
        if (op === "update_message" && body.batchId === childId) {
          child.messages[0].reviewed = true; return route.fulfill({ json: child });
        }
        if (op === "prepare_retry") {
          assert.equal(body.confirmNotSentElsewhere, true);
          assert.deepEqual(body.messageIds, ["m1"]);
          child = { batch: { id: childId, name: "Retry review", status: "editing", mode: "", recipientCount: 1,
            attachmentIds: [], warningMessage: "Prepared for retry. Nothing has been sent." },
            messages: [{ id: "new1", state: "editing", recipientEmail: "example1@example.test",
              recipientName: "Example 1", subject: "Preserved subject", bodyText: "Preserved wording",
              bodyHtml: "<p>Preserved wording</p>", attachments: [], validation: { errors: [], warnings: [] } }] };
          return route.fulfill({ json: child });
        }
        if (op === "check_status") {
          const m = data.messages.find(m => m.id === body.messageId);
          Object.assign(m, { outlookCheckStatus: "draft", outlookCheckedUtc: new Date().toISOString(), retryEligibility: { phase: "draft", reason: "Original draft found; retry preparation." } });
        }
        if (op === "retry_selected") {
          assert.equal(body.confirmNotSentElsewhere, true);
          assert.deepEqual(body.messageIds, ["m1"]);
          data.messages[0].state = "draft_pending";
          data.messages[0].retryEligibility = { reason: "Active work" };
          data.retryResults = [{ messageId: "m1", result: "queued" }];
        }
        if (op === "handled_manually") {
          assert.equal(body.confirmHandledManually, true);
          const m = data.messages.find(m => m.id === body.messageId);
          Object.assign(m, { state: "canceled", handledManuallyUtc: new Date().toISOString(), retryEligibility: { reason: "Handled manually; excluded from sending." } });
        }
        return route.fulfill({ json: data });
      });
      await page.goto("https://email-test.invalid/");
      await page.evaluate(id => window.EmailComposer.openBatch(id), batchId);
      await page.getByRole("button", { name: "Check status & retry", exact: true }).click();
      await page.getByRole("button", { name: "Retry selected", exact: true }).click();
      assert.equal(requests.filter(r => r.op === "retry_selected").length, 0);
      await page.locator('input[value="m1"]').check();
      await page.getByRole("button", { name: "Check sent status", exact: true }).click();
      await page.getByRole("status").filter({ hasText: "No emails were sent" }).waitFor();
      assert.equal(requests.filter(r => r.op === "retry_selected").length, 0);
      page.once("dialog", dialog => dialog.dismiss());
      await page.getByRole("button", { name: "Retry selected", exact: true }).click();
      assert.equal(requests.filter(r => r.op === "retry_selected").length, 0);
      page.once("dialog", dialog => dialog.accept());
      await page.getByRole("button", { name: "Retry selected", exact: true }).click();
      await page.getByRole("status").filter({ hasText: "1 accepted for retry" }).waitFor();
      assert.equal(requests.filter(r => r.op === "retry_selected").length, 1);
      await page.locator('input[value="m2"]').check();
      page.once("dialog", dialog => dialog.accept());
      await page.getByRole("button", { name: "Already sent manually", exact: true }).click();
      await page.getByRole("status").filter({ hasText: "Marked messages" }).waitFor();
      assert.match(await page.locator(".email-review-table").innerText(), /Handled manually/);
      assert.deepEqual(errors, []);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      assert.equal(overflow, false, "table scroll must not overflow the page");
      await page.locator('input[value="m2"]').uncheck();
      await page.locator('input[value="m1"]').check();
      page.once("dialog", dialog => dialog.dismiss());
      await page.getByRole("button", { name: "Prepare selected for retry", exact: true }).click();
      assert.equal(requests.filter(r => r.op === "prepare_retry").length, 0);
      page.once("dialog", dialog => dialog.accept());
      await page.getByRole("button", { name: "Prepare selected for retry", exact: true }).click();
      await page.locator(".email-rendered").filter({ hasText: "Preserved wording" }).waitFor({ timeout: 10000 })
        .catch(async error => { console.error(await page.locator("body").innerText(), errors, requests.map(r => r.op)); throw error; });
      assert.equal(requests.filter(r => r.op === "prepare_retry").length, 1);
      assert.equal(requests.filter(r => r.op === "approve").length, 0);
      assert.equal(requests.filter(r => r.op === "retry_selected").length, 1, "preparation must not queue retry");
      assert.ok(requests.some(r => r.op === "capacity_plan" && r.body.batchId === childId));
      assert.deepEqual(errors, []);
      assert.equal(await page.evaluate(() => new URL(location.href).searchParams.get("emailBatch")), childId);
      console.log(`PASS ${viewport.width}px: selection, check-only, confirmation, selective retry, manual handling, prepare unapproved review, layout`);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
