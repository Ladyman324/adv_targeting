"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const appStore = require("./store");
const store = require("./email-store");
const core = require("./email-core");
const seed = require("./signature-photos/seed.json").photos;
const persisted = new Map();
const MAX_PHOTO_BYTES = 512 * 1024;
const HASH = /^[a-f0-9]{64}$/;
const USER = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const problem = (message, statusCode = 400) => Object.assign(new Error(message), { statusCode });
const digest = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");

function blobName(who, hash) {
  if (!USER.test(String(who.id)) || !HASH.test(String(hash))) throw problem("Invalid signature photo reference.");
  return `signatures/${who.id}/${hash}`;
}

function descriptor(who, photo) {
  if (!photo || !HASH.test(String(photo.sha256))) throw problem("The signature photo reference is unavailable.", 503);
  return { kind: "signature-photo", id: `sender-photo-${photo.sha256.slice(0, 24)}`,
    name: `sender-photo-${photo.sha256.slice(0, 24)}.${photo.contentType === "image/png" ? "png" : "jpg"}`,
    cid: `sender-photo-${photo.sha256}@eicadvisormap`, blobName: blobName(who, photo.sha256),
    sha256: photo.sha256, contentType: photo.contentType, size: photo.size,
    width: photo.width, height: photo.height };
}

async function selected(who, deps = {}) {
  const custom = await (deps.store || store).getSignaturePhoto(who.id);
  const photo = custom || seed[who.id] || null;
  if (!photo) return null;
  // Website inventory predates dimension metadata. Read its small, packaged
  // JPEG (or the account's legacy upload), rather than guessing its aspect.
  const dimensions = photo.width && photo.height ? photo : validatePhoto(await bytes(who, photo.sha256, deps));
  return { ...descriptor(who, { ...photo, width: dimensions.width, height: dimensions.height }), custom: !!custom };
}

function seedBytes(photo) {
  const bytes = fs.readFileSync(path.join(__dirname, "signature-photos", `${photo.sha256}.jpg`));
  if (digest(bytes) !== photo.sha256) throw problem("The supplied signature photo failed its integrity check.", 503);
  return bytes;
}

async function ensureSeed(who, photo, deps = {}) {
  const key = photo.blobName;
  if (!persisted.has(key)) {
    const task = (async () => {
      const container = await (deps.store || store).signatureImageContainer();
      try {
        await container.getBlockBlobClient(key).uploadData(seedBytes(photo), {
          blobHTTPHeaders: { blobContentType: "image/jpeg" }, metadata: { sha256: photo.sha256 },
          conditions: { ifNoneMatch: "*" },
        });
      } catch (error) { if (![409, 412].includes(Number(error.statusCode))) throw error; }
    })();
    persisted.set(key, task);
    task.catch(() => persisted.delete(key));
  }
  await persisted.get(key);
}

async function bundle(who, profile, options = {}) {
  const cr = options.core || core;
  const cfg = options.cfg || (typeof cr.config === "function" ? cr.config() : core.config());
  const prefs = options.prefs || await (options.appStore || appStore).getSettings(who).catch(() => ({}));
  let photo = null;
  if (prefs.signaturePhotoEnabled === "1") {
    photo = await selected(who, options);
    if (!photo) throw problem("Choose a signature photo in Settings, or switch Include my photo off.", 409);
    if (!photo.custom && options.persist !== false) await ensureSeed(who, photo, options);
  }
  return { signatureHtml: cr.corporateSignature ? cr.corporateSignature(profile || {}, options.manageUrl || "", cfg, photo) : "",
    photo, images: photo ? [photo] : [] };
}

async function settings(who) {
  const [connection, prefs, photo, custom] = await Promise.all([
    require("./email-auth").status(who.id), appStore.getSettings(who), selected(who), store.getSignaturePhoto(who.id),
  ]);
  const enabled = prefs.signaturePhotoEnabled === "1";
  const profile = connection.profile;
  return { enabled, photoAvailable: !!photo, websitePhotoAvailable: !!seed[who.id],
    custom: !!(photo && photo.custom), connected: !!profile,
    photoUrl: photo ? `/api/email?op=signature_photo&sha=${photo.sha256}` : "",
    originalPhotoUrl: photo ? `/api/email?op=signature_photo&sha=${(custom && custom.original || photo).sha256}` : "",
    originalPhotoHash: photo ? (custom && custom.original || photo).sha256 : "",
    crop: custom && custom.crop || null,
    cropImages: photo ? [photo] : [],
    previewHtml: profile ? core.corporateSignature(profile, "", core.config(), enabled ? photo : null) : "",
    // The editor's output is always 264x336, which can differ from the current
    // uncropped website photo. Preview the shape that Save crop will produce.
    cropPreviewHtml: profile && photo ? core.corporateSignature(profile, "", core.config(), { ...photo, width: 264, height: 336 }) : "",
    images: enabled && photo ? [photo] : [] };
}

async function bytes(who, hash, deps = {}) {
  const key = blobName(who, hash);
  let content;
  if (seed[who.id] && seed[who.id].sha256 === hash) content = seedBytes(seed[who.id]);
  else content = await (await (deps.store || store).signatureImageContainer()).getBlobClient(key).downloadToBuffer();
  if (digest(content) !== hash) throw problem("The signature photo failed its integrity check.", 503);
  return content;
}

async function reset(who) {
  await store.resetSignaturePhoto(who);
  if (!seed[who.id]) await appStore.putSettings(who, { signaturePhotoEnabled: "0" });
}

function validatePhoto(content) {
  if (!Buffer.isBuffer(content) || content.length < 100 || content.length > MAX_PHOTO_BYTES)
    throw problem("Choose a JPEG or PNG photo smaller than 512 KB.");
  let width = 0, height = 0, contentType = "";
  if (content.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
      && content.length >= 33 && content.toString("ascii", 12, 16) === "IHDR") {
    width = content.readUInt32BE(16); height = content.readUInt32BE(20); contentType = "image/png";
  } else if (content[0] === 255 && content[1] === 216 && content[2] === 255) {
    for (let i = 2; i + 4 < content.length;) {
      if (content[i++] !== 255) break;
      while (content[i] === 255) i++;
      const marker = content[i++];
      if (marker === 217 || marker === 218) break;
      if (marker === 216 || marker === 1 || (marker >= 208 && marker <= 215)) continue;
      if (i + 2 > content.length) break;
      const length = content.readUInt16BE(i);
      if (length < 2 || i + length > content.length) break;
      if ([192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207].includes(marker) && length >= 8) {
        height = content.readUInt16BE(i + 3); width = content.readUInt16BE(i + 5); contentType = "image/jpeg"; break;
      }
      i += length;
    }
  }
  if (!contentType || width < 32 || height < 32 || width > 2048 || height > 2048)
    throw problem("Choose a valid JPEG or PNG photo between 32 and 2,048 pixels on each side.");
  return { contentType, width, height };
}

async function upload(who, dataBase64, deps = {}, sourceHash = "", crop = null) {
  if (typeof dataBase64 !== "string" || dataBase64.length > Math.ceil(MAX_PHOTO_BYTES / 3) * 4
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(dataBase64)) throw problem("Choose a valid JPEG or PNG photo smaller than 512 KB.");
  const content = Buffer.from(dataBase64, "base64");
  const info = validatePhoto(content), sha256 = digest(content);
  const photo = { ...info, sha256, size: content.length };
  const st = deps.store || store;
  if (sourceHash) {
    const current = await st.getSignaturePhoto(who.id);
    const original = current && (current.original || current) || seed[who.id];
    if (!original || original.sha256 !== sourceHash)
      throw problem("Your photo changed. Reopen Adjust photo and try again.", 409);
    // Keep the original available without changing immutable message images.
    photo.original = { sha256: original.sha256, contentType: original.contentType, size: original.size };
    if (!crop || ![crop.zoom, crop.x, crop.y].every(Number.isFinite)
        || crop.zoom < 1 || crop.zoom > 3 || Math.abs(crop.x) > 100000 || Math.abs(crop.y) > 100000)
      throw problem("The photo crop is invalid. Reopen Adjust photo and try again.");
    photo.crop = { zoom: crop.zoom, x: crop.x, y: crop.y };
  }
  const container = await st.signatureImageContainer();
  const key = blobName(who, sha256);
  try {
    await container.getBlockBlobClient(key).uploadData(content, {
      blobHTTPHeaders: { blobContentType: info.contentType }, metadata: { sha256 },
      conditions: { ifNoneMatch: "*" },
    });
  } catch (error) { if (![409, 412].includes(Number(error.statusCode))) throw error; }
  await st.putSignaturePhoto(who, photo);
  // Keep previous immutable blobs for generated and scheduled messages.
  return { ok: true };
}

module.exports = { bundle, settings, selected, bytes, upload, reset, validatePhoto, descriptor, MAX_PHOTO_BYTES };
