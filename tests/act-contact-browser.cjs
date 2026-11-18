// Local Chromium test with a mocked ACT endpoint. It never contacts ACT!.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const web = path.join(__dirname, "../webapp");
const primary = { line1: "1285 Avenue of the Americas", city: "New York", state: "NY", postalCode: "10019" };
const alternate = { line1: "401 West 4th Street", line2: "Suite 3100", city: "Austin", state: "TX", postalCode: "78701" };
const linked = { status: "linked", primary, revision: "address-version",
  candidates: [{ id: "ubs-austin", source: "UBS", address: alternate }] };
const unlinked = { status: "unlinked", revision: "creation-version",
  draft: { fullName: "Jane Advisor", company: "Firm", emailAddress: "jane@example.test", customFields: { crd: "5551234" } } };
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
          '<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/email.css"><body><main></main><button data-act-directory>Search ACT! contacts</button><script src="/act-addresses.js"></script></body>' });
        if (url.pathname === "/email.css" || url.pathname === "/act-addresses.js")
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
            primary, coordinates: [40.76, -73.98] }] } });
        return route.fulfill({ json: url.searchParams.get("crd") === "2066775" ? linked : unlinked });
      });
      await page.goto(origin);
      await page.evaluate(() => {
        document.querySelector("main").innerHTML = ActAddresses.slot("2066775") + ActAddresses.slot("5551234");
        ActAddresses.setLocator(contact => { window.locatedContact = contact.name; });
      });
      await page.getByText("UBS lists this advisor in Austin, TX.").waitFor();
      await page.getByText("1285 Avenue of the Americas", { exact: false }).waitFor();
      const replace = page.getByRole("button", { name: "Replace ACT! primary address with Austin, TX" });
      await replace.click();
      assert.equal(writes.length, 0);
      await page.getByRole("button", { name: "Continue" }).click();
      assert.equal(writes.length, 0);
      accept = true;
      await page.getByRole("button", { name: "Continue" }).click();
      await page.getByText("Primary address saved in ACT!.", { exact: false }).waitFor();
      assert.deepEqual(writes[0], { crd: "2066775", action: "replace_primary", candidateId: "ubs-austin",
        revision: "address-version", confirmed: true });

      await page.getByRole("button", { name: "Add to ACT!" }).click();
      await page.getByRole("dialog", { name: "Add contact to ACT!" }).getByText("Jane Advisor").waitFor();
      assert.equal(writes.length, 1);
      await page.getByRole("button", { name: "Continue" }).click();
      await page.getByText("? Added to ACT!").waitFor();
      assert.deepEqual(writes[1], { crd: "5551234", action: "add_contact", revision: "creation-version", confirmed: true });

      await page.getByRole("button", { name: "Search ACT! contacts" }).click();
      await page.getByRole("searchbox").fill("act@example.test");
      await page.getByRole("button", { name: "Search", exact: true }).click();
      await page.getByText("No matching CRD", { exact: true }).waitFor();
      await page.getByRole("button", { name: "Show on map" }).click();
      assert.equal(await page.evaluate(() => window.locatedContact), "ACT Only");
      assert.deepEqual(errors, []);
      console.log("PASS " + width + "px ACT primary, both confirmations, verified creation and ACT-only contact search");
      await page.close();
    }
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
