#!/bin/sh
# Build the SOAR app TAR: releases/soar_form_service_v<version>.tar with root directory soar_form_service/.
# Usage: scripts/package.sh [output_dir]     (default: releases/)
set -eu
APP=soar_form_service
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['app_version'])" "$ROOT/$APP.json")
OUT=${1:-$ROOT/releases}
mkdir -p "$OUT"
TAR="$OUT/${APP}_v$VERSION.tar"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir "$STAGE/$APP"
for f in __init__.py "$APP.json" "${APP}_connector.py" "${APP}_utils.py" "$APP.svg" "${APP}_dark.svg" README.md; do
  cp "$ROOT/$f" "$STAGE/$APP/"
done
COPYFILE_DISABLE=1 tar -cf "$TAR" -C "$STAGE" "$APP"
echo "Built $TAR"
tar -tf "$TAR"
