#!/usr/bin/env bash
# The full data rebuild, in dependency order, failing loudly.
#
# WHY THIS EXISTS: the steps below have to run in this order and each one reads
# what the previous wrote. Run by hand, it is easy to skip one, or to run
# export_crm_review without its --tier/--out flags and quietly write a different
# file than the team reads.
#
# NEVER PIPE A STEP THROUGH grep. A traceback contains none of the words you
# would grep for, so `python step.py 2>&1 | grep summary` prints nothing, exits
# 0 from grep, and reads exactly like success -- which is how two crashed builds
# in a row got reported as "ran but had no effect". tail keeps the last lines
# whatever they are.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
FAILED=()

step() {
  local label=$1; shift
  echo ""
  echo "=============================================================="
  echo "[>] $label"
  echo "=============================================================="
  if ! "$@" 2>&1 | tail -12; then
    :
  fi
  # tail swallows the exit status, so ask for the real one.
  local rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    echo "[!] FAILED: $label (exit $rc)"
    FAILED+=("$label")
  fi
}

step "build_contacts        salutation + all CRM detail"   python src/build_contacts.py
step "build_name_index      the desk's display names"      python src/build_name_index.py
step "build_field_tiles     tiles + practice shards"       python src/build_field_tiles.py
step "build_act_assets      EIC book by advisor"           python src/build_act_assets.py
step "export_advisor_emails the CRD<->email lookup"        python src/export_advisor_emails.py
step "act_crosswalk         identity gate + review triage" python src/act_crosswalk.py
step "build_act_lookup      act_contacts.json"             python src/build_act_lookup.py
step "export_crm_review     the team's workbook"           python src/export_crm_review.py \
        --tier review --out data/output/crm_crd_review.xlsx
step "web_assets            gzip + version stamps"         python src/web_assets.py
step "audit                 every check"                   python src/audit.py

echo ""
echo "=============================================================="
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "[*] every step completed"
else
  echo "[!] ${#FAILED[@]} step(s) FAILED:"
  for f in "${FAILED[@]}"; do echo "      $f"; done
  exit 1
fi
