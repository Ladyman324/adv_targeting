#!/usr/bin/env bash
# Deprecated compatibility entry point. No implicit in-place data mutation.
# Use: scripts/rebuild_all.sh plan --mode act-only
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
echo "Use src/refresh_data.py. See docs/data_refresh.md. No sources are refreshed implicitly." >&2
exec "${REFRESH_PYTHON:-python}" "$ROOT/src/refresh_data.py" "$@"
