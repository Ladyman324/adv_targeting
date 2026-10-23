"use strict";
// Preloaded for offline validation: imports cannot quietly reach a live
// mailbox/storage service, even if a caller catches the resulting exception.
if (process.env.ADVISOR_REFRESH_OFFLINE === "1") {
  const fs = require("node:fs"), net = require("node:net"), dns = require("node:dns");
  function check(host) {
    if (["localhost", "127.0.0.1", "::1", undefined, null].includes(host)) return;
    const marker = process.env.REFRESH_NETWORK_ATTEMPT_FILE;
    if (marker) fs.appendFileSync(marker, "external_node_network_denied\n");
    throw new Error("External network access is disabled during refresh validation");
  }
  const connect = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function (...args) {
    // net.connect/createConnection and TLS pass Node's normalized argument
    // array as a single argument to Socket.connect. Inspect that inner options
    // object, not the outer array (whose host property is always undefined).
    let parameters = args;
    for (let depth = 0; Array.isArray(parameters[0]); depth++) {
      if (depth > 8 || parameters[0] === parameters) throw new Error("Invalid socket connection arguments");
      parameters = parameters[0];
    }
    const options = parameters[0];
    if (!(typeof options === "string" && !/^\d+$/.test(options))) {
      check(options && typeof options === "object" ? options.host || options.hostname
        : typeof parameters[1] === "string" ? parameters[1] : undefined);
    }
    return connect.apply(this, args);
  };
  for (const name of ["lookup", "resolve", "resolve4", "resolve6", "resolveAny"]) {
    const original = dns[name];
    if (original) dns[name] = function (host, ...args) { check(host); return original.call(this, host, ...args); };
    if (dns.promises && dns.promises[name]) {
      const promised = dns.promises[name];
      dns.promises[name] = async function (host, ...args) { check(host); return promised.call(this, host, ...args); };
    }
  }
}
