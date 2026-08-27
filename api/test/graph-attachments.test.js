"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const graph = require("../shared/graph-mail");

test("attachment filenames always carry a .pdf extension", () => {
  // Outlook picks its handler from the EXTENSION, not contentType. Documents are
  // stored under an admin's free-text display name, so without this the advisor
  // gets a "how do you want to open this file?" dialog on a valid PDF.
  const f = graph.attachmentFileName;
  assert.equal(f({ name: "Q226 EIC ACV & LCV Client Commentary - 26071102" }),
                  "Q226 EIC ACV & LCV Client Commentary - 26071102.pdf");
  assert.equal(f({ name: "Fact Sheet.pdf" }), "Fact Sheet.pdf");   // no doubling
  assert.equal(f({ name: "Fact Sheet.PDF" }), "Fact Sheet.PDF");   // case tolerant
  assert.equal(f({ name: "" }), "attachment.pdf");
  assert.equal(f({}), "attachment.pdf");
});

test("an approved document is attached under the name it was uploaded with", () => {
  // The display name is a label for the picker in the app; the advisor should
  // receive the file called what it is actually called, because that is what
  // their compliance archive files it under.
  const f = graph.attachmentFileName;
  assert.equal(
    f({ name: "Q2 2026 ACV & LCV Client Commentary",
        fileName: "EIC_ACV_LCV_Q2_2026.pdf" }),
    "EIC_ACV_LCV_Q2_2026.pdf");
  // An uploaded name missing the extension still gets one: Outlook chooses its
  // handler from the extension and the approved library is PDF by construction.
  assert.equal(f({ name: "Display", fileName: "commentary_q2" }), "commentary_q2.pdf");
  // Sanitised on the same terms as a display name -- a filename is user input.
  assert.equal(f({ name: "Display", fileName: 'a/b:c*d?e"f<g>h|i.pdf' }),
                  "a-b-c-d-e-f-g-h-i.pdf");
  // Published BEFORE fileName was recorded: the display name is the only name
  // there is, and those documents must keep working exactly as before.
  assert.equal(f({ name: "Fact Sheet", fileName: "" }), "Fact Sheet.pdf");
  assert.equal(f({ name: "Fact Sheet" }), "Fact Sheet.pdf");
  // A rep's own attachment keeps its real name and is never given .pdf -- and
  // fileName must not smuggle its way past that guard.
  assert.equal(f({ name: "forecast.xlsx", keepName: true }), "forecast.xlsx");
  assert.equal(f({ name: "forecast.xlsx", fileName: "other.pdf", keepName: true }),
                  "forecast.xlsx");
});

test("filenames illegal on Windows are sanitised", () => {
  assert.equal(graph.attachmentFileName({ name: 'a/b:c*d?e"f<g>h|i' }), "a-b-c-d-e-f-g-h-i.pdf");
});

test("the inline attach still carries isInline and a contentId", () => {
  // Without both of these the image arrives as an ordinary attachment and the
  // <img src="cid:..."> in the body resolves to nothing -- a red X, which is
  // indistinguishable to the reader from the bug this file exists to prevent.
  const src = require("fs").readFileSync(require.resolve("../shared/graph-mail.js"), "utf8");
  const inline = src.split("async function attachInlineImages")[1]
    .split(String.fromCharCode(10) + "}")[0];
  assert.match(inline, /isInline:\s*true/);
  assert.match(inline, /contentId:\s*image\.cid/);
  // Idempotency keys on the same value that is sent as the name, or a retry
  // attaches a second copy beside the first.
  assert.match(inline, /existing\.has\(name\)/);
  assert.ok(/name,\s*$/m.test(inline), "the POST sends the same name it deduped on");
});

test("the inline $select asks only for base-type properties", () => {
  const src = require("fs").readFileSync(require.resolve("../shared/graph-mail.js"), "utf8");
  const inline = src.split("async function attachInlineImages")[1].split("\n}")[0];
  const selects = [...inline.matchAll(/\$select=([^`"'\s]+)/g)].map((m) => m[1]);
  assert.ok(selects.length, "the listing call should still constrain $select");
  const BASE_OK = new Set(["id", "name", "size", "isInline", "lastModifiedDateTime", "contentType"]);
  for (const sel of selects)
    for (const field of sel.split(","))
      assert.ok(BASE_OK.has(field),
        `$select=${field} is not a base attachment property; Graph will 400`);
});
