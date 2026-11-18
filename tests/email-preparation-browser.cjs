// Uses the real composer UI with mocked recipient, attachment and draft APIs.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const web = path.join(__dirname, "../webapp");
const origin = "https://preparation-test.invalid";
const contacts = Array.from({ length: 9 }, (_, i) => ({
  crd: String(100 + i), name: "Advisor " + (i + 1),
  email: "person" + i + "@" + (i % 8) + ".example.test", firstName: "Advisor", lastName: "Test",
}));
const template = { id: "intro", name: "Introduction", subject: "Example", bodyText: "Hello",
  requiredMaterialFamilyIds: ["commentary"], requiredDocumentIds: [] };
const doc = { id: "series-june", familyId: "commentary", familyName: "Quarterly commentary",
  name: "June commentary", periodKey: "2026-Q2", channel: "generic", approved: true };

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE });
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      page.setDefaultTimeout(10000);
      const calls = [], errors = [], dialogs = [];
      let missing = 4, failSave = false;
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("dialog", async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); });
      await page.route("**/*", async (route) => {
        const url = new URL(route.request().url());
        assert.equal(url.origin, origin);
        if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body:
          '<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/email.css"><body><script src="/email.js"></script>' });
        if (["/email.js", "/email.css", "/style.css"].includes(url.pathname))
          return route.fulfill({ contentType: url.pathname.endsWith(".js") ? "application/javascript" : "text/css",
            body: fs.readFileSync(path.join(web, url.pathname.slice(1))) });
        const input = route.request().postDataJSON();
        if (url.pathname === "/api/queue") {
          assert.equal(route.request().method(), "PUT");
          calls.push({ op: "save_list", input });
          return failSave
            ? route.fulfill({ status: 503, json: { error: "Lists are unavailable." } })
            : route.fulfill({ json: { items: input.items } });
        }
        assert.equal(url.pathname, "/api/email");
        const op = url.searchParams.get("op");
        calls.push({ op, input });
        if (op === "catalog") return route.fulfill({ json: { templates: [template], documents: [doc],
          connection: { connected: true, mailbox: "rep@example.test", profile: {} }, limits: {}, policy: {}, isAdmin: true } });
        if (op === "daily_caps") return route.fulfill({ json: {
          defaultLimit: 50, maxLimit: 300, defaultIntervalSeconds: 10, maxIntervalSeconds: 300,
          reps: Array.from({ length: 16 }, (_, i) => ({ userId: String(i), mailbox: "rep" + i + "@example.test",
            limit: 50, intervalSeconds: 10 })),
        } });
        if (op === "set_daily_cap") return route.fulfill({ json: { limit: input.limit, override: input.limit } });
        if (op === "set_mailbox_interval") return input.seconds === 37
          ? route.fulfill({ status: 503, json: { error: "Pacing could not be saved." } })
          : route.fulfill({ json: { seconds: input.seconds, override: input.seconds } });
        if (op === "check_recipients")
          return route.fulfill({ json: { checkedCount: 9, eligibleCount: 9, excluded: [] } });
        if (op === "preview_materials") {
          const rows = input.recipients.map((r, i) => ({
            contactId: r.contactId, email: r.email, name: r.name, channel: "generic", signature: "material-" + i,
            documents: i < missing ? [] : [{ ...doc, periodKey: i === missing ? "2026-Q1" : "2026-Q2" }],
            unavailable: i < missing ? [{ familyId: "commentary", reason: "Commentary is unavailable for this platform." }] : [],
            older: i === missing,
          }));
          return route.fulfill({ json: { rows, unavailableCount: missing, olderCount: 1 } });
        }
        if (op === "create_batch") {
          const messages = input.recipients.map((r, i) => ({ id: String(i), recipientName: r.name,
            recipientEmail: r.email, subject: "Example", bodyText: "Hello", bodyHtml: "<p>Hello</p>",
            state: "editing", reviewed: true, attachments: [], validation: { errors: [], warnings: [] } }));
          return route.fulfill({ json: { batch: { id: "test-batch", name: "Preview", status: "editing",
            recipientCount: messages.length, externalCount: messages.length, commonSubject: "Example",
            commonBodyText: "Hello", attachmentIds: [], commonRevision: 1 }, messages, approvedText: template } });
        }
        if (op === "capacity_plan") return route.fulfill({ json: { deliveryPlan: { fit: true, days: [] } } });
        if (op === "lint_template") return route.fulfill({ json: { ok: true, errors: [], warnings: [] } });
        throw new Error("Unexpected operation " + op);
      });
      await page.goto(origin);
      const pick = async (allExcluded = false) => page.evaluate(({ contacts, allExcluded }) => {
        const selected = allExcluded ? [] : contacts;
        selected.sourceList = { id: "rotation", name: "Rotation" };
        selected.eligibilitySummary = { excluded: 1,
          excludedContacts: [{ name: "Needs Review", reasonCode: "stale" }] };
        return EmailComposer.open(selected);
      }, { contacts, allExcluded });
      const review = page.locator("#emailPreparationReview");
      const reviewButton = page.getByRole("button", { name: "Review recipients" });
      await pick();
      await reviewButton.click();
      await review.getByRole("heading", { name: "10 contacts selected" }).waitFor();
      await review.getByText("1 need contact review").click();
      await review.getByText("Needs Review", { exact: true }).waitFor();
      await review.getByText("4 waiting for approved material").click();
      await review.getByText("Commentary is unavailable for this platform.").first().waitFor();
      await review.getByRole("button", { name: "Prepare 5 emails" }).waitFor();
      await review.getByText("Ready recipients by email domain").click();
      await review.getByText("4.example.test", { exact: false }).last().waitFor();
      await review.getByLabel("Hold contacts whose approved edition is from an earlier period").click();
      await review.getByRole("button", { name: "Prepare 4 emails" }).waitFor();
      assert.equal(calls.some((item) => item.op === "save_list" || item.op === "create_batch"), false);
      // A changed server result must be shown before any list or draft is saved.
      missing = 3;
      await review.getByRole("button", { name: "Prepare 4 emails" }).click();
      await page.getByText("The results changed.", { exact: false }).waitFor();
      await review.getByRole("button", { name: "Prepare 5 emails" }).waitFor();
      assert.equal(calls.some((item) => item.op === "save_list" || item.op === "create_batch"), false);

      // A change to attachments invalidates the selection while keeping the user's choices.
      const material = page.locator(".email-family");
      await material.uncheck();
      await review.waitFor({ state: "hidden" });
      await material.check();
      missing = 4;
      await reviewButton.click();
      failSave = true;
      await review.getByRole("button", { name: "Prepare 5 emails" }).click();
      await page.getByText("Lists are unavailable.").waitFor();
      assert.equal(calls.some((item) => item.op === "create_batch"), false);
      failSave = false;
      await reviewButton.click();
      await review.getByRole("button", { name: "Prepare 5 emails" }).click();
      await page.getByText('Prepared 5 emails. Saved 4 to "Awaiting material - Rotation".').waitFor();
      const writeOps = calls.filter((item) => ["save_list", "create_batch"].includes(item.op));
      assert.deepEqual(writeOps.map((item) => item.op), ["save_list", "save_list", "create_batch"]);
      assert.equal(writeOps[0].input.id, writeOps[1].input.id);
      assert.deepEqual(writeOps[1].input.items.map((row) => row.crd), ["100", "101", "102", "103"]);
      assert.deepEqual(writeOps[2].input.recipients.map((row) => row.contactId), ["104", "105", "106", "107", "108"]);
      assert.equal(Object.keys(writeOps[2].input.expectedMaterialSignatures).length, 5);

      await pick(true);
      await review.getByText("0 ready to prepare").waitFor();
      assert.equal(await review.getByRole("button", { name: /^Prepare/ }).count(), 0);
      await page.evaluate(() => EmailComposer.openAdmin("caps"));
      const row = page.locator('[data-cap-user="12"]');
      const nextRow = page.locator('[data-cap-user="13"]');
      const getScroll = () => page.locator("#emailBody").evaluate((el) => el.scrollTop);
      await nextRow.getByLabel("Recipients per day").fill("73");
      await row.getByLabel("Recipients per day").fill("80");
      const saveLimit = row.getByRole("button", { name: "Save limit" });
      await saveLimit.scrollIntoViewIfNeeded();
      const limitScroll = await getScroll();
      assert.ok(limitScroll > 0);
      await saveLimit.click();
      await row.getByRole("status").filter({ hasText: "Daily limit saved." }).waitFor();
      assert.equal(await getScroll(), limitScroll);
      assert.equal(await nextRow.getByLabel("Recipients per day").inputValue(), "73");
      assert.equal(await saveLimit.evaluate((button) => button === document.activeElement), true);
      const gap = row.getByLabel("Minimum seconds between sends");
      const savePacing = row.getByRole("button", { name: "Save pacing" });
      await gap.fill("37");
      await savePacing.scrollIntoViewIfNeeded();
      const paceScroll = await getScroll();
      await savePacing.click();
      await row.getByRole("status").filter({ hasText: "Pacing could not be saved." }).waitFor();
      assert.equal(await getScroll(), paceScroll);
      assert.equal(await gap.inputValue(), "37");
      await gap.fill("25");
      await savePacing.click();
      await row.getByRole("status").filter({ hasText: "Minimum sending gap saved." }).waitFor();
      assert.equal(await getScroll(), paceScroll);
      assert.equal(await nextRow.getByLabel("Recipients per day").inputValue(), "73");
      assert.equal(calls.filter(({ op }) => op === "daily_caps").length, 1);
      assert.deepEqual(dialogs, []);
      assert.deepEqual(errors, []);
      console.log("PASS " + width + "px preparation review, stale results, hold-list retry and in-place sending limit saves");
      await page.close();
    }
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
