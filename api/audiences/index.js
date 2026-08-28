"use strict";

const store = require("../shared/store");
const audience = require("../shared/audience-definition");

module.exports = async function (context, req) {
  try {
    const who = store.identity(req);
    const id = (req.query && req.query.id) || "";

    if (req.method === "GET") {
      return store.ok(context, id
        ? await store.getAudience(who, audience.safeId(id))
        : { audiences: await store.listAudiences(who) });
    }
    if (req.method === "DELETE") {
      if (!id) {
        const error = new Error("id is required.");
        error.statusCode = 400;
        throw error;
      }
      return store.ok(context, await store.deleteAudience(who, audience.safeId(id)));
    }
    if (req.method !== "PUT") {
      const error = new Error("Method not allowed.");
      error.statusCode = 405;
      throw error;
    }

    const body = req.body || {};
    const normalized = audience.normalizeDefinition(body.definition);
    return store.ok(context, await store.putAudience(who, {
      id: body.id ? audience.safeId(body.id) : audience.newId(),
      name: body.name,
      description: body.description,
      definition: normalized.definition,
      definitionJson: normalized.json,
      etag: body.etag || "",
    }));
  } catch (error) {
    return store.fail(context, error);
  }
};
