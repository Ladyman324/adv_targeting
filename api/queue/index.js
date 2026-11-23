/* GET    /api/queue           every list I have, as summaries
 * GET    /api/queue?id=X      one list, with its people
 * PUT    /api/queue           create or replace a list
 * PATCH  /api/queue           atomically add/remove one member from any list
 * DELETE /api/queue?id=X      remove a list
 *
 * Server-side rather than in the browser so a list built at a desk in the
 * morning is the same list worked from a phone in the afternoon. One row per
 * list per user; replace rather than patch, because the client always holds
 * the whole thing and a partial-update protocol would buy nothing but ways for
 * the two to disagree.
 *
 * PROGRESS IS NOT STORED HERE. How far through a cycle a rep is comes from the
 * call log, filtered to entries since the cycle began. Storing it would mean
 * two records of the same fact -- and the one written by hand would be the one
 * that went wrong when a list was reordered or worked from two devices.
 */
"use strict";

const store = require("../shared/store");
const delegation = require("../shared/list-delegation");

module.exports = async function (context, req) {
  try {
    const actor = store.identity(req);
    const query = req.query || {};
    const id = query.id || "";
    if (req.method === "GET" && query.delegated === "1")
      return store.ok(context, { owners:await store.delegatedListOwners(actor) });
    let who = actor;
    const delegated = typeof query.owner === "string" && query.owner && query.owner !== actor.id;
    if (delegated) {
      const grant = /^[a-zA-Z0-9._-]{1,120}$/.test(query.owner) && await store.getListGrant(query.owner);
      if (!delegation.canUse(actor, grant)) throw Object.assign(
        new Error("You do not have access to this salesperson's lists."), { statusCode:403 });
      const requested = req.body?.id || id;
      if ((requested || req.method !== "GET") && !delegation.staticListId(requested))
        throw Object.assign(new Error("Choose a static contact list."), { statusCode:400 });
      if (requested && id && requested !== id)
        throw Object.assign(new Error("The list identifiers do not match."), { statusCode:400 });
      who = { id:query.owner, name:grant.email };
      context.log?.info?.("Delegated list operation", { method:req.method, actor:actor.id, owner:who.id });
    }

    if (req.method === "GET") {
      if (id) return store.ok(context, await store.getQueue(who, id));
      const lists = await store.listQueues(who);
      return store.ok(context, { lists:delegated ? lists.filter(list => delegation.staticListId(list.id)) : lists,
        defaultId:store.DEFAULT_LIST });
    }

    if (req.method === "DELETE") {
      if (!id) {
        const err = new Error("id is required to delete a list.");
        err.statusCode = 400;
        throw err;
      }
      return store.ok(context, await store.deleteQueue(who, id));
    }

    const body = req.body || {};
    if (req.method === "PATCH") {
      const operation = String(body.operation || "").toLowerCase();
      const value = operation === "add" ? body.item : body.crd;
      const saved = await store.mutateQueueMember(who, body.id || id, operation, value);
      return store.ok(context, { ...saved, max: store.MAX_QUEUE });
    }
    if (!Array.isArray(body.items)) {
      const err = new Error("items must be an array of advisor snapshots.");
      err.statusCode = 400;
      throw err;
    }
    const saved = await store.putQueue(who, delegated ? { ...body, id:body.id || id, createOnly:!body.etag } : body);
    // `dropped` is reported rather than swallowed: a rep who queued more than
    // the cap should be told, not left to find the tail missing.
    return store.ok(context, { ...saved, max: store.MAX_QUEUE });
  } catch (err) {
    if (err && (err.code === "PropertyValueTooLarge"
        || /PropertyValueTooLarge/.test(String(err.message || "")))) {
      err.statusCode = 413;
      err.message = "This list is too large for storage even though its person count is allowed. "
        + "Narrow this territory's selection or split it into smaller lists, then try again.";
    }
    return store.fail(context, err);
  }
};
