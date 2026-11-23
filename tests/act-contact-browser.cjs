// Local Chromium test with a mocked ACT endpoint. It never contacts ACT!.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const web = path.join(__dirname, "../webapp");
const primary = { line1: "1285 Avenue of the Americas", city: "New York", state: "NY", postalCode: "10019" };
const alternate = { line1: "401 West 4th Street", line2: "Suite 3100", city: "Austin", state: "TX", postalCode: "78701" };
const linked = { status: "linked", primary, revision: "address-version",
  candidates: [{ id: "ubs-austin", source: "UBS", address: alternate, url: "https://www.ubs.com/" }] };
const unlinked = { status: "unlinked", revision: "creation-version",
  draft: { fullName: "Jane Advisor", company: "Firm", emailAddress: "jane@example.test", department: "Advisory team",
    businessAddress: primary, customFields: { crd: "5551234", email__y_n: "2" } } };
const origin = "https://act-fixture.invalid";

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE });
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 850 } });
      const writes = [], errors = [];
      let accept = false;
      page.setDefaultTimeout(10000);
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("dialog", (dialog) => accept ? dialog.accept() : dialog.dismiss());
      await page.route("**/*", async (route) => {
        const req = route.request(), url = new URL(req.url());
        assert.equal(url.origin, origin);
        if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body:
          '<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/email.css"><body><main></main><div id="actTestSearch" hidden></div><script src="/act-addresses.js"></script></body>' });
        if (url.pathname === "/act-icon.png")
          return route.fulfill({ contentType: "image/png", body: fs.readFileSync(path.join(web, "act-icon.png")) });
        if (["/email.css", "/style.css", "/act-addresses.js"].includes(url.pathname))
          return route.fulfill({ contentType: url.pathname.endsWith(".js") ? "application/javascript" : "text/css",
            body: fs.readFileSync(path.join(web, url.pathname.slice(1))) });
        assert.equal(url.pathname, "/api/contact-address");
        if (req.method() === "POST") {
          const data = req.postDataJSON();
          writes.push(data);
          return route.fulfill({ json: data.action === "replace_primary"
            ? { status: "linked", verified: true, primary: alternate, candidates: [] }
            : { status: "added", verified: true } });
        }
        if (url.searchParams.has("search")) return route.fulfill({ json: { count: 1,
          contacts: [{ name: "ACT Only", email: "act@example.test", company: "Firm",
            contactKey:"act-00000000000000000000000001", phone:"+14045550101",
            emailApproved:true, greeting:"Avery", firm:"Example Advisors", firmCrd:"123456",
            primary, coordinates: [40.76, -73.98] }] } });
        return route.fulfill({ json: url.searchParams.get("crd") === "2066775" ? linked : unlinked });
      });
      await page.goto(origin);
      await page.evaluate(() => {
        document.querySelector("main").innerHTML = ActAddresses.badgeSlot("2066775") + ActAddresses.slot("2066775")
          + '<section id="test-location"><p class="remote-note">Working office from ACT!.</p>'
          + ActAddresses.officeSlot("2066775") + "</section>"
          + ActAddresses.badgeSlot("5551234") + ActAddresses.slot("5551234") + ActAddresses.officeSlot("5551234");
        ActAddresses.setLocator(contact => { window.locatedContact = contact.name; },
          crd => { window.firmOpened = crd; });
        window.Dial = { state:{ ready:true, contactRouteVersion:"test-release" }, emailTierKey:() => "test-policy",
          telHref:(_key, phone) => "tel:" + phone,
          loadLists:async () => [{id:"current", name:"Call list"}],
          addToList:async (id, row) => { window.addedTo = [id, row]; return { added:true }; } };
        window.EmailComposer = { open:async recipients => { window.emailOpened = recipients; } };
      });
      const location = page.locator("#test-location");
      await location.getByText("UBS lists Austin, TX").waitFor();
      const workingOffice = await location.getByText("Working office from ACT!.", { exact: true }).boundingBox();
      const discrepancy = await location.locator(".act-office-note").boundingBox();
      assert.ok(discrepancy.y >= workingOffice.y + workingOffice.height);
      await page.locator(".act-badge.linked").waitFor();
      await page.locator(".act-badge.unlinked").waitFor();
      assert.equal(await page.getByRole("link", { name: /Source profile/ }).getAttribute("href"), "https://www.ubs.com/");
      assert.equal(await page.getByText("1285 Avenue of the Americas", { exact: false }).count(), 0);
      const replace = location.getByRole("button", { name: "Replace ACT! primary address with Austin, TX" });
      await replace.click();
      const addressReview = page.getByRole("dialog", { name: "Replace ACT! primary address" });
      await addressReview.getByText("1285 Avenue of the Americas", { exact: false }).waitFor();
      await addressReview.getByText("401 West 4th Street", { exact: false }).waitFor();
      assert.equal(writes.length, 0);
      await page.getByRole("button", { name: "Continue" }).click();
      assert.equal(writes.length, 0);
      accept = true;
      await page.getByRole("button", { name: "Continue" }).click();
      await location.getByText("Primary address saved in ACT!.", { exact: false }).waitFor();
      assert.equal(await location.locator(".act-office-note").count(), 0);
      assert.deepEqual(writes[0], { crd: "2066775", action: "replace_primary", candidateId: "ubs-austin",
        revision: "address-version", confirmed: true });

      accept = false;
      await page.getByRole("button", { name: "Add contact to ACT!" }).click();
      const review = page.getByRole("dialog", { name: "Add contact to ACT!" });
      await review.getByText("Jane Advisor").waitFor();
      await review.getByText("Email only", { exact: true }).waitFor();
      await review.getByText("Advisory team", { exact: true }).waitFor();
      assert.equal(writes.length, 1);
      await review.getByRole("button", { name: "Add contact", exact: true }).click();
      await page.getByRole("img", { name: "Added to ACT!" }).waitFor();
      assert.deepEqual(writes[1], { crd: "5551234", action: "add_contact", revision: "creation-version", confirmed: true });

      await page.evaluate(() => ActAddresses.searchInto(document.getElementById("actTestSearch"), "ACT Only"));
      await page.locator("#actTestSearch").getByRole("button", { name: /ACT Only/ }).click();
      const contactDialog = page.getByRole("dialog", { name: "ACT Only" });
      await contactDialog.getByText("No matching CRD").waitFor();
      const call = contactDialog.getByRole("link", { name:"Call" });
      assert.equal(await call.getAttribute("href"), "tel:+14045550101");
      await contactDialog.getByRole("button", { name:"Add", exact:true }).click();
      await contactDialog.getByText("Added to the list.").waitFor();
      const added = await page.evaluate(() => window.addedTo);
      assert.equal(added[0], "current");
      assert.equal(added[1].emailConfirmed, true);
      await contactDialog.getByRole("button", { name:"Email", exact:true }).click();
      const opened = await page.evaluate(() => window.emailOpened);
      assert.equal(opened[0].crd, added[1].crd);
      assert.equal(opened[0].firstName, "Avery");
      await page.locator("#actTestSearch").getByRole("button", { name:/ACT Only/ }).click();
      await contactDialog.getByRole("button", { name:"Example Advisors" }).click();
      assert.equal(await page.evaluate(() => window.firmOpened), "123456");
      await page.locator("#actTestSearch").getByRole("button", { name:/ACT Only/ }).click();
      await contactDialog.getByRole("button", { name: "Show on map" }).click();
      assert.equal(await page.evaluate(() => window.locatedContact), "ACT Only");
      const scrolls = await page.evaluate(() => {
        document.body.insertAdjacentHTML("beforeend", '<div class="ask-back"><div class="ask ask-scroll freshness-view"><div style="height:2400px"></div></div></div>');
        const panel = document.querySelector(".freshness-view");
        panel.scrollTop = panel.scrollHeight;
        return panel.getBoundingClientRect().height <= innerHeight && panel.scrollTop > 0;
      });
      assert.equal(scrolls, true);
      assert.deepEqual(errors, []);
      console.log("PASS " + width + "px ACT primary, both confirmations, verified creation and ACT-only contact search");
      await page.close();
    }
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
