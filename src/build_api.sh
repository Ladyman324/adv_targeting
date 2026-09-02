#!/usr/bin/env bash
# Build a fresh, provenance-stamped api.tgz for the manual Cloud Shell deploy.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
API="$ROOT/api"
DIST="$ROOT/dist"
TEMP_BASE=$(cd "${TMPDIR:-/tmp}" && pwd -P)
TEMP_ROOT=""
STAGE_DIR=""
# Bumped deliberately whenever a test FILE is added or removed. That is the
# point of it: the count is a tripwire for tests quietly falling out of the
# package, so it must not be derived from whatever happens to be on disk.
#   26: contact-flags.test.js -- flags as a set of reps, not a shared boolean
#   27: email-follow-up.test.js -- who is left after a campaign, and who is not
#   28: email-direct-store.test.js -- atomic operation ownership and outbox
#   29: email-direct-send.test.js -- no-resubmit reconciliation boundary
#   30: email-preferences.test.js -- scanner-resistant unsubscribe confirmation
#   33: email-approve-pacing.test.js -- drafts are spaced, not fired at once
#   34: template-publish.test.js -- held templates never reach a rep
#   35: email-campaign-repair.test.js -- approved work survives lost queue hints
#   36: queue-member.test.js -- arbitrary-list membership is atomic and session-safe
#   37: role-list-contract.test.js -- derived role lists stay distinct and disposable
#   38: audiences.test.js -- personal dynamic definitions, validation, and ETags
#   39: email-materials.test.js -- material routing, lifecycle, provenance, and freshness
#   40: email-schedule.test.js -- durable scheduling, preflight, holds, and notification
#   41: email-calendar-capacity.test.js -- Eastern/DST planning, locking, and release
#   42: email-capacity-service.test.js -- final envelope counting and deferred cleanup
#   43: email-capacity-ui.test.js -- visible, server-authored multi-day plan contract
#   44: email-template-material-series-ui.test.js -- templates require a routed series, not one variant
#   45: map-activity-filter-ui.test.js -- activity filters stay scoped and never blank the map at zero
#   46: map-assets-filter-ui.test.js -- desktop and Field use the canonical CRD-keyed EIC asset book
EXPECTED_API_TEST_FILE_COUNT=46

cleanup() {
  test -z "$TEMP_ROOT" && return
  case "$TEMP_ROOT" in
    "$TEMP_BASE"/api-build.*) rm -rf -- "$TEMP_ROOT" ;;
    *) echo "[!] refusing to remove unexpected temp path: $TEMP_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT
die() { echo "[!] $*" >&2; exit 1; }

validate_recipient_registry_source() {
  local registry="$ROOT/data/identity/approved_recipients.json.gz"
  local shard_manifest="$ROOT/data/identity/approved_recipients_manifest.json"
  local descriptor="$API/shared/approved-recipient-release.json"
  local act_lookup="$API/shared/act_contacts.json"
  test -s "$registry" || die \
    "approved recipient registry is missing; run python src/export_approved_recipients.py"
  test -s "$shard_manifest" || die \
    "approved recipient shard manifest is missing; run python src/export_approved_recipients.py"
  test -s "$act_lookup" || die "approved Act lookup is missing"
  test -s "$descriptor" || die \
    "approved recipient release descriptor is missing; run python src/export_approved_recipients.py"
  APPROVED_REGISTRY_PATH="$registry" APPROVED_SHARD_MANIFEST_PATH="$shard_manifest" \
    REGISTRY_DESCRIPTOR_PATH="$descriptor" \
    ACT_LOOKUP_PATH="$act_lookup" node <<'NODE'
const fs = require("node:fs");
const zlib = require("node:zlib");
const crypto = require("node:crypto");
const path = process.env.APPROVED_REGISTRY_PATH;
const canonical = (value) => Array.isArray(value)
  ? `[${value.map(canonical).join(",")}]`
  : value && typeof value === "object"
    ? `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`
    : JSON.stringify(value);
const payload = JSON.parse(zlib.gunzipSync(fs.readFileSync(path)).toString("utf8"));
if (Number(payload.schemaVersion) !== 1) throw new Error("unsupported registry schema");
const core = { schemaVersion: payload.schemaVersion, recipients: payload.recipients,
  ineligible: payload.ineligible || {}, provenance: payload.provenance || {} };
const hash = crypto.createHash("sha256").update(canonical(core), "utf8").digest("hex");
if (hash !== payload.contentHash) throw new Error("registry content hash is invalid");
const descriptor = JSON.parse(fs.readFileSync(
  process.env.REGISTRY_DESCRIPTOR_PATH, "utf8"));
const manifest = JSON.parse(fs.readFileSync(
  process.env.APPROVED_SHARD_MANIFEST_PATH, "utf8"));
const manifestCore = { schemaVersion: manifest.schemaVersion,
  registrySchemaVersion: manifest.registrySchemaVersion,
  registryContentHash: manifest.registryContentHash,
  recipientCount: manifest.recipientCount,
  ineligibleCount: manifest.ineligibleCount,
  provenance: manifest.provenance || {}, shards: manifest.shards || {} };
const manifestHash = crypto.createHash("sha256")
  .update(canonical(manifestCore), "utf8").digest("hex");
if (Number(manifest.schemaVersion) !== 1
    || manifestHash !== manifest.contentHash
    || manifestHash !== descriptor.shardManifestHash
    || manifest.registryContentHash !== hash)
  throw new Error("recipient shard manifest is invalid or belongs to another release");
const descriptorCore = { schemaVersion: descriptor.schemaVersion,
  registrySchemaVersion: descriptor.registrySchemaVersion,
  registryContentHash: descriptor.registryContentHash,
  recipientCount: descriptor.recipientCount,
  ineligibleCount: descriptor.ineligibleCount,
  provenance: descriptor.provenance || {},
  shardManifestHash: descriptor.shardManifestHash };
const descriptorHash = crypto.createHash("sha256")
  .update(canonical(descriptorCore), "utf8").digest("hex");
if (Number(descriptor.schemaVersion) !== 1
    || Number(descriptor.registrySchemaVersion) !== Number(payload.schemaVersion)
    || !/^[a-f0-9]{64}$/.test(String(descriptor.shardManifestHash || ""))
    || descriptorHash !== descriptor.descriptorHash)
  throw new Error("registry release descriptor is invalid");
if (String(descriptor.registryContentHash || "") !== hash)
  throw new Error("registry release descriptor pins a different content hash");
if (canonical(descriptor.provenance || {}) !== canonical(payload.provenance || {}))
  throw new Error("registry release descriptor pins different provenance");
if (Number(descriptor.recipientCount) !== Object.keys(payload.recipients || {}).length
    || Number(descriptor.ineligibleCount) !== Object.keys(payload.ineligible || {}).length)
  throw new Error("registry release descriptor pins different row counts");
for (const key of ["identityManifestHash", "identityLinksSha256",
                   "contactsSha256", "actSource", "actSourceSha256"])
  if (!String((payload.provenance || {})[key] || ""))
    throw new Error("registry provenance is missing " + key);
const actLookup = JSON.parse(fs.readFileSync(process.env.ACT_LOOKUP_PATH, "utf8"));
if (String(actLookup.identity_manifest_hash || "") !==
    String(payload.provenance.identityManifestHash || ""))
  throw new Error("registry and approved Act lookup use different identity manifests");
if (String(actLookup.act_source || "") !== String(payload.provenance.actSource || ""))
  throw new Error("registry and approved Act lookup use different Act snapshots");
if (String(actLookup.act_source_sha256 || "") !==
    String(payload.provenance.actSourceSha256 || ""))
  throw new Error("registry and approved Act lookup use different Act source bytes");
if (Object.keys(payload.recipients || {}).length < 1000)
  throw new Error("registry recipient count is implausibly small");
NODE
}

validate_package_files() {
  local tree=$1
  local allow_generated_stub=${2:-false}
  local candidate relative

  candidate=$(find "$tree" \
    \( -path "$tree/node_modules" -o -path "$tree/test" -o \
       -path "$tree/tools" -o -path "$tree/.git" -o \
       -path "$tree/.vscode" \) -prune -o \
    -type l -print -quit)
  if test -n "$candidate"; then
    relative=${candidate#"$tree"/}
    die "symbolic links are not allowed in the API package: $relative"
  fi

  while IFS= read -r candidate; do
    if test "$allow_generated_stub" = "true" \
       && test "$candidate" = "$tree/local.settings.json"; then
      continue
    fi
    relative=${candidate#"$tree"/}
    die "secret, backup, key, or nested archive file is not allowed: $relative"
  done < <(find "$tree" \
    \( -path "$tree/node_modules" -o -path "$tree/test" -o \
       -path "$tree/tools" -o -path "$tree/.git" -o \
       -path "$tree/.vscode" \) -prune -o -type f \
    \( -iname '.env' -o -iname '.env.*' -o -iname '.git-credentials' -o \
       -iname '.netrc' -o -iname '.npmrc' -o \
       -iname 'local.settings.json' -o -iname 'local.settings.*.json' -o \
       -iname 'secrets.json' -o -iname 'secrets.yaml' -o \
       -iname 'secrets.yml' -o \
       -iname '*.bak' -o -iname '*.backup' -o -iname '*.old' -o \
       -iname '*.orig' -o -iname '*~' -o -iname '*.zip' -o \
       -iname '*.tgz' -o -iname '*.tar' -o -iname '*.tar.gz' -o \
       -iname '*.tar.bz2' -o -iname '*.tar.xz' -o \
       -iname '*.7z' -o -iname '*.rar' -o -iname '*.swo' -o \
       -iname '*.swp' -o -iname '*.cer' -o -iname '*.crt' -o \
       -iname '*.der' -o -iname '*.key' -o -iname '*.pem' -o \
       -iname '*.pfx' -o -iname '*.p12' -o -iname '*.pkcs12' -o \
       -iname '*.ppk' -o -iname '*.jks' -o -iname '*.keystore' -o \
       -iname '*.kdbx' -o -iname '*.publishsettings' -o \
       -iname '*.secrets.json' -o -iname 'credentials.json' -o \
       -iname 'id_rsa' -o -iname 'id_dsa' -o -iname 'id_ecdsa' -o \
       -iname 'id_ed25519' \) -print)
}

validate_tree() {
  local tree=$1
  validate_package_files "$tree" true
  API_DEPLOY_TREE="$tree" node <<'NODE'
const fs = require("node:fs");
const path = require("node:path");
const root = process.env.API_DEPLOY_TREE;
const fail = (message) => { throw new Error(message); };
const load = (name) => {
  const file = path.join(root, "shared", name);
  if (!fs.existsSync(file) || fs.statSync(file).size === 0)
    fail(name + " is missing or empty");
  try { return JSON.parse(fs.readFileSync(file, "utf8")); }
  catch (error) { fail(name + " is not valid JSON: " + error.message); }
};
const date = (value, label) => {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T/.test(value)
      || !Number.isFinite(Date.parse(value))) fail(label + " has no valid timestamp");
};
const mapping = (value, label, minimum) => {
  if (!value || Array.isArray(value) || typeof value !== "object")
    fail(label + " is not an object mapping");
  const entries = Object.entries(value);
  if (entries.length < minimum)
    fail(label + " has only " + entries.length + " rows; expected at least " + minimum);
  if (entries.some(([key, item]) => !String(key).trim()
      || typeof item !== "string" || !item.trim()))
    fail(label + " contains an empty or non-string mapping");
};
for (const name of ["host.json", "package.json", "package-lock.json",
                    "release.json", "local.settings.json"])
  if (!fs.existsSync(path.join(root, name))) fail(name + " is missing");
const settings = JSON.parse(fs.readFileSync(path.join(root, "local.settings.json"), "utf8"));
const settingKeys = Object.keys(settings.Values || {}).sort();
if (settings.IsEncrypted !== false
    || JSON.stringify(settingKeys) !== JSON.stringify(["AzureWebJobsStorage", "FUNCTIONS_WORKER_RUNTIME"])
    || settings.Values.FUNCTIONS_WORKER_RUNTIME !== "node"
    || settings.Values.AzureWebJobsStorage !== "")
  fail("local.settings.json is not the generated non-secret runtime stub");
const contacts = load("act_contacts.json");
date(contacts.built_utc, "act_contacts.json");
mapping(contacts.contacts, "act_contacts.json contacts", 1000);
const mail = load("act_mail_codes.json");
date(mail.built_utc, "act_mail_codes.json");
mapping(mail.addresses, "act_mail_codes.json addresses", 1000);
mapping(mail.crds, "act_mail_codes.json approved crds", 100);
for (const crd of Object.keys(mail.crds))
  if (!contacts.contacts[crd])
    fail("act_mail_codes.json contains a CRD outside the approved Act lookup: " + crd);
const descriptor = load("approved-recipient-release.json");
const descriptorCore = { schemaVersion: descriptor.schemaVersion,
  registrySchemaVersion: descriptor.registrySchemaVersion,
  registryContentHash: descriptor.registryContentHash,
  recipientCount: descriptor.recipientCount,
  ineligibleCount: descriptor.ineligibleCount,
  provenance: descriptor.provenance || {},
  shardManifestHash: descriptor.shardManifestHash };
const crypto = require("node:crypto");
const canonical = (value) => Array.isArray(value)
  ? `[${value.map(canonical).join(",")}]`
  : value && typeof value === "object"
    ? `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`
    : JSON.stringify(value);
const descriptorHash = crypto.createHash("sha256")
  .update(canonical(descriptorCore), "utf8").digest("hex");
if (Number(descriptor.schemaVersion) !== 1
    || Number(descriptor.registrySchemaVersion) !== 1
    || !/^[a-f0-9]{64}$/.test(String(descriptor.registryContentHash || ""))
    || !/^[a-f0-9]{64}$/.test(String(descriptor.shardManifestHash || ""))
    || descriptorHash !== descriptor.descriptorHash)
  fail("approved-recipient-release.json is invalid");
for (const key of ["identityManifestHash", "identityLinksSha256",
                   "contactsSha256", "actSource", "actSourceSha256"])
  if (!String((descriptor.provenance || {})[key] || ""))
    fail("approved-recipient-release.json provenance is missing " + key);
const release = JSON.parse(fs.readFileSync(path.join(root, "release.json"), "utf8"));
if (!/^[A-Za-z0-9._-]{1,128}$/.test(String(release.id || "")))
  fail("release.json has an invalid id");
if (!/^[0-9a-f]{40}$/.test(String(release.commit || "")))
  fail("release.json has an invalid commit");
date(release.builtUtc, "release.json");
NODE
  for excluded in node_modules test tools .git .vscode; do
    test ! -e "$tree/$excluded" || die "excluded path entered package: $excluded"
  done
}

run_api_tests() {
  local tree=$1
  local expected_count=$2
  local test_dir="$tree/test"
  local sentinel="$test_dir/deployment-package-guard.test.js"
  local candidate
  local -a test_files=()

  [[ "$expected_count" =~ ^[1-9][0-9]*$ ]] \
    || die "expected API test file count is invalid: $expected_count"
  test -d "$test_dir" || die "API test directory is missing: $test_dir"
  test -f "$sentinel" \
    || die "API test sentinel is missing: ${sentinel#"$tree"/}"
  while IFS= read -r -d '' candidate; do
    test_files+=("$candidate")
  done < <(find "$test_dir" -type f -name '*.test.js' -print0 | sort -z)
  test "${#test_files[@]}" -eq "$expected_count" \
    || die "found ${#test_files[@]} API test files; expected $expected_count"

  echo "[*] running ${#test_files[@]} explicit API test files from local temp cwd"
  (
    # npm.cmd cannot preserve a UNC current directory and may silently fall back
    # to a Windows system directory. Invoke Node directly with explicit files.
    cd "$TEMP_BASE"
    unset NODE_TEST_CONTEXT
    API_PACKAGE_TEST_ROOT="$tree" REPOSITORY_TEST_ROOT="$ROOT" node --test "${test_files[@]}"
  )
}

verify_archive() {
  local archive=$1
  local expected=${2:-}
  test -f "$archive" || die "archive not found: $archive"
  if test -n "$expected" && test -f "$expected"; then
    expected=$(awk 'NR == 1 { print $1 }' "$expected")
  fi
  local actual
  actual=$(sha256sum "$archive" | awk '{print $1}')
  test -z "$expected" || test "$actual" = "$expected" \
    || die "SHA-256 mismatch: expected $expected, got $actual"
  while IFS= read -r member; do
    case "/$member/" in *"/../"*) die "unsafe parent path in archive: $member" ;; esac
    case "$member" in /*|*\\*) die "unsafe absolute or Windows path in archive: $member" ;; esac
  done < <(tar -tzf "$archive")
  while read -r mode _; do
    case "${mode:0:1}" in
      -|d) ;;
      *) die "archive contains a link or special filesystem entry" ;;
    esac
  done < <(tar -tvzf "$archive")
  local verify_dir
  if test -z "$TEMP_ROOT"; then
    TEMP_ROOT=$(mktemp -d "$TEMP_BASE/api-build.XXXXXX")
  fi
  verify_dir="$TEMP_ROOT/verify"
  test ! -e "$verify_dir" || die "verification directory already exists: $verify_dir"
  mkdir -p "$verify_dir"
  tar -xzf "$archive" -C "$verify_dir"
  validate_tree "$verify_dir"
  echo "[*] verified $archive"
  echo "[*] sha256 $actual"
}

if test "${1:-}" = "--verify"; then
  test $# -ge 2 || die "usage: $0 --verify <api.tgz> [expected-sha-or-file]"
  verify_archive "$2" "${3:-}"
  exit 0
fi
if test "${1:-}" = "--check-source"; then
  test $# -eq 2 || die "usage: $0 --check-source <api-directory>"
  validate_package_files "$2" false
  echo "[*] source package boundary is clean"
  exit 0
fi
if test "${1:-}" = "--run-tests"; then
  test $# -eq 3 || die "usage: $0 --run-tests <api-directory> <expected-file-count>"
  run_api_tests "$2" "$3"
  exit 0
fi
test $# -eq 0 || die "usage: $0 [--verify <api.tgz> [expected-sha-or-file] | --check-source <api-directory> | --run-tests <api-directory> <expected-file-count>]"

validate_recipient_registry_source

for command in node git tar sha256sum mktemp find sort diff; do
  command -v "$command" >/dev/null || die "required command is missing: $command"
done
NODE_VERSION=$(node -p 'process.versions.node')
NODE_MAJOR=${NODE_VERSION%%.*}
NODE_MINOR=${NODE_VERSION#*.}
NODE_MINOR=${NODE_MINOR%%.*}
if test "$NODE_MAJOR" -lt 22 || { test "$NODE_MAJOR" -eq 22 && test "$NODE_MINOR" -lt 12; }; then
  die "Node 22.12 or newer is required; found $NODE_VERSION"
fi
test -d "$API/node_modules" \
  || die "api/node_modules is missing; run 'npm ci --prefix api' before packaging"
validate_package_files "$API" false

COMMIT=$(git -C "$ROOT" rev-parse HEAD)
test "${#COMMIT}" -eq 40 || die "could not resolve a full Git commit"
DIRTY=false
if test -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)"; then
  DIRTY=true
  test "${ALLOW_DIRTY_BUILD:-0}" = "1" \
    || die "tracked or untracked source files are uncommitted; commit them or set ALLOW_DIRTY_BUILD=1 for an explicitly dirty test package"
fi
BUILT_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RELEASE_ID="${COMMIT:0:12}-$STAMP"
WORKFLOW_RUN="${RELEASE_RUN_ID:-manual}"

TEMP_ROOT=$(mktemp -d "$TEMP_BASE/api-build.XXXXXX")
STAGE_DIR="$TEMP_ROOT/stage"
mkdir -p "$STAGE_DIR"
tar -cf - -C "$API" \
  --exclude='./node_modules' --exclude='./test' --exclude='./tools' \
  --exclude='./.git' --exclude='./.vscode' --exclude='./local.settings.json' \
  --exclude='*.log' --exclude='*.tmp' . | tar -xf - -C "$STAGE_DIR"
cat > "$STAGE_DIR/local.settings.json" <<'JSON'
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "node",
    "AzureWebJobsStorage": ""
  }
}
JSON
RELEASE_FILE="$STAGE_DIR/release.json"
RELEASE_ID="$RELEASE_ID" RELEASE_COMMIT="$COMMIT" RELEASE_BUILT_UTC="$BUILT_UTC" \
RELEASE_WORKFLOW_RUN="$WORKFLOW_RUN" RELEASE_DIRTY="$DIRTY" \
node -e 'const fs=require("node:fs"); fs.writeFileSync(process.argv[1], JSON.stringify({id:process.env.RELEASE_ID,commit:process.env.RELEASE_COMMIT,builtUtc:process.env.RELEASE_BUILT_UTC,workflowRun:process.env.RELEASE_WORKFLOW_RUN,dirty:process.env.RELEASE_DIRTY==="true"},null,2)+"\n")' \
  "$RELEASE_FILE"

validate_tree "$STAGE_DIR"
TEST_ROOT="$TEMP_ROOT/test-workspace"
TEST_API="$TEST_ROOT/api"
mkdir -p "$TEST_API/test" "$TEST_ROOT/webapp"
cp -R "$STAGE_DIR/." "$TEST_API/"
cp -R "$API/test/." "$TEST_API/test/"
# UI contract tests inspect both clients. Keep the isolated test workspace
# explicit so a missing production dependency fails here, before packaging.
cp "$ROOT/webapp/app.js" "$ROOT/webapp/dial.js" "$ROOT/webapp/email.js" \
  "$ROOT/webapp/email.css" "$ROOT/webapp/field.js" "$ROOT/webapp/field.html" \
  "$TEST_ROOT/webapp/"
API_PACKAGE_SOURCE_ROOT="$ROOT" NODE_PATH="$API/node_modules" \
  run_api_tests "$TEST_API" "$EXPECTED_API_TEST_FILE_COUNT"
diff -r -q --exclude=test "$STAGE_DIR" "$TEST_API" >/dev/null \
  || die "API tests modified the staged production tree"
mkdir -p "$DIST"
ARCHIVE="$DIST/api-$RELEASE_ID.tgz"
HASH_FILE="$ARCHIVE.sha256"
test ! -e "$ARCHIVE" || die "immutable artifact already exists: $ARCHIVE"
tar -czf "$ARCHIVE" -C "$STAGE_DIR" .
HASH=$(sha256sum "$ARCHIVE" | awk '{print $1}')
printf '%s  %s\n' "$HASH" "$(basename "$ARCHIVE")" > "$HASH_FILE"
cp -f "$ARCHIVE" "$DIST/api.tgz"
printf '%s  api.tgz\n' "$HASH" > "$DIST/api.tgz.sha256"
verify_archive "$ARCHIVE" "$HASH"
echo "[*] release $RELEASE_ID  commit $COMMIT  dirty=$DIRTY"
SHARD_MANIFEST="$ROOT/data/identity/approved_recipients_manifest.json"
REGISTRY_HASH=$(node -p "require(process.argv[1]).registryContentHash" "$SHARD_MANIFEST")
SHARD_COUNT=$(node -p "Object.keys(require(process.argv[1]).shards || {}).length" "$SHARD_MANIFEST")
echo "[*] upload $DIST/api.tgz and retain $HASH before publishing"
echo
echo "[!] THIS PACKAGE IS HALF A RELEASE. It pins an approved-recipient shard set"
echo "    that is NOT inside the archive, and the emailer refuses to address mail"
echo "    until every matching immutable blob is in storage:"
echo
echo "      manifest  ${SHARD_MANIFEST#"$ROOT"/}"
echo "      shards    $SHARD_COUNT"
echo "      prefix    lookups/approved_recipients/releases/$REGISTRY_HASH/"
echo
echo "    Upload the shards first and manifest last, deploy static assets, then"
echo "    publish this Function artifact. The legacy monolith stays untouched."
