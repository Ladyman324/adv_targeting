"use strict";

const fs = require("node:fs");
const path = require("node:path");

const isContactKey = key => /^act-[a-f0-9]{26}$/.test(String(key || ""));
let contacts;

function indexDirectory(directory) {
  return new Map((directory?.contacts || []).filter(row => isContactKey(row.contactKey))
    .map(row => [row.contactKey, row]));
}

function get(key) {
  if (!isContactKey(key)) return null;
  if (!contacts) {
    const payload = JSON.parse(fs.readFileSync(path.join(__dirname, "act_contacts.json"), "utf8"));
    contacts = indexDirectory(payload.crm_directory);
  }
  return contacts.get(key) || null;
}

module.exports = { isContactKey, indexDirectory, get };
