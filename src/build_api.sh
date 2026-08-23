#!/usr/bin/env bash
# Build a fresh, provenance-stamped api.tgz for the manual Cloud Shell deploy.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
API="$ROOT/api"
DIST="$ROOT/dist"
TEMP_BASE=$(cd "${TMPDIR:-/tmp}" && pwd -P)
TEMP_ROOT=""
STAGE_DIR=""

cleanup() {
  test -z "$TEMP_ROOT" && return
  case "$TEMP_ROOT" in
    "$TEMP_BASE"/api-build.*) rm -rf -- "$TEMP_ROOT" ;;
    *) echo "[!] refusing to remove unexpected temp path: $TEMP_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT
die() { echo "[!] $*" >&2; exit 1; }

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
mapping(mail.crds, "act_mail_codes.json crds", 1000);
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
test $# -eq 0 || die "usage: $0 [--verify <api.tgz> [expected-sha-or-file] | --check-source <api-directory>]"

for command in node npm git tar sha256sum mktemp find; do
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
npm test --prefix "$API"

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
echo "[*] upload $DIST/api.tgz and retain $HASH before publishing"
