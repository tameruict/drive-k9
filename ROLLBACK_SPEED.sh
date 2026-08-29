#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$ROOT}"
TARGET="$(cd "$TARGET" && pwd)"

case "$TARGET" in
  "$ROOT"|"$ROOT"/*) ;;
  *) echo "ROLLBACK ERROR: target must be inside $ROOT" >&2; exit 2 ;;
esac

cp "$ROOT/sangsang_reupload.speed.baseline.py" "$TARGET/sangsang_reupload.py"
cp "$ROOT/sangsang_dgnl_bca.speed.baseline.yml" "$TARGET/.github/workflows/sangsang_dgnl_bca.yml"
echo "ROLLBACK PASS: restored=$TARGET"
sha256sum "$TARGET/sangsang_reupload.py" "$TARGET/.github/workflows/sangsang_dgnl_bca.yml"
