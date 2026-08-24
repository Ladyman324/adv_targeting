"use strict";

const crypto = require("crypto");
const msal = require("@azure/msal-node");
const store = require("./email-store");
const crypt = require("./email-crypto");

/* Bump whenever graphMe's $select changes.
 *
 * The profile is captured ONCE, when a mailbox is connected, and then used to
 * build every signature that person ever sends. So adding a field to the
 * $select above does nothing for anyone already connected -- their stored
 * profile predates it and has no such key. That is exactly what happened to
 * jobTitle and mobilePhone: the signature code read them correctly, the stored
 * profiles simply did not have them, and the only cure was to disconnect and
 * reconnect. This marker lets a stale profile be refreshed in place on the next
 * silent token acquisition instead.
 */
const PROFILE_VERSION = 2;
const TOKEN_WRITE_ATTEMPTS = 3;

const GRAPH_SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send", "offline_access", "openid", "profile"];

function settings() {
  const clientId = process.env.GRAPH_CLIENT_ID || process.env.AZURE_CLIENT_ID || "";
  const clientSecret = process.env.GRAPH_CLIENT_SECRET || process.env.AZURE_CLIENT_SECRET || "";
  const tenantId = process.env.GRAPH_TENANT_ID || "";
  const redirectUri = process.env.GRAPH_REDIRECT_URI || "";
  if (!clientId || !clientSecret || !tenantId || !redirectUri) {
    const err = new Error("Microsoft Graph is not configured. Set GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_TENANT_ID, and GRAPH_REDIRECT_URI.");
    err.statusCode = 503;
    throw err;
  }
  return { clientId, clientSecret, tenantId, redirectUri,
    authority: `https://login.microsoftonline.com/${tenantId}` };
}

function client(cache) {
  const s = settings();
  const cca = new msal.ConfidentialClientApplication({ auth: {
    clientId: s.clientId, clientSecret: s.clientSecret, authority: s.authority,
  }, system: { loggerOptions: { piiLoggingEnabled: false, loggerCallback: () => {} } } });
  if (cache) cca.getTokenCache().deserialize(cache);
  return cca;
}

async function begin(who, returnTo = "/?email=connected") {
  const s = settings();
  const safeReturnTo = /^\/(?!\/)/.test(String(returnTo || "")) ? String(returnTo).slice(0, 300) : "/?email=connected";
  const state = crypto.randomBytes(24).toString("hex");
  const nonce = crypto.randomBytes(24).toString("hex");
  await store.putAuthState(who, state, nonce, safeReturnTo);
  const authorizeUrl = await client().getAuthCodeUrl({ scopes: GRAPH_SCOPES,
    redirectUri: s.redirectUri, state, nonce, prompt: "select_account" });
  return { authorizeUrl };
}

async function graphMe(accessToken) {
  const response = await fetch("https://graph.microsoft.com/v1.0/me?$select=id,displayName,givenName,surname,mail,userPrincipalName,jobTitle,businessPhones,mobilePhone", {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
  });
  if (!response.ok) {
    const err = new Error(`Microsoft Graph profile check failed (${response.status}).`);
    err.statusCode = 502;
    throw err;
  }
  return response.json();
}

// `who` is whatever the platform could tell us about the caller, and is null
// when the Static Web Apps session cookie did not survive the redirect back from
// Microsoft. The user is therefore taken from the state row, which was written
// against them when the flow began; when `who` is present it is passed through
// as an extra equality check rather than being the only one.
async function complete(who, code, state) {
  const s = settings();
  const authState = await store.consumeAuthState(state, who ? who.id : null);
  const user = { id: authState.userId, name: authState.userName || (who && who.name) || "" };
  const cca = client();
  const result = await cca.acquireTokenByCode({ code, scopes: GRAPH_SCOPES, redirectUri: s.redirectUri });
  if (!result || !result.accessToken || !result.account) throw new Error("Microsoft did not return a usable delegated token.");
  const profile = { ...(await graphMe(result.accessToken)), profileVersion: PROFILE_VERSION };
  // The real control: whoever just signed in at Microsoft must be the same
  // person the state row was issued to. A stolen state gets a 403 here, because
  // the thief's own Graph profile will not match.
  if (String(profile.id).toLowerCase() !== String(user.id).toLowerCase()) {
    const err = new Error("The connected Microsoft mailbox does not belong to the employee signed into this application.");
    err.statusCode = 403;
    throw err;
  }
  const mailbox = profile.mail || profile.userPrincipalName || "";
  await store.putConnection(user.id, {
    userName: user.name, homeAccountId: result.account.homeAccountId,
    mailboxId: profile.id, mailbox, profileJson: JSON.stringify(profile),
    tokenCache: crypt.encrypt(cca.getTokenCache().serialize(), `${user.id}|${profile.id}`),
    connectedUtc: new Date().toISOString(), needsReconnect: false,
  });
  return { mailbox, profile, returnTo: authState.returnTo || "/?email=connected" };
}

async function status(userId) {
  const c = await store.getConnection(userId);
  return c ? { connected: !c.needsReconnect, needsReconnect: !!c.needsReconnect,
    mailbox: c.mailbox || "", profile: JSON.parse(c.profileJson || "{}"), connectedUtc: c.connectedUtc || "" }
    : { connected: false, needsReconnect: false, mailbox: "", profile: null };
}

async function tokenFor(userId) {
  let lastConflict = null;
  for (let attempt = 0; attempt < TOKEN_WRITE_ATTEMPTS; attempt++) {
    const c = await store.getConnection(userId);
    if (!c || c.needsReconnect) {
      const err = new Error("Connect your Microsoft 365 mailbox before creating drafts.");
      err.statusCode = 409; err.code = "graph_not_connected"; throw err;
    }
    if (!c.etag) {
      const err = new Error("The stored Microsoft mailbox connection has no concurrency version.");
      err.statusCode = 503;
      err.code = "graph_connection_version_missing";
      throw err;
    }
    const cache = crypt.decrypt(c.tokenCache, `${userId}|${c.mailboxId}`);
    const cca = client(cache);
    const accounts = await cca.getTokenCache().getAllAccounts();
    const account = accounts.find((a) => a.homeAccountId === c.homeAccountId);
    if (!account) throw new Error("The stored Microsoft account is missing from its token cache.");
    try {
      const result = await cca.acquireTokenSilent({ account, scopes: GRAPH_SCOPES });
      const serialized = cca.getTokenCache().serialize();
      let profile = JSON.parse(c.profileJson || "{}");
      let profileJson = c.profileJson;
      if (Number(profile.profileVersion || 0) !== PROFILE_VERSION) {
        // Best effort. A failure here must not stop somebody sending mail -- the
        // old profile still produces a correct, merely less complete, signature.
        try {
          profile = { ...(await graphMe(result.accessToken)), profileVersion: PROFILE_VERSION };
          profileJson = JSON.stringify(profile);
        } catch { /* keep what we had */ }
      }
      if (serialized !== cache || profileJson !== c.profileJson) {
        // The cache may contain a rotated refresh token. Replacing a row read
        // before another invocation refreshed it would lose that newer token,
        // so the whole acquisition is conditional on the version we decrypted.
        await store.putConnection(userId, { ...c,
          profileJson, tokenCache: crypt.encrypt(serialized, `${userId}|${c.mailboxId}`),
          needsReconnect: false }, c.etag);
      }
      return { accessToken: result.accessToken, mailboxId: c.mailboxId,
               mailbox: c.mailbox, profile };
    } catch (e) {
      if (e && e.statusCode === 412) {
        // Do not retry the stale write. Reload the row, decrypt the cache that
        // won, and repeat silent acquisition from that newer state.
        lastConflict = e;
        continue;
      }
      if (e instanceof msal.InteractionRequiredAuthError
          || String(e.errorCode || "").includes("interaction_required")) {
        try {
          // A stale token failure must not mark a mailbox disconnected after a
          // successful OAuth callback has already replaced the connection.
          await store.putConnection(userId, { ...c, needsReconnect: true }, c.etag);
        } catch (writeError) {
          if (writeError && writeError.statusCode === 412) {
            lastConflict = writeError;
            continue;
          }
          throw writeError;
        }
        const err = new Error("Microsoft requires this employee to reconnect their mailbox.");
        err.statusCode = 409; err.code = "graph_reconnect_required"; throw err;
      }
      throw e;
    }
  }
  const err = new Error("The Microsoft mailbox connection changed while its token was being refreshed. Try again.");
  err.statusCode = 409;
  err.code = "graph_connection_changed";
  if (lastConflict) err.cause = lastConflict;
  throw err;
}

/* The stored profile, refreshed first if it predates the current field set.
 * Returns null rather than throwing: every caller has a usable fallback, and
 * none of them should fail a send over a signature detail. */
async function refreshProfile(userId) {
  try { return (await tokenFor(userId)).profile || null; } catch { return null; }
}

module.exports = { GRAPH_SCOPES, PROFILE_VERSION, begin, complete, status, tokenFor, refreshProfile };
