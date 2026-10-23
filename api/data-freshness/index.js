"use strict";
const store = require("../shared/store");
const freshness = require("../shared/data-freshness");
function createHandler(deps = {}) {
  const identity = deps.identity || store.identity;
  const read = deps.read || freshness.read;
  return async (context, req) => {
    let status = 200, body;
    try {
      const who = identity(req);
      if (!freshness.isAdmin(who)) { status = 403; body = { error: "Administrator access is required." }; }
      else if (req.method && req.method !== "GET") { status = 405; body = { error: "This view is read-only." }; }
      else body = read();
    } catch (error) {
      status = error.statusCode === 401 ? 401 : 503;
      body = { error: status === 401 ? "Not signed in." : "Data freshness metadata is unavailable." };
    }
    context.res = { status, headers: { "Content-Type": "application/json", "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff" }, body: JSON.stringify(body) };
  };
}
module.exports = createHandler();
module.exports.createHandler = createHandler;
