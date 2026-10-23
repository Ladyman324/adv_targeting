"use strict";
// Synthetic network transports: these fixtures can never contact a real host.
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/refresh_offline/node.cjs"), "utf8");
let connects = 0, dnsCalls = 0, denied = 0;
function Socket() {}
Socket.prototype.connect = function () { connects++; return this; };
const net = { Socket,
  connect(...args) {
    // Mirrors Node normalizeArgs(): Socket receives [options, callback] as
    // its one argument. This exact shape previously bypassed the guard.
    const options = typeof args[0] === "object" ? args[0]
      : { port: args[0], host: typeof args[1] === "string" ? args[1] : undefined };
    return new Socket().connect([options, typeof args[1] === "function" ? args[1] : undefined]);
  },
};
const dns = { promises: {} };
for (const name of ["lookup", "resolve", "resolve4", "resolve6", "resolveAny"]) {
  dns[name] = () => { dnsCalls++; };
  dns.promises[name] = async () => { dnsCalls++; };
}
const tls = { connect: options => new Socket().connect([options]) };
const https = { get: value => {
  const parsed = new URL(value);
  return tls.connect({ host: parsed.hostname, port: Number(parsed.port) || 443 });
} };
const modules = { "node:fs": { appendFileSync(marker, message) {
  assert.equal(marker, "fixture-marker"); assert.equal(message, "external_node_network_denied\n"); denied++;
} }, "node:net": net, "node:dns": dns };
vm.runInNewContext(source, { process: { env: { ADVISOR_REFRESH_OFFLINE: "1", REFRESH_NETWORK_ATTEMPT_FILE: "fixture-marker" } },
  require(name) { assert.ok(modules[name], "unexpected module " + name); return modules[name]; } });

async function main() {
  const forbidden = [
    () => new Socket().connect({ host: "203.0.113.1", port: 443 }),
    () => new Socket().connect(443, "203.0.113.1"),
    () => new Socket().connect([{ host: "203.0.113.1", port: 443 }]),
    () => net.connect({ host: "203.0.113.1", port: 443 }),
    () => net.connect(443, "203.0.113.1"),
    () => tls.connect({ host: "203.0.113.1", port: 443 }),
    () => https.get("https://203.0.113.1/"),
    () => new Socket().connect({ hostname: "203.0.113.1", port: 443 }),
    () => net.connect({ host: "external.example.invalid", port: 443 }),
    () => tls.connect({ host: "external.example.invalid", port: 443 }),
    () => https.get("https://external.example.invalid/"),
    () => dns.lookup("external.example.invalid"),
  ];
  for (const operation of forbidden) assert.throws(operation, /External network access is disabled/);
  await assert.rejects(dns.promises.lookup("external.example.invalid"), /External network access is disabled/);
  assert.equal(connects, 0, "blocked requests never reach even the stub transport");
  assert.equal(dnsCalls, 0);
  assert.equal(denied, forbidden.length + 1, "every blocked attempt leaves a marker even if caught");
  for (const host of ["localhost", "127.0.0.1", "::1"]) {
    new Socket().connect({ host, port: 12345 });
    net.connect({ host, port: 12345 });
    tls.connect({ host, port: 12345 });
    dns.lookup(host);
  }
  net.connect(12345);
  new Socket().connect("fixture-local-pipe");
  assert.equal(connects, 11, "loopback, default-host and local pipes remain usable");
  assert.equal(dnsCalls, 3);
  console.log("Offline Node guard: literal-IP and hostname denial, nested normalization, TLS/HTTPS, DNS, and loopback passed.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
