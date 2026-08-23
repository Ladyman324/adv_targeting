#!/usr/bin/env bash
# Pack api/ into dist/api.tgz for `func azure functionapp publish`.
#
# Two things this does that a bare `tar -czf` does not:
#
#   1. tar, not Compress-Archive or ZipFile.CreateFromDirectory. Both of those
#      write Windows path separators into the archive, and the Linux unzip in
#      Cloud Shell then treats "shared\store.js" as a FILENAME rather than a
#      path -- producing a flat directory of oddly named files and a deploy that
#      cannot resolve a single require().
#
#   2. It injects a local.settings.json stub. Without one the Functions CLI
#      cannot identify the project ("Can't determine project language" /
#      "Worker runtime cannot be 'None'") and the publish stops before it
#      starts. The stub is written from a literal here, NEVER copied from disk:
#      a developer's real local.settings.json holds live connection strings, and
#      an archive that is handed around must not be able to carry them.
set -euo pipefail
cd "$(dirname "$0")/.."

STUB=api/local.settings.json
test -e "$STUB" && { echo "refusing to overwrite an existing $STUB" >&2; exit 1; }
cat > "$STUB" <<'JSON'
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "node",
    "AzureWebJobsStorage": ""
  }
}
JSON
trap 'rm -f "$STUB"' EXIT

mkdir -p dist
rm -f dist/api.tgz
tar -czf dist/api.tgz -C api --exclude=node_modules --exclude=test .

echo "[*] dist/api.tgz  $(stat -c%s dist/api.tgz) bytes"
echo "[*] sha256 $(sha256sum dist/api.tgz | cut -d' ' -f1)"
echo "[*] runtime stub inside: $(tar -xzOf dist/api.tgz ./local.settings.json | tr -d ' \n')"
