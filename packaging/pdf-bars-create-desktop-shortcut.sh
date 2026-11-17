#!/usr/bin/env sh
set -eu
APP_ROOT="${REDOS_PDF_BARS_ROOT:-/usr/share/pdf-bars}"
exec "${PYTHON:-python3}" "$APP_ROOT/src/redos_pdf_combiner/desktop_shortcut.py" "$@"
