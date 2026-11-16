#!/usr/bin/env sh
set -eu

APP_NAME="pdf-bars"
VERSION="1.7.1"
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_ROOT="${BUILD_ROOT:-$HOME/rpmbuild}"

[ -d "$ROOT_DIR/packaging/wheelhouse" ] || {
  echo "Run sh scripts/build-wheelhouse.sh before building the offline RPM." >&2
  exit 1
}

command -v rpmbuild >/dev/null 2>&1 || {
  echo "Не найден rpmbuild. Установите rpm-build." >&2
  exit 1
}

mkdir -p "$BUILD_ROOT/SOURCES" "$BUILD_ROOT/SPECS" "$BUILD_ROOT/RPMS" "$BUILD_ROOT/SRPMS" "$BUILD_ROOT/BUILD"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/${APP_NAME}-${VERSION}"
cp -R "$ROOT_DIR/src" "$ROOT_DIR/packaging" "$ROOT_DIR/README.md" "$ROOT_DIR/LICENSE" "$ROOT_DIR/THIRD_PARTY_NOTICES.md" "$ROOT_DIR/requirements.txt" "$TMP_DIR/${APP_NAME}-${VERSION}/"
mkdir -p "$TMP_DIR/${APP_NAME}-${VERSION}/docs"
cp "$ROOT_DIR/docs/PDF_Bars_User_Guide.pdf" "$TMP_DIR/${APP_NAME}-${VERSION}/docs/"
cp "$ROOT_DIR/docs/PDF_Bars_User_Guide_en.pdf" "$TMP_DIR/${APP_NAME}-${VERSION}/docs/"
find "$TMP_DIR/${APP_NAME}-${VERSION}" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$TMP_DIR/${APP_NAME}-${VERSION}" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
tar -C "$TMP_DIR" -czf "$BUILD_ROOT/SOURCES/${APP_NAME}-${VERSION}.tar.gz" "${APP_NAME}-${VERSION}"
cp "$ROOT_DIR/packaging/${APP_NAME}.spec" "$BUILD_ROOT/SPECS/"

rpmbuild --define "_topdir $BUILD_ROOT" -ba "$BUILD_ROOT/SPECS/${APP_NAME}.spec"
find "$BUILD_ROOT/RPMS" -type f -name "${APP_NAME}-${VERSION}-*.rpm" -print
