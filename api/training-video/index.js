/* Authenticated, short-lived access to the internal Advisor Map introduction.
 *
 * The Function authorizes the signed-in employee and mints read-only URLs. It
 * never proxies the large media bytes: Blob Storage handles streaming and
 * byte-range seeks directly, without tying up a Function invocation.
 */
"use strict";

const { BlobServiceClient, BlobSASPermissions } = require("@azure/storage-blob");
const store = require("../shared/store");

const DEFAULTS = Object.freeze({
  container: "training",
  videoBlob: "advisor-map-introduction-v1.mp4",
  posterBlob: "advisor-map-introduction-v1.jpg",
  captionsBlob: "advisor-map-introduction-v1.vtt",
  version: "1",
  title: "Advisor Map introduction",
  durationSeconds: 996,
  lifetimeMinutes: 60,
});

function text(value, fallback, max = 240) {
  const answer = String(value || fallback || "").trim();
  return answer.slice(0, max);
}

function safeBlobName(value, fallback) {
  const name = text(value, fallback, 512).replace(/\\/g, "/");
  if (!name || name.startsWith("/") || name.split("/").includes("..")) {
    const err = new Error("Training media configuration is invalid.");
    err.statusCode = 503;
    throw err;
  }
  return name;
}

function config(env = process.env) {
  const lifetime = Number(env.TRAINING_VIDEO_SAS_MINUTES || DEFAULTS.lifetimeMinutes);
  return {
    container: text(env.TRAINING_VIDEO_CONTAINER, DEFAULTS.container, 63),
    videoBlob: safeBlobName(env.TRAINING_VIDEO_BLOB, DEFAULTS.videoBlob),
    posterBlob: safeBlobName(env.TRAINING_VIDEO_POSTER_BLOB, DEFAULTS.posterBlob),
    captionsBlob: safeBlobName(env.TRAINING_VIDEO_CAPTIONS_BLOB, DEFAULTS.captionsBlob),
    version: text(env.TRAINING_VIDEO_VERSION, DEFAULTS.version, 64),
    title: text(env.TRAINING_VIDEO_TITLE, DEFAULTS.title, 120),
    durationSeconds: Math.max(1, Number(env.TRAINING_VIDEO_DURATION_SECONDS)
      || DEFAULTS.durationSeconds),
    lifetimeMinutes: Math.min(120, Math.max(20,
      Number.isFinite(lifetime) ? lifetime : DEFAULTS.lifetimeMinutes)),
  };
}

function makeService(env = process.env) {
  const connection = String(env.AZURE_STORAGE_CONNECTION_STRING || "").trim();
  if (!connection) {
    const err = new Error("Training video storage is not configured.");
    err.statusCode = 503;
    throw err;
  }
  return BlobServiceClient.fromConnectionString(connection);
}

async function signedAsset(container, blobName, startsOn, expiresOn, required) {
  const blob = container.getBlobClient(blobName);
  try {
    await blob.getProperties();
  } catch (err) {
    if (!required && err && err.statusCode === 404) return "";
    const wrapped = new Error(required
      ? "The Advisor Map introduction is temporarily unavailable."
      : "An optional training asset is unavailable.");
    wrapped.statusCode = required && err && err.statusCode === 404 ? 503
      : (err && err.statusCode) || 500;
    wrapped.cause = err;
    throw wrapped;
  }
  return blob.generateSasUrl({
    permissions: BlobSASPermissions.parse("r"),
    startsOn,
    expiresOn,
  });
}

function createHandler(deps = {}) {
  const identity = deps.identity || store.identity;
  const ok = deps.ok || store.ok;
  const fail = deps.fail || store.fail;
  const serviceFactory = deps.serviceFactory || makeService;
  const now = deps.now || (() => new Date());

  return async function trainingVideo(context, req) {
    try {
      identity(req); // Fail closed even if an edge route is ever misconfigured.
      const cfg = config(deps.env || process.env);
      const current = now();
      // Five minutes of clock-skew tolerance; the expiry is intentionally long
      // enough to pause during a 16-minute lesson without losing the next seek.
      const startsOn = new Date(current.getTime() - 5 * 60 * 1000);
      const expiresOn = new Date(current.getTime() + cfg.lifetimeMinutes * 60 * 1000);
      const container = serviceFactory(deps.env || process.env)
        .getContainerClient(cfg.container);

      const videoUrl = await signedAsset(
        container, cfg.videoBlob, startsOn, expiresOn, true);
      const [posterUrl, captionsUrl] = await Promise.all([
        signedAsset(container, cfg.posterBlob, startsOn, expiresOn, false)
          .catch(() => ""),
        signedAsset(container, cfg.captionsBlob, startsOn, expiresOn, false)
          .catch(() => ""),
      ]);

      return ok(context, {
        version: cfg.version,
        title: cfg.title,
        durationSeconds: cfg.durationSeconds,
        videoUrl,
        posterUrl: posterUrl || undefined,
        captionsUrl: captionsUrl || undefined,
        expiresUtc: expiresOn.toISOString(),
      });
    } catch (err) {
      return fail(context, err);
    }
  };
}

module.exports = createHandler();
module.exports.createHandler = createHandler;
module.exports.config = config;
module.exports.DEFAULTS = DEFAULTS;
