"use strict";
const { TableClient } = require("@azure/data-tables");
const crypto = require("node:crypto");
let client;
async function table() {
  const connection = process.env.AZURE_STORAGE_CONNECTION_STRING;
  if (!connection) throw Object.assign(new Error("Address-change recording is unavailable. Nothing was changed."), { statusCode: 503, addressReviewSafe: true });
  if (!client) {
    const next = TableClient.fromConnectionString(connection, "ContactAddressChanges", { allowInsecureConnection: false });
    await next.createTable().catch(e => { if (e.statusCode !== 409) throw e; });
    client = next;
  }
  return client;
}
async function lock(contactId, operation) {
  const t = await table(), partitionKey = contactId.toLowerCase(), rowKey = "lock";
  const lease = { partitionKey, rowKey, expires: new Date(Date.now() + 180000).toISOString() };
  let held;
  try { await t.createEntity(lease); }
  catch (error) {
    if (error.statusCode !== 409) throw error;
    held = await t.getEntity(partitionKey, rowKey);
    if (Date.parse(held.expires) > Date.now())
      throw Object.assign(new Error("Another address update is in progress. Reopen this contact shortly."), { statusCode: 409, addressReviewSafe: true });
    try { await t.updateEntity(lease, "Replace", { etag: held.etag }); }
    catch { throw Object.assign(new Error("Another address update started. Reopen this contact shortly."), { statusCode: 409, addressReviewSafe: true }); }
  }
  held = await t.getEntity(partitionKey, rowKey);
  try { return await operation(); }
  finally { await t.deleteEntity(partitionKey, rowKey, { etag: held.etag }).catch(() => {}); }
}
async function begin(change) {
  const t = await table();
  const key = { partitionKey: change.contactId.toLowerCase(), rowKey: "change-" + crypto.randomUUID() };
  await t.createEntity({ ...key, atUtc: new Date().toISOString(), state: "pending", details: JSON.stringify(change) });
  return key;
}
async function finish(key, state) {
  const t = await table();
  await t.updateEntity({ ...key, state, checkedUtc: new Date().toISOString() }, "Merge");
}
// An uncertain POST may have created the contact. Keep its intent instead of
// retrying blindly. A new ACT snapshot can later prove the link by CRD.
async function creationStatus(crd) {
  const t = await table();
  try { return await t.getEntity("create-" + crd, "contact"); }
  catch (error) { if (error.statusCode === 404) return null; throw error; }
}
async function reserveCreation(crd, who, draft) {
  const t = await table();
  await t.createEntity({ partitionKey: "create-" + crd, rowKey: "contact", state: "pending",
    actorId: who.id, utc: new Date().toISOString(), draft: JSON.stringify(draft) });
}
async function finishCreation(crd, contactId) {
  const t = await table();
  await t.updateEntity({ partitionKey: "create-" + crd, rowKey: "contact",
    state: "verified", contactId, verifiedUtc: new Date().toISOString() }, "Merge");
}
module.exports = { lock, begin, finish, creationStatus, reserveCreation, finishCreation };
