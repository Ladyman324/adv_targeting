"use strict";

const internal = /^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@eicatlanta\.com$/;
const emailOf = who => String(who?.name || "").trim().toLowerCase();
const internalUser = who => !!who?.id && !who.dev && internal.test(emailOf(who));
const canUse = (who, grant) => internalUser(who) && who.id !== grant?.id
  && internal.test(String(grant?.email || "")) && grant.delegates.includes(emailOf(who));

function delegates(value, who) {
  const addresses = [...new Set(String(value || "").toLowerCase().split(/[,;\s]+/).filter(Boolean))];
  if (!internalUser(who) || addresses.length > 12
      || addresses.some(address => address.length > 254 || !internal.test(address) || address === emailOf(who))) {
    throw Object.assign(new Error("Enter up to 12 other EIC email addresses."), { statusCode: 400 });
  }
  return addresses.sort();
}

function staticListId(value) {
  return /^[a-z0-9-]{1,40}$/.test(String(value || "")) && !String(value).startsWith("role-")
    && !new Set(["keypeople", "keycontacts", "analyst", "analysts", "research",
      "duediligence", "scheduler", "schedulers"]).has(String(value));
}

module.exports = { delegates, canUse, internalUser, emailOf, staticListId };
