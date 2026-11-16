#!/usr/bin/env sh
set -eu

APP_NAME="pdf-bars"
PREFIX="${PREFIX:-/opt/${APP_NAME}}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
DESKTOP_DIR="${DESKTOP_DIR:-/usr/local/share/applications}"
ICON_ROOT="${ICON_ROOT:-/usr/local/share/icons}"
PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Не найден python3. Установите Python 3.8 или новее." >&2
  exit 1
fi

if ! "$PYTHON" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
then
  echo "Нужен Python 3.8 или новее." >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
WHEELHOUSE="$PROJECT_DIR/packaging/wheelhouse"

if [ "$(id -u)" = "0" ] && command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-tkinter ghostscript
fi

mkdir -p "$PREFIX" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_ROOT"
cp -R "$PROJECT_DIR/src" "$PREFIX/"
cp "$PROJECT_DIR/README.md" "$PREFIX/"
cp "$PROJECT_DIR/requirements.txt" "$PREFIX/"
if [ -d "$PROJECT_DIR/docs" ]; then
  cp -R "$PROJECT_DIR/docs" "$PREFIX/"
fi
if [ -d "$PROJECT_DIR/packaging/icons/hicolor" ]; then
  cp -R "$PROJECT_DIR/packaging/icons/hicolor" "$ICON_ROOT/"
fi
rm -rf "$PREFIX/vendor" "$PREFIX/vendor-common" "$PREFIX/vendor-py38" "$PREFIX/vendor-py311"
if [ -d "$WHEELHOUSE" ]; then
  "$PYTHON" "$PROJECT_DIR/packaging/build_universal_vendor.py" "$WHEELHOUSE" "$PREFIX"
fi

cp "$PROJECT_DIR/packaging/pdf-bars-launcher.sh" "$PREFIX/pdf-bars-launch"
chmod +x "$PREFIX/pdf-bars-launch"

if ! REDOS_PDF_BARS_ROOT="$PREFIX" PYTHON="$PYTHON" "$PREFIX/pdf-bars-launch" redos_pdf_combiner.selfcheck >/dev/null 2>&1; then
  echo "Не удалось подключить зависимости. Проверьте python3-tkinter и packaging/wheelhouse." >&2
  exit 1
fi

cat > "$BIN_DIR/pdf-bars" <<EOF
#!/usr/bin/env sh
REDOS_PDF_BARS_ROOT="$PREFIX" PYTHON="$PYTHON" exec "$PREFIX/pdf-bars-launch" redos_pdf_combiner.cli "\$@"
EOF

cat > "$BIN_DIR/pdf-bars-gui" <<EOF
#!/usr/bin/env sh
REDOS_PDF_BARS_ROOT="$PREFIX" PYTHON="$PYTHON" exec "$PREFIX/pdf-bars-launch" redos_pdf_combiner.launcher "\$@"
EOF

cat > "$BIN_DIR/pdf-bars-selfcheck" <<EOF
#!/usr/bin/env sh
REDOS_PDF_BARS_ROOT="$PREFIX" PYTHON="$PYTHON" exec "$PREFIX/pdf-bars-launch" redos_pdf_combiner.selfcheck "\$@"
EOF

chmod +x "$BIN_DIR/pdf-bars" "$BIN_DIR/pdf-bars-gui" "$BIN_DIR/pdf-bars-selfcheck"
cp "$PROJECT_DIR/packaging/pdf-bars.desktop" "$DESKTOP_DIR/pdf-bars.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q "$ICON_ROOT/hicolor" >/dev/null 2>&1 || true
fi

echo "Установлено."
echo "GUI: pdf-bars-gui"
echo "CLI: pdf-bars --help"
