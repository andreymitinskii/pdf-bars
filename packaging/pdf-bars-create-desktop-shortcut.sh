#!/usr/bin/env sh
set -eu

APP_ENTRY="${PDF_BARS_APP_ENTRY:-/usr/share/applications/pdf-bars.desktop}"
MARKER_DIR="${XDG_CONFIG_HOME:-${HOME:?}/.config}/pdf-bars"
MARKER="$MARKER_DIR/desktop-shortcut-1.7"

if [ "${PDF_BARS_FORCE:-0}" != "1" ] && [ -f "$MARKER" ]; then
  exit 0
fi

if [ ! -f "$APP_ENTRY" ]; then
  exit 0
fi

desktop_dir=""
if command -v xdg-user-dir >/dev/null 2>&1; then
  desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null || true)
fi

case "$desktop_dir" in
  ""|"$HOME") desktop_dir="" ;;
esac

if [ -z "$desktop_dir" ]; then
  for candidate in "$HOME/Рабочий стол" "$HOME/Desktop"; do
    if [ -d "$candidate" ]; then
      desktop_dir="$candidate"
      break
    fi
  done
fi

if [ -z "$desktop_dir" ]; then
  case "${LANG:-}" in
    ru*) desktop_dir="$HOME/Рабочий стол" ;;
    *) desktop_dir="$HOME/Desktop" ;;
  esac
fi

mkdir -p "$desktop_dir"
shortcut="$desktop_dir/pdf bars.desktop"
temporary="$shortcut.tmp.$$"
cp "$APP_ENTRY" "$temporary"
chmod 0755 "$temporary"
mv -f "$temporary" "$shortcut"

if command -v gio >/dev/null 2>&1; then
  gio set "$shortcut" metadata::trusted true >/dev/null 2>&1 || true
fi

if [ "${PDF_BARS_CREATE_MARKER:-1}" != "0" ]; then
  mkdir -p "$MARKER_DIR"
  : > "$MARKER"
fi
