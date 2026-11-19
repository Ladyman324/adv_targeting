// Local Field browser regression. Every request is intercepted.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const origin = "https://field-fixture.invalid";
const web = path.join(__dirname, "../webapp");
const columns = ["crd", "name", "title", "firm", "city", "state", "phone", "phone_kind",
  "email", "tier", "source", "owner", "ranked", "ranked_year"];
const title = "Senior Vice President and Managing Director of a Very Long Advisory Practice";
const advisor = ["2612169", "Christopher (Andrew) Harbour", title, "Morgan Stanley", "Atlanta", "GA",
  "4045550101", "direct", "andrew@example.test", "confirmed", "crm", "MK", 1, 2026];
const prospect = ["1234567", "Jane Prospect", "Financial Advisor", "Firm", "Atlanta", "GA"];
const entry = { advisorCrd: advisor[0], advisorEmail: advisor[8], reason: "reply_followup",
  reasonLabel: "Follow-up needed", actions: ["follow_up", "done", "snooze"] };

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE });
  try {
    for (const width of [320, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 750 }, hasTouch: true,
        isMobile: true, colorScheme: "dark", serviceWorkers: "block" });
      page.setDefaultTimeout(8000);
      const errors = [], writes = [];
      let queue = [entry];
      page.on("pageerror", error => errors.push(error.message));
      await page.route("**/*", async route => {
        const request = route.request(), url = new URL(request.url());
        assert.equal(url.origin, origin);
        const json = data => route.fulfill({ json: data });
        if (request.method() === "POST") {
          const payload = request.postDataJSON();
          writes.push([url.pathname, payload]);
          if (url.pathname === "/api/email" && payload.op === "reply_state") queue = [];
          return json({ ok: true, added: true });
        }
        switch (url.pathname) {
          case "/.auth/me": return json({ clientPrincipal: { userDetails: "fixture@example.test" } });
          case "/api/health": return json({ configured: true, storageOk: true });
          case "/api/queue": return json({ items: [], lists: [{ id: "custom", name: "Atlanta Rotation", count: 2 }] });
          case "/api/email": return url.searchParams.get("op") === "queue_work"
            ? json({ count: queue.length, entries: queue }) : json({ reps: [] });
          case "/data/tile_index.json": return json({ columns, cells: [] });
          case "/data/act_assets.json": return json({ advisors: { [advisor[0]]: { acv: 1500000 } } });
          case "/api/contact-address": return json({ status: "unlinked", draft: {} });
          default: {
            const ext = path.extname(url.pathname);
            const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" }[ext];
            if (mime) return route.fulfill({ contentType: mime,
              body: fs.readFileSync(path.join(web, path.basename(url.pathname))) });
            return json({});
          }
        }
      });

      await page.addInitScript(() => {
        addEventListener("DOMContentLoaded", () => { TrainingVideo.maybePrompt = () => {}; });
      });
      await page.goto(origin + "/field.html");
      await page.waitForFunction(() => BOOK_STATE === "ready" && Dial.state.lists.length);
      await page.evaluate(rows => { TILE_CACHE.set("sample", rows); render(rows, "Fixture"); }, [advisor, prospect]);

      const chipBoxes = await page.locator("#chips button").evaluateAll(chips =>
        chips.map(chip => ({ top: chip.offsetTop, scroll: chip.scrollWidth, width: chip.clientWidth })));
      assert.equal(new Set(chipBoxes.map(chip => chip.top)).size, 1);
      assert.ok(chipBoxes.every(chip => chip.scroll <= chip.width));
      const assets = page.getByRole("button", { name: "EIC Assets" });
      await assets.tap();
      assert.equal(await page.locator("#list > li").count(), 1);
      await assets.tap();
      assert.equal(await page.locator("#list > li").count(), 2);

      const row = page.locator("#list > li").first();
      const name = row.locator(".nm"), dots = row.locator("[data-person-actions]");
      const call = row.locator("a.call"), email = row.locator("a.mail");
      const [rowBox, nameBox, dotsBox, callBox, emailBox] = await Promise.all(
        [row, name, dots, call, email].map(locator => locator.boundingBox()));
      assert.ok(rowBox.height <= 125, "representative contact fits a compact row");
      assert.ok(nameBox.x + nameBox.width <= dotsBox.x + 5, "name has its own space beside the menu");
      assert.equal(callBox.y, emailBox.y, "actions share the bottom row");
      for (const box of [callBox, emailBox]) assert.ok(box.width >= 44 && box.height >= 44);
      const badgeTops = await row.locator(".contact-badges .badge").evaluateAll(badges =>
        badges.map(badge => badge.getBoundingClientRect().top));
      assert.equal(badgeTops.length, 3);
      assert.ok(Math.max(...badgeTops) - Math.min(...badgeTops) <= 1);
      assert.equal(await row.locator(".acts svg").count(), 2);

      const sheet = page.locator("#sheet");
      await page.touchscreen.tap(rowBox.x + 24, callBox.y + 20);
      await sheet.waitFor({ state: "visible" });
      assert.ok(await sheet.getByText(title, { exact: true }).isVisible());
      const close = sheet.getByRole("button", { name: "Close contact" });
      const scheduler = sheet.locator('[data-flag="scheduler"]');
      const [closeBox, schedulerBox] = await Promise.all([close.boundingBox(), scheduler.boundingBox()]);
      assert.ok(closeBox.x >= schedulerBox.x + schedulerBox.width, "close and scheduler do not overlap");
      await close.tap();
      await sheet.waitFor({ state: "hidden" });
      await row.locator(".who").press("Enter");
      await sheet.waitFor({ state: "visible" });
      await close.tap();

      await dots.tap();
      const picker = page.getByRole("dialog", { name: advisor[1] });
      assert.ok(await picker.getByRole("button", { name: "Atlanta Rotation 2" }).isVisible());
      await picker.getByRole("button", { name: "Close list picker" }).tap();
      await picker.waitFor({ state: "hidden" });
      assert.equal(await dots.getAttribute("aria-expanded"), "false");
      assert.equal(writes.length, 0, "row and close taps do not trigger role, list, or contact actions");

      // Intercept the OS handoff while exercising the real call and email handlers.
      await page.evaluate(() => {
        document.addEventListener("click", event => {
          if (event.target.closest('a[href^="tel:"]')) event.preventDefault();
        }, true);
        AdvisorEmailData.recipientFor = async crd => { window.testEmailCrd = crd; return null; };
      });
      await call.tap();
      await sheet.locator(".log-out[open]").waitFor();
      await close.tap();
      await email.tap();
      await page.waitForFunction(crd => window.testEmailCrd === crd, advisor[0]);
      assert.equal(await sheet.isVisible(), false);
      assert.equal(writes.shift()?.[0], "/api/log");

      await page.locator("#workBtn").tap();
      const work = page.getByRole("dialog", { name: "Needs attention" });
      await work.getByText("Mark done", { exact: true }).tap();
      await work.getByText(/Nothing waiting/).waitFor();
      assert.deepEqual(writes, [["/api/email", { op: "reply_state", crd: advisor[0], state: "done" }]]);
      assert.deepEqual(errors, []);
      console.log("Field layout and queue actions verified at " + width
        + " px (sample row height " + Math.round(rowBox.height) + " px)");
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
