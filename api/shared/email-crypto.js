"use strict";

const crypto = require("crypto");

function key() {
  const raw = process.env.EMAIL_TOKEN_ENCRYPTION_KEY || "";
  let value;
  try { value = Buffer.from(raw, "base64"); } catch { value = Buffer.alloc(0); }
  if (value.length !== 32) {
    const err = new Error("EMAIL_TOKEN_ENCRYPTION_KEY must be a base64-encoded 32-byte key.");
    err.statusCode = 503;
    throw err;
  }
  return value;
}

function encrypt(plain, aad) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key(), iv);
  cipher.setAAD(Buffer.from(String(aad || "")));
  const ciphertext = Buffer.concat([cipher.update(String(plain), "utf8"), cipher.final()]);
  return ["v1", iv.toString("base64"), cipher.getAuthTag().toString("base64"), ciphertext.toString("base64")].join(".");
}

function decrypt(value, aad) {
  const [version, iv, tag, ciphertext] = String(value || "").split(".");
  if (version !== "v1" || !iv || !tag || !ciphertext) throw new Error("Stored Microsoft token cache is invalid.");
  const decipher = crypto.createDecipheriv("aes-256-gcm", key(), Buffer.from(iv, "base64"));
  decipher.setAAD(Buffer.from(String(aad || "")));
  decipher.setAuthTag(Buffer.from(tag, "base64"));
  return Buffer.concat([decipher.update(Buffer.from(ciphertext, "base64")), decipher.final()]).toString("utf8");
}

module.exports = { encrypt, decrypt };
