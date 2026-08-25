"use strict";

const { QueueClient } = require("@azure/storage-queue");

const QUEUE_NAME = "email-direct-work";

function queueClient() {
  const connection = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  if (!connection) {
    const err = new Error("Direct-send queue storage is not configured.");
    err.statusCode = 503;
    err.code = "direct_send_queue_unavailable";
    throw err;
  }
  return new QueueClient(connection, QUEUE_NAME);
}

function workItem(kind, userId, operationId) {
  return { v: 1, kind: String(kind), userId: String(userId),
    operationId: String(operationId).toLowerCase() };
}

async function enqueue(kind, userId, operationId, visibilityTimeout = 0, deps = {}) {
  const q = deps.client || queueClient();
  await q.createIfNotExists();
  const payload = Buffer.from(JSON.stringify(workItem(kind, userId, operationId)), "utf8")
    .toString("base64");
  await q.sendMessage(payload, { visibilityTimeout: Math.max(0,
    Math.min(Number(visibilityTimeout) || 0, 7 * 86400)) });
}

function parse(value) {
  if (value && typeof value === "object") return value;
  const raw = String(value || "");
  try { return JSON.parse(Buffer.from(raw, "base64").toString("utf8")); }
  catch {
    try { return JSON.parse(raw); }
    catch {
      const err = new Error("Malformed direct-send queue message.");
      err.code = "direct_send_work_malformed";
      throw err;
    }
  }
}

module.exports = { QUEUE_NAME, enqueue, parse, workItem };
