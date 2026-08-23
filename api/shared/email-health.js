"use strict";

/* Sender health, per rep and per recipient domain.
 *
 * WHY THE OBVIOUS METRIC IS ABSENT
 * --------------------------------
 * Every deliverability guide leads with the spam-complaint rate: keep it under
 * 0.1%, never reach 0.3%. Those are Gmail and Yahoo bulk-sender rules, and of
 * 114,309 mailable advisor addresses in this application, 20 are consumer
 * mailboxes. Everyone else is behind a corporate gateway -- LPL, Edward Jones,
 * Morgan Stanley, Merrill -- and corporate tenants do not run feedback loops.
 * There is no "this person clicked Report Spam" event coming back, and a
 * complaint-rate dashboard here would read zero forever while telling you
 * nothing.
 *
 * So this is built from what a corporate gateway DOES tell us:
 *
 *   hard bounces   the address is dead
 *   deferrals      4.x.x -- they are slowing us down, the earliest warning there is
 *   policy         5.7.x -- refused on reputation or content, not on the address
 *   unsubscribes   somebody asked us to stop
 *
 * Rates are per DOMAIN as well as per rep, because a gateway throttles at the
 * domain level and a firm-wide average hides the one wirehouse that has started
 * refusing.
 */

// Thresholds. Deliberately ours, not anybody's published standard -- there is no
// published standard for this shape of sending. They mark where a number stops
// being noise and starts being worth acting on.
const LEVELS = {
  hard: { warn: 2, bad: 5 },        // % of delivered
  policy: { warn: 1, bad: 3 },
  soft: { warn: 10, bad: 25 },
  unsubscribe: { warn: 1, bad: 3 },
};

// Below this, a percentage is arithmetic rather than evidence. Two bounces out
// of six is 33% and means nothing at all.
const MIN_SAMPLE = 30;

const pct = (n, of) => (of > 0 ? (n / of) * 100 : 0);

function level(kind, rate, sample) {
  if (sample < MIN_SAMPLE) return "unknown";
  const t = LEVELS[kind];
  if (!t) return "ok";
  if (rate >= t.bad) return "bad";
  if (rate >= t.warn) return "warn";
  return "ok";
}

/**
 * @param sends    [{userId, userName, domain, sentUtc}]  one per message that reached Graph
 * @param events   [{userId, kind, domain, code, atUtc}]  delivery reports
 * @param optOuts  [{userId, domain}]                     unsubscribes attributable to a rep
 */
function summarise(sends, events, optOuts = []) {
  const reps = new Map();

  const repOf = (userId, userName) => {
    if (!reps.has(userId)) {
      reps.set(userId, { userId, userName: userName || userId,
                         sent: 0, hard: 0, soft: 0, policy: 0, unsubscribed: 0,
                         domains: new Map(), codes: new Map(), lastSentUtc: "" });
    }
    const r = reps.get(userId);
    if (userName && r.userName === userId) r.userName = userName;
    return r;
  };

  const domainOf = (rep, domain) => {
    const key = domain || "(unknown)";
    if (!rep.domains.has(key)) {
      rep.domains.set(key, { domain: key, sent: 0, hard: 0, soft: 0, policy: 0, unsubscribed: 0 });
    }
    return rep.domains.get(key);
  };

  for (const s of sends) {
    const rep = repOf(s.userId, s.userName);
    rep.sent++;
    domainOf(rep, s.domain).sent++;
    if (s.sentUtc && s.sentUtc > rep.lastSentUtc) rep.lastSentUtc = s.sentUtc;
  }

  for (const e of events) {
    const rep = repOf(e.userId);
    const kind = ["hard", "soft", "policy"].includes(e.kind) ? e.kind : null;
    if (!kind) continue;
    rep[kind]++;
    domainOf(rep, e.domain)[kind]++;
    if (e.code) rep.codes.set(e.code, (rep.codes.get(e.code) || 0) + 1);
  }

  for (const o of optOuts) {
    const rep = repOf(o.userId);
    rep.unsubscribed++;
    domainOf(rep, o.domain).unsubscribed++;
  }

  return [...reps.values()].map((r) => {
    const rates = {
      hard: pct(r.hard, r.sent), soft: pct(r.soft, r.sent),
      policy: pct(r.policy, r.sent), unsubscribe: pct(r.unsubscribed, r.sent),
    };
    const levels = Object.fromEntries(Object.entries(rates)
      .map(([k, v]) => [k, level(k, v, r.sent)]));

    const domains = [...r.domains.values()].map((d) => ({
      ...d,
      rates: { hard: pct(d.hard, d.sent), soft: pct(d.soft, d.sent),
               policy: pct(d.policy, d.sent), unsubscribe: pct(d.unsubscribed, d.sent) },
      levels: {
        hard: level("hard", pct(d.hard, d.sent), d.sent),
        soft: level("soft", pct(d.soft, d.sent), d.sent),
        policy: level("policy", pct(d.policy, d.sent), d.sent),
        unsubscribe: level("unsubscribe", pct(d.unsubscribed, d.sent), d.sent),
      },
    })).sort((a, b) => b.sent - a.sent);

    return { ...r, domains, rates, levels,
             codes: [...r.codes.entries()].map(([code, n]) => ({ code, n }))
               .sort((a, b) => b.n - a.n).slice(0, 8),
             advice: advise({ ...r, rates, levels, domains }) };
  }).sort((a, b) => b.sent - a.sent);
}

/* What to actually do about it.
 *
 * A dashboard that shows four percentages and stops has moved the problem from
 * "I do not know my numbers" to "I do not know what my numbers mean", which is
 * not progress. Each rule below names the observation, the likely cause, and one
 * concrete next step -- and stays quiet when there is nothing worth saying,
 * because advice that is always present is advice nobody reads.
 */
function advise(r) {
  const out = [];
  if (r.sent < MIN_SAMPLE) {
    out.push({ level: "info", text: `Only ${r.sent} message(s) sent so far. `
      + `Rates need about ${MIN_SAMPLE} before they mean anything.` });
    return out;
  }

  if (r.levels.hard !== "ok") {
    out.push({ level: r.levels.hard, text:
      `${r.rates.hard.toFixed(1)}% of messages hard bounced (${r.hard} of ${r.sent}). `
      + `That is a list-quality problem, not a sending problem: the addresses no longer exist. `
      + `Work from the CRM crosswalk rather than older lists, and let the suppression list do its job.` });
  }

  if (r.levels.policy !== "ok") {
    out.push({ level: r.levels.policy, text:
      `${r.rates.policy.toFixed(1)}% were refused on policy (${r.policy} of ${r.sent}). `
      + `These are 5.7.x rejections -- the receiving gateway declined the message on reputation `
      + `or content, not because the address was wrong. Check for large attachments, link-heavy `
      + `bodies, and whether one domain is refusing everything.` });
  }

  if (r.levels.soft !== "ok") {
    out.push({ level: r.levels.soft, text:
      `${r.rates.soft.toFixed(1)}% were deferred (${r.soft} of ${r.sent}). `
      + `Deferrals are the earliest warning that a gateway is slowing you down. `
      + `Send to fewer people at that firm per day, and space sends further apart.` });
  }

  if (r.levels.unsubscribe !== "ok") {
    out.push({ level: r.levels.unsubscribe, text:
      `${r.rates.unsubscribe.toFixed(1)}% unsubscribed (${r.unsubscribed} of ${r.sent}). `
      + `Unsubscribes track relevance more than anything else. Narrow the list rather than `
      + `rewording the template.` });
  }

  // The domain-level warning a firm-wide average hides.
  const worst = r.domains.filter((d) => d.sent >= MIN_SAMPLE
    && (d.levels.hard !== "ok" || d.levels.policy !== "ok" || d.levels.soft !== "ok"));
  for (const d of worst.slice(0, 3)) {
    const parts = [];
    if (d.levels.hard !== "ok") parts.push(`${d.rates.hard.toFixed(1)}% hard bounces`);
    if (d.levels.policy !== "ok") parts.push(`${d.rates.policy.toFixed(1)}% policy refusals`);
    if (d.levels.soft !== "ok") parts.push(`${d.rates.soft.toFixed(1)}% deferrals`);
    out.push({ level: "warn", text: `${d.domain}: ${parts.join(", ")} across ${d.sent} messages. `
      + `A gateway throttles per domain, so treat this one on its own rather than changing `
      + `everything you send.` });
  }

  if (!out.length) {
    out.push({ level: "ok", text: `Nothing to act on. ${r.sent} messages sent, `
      + `${r.rates.hard.toFixed(1)}% hard bounces, ${r.rates.soft.toFixed(1)}% deferrals, `
      + `${r.rates.policy.toFixed(1)}% policy refusals.` });
  }
  return out;
}

module.exports = { summarise, advise, level, LEVELS, MIN_SAMPLE, pct };
