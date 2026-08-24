"use strict";

/* A Table Storage double that actually behaves like Table Storage.
 *
 * WHY THIS EXISTS
 * ---------------
 * 186 tests passed while two of the worst defects in the email-activity work
 * were live, and both were the SAME mistake: writing a partial entity with
 * "Replace" mode, which deletes every property not in the payload.
 *
 *   putSweepState() wrote {lastError} on the error path and erased the
 *   watermark, so one failed sweep sent that rep back to a 48-hour window.
 *
 *   putEngagement() wrote a folded state carrying no actedAt and erased it, so
 *   a reply a rep had already dealt with came back as new on the next refresh.
 *
 * The hand-written mocks could not catch either, because they recorded what was
 * WRITTEN and nothing modelled what was DESTROYED. A mock that merges where the
 * real store replaces does not merely miss the bug -- it certifies it.
 *
 * So this implements the semantics that actually bit:
 *
 *   Replace   the stored entity becomes exactly the payload; anything absent
 *             from it is gone
 *   Merge     absent properties are left alone
 *   ordering  listEntities yields ascending by (partitionKey, rowKey), which is
 *             what the service does and what a JS Map does not
 *   keys      characters Azure forbids in a key are rejected, loudly
 *   etags     stale conditional updates/deletes fail with 412
 *   create    a duplicate create fails with 409 rather than becoming an upsert
 *
 * Deliberately not a full emulator. Azurite is the fuller answer and is worth
 * adding; this is the part that catches the classes of bug that got through.
 */

// Azure Table Storage rejects these in PartitionKey and RowKey. A Graph
// immutable id is base64-flavoured and can contain "/" and "+", so this is not
// hypothetical -- it is why message ids are hashed before being used as keys.
const ILLEGAL_KEY_CHARS = ["/", "\\", "#", "?"];

function illegalKey(text) {
  if (ILLEGAL_KEY_CHARS.some((c) => text.includes(c))) return true;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code < 0x20 || (code >= 0x7f && code <= 0x9f)) return true;
  }
  return false;
}

function assertKey(which, value) {
  const text = String(value == null ? "" : value);
  if (!text) throw new Error(`${which} must not be empty`);
  if (illegalKey(text)) {
    const err = new Error(
      `${which} contains a character Azure Table Storage forbids: ${JSON.stringify(text)}`);
    err.statusCode = 400;
    throw err;
  }
  if (text.length > 1024) {
    const err = new Error(`${which} is longer than 1024 characters`);
    err.statusCode = 400;
    throw err;
  }
}

class FakeTable {
  constructor(name) {
    this.name = name;
    this.version = 0;
    this.rows = new Map();          // JSON tuple -> entity
  }

  static key(pk, rk) { return JSON.stringify([String(pk), String(rk)]); }

  async createTable() { /* always fine */ }

  nextEtag() { return `W/"${++this.version}"`; }

  static error(statusCode, message) {
    const err = new Error(message);
    err.statusCode = statusCode;
    return err;
  }

  checked(entity) {
    assertKey("PartitionKey", entity.partitionKey);
    assertKey("RowKey", entity.rowKey);
    // etag is service metadata, never an ordinary user property written back
    // as part of Replace or Merge.
    const { etag: _etag, ...properties } = entity;
    return properties;
  }

  assertCondition(current, options = {}) {
    const wanted = options && options.etag;
    if (wanted && wanted !== "*" && wanted !== current.etag)
      throw FakeTable.error(412, "The entity changed since it was read.");
  }

  save(key, entity) {
    const saved = { ...entity, etag: this.nextEtag() };
    this.rows.set(key, saved);
    return { etag: saved.etag };
  }

  async createEntity(entity) {
    const properties = this.checked(entity);
    const key = FakeTable.key(properties.partitionKey, properties.rowKey);
    if (this.rows.has(key)) throw FakeTable.error(409, "The entity already exists.");
    return this.save(key, properties);
  }

  async upsertEntity(entity, mode = "Merge") {
    const properties = this.checked(entity);
    const key = FakeTable.key(properties.partitionKey, properties.rowKey);
    if (mode === "Replace" || !this.rows.has(key)) {
      // REPLACE: the entity becomes exactly this payload. This one line is the
      // whole reason this file exists.
      return this.save(key, properties);
    }
    return this.save(key, { ...this.rows.get(key), ...properties });
  }

  async updateEntity(entity, mode = "Merge", options = {}) {
    const properties = this.checked(entity);
    const key = FakeTable.key(properties.partitionKey, properties.rowKey);
    const current = this.rows.get(key);
    if (!current) throw FakeTable.error(404, "Not found");
    this.assertCondition(current, options);
    return this.save(key, mode === "Replace"
      ? properties
      : { ...current, ...properties });
  }

  async getEntity(pk, rk) {
    const row = this.rows.get(FakeTable.key(pk, rk));
    if (!row) throw FakeTable.error(404, "Not found");
    return { ...row };
  }

  async deleteEntity(pk, rk, options = {}) {
    const key = FakeTable.key(pk, rk);
    const current = this.rows.get(key);
    if (!current) throw FakeTable.error(404, "Not found");
    this.assertCondition(current, options);
    this.rows.delete(key);
    return {};
  }

  /* Enough transaction behavior for store tests: one partition, atomic
   * rollback, and the create/update/delete actions used by the Tables SDK. */
  async submitTransaction(actions) {
    if (!Array.isArray(actions) || !actions.length || actions.length > 100)
      throw FakeTable.error(400, "A transaction must contain 1 to 100 actions.");
    const partition = String(((actions[0] || [])[1] || {}).partitionKey || "");
    if (actions.some((action) => String(((action || [])[1] || {}).partitionKey || "") !== partition))
      throw FakeTable.error(400, "Every transaction action must use one partition.");
    const snapshot = new Map([...this.rows].map(([key, row]) => [key, { ...row }]));
    const version = this.version;
    const results = [];
    try {
      for (const action of actions) {
        const [kind, entity, third, fourth] = action;
        if (kind === "create") results.push(await this.createEntity(entity));
        else if (kind === "upsert") results.push(await this.upsertEntity(entity, third || "Merge"));
        else if (kind === "update") results.push(await this.updateEntity(
          entity, typeof third === "string" ? third : "Merge",
          (typeof third === "object" ? third : fourth) || {}));
        else if (kind === "delete") results.push(await this.deleteEntity(
          entity.partitionKey, entity.rowKey, third || {}));
        else throw FakeTable.error(400, `Unsupported transaction action ${kind}.`);
      }
      return { subResponses: results };
    } catch (err) {
      this.rows = snapshot;
      this.version = version;
      throw err;
    }
  }

  /* Ascending by (partitionKey, rowKey), like the service.
   *
   * The order is not a detail. listActivity() broke at a row count while
   * reading ascending keys that began with a FORWARD timestamp -- so it kept
   * the oldest rows, sorted them, and returned them as "the most recent
   * activity". A Map yielding insertion order hides that completely.
   *
   * Filter support covers the two shapes this codebase uses: PartitionKey
   * equality, and the ge/lt prefix range.
   */
  listEntities(options = {}) {
    const filter = ((options.queryOptions || {}).filter) || "";
    const eq = /PartitionKey eq '([^']*)'/.exec(filter);
    const range = /PartitionKey ge '([^']*)' and PartitionKey lt '([^']*)'/.exec(filter);
    const rowGe = /RowKey ge '([^']*)'/.exec(filter);
    const rowLt = /RowKey lt '([^']*)'/.exec(filter);
    const userEq = /userId eq '([^']*)'/.exec(filter);
    const rows = [...this.rows.values()].sort((a, b) =>
      String(a.partitionKey).localeCompare(String(b.partitionKey))
      || String(a.rowKey).localeCompare(String(b.rowKey))).filter((row) => {
      if (eq && String(row.partitionKey) !== eq[1]) return false;
      if (range && !(String(row.partitionKey) >= range[1]
                     && String(row.partitionKey) < range[2])) return false;
      if (rowGe && String(row.rowKey) < rowGe[1]) return false;
      if (rowLt && String(row.rowKey) >= rowLt[1]) return false;
      if (userEq && String(row.userId || "") !== userEq[1]) return false;
      return true;
    });
    return {
      async *[Symbol.asyncIterator]() {
        for (const row of rows) yield { ...row };
      },
      byPage(settings = {}) {
        const start = Math.max(0, Number(settings.continuationToken) || 0);
        const size = Math.max(1, Number(settings.maxPageSize) || rows.length || 1);
        return {
          async *[Symbol.asyncIterator]() {
            for (let offset = start; offset < rows.length; offset += size) {
              const page = rows.slice(offset, offset + size).map((row) => ({ ...row }));
              page.continuationToken = offset + size < rows.length ? String(offset + size) : undefined;
              yield page;
            }
          },
        };
      },
    };
  }
}

class FakeTableService {
  constructor() { this.tables = new Map(); }
  table(name) {
    if (!this.tables.has(name)) this.tables.set(name, new FakeTable(name));
    return this.tables.get(name);
  }
  all(name) { return [...this.table(name).rows.values()]; }
  reset() { this.tables.clear(); }
}

module.exports = { FakeTable, FakeTableService, illegalKey };
