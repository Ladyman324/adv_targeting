// Local browser checks only. Requests are mocked; no ACT! writes or email sends.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), core = require("../api/shared/email-core");
const photos = require("../api/shared/signature-photos/seed.json").photos;
const photo = Object.values(photos)[0];
const photoBytes = fs.readFileSync(path.join(root, "api/shared/signature-photos", photo.sha256 + ".jpg"));
const image = { ...photo, kind:"signature-photo", cid: `sender-photo-${photo.sha256}@eicadvisormap` };
const profile = { displayName: "Example Rep", mail: "rep@example.test", jobTitle: "Director" };
(async () => {
  // Downloads can bypass Playwright request routing; serve only the fixture
  // image on loopback so a real download can complete without external access.
  const server = require("node:http").createServer((req, res) => {
    if (!req.url.startsWith("/api/email?op=signature_photo&")) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "Content-Type":"image/jpeg" }); res.end(photoBytes);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless:true, executablePath:process.env.CHROME_EXECUTABLE });
  try {
    for (const viewport of [{ width:1440, height:1000 }, { width:390, height:844 }]) {
      let designation = "", enabled = true;
      const requests = [], errors = [];
      const context = await browser.newContext({ viewport, acceptDownloads:true });
      const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
      await page.route("**/*", async route => {
        const req = route.request(), url = new URL(req.url());
        assert.equal(url.origin, origin, "no external requests");
        if (url.pathname === "/") return route.fulfill({ contentType:"text/html", body:'<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/email.css"></head><body><div class="ask lists ask-scroll" style="margin:8px;max-width:650px"><div id="signature"></div></div><script src="/email.js"></script></body></html>' });
        if (["/style.css", "/email.css", "/email.js"].includes(url.pathname)) return route.fulfill({ contentType:url.pathname.endsWith(".js") ? "text/javascript" : "text/css", body:fs.readFileSync(path.join(root,"webapp",url.pathname.slice(1))) });
        if (url.pathname === "/api/settings") {
          assert.equal(req.method(), "PUT");
          const patch = req.postDataJSON(); requests.push(patch);
          if ("signatureDesignation" in patch) designation = patch.signatureDesignation;
          if ("signaturePhotoEnabled" in patch) enabled = patch.signaturePhotoEnabled === "1";
          return route.fulfill({ json:{ settings:{ signatureDesignation:designation, signaturePhotoEnabled:enabled ? "1" : "0" } } });
        }
        assert.equal(url.pathname, "/api/email"); assert.equal(req.method(), "GET");
        if (url.searchParams.get("op") === "signature_photo") return route.fulfill({ contentType:"image/jpeg", body:photoBytes });
        assert.equal(url.searchParams.get("op"), "signature_settings");
        return route.fulfill({ json:{ connected:true, enabled, designation, photoAvailable:true, websitePhotoAvailable:true,
          photoUrl:`/api/email?op=signature_photo&sha=${photo.sha256}`, photoDownloadName:"signature-photo.jpg",
          previewHtml:core.corporateSignature(profile,"",core.config(),enabled ? image : null,designation), images:enabled ? [image] : [] } });
      });
      await page.goto(origin + "/");
      await page.evaluate(async () => {
        window.Dial = { saveSettings:async patch => (await fetch("/api/settings", { method:"PUT", headers:{ "Content-Type":"application/json" }, body:JSON.stringify(patch) })).json() };
        await EmailComposer.mountSignatureSettings(document.getElementById("signature"));
      });
      await page.locator("[data-signature-designation]").fill("CIMA\u00ae");
      await page.locator("[data-signature-save-designation]").click();
      await page.locator(".signature-photo-status").filter({ hasText:"Saved." }).waitFor();
      assert.equal(designation,"CIMA\u00ae");
      await page.locator(".signature-preview summary").click();
      await page.getByText("Example Rep, CIMA\u00ae", { exact:true }).waitFor();
      await page.locator(".signature-outlook-help summary").click();
      assert.match(await page.locator(".signature-outlook-help").innerText(), /insert the saved file/);
      const aligned = await page.locator(".signature-photo-controls").evaluate((controls) => {
        const boxes = [...controls.querySelectorAll(".set-btn")].map((control) => {
          const rect = control.getBoundingClientRect();
          const text = document.createRange();
          text.selectNodeContents(control);
          const label = text.getBoundingClientRect();
          return { height: rect.height, centered: Math.abs((rect.top + rect.bottom) - (label.top + label.bottom)) < 3 };
        });
        return boxes.every((box) => box.centered && box.height === boxes[0].height);
      });
      assert.equal(aligned, true, "photo actions use the same height and vertical label alignment");
      const pending = page.waitForEvent("download");
      await page.getByRole("link",{ name:"Download photo for Outlook" }).click();
      const download = await pending;
      assert.equal(download.suggestedFilename(),"signature-photo.jpg");
      assert.deepEqual(fs.readFileSync(await download.path()),photoBytes);
      await page.locator("[data-signature-designation]").fill("");
      await page.locator("[data-signature-save-designation]").click();
      await page.locator(".signature-photo-status").filter({ hasText:"Saved." }).waitFor();
      assert.equal(designation, "");
      assert.ok(requests.every(patch => Object.keys(patch).every(key => key === "signatureDesignation")));
      const app = fs.readFileSync(path.join(root,"webapp/app.js"),"utf8").replace(/\r\n/g,"\n");
      const rowStart = app.indexOf("function listRowActions(l){");
      const rowSource = app.slice(rowStart,app.indexOf("\n}",rowStart)+2);
      const markup = new Function("Dial","esc","listWorkspace",rowSource + "\nreturn listRowActions({id:'rotation',name:'Clients and top prospects',count:25});")(
        { state:{ listId:"other" } },value => String(value),null);
      await page.evaluate(markup => {
        const panel = document.createElement("div"); panel.className = "ask lists lists-workspace";
        panel.style.cssText = "margin:8px;max-width:calc(100vw - 16px);box-sizing:border-box";
        panel.innerHTML = '<ul class="lists-ul">' + markup + '</ul>'; document.body.appendChild(panel);
      },markup);
      assert.equal(await page.getByRole("button", { name:"Filter Map", exact:true }).count(), 1);
      const exportButton = page.getByRole("button",{name:"Export CSV",exact:true});
      await exportButton.scrollIntoViewIfNeeded();
      const rect = await exportButton.boundingBox();
      assert.ok(rect.x >= 0 && rect.x + rect.width <= viewport.width,"list export is reachable without horizontal scrolling");
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow,false,"settings fit the viewport");
      assert.deepEqual(errors,[]);
      console.log(`PASS signature settings, saved/cleared designation, Outlook help and actual photo download ${viewport.width}x${viewport.height}`);
      await context.close();
    }
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
