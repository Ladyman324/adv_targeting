"use strict";

const direct = require("../shared/email-direct-send");

module.exports = async function (context, workItem) {
  await direct.processWork(workItem);
};

module.exports.processWork = direct.processWork;
