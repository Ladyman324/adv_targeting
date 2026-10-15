"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const signature = require("../shared/email-signature");
const core = require("../shared/email-core");
const seed = require("../shared/signature-photos/seed.json").photos;
const [userId, photo] = Object.entries(seed)[0];
const who = { id: userId };
const profile = { displayName: photo.name, mail: photo.emails[0], jobTitle: "Sales Director", businessPhones: ["404-239-0111"] };
const content = fs.readFileSync(path.join(__dirname, "../shared/signature-photos", photo.sha256 + ".jpg"));
const hash = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");

test("crop retains original metadata across repeated edits without changing opt-in", async () => {
  let saved = null;
  const st = {
    getSignaturePhoto: async () => saved,
    putSignaturePhoto: async (_owner, value) => { saved = value; },
    signatureImageContainer: async () => ({ getBlockBlobClient: () => ({ uploadData: async () => {} }) }),
  };
  const crop = { zoom: 2, x: 10, y: -15 };
  await signature.upload(who, content.toString("base64"), { store: st }, photo.sha256, crop);
  assert.equal(saved.original.sha256, photo.sha256);
  assert.deepEqual(saved.crop, crop);
  assert.equal(saved.signaturePhotoEnabled, undefined);
  await signature.upload(who, content.toString("base64"), { store: st }, photo.sha256, { zoom: 1, x: 0, y: 0 });
  assert.equal(saved.original.sha256, photo.sha256);
  assert.equal(saved.original.original, undefined);
  await signature.upload(who, content.toString("base64"), { store: st });
  assert.equal(saved.original, undefined, "replacement starts a new original");
});

test("crop rejects stale or foreign originals and invalid framing before any storage write", async () => {
  let writes = 0;
  const st = { getSignaturePhoto: async () => null,
    signatureImageContainer: async () => { writes++; throw new Error("must not write"); } };
  await assert.rejects(() => signature.upload(who, content.toString("base64"), { store: st }, "a".repeat(64),
    { zoom: 1, x: 0, y: 0 }), error => error.statusCode === 409);
  for (const crop of [null, {}, { zoom: 4, x: 0, y: 0 }, { zoom: 2, x: Infinity, y: 0 },
    { zoom: 2, x: 0, y: 100001 }, { zoom: "2", x: 0, y: 0 }]) {
    await assert.rejects(() => signature.upload(who, content.toString("base64"), { store: st }, photo.sha256, crop),
      error => error.statusCode === 400);
  }
  assert.equal(writes, 0);
});

test("website inventory contains compact, integrity-checked photos bound to specific accounts", () => {
  assert.ok(Object.keys(seed).length >= 26);
  let size = 0;
  for (const [id, image] of Object.entries(seed)) {
    const bytes = fs.readFileSync(path.join(__dirname, "../shared/signature-photos", image.sha256 + ".jpg"));
    assert.equal(hash(bytes), image.sha256);
    assert.equal(bytes.length, image.size);
    assert.equal(signature.validatePhoto(bytes).contentType, "image/jpeg");
    assert.ok(image.emails.every(email => email.endsWith("@eicatlanta.com")));
    assert.match(signature.descriptor({ id }, image).blobName, new RegExp(`^signatures/${id}/`));
    size += bytes.length;
  }
  assert.ok(size < 1024 * 1024);
});

test("photos default off without a photo lookup and retain the existing text signature", async () => {
  const result = await signature.bundle(who, profile, { prefs: {},
    store: { getSignaturePhoto: async () => { throw new Error("Photo lookup must not run"); } } });
  assert.equal(result.signatureHtml, core.corporateSignature(profile));
  assert.deepEqual(result.images, []);
});

test("opt-in snapshots the photo to an immutable private blob while contact text remains clickable", async () => {
  const saved = [];
  const result = await signature.bundle(who, profile, { prefs: { signaturePhotoEnabled: "1" },
    store: { getSignaturePhoto: async () => null, signatureImageContainer: async () => ({
      getBlockBlobClient: (name) => ({ uploadData: async (bytes, options) => saved.push({ name, bytes, options }) }),
    }) } });
  assert.equal(saved.length, 1);
  assert.equal(hash(saved[0].bytes), photo.sha256);
  assert.equal(saved[0].options.conditions.ifNoneMatch, "*");
  assert.match(result.signatureHtml, /<table/);
  const dimensions = signature.validatePhoto(content);
  assert.equal(result.photo.width, dimensions.width);
  assert.equal(result.photo.height, dimensions.height);
  assert.match(result.signatureHtml, /height="\d+"/);
  assert.match(result.signatureHtml, /vertical-align:top/);
  assert.match(result.signatureHtml, new RegExp(`href="mailto:${profile.mail}"`));
  assert.match(result.signatureHtml, new RegExp(`src="cid:${result.photo.cid}"`));
  assert.match(result.signatureHtml, /EIC e-mails are archived/);
  assert.equal(result.images[0].kind, "signature-photo");
});

test("a requested older signature photo resolves only within the signed-in owner's blob prefix", async () => {
  const other = { id: "11111111-2222-3333-4444-555555555555" };
  let key = "";
  await assert.rejects(() => signature.bytes(other, photo.sha256, { store: {
    signatureImageContainer: async () => ({ getBlobClient: (name) => ({ downloadToBuffer: async () => {
      key = name; throw Object.assign(new Error("missing"), { statusCode: 404 });
    } }) }),
  } }), /missing/);
  assert.equal(key, `signatures/${other.id}/${photo.sha256}`);
  await assert.rejects(() => signature.bytes(other, "../anything"), /Invalid signature photo/);
});

test("custom uploads keep original blobs and bind metadata to the signed-in account without opting in", async () => {
  let metadata, key;
  await signature.upload(who, content.toString("base64"), { store: {
    putSignaturePhoto: async (owner, image) => { metadata = { owner, image }; },
    signatureImageContainer: async () => ({ getBlockBlobClient: (name) => ({ uploadData: async (bytes, options) => {
      key = name; assert.equal(hash(bytes), photo.sha256); assert.equal(options.conditions.ifNoneMatch, "*");
    } }) }),
  } });
  assert.equal(metadata.owner, who);
  assert.equal(metadata.image.sha256, photo.sha256);
  assert.equal(key, `signatures/${who.id}/${photo.sha256}`);
  assert.equal(metadata.image.signaturePhotoEnabled, undefined);
});

test("invalid, executable, oversized and dimension-bomb photo uploads are refused before storage", async () => {
  const png = Buffer.alloc(100);
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).copy(png);
  png.write("IHDR", 12); png.writeUInt32BE(100000, 16); png.writeUInt32BE(100000, 20);
  for (const bytes of [Buffer.from('<svg onload="alert(1)">'), Buffer.from("GIF89a"), png,
    Buffer.alloc(signature.MAX_PHOTO_BYTES + 1), Buffer.from([255, 216, 255, 0])]) {
    await assert.rejects(() => signature.upload(who, bytes.toString("base64"), { store: {} }),
      error => error.statusCode === 400);
  }
});

test("a replaced photo does not change a previously generated signature or its immutable reference", async () => {
  const dimensions = signature.validatePhoto(content);
  let selected = { ...photo, ...dimensions };
  const store = { getSignaturePhoto: async () => selected };
  const original = await signature.bundle(who, profile, { prefs: { signaturePhotoEnabled: "1" }, store });
  selected = { ...photo, ...dimensions, sha256: "a".repeat(64) };
  const next = await signature.bundle(who, profile, { prefs: { signaturePhotoEnabled: "1" }, store });
  assert.notEqual(original.photo.sha256, next.photo.sha256);
  assert.match(original.signatureHtml, new RegExp(photo.sha256));
  assert.equal(original.images[0].sha256, photo.sha256);
});

test("photo matches each sender's contact lines while retaining the selected aspect ratio", () => {
  const keys = ["EMAIL_SIGNATURE_ADDRESS", "EMAIL_SIGNATURE_COMPANY_NAME", "EMAIL_SIGNATURE_WEBSITE", "EMAIL_SIGNATURE_DISCLOSURE"];
  const before = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  try {
    process.env.EMAIL_SIGNATURE_ADDRESS = "Suite 600\\nAtlanta, GA";
    process.env.EMAIL_SIGNATURE_COMPANY_NAME = "EIC";
    process.env.EMAIL_SIGNATURE_WEBSITE = "https://www.eicatlanta.com";
    process.env.EMAIL_SIGNATURE_DISCLOSURE = "Disclosure\\nMore disclosure";
    for (const [person, lines] of [[profile, 8], [{ ...profile, jobTitle: "", businessPhones: [] }, 6], [{ displayName: "Rep" }, 5]]) {
      for (const dimensions of [{ width: 256, height: 384 }, { width: 264, height: 336 }, { width: 320, height: 320 }]) {
        const image = { ...signature.descriptor(who, photo), ...dimensions };
        const html = core.corporateSignature(person, "", core.config(), image);
        const height = lines * 16, width = Math.round(height * dimensions.width / dimensions.height);
        assert.match(html, new RegExp(`width="${width}" height="${height}"`));
        assert.match(html, new RegExp(`width:${width}px;height:${height}px;display:block`));
        assert.match(html, /padding:0;font-family:Arial,sans-serif;font-size:10pt;color:#333;line-height:16px;mso-line-height-rule:exactly/);
        assert.match(html, /Disclosure/, "disclosures remain outside the contact-height calculation");
      }
    }
    process.env.EMAIL_SIGNATURE_ADDRESS = "Suite 600\nAtlanta, GA";
    assert.match(core.corporateSignature(profile, "", core.config(), { ...signature.descriptor(who, photo), width: 264, height: 336 }), /height="128"/);
    process.env.EMAIL_SIGNATURE_ADDRESS = "Atlanta <br> GA";
    const escaped = core.corporateSignature(profile, "", core.config(), { ...signature.descriptor(who, photo), width: 264, height: 336 });
    assert.match(escaped, /height="112"/);
    assert.match(escaped, /Atlanta &lt;br&gt; GA/);
  } finally {
    for (const key of keys) { if (before[key] === undefined) delete process.env[key]; else process.env[key] = before[key]; }
  }
});

test("seed and uploaded dimensions survive selection and generated messages freeze their layout", async () => {
  const dimensions = signature.validatePhoto(content);
  const selected = await signature.selected(who, { store: { getSignaturePhoto: async () => null } });
  assert.equal(selected.width, dimensions.width);
  assert.equal(selected.height, dimensions.height);
  const uploaded = { ...photo, width: 264, height: 336 };
  const st = { getSignaturePhoto: async () => uploaded };
  const before = await signature.bundle(who, profile, { prefs: { signaturePhotoEnabled: "1" }, store: st });
  uploaded.width = 320; uploaded.height = 320;
  const after = await signature.bundle(who, { ...profile, jobTitle: "" }, { prefs: { signaturePhotoEnabled: "1" }, store: st });
  assert.notEqual(before.signatureHtml, after.signatureHtml);
  assert.equal(before.images[0].width, 264);
  assert.equal(before.images[0].height, 336);
});
