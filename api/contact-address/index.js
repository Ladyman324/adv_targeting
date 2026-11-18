"use strict";
const store = require("../shared/store");
const contextService = require("../shared/act-contact-context");

module.exports = async function (context, req) {
  try {
    const who = store.identity(req);
    if (who.dev) throw Object.assign(new Error("Sign in to view ACT! contacts."),
      { statusCode: 401, addressReviewSafe: true });
    const crm = contextService.service();
    if (req.method === "GET") {
      const query = req.query || {};
      return store.ok(context, query.search !== undefined
        ? crm.search(String(query.search)) : await crm.preview(String(query.crd || "")));
    }
    if (req.method !== "POST" || !/^application\/json\b/i.test((req.headers || {})["content-type"] || ""))
      throw Object.assign(new Error("Send a confirmed ACT! contact action as JSON."),
        { statusCode: 400, addressReviewSafe: true });
    return store.ok(context, await crm.change(who, req.body || {}));
  } catch (error) {
    // Provider errors can contain contact details. Return only known messages.
    const safe = error.addressReviewSafe || (error.statusCode === 401 && error.message === "Not signed in.");
    return store.fail(context, safe ? error : Object.assign(
      new Error("The ACT! contact service could not confirm this request."), { statusCode: 502 }));
  }
};
