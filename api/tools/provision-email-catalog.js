"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { BlobServiceClient } = require("@azure/storage-blob");
const { TableClient } = require("@azure/data-tables");
const core = require("../shared/email-core");

function usage() {
  console.error("Usage:\n  npm run email:provision -- document <id> <file> [display name]\n  npm run email:provision -- template <id> <template.json>");
  process.exitCode = 2;
}

function safeId(value) {
  const id = String(value || "").toLowerCase().replace(/[^a-z0-9-]/g, "").slice(0, 80);
  if (!id) throw new Error("Catalog id must contain letters, numbers, or hyphens.");
  return id;
}

async function storage() {
  const connection = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!connection) throw new Error("AZURE_STORAGE_CONNECTION_STRING is required.");
  return connection;
}

async function document(id, file, displayName) {
  const connection = await storage();
  const full = path.resolve(file || "");
  const stat = fs.statSync(full);
  if (!stat.isFile()) throw new Error(`${full} is not a file.`);
  if (stat.size > core.config().maxAttachmentBytes) throw new Error("Document exceeds EMAIL_MAX_ATTACHMENT_BYTES.");
  const bytes = fs.readFileSync(full);
  const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
  const containerName = process.env.EMAIL_DOCUMENT_CONTAINER || "email-documents";
  const container = BlobServiceClient.fromConnectionString(connection).getContainerClient(containerName);
  await container.createIfNotExists();
  const blobName = `${id}/${sha256.slice(0, 16)}-${path.basename(full)}`;
  const contentType = process.env.EMAIL_DOCUMENT_CONTENT_TYPE || "application/octet-stream";
  await container.getBlockBlobClient(blobName).uploadData(bytes, { blobHTTPHeaders: { blobContentType: contentType },
    metadata: { approved: "true", sha256 } });
  const table = TableClient.fromConnectionString(connection, "EmailDocuments", { allowInsecureConnection: false });
  await table.createTable().catch((e) => { if (e.statusCode !== 409) throw e; });
  const old = await table.getEntity("approved", id).catch((e) => { if (e.statusCode === 404) return null; throw e; });
  await table.upsertEntity({ partitionKey: "approved", rowKey: id, name: displayName || path.basename(full),
    blobName, contentType, size: stat.size, sha256, version: (Number(old && old.version) || 0) + 1,
    approved: true, updatedUtc: new Date().toISOString() }, "Replace");
  console.log(`Approved document ${id}: ${displayName || path.basename(full)} (${stat.size} bytes, sha256 ${sha256})`);
}

async function template(id, jsonFile) {
  const connection = await storage();
  const value = JSON.parse(fs.readFileSync(path.resolve(jsonFile || ""), "utf8"));
  if (!value.name || !value.subject || !value.bodyText) throw new Error("Template JSON requires name, subject, and bodyText.");
  const fields = [...`${value.subject}\n${value.bodyText}`.matchAll(/\{\{\s*([^}]+)\s*\}\}/g)].map((m) => m[1]);
  const invalid = fields.filter((x) => !core.ALLOWED_FIELDS.has(x));
  if (invalid.length) throw new Error(`Unapproved merge field(s): ${[...new Set(invalid)].join(", ")}.`);
  const table = TableClient.fromConnectionString(connection, "EmailTemplates", { allowInsecureConnection: false });
  await table.createTable().catch((e) => { if (e.statusCode !== 409) throw e; });
  const old = await table.getEntity("approved", id).catch((e) => { if (e.statusCode === 404) return null; throw e; });
  await table.upsertEntity({ partitionKey: "approved", rowKey: id, name: String(value.name).slice(0, 120),
    subject: String(value.subject).slice(0, 500), bodyText: String(value.bodyText).slice(0, core.config().maxBodyChars),
    defaultAttachmentIdsJson: JSON.stringify(value.defaultAttachmentIds || []),
    requiredDocumentIdsJson: JSON.stringify(value.requiredDocumentIds || value.defaultAttachmentIds || []),
    requiredMaterialFamilyIdsJson: JSON.stringify(value.requiredMaterialFamilyIds || []),
    version: (Number(old && old.version) || 0) + 1, approved: true, updatedUtc: new Date().toISOString() }, "Replace");
  console.log(`Approved template ${id}: ${value.name}`);
}

async function main() {
  const [, , kind, rawId, source, ...nameParts] = process.argv;
  if (!kind || !rawId || !source) return usage();
  const id = safeId(rawId);
  if (kind === "document") return document(id, source, nameParts.join(" "));
  if (kind === "template") return template(id, source);
  return usage();
}

main().catch((err) => { console.error(err.message); process.exitCode = 1; });
