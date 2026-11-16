#!/usr/bin/env sh
set -eu

APP_NAME="pdf-bars"
VERSION="1.7"
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT_DIR="$ROOT_DIR/dist"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$OUT_DIR" "$TMP_DIR/${APP_NAME}-${VERSION}"
cp -R "$ROOT_DIR/src" "$ROOT_DIR/packaging" "$ROOT_DIR/scripts" "$ROOT_DIR/tests" "$ROOT_DIR/README.md" "$ROOT_DIR/requirements.txt" "$ROOT_DIR/pyproject.toml" "$ROOT_DIR/pytest.ini" "$TMP_DIR/${APP_NAME}-${VERSION}/"
mkdir -p "$TMP_DIR/${APP_NAME}-${VERSION}/docs"
cp "$ROOT_DIR/docs/PDF_Bars_User_Guide.pdf" "$TMP_DIR/${APP_NAME}-${VERSION}/docs/"
find "$TMP_DIR/${APP_NAME}-${VERSION}" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$TMP_DIR/${APP_NAME}-${VERSION}" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
tar -C "$TMP_DIR" -czf "$OUT_DIR/${APP_NAME}-${VERSION}.tar.gz" "${APP_NAME}-${VERSION}"
echo "$OUT_DIR/${APP_NAME}-${VERSION}.tar.gz"
