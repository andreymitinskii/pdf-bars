#!/usr/bin/env sh
set -eu

APP_ROOT="${REDOS_PDF_BARS_ROOT:-/usr/share/pdf-bars}"
PYTHON="${PYTHON:-python3}"
MODULE="${1:-}"

if [ -z "$MODULE" ]; then
  echo "Не указан модуль pdf bars" >&2
  exit 2
fi
shift

PYTHON_VERSION=$(
  "$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)
case "$PYTHON_VERSION" in
  3.8) VERSION_VENDOR="$APP_ROOT/vendor-py38" ;;
  3.11) VERSION_VENDOR="$APP_ROOT/vendor-py311" ;;
  *)
    echo "pdf bars поддерживает системный Python 3.8 и 3.11. Обнаружен Python $PYTHON_VERSION." >&2
    exit 1
    ;;
esac

if [ ! -d "$VERSION_VENDOR" ] || [ ! -d "$APP_ROOT/vendor-common" ]; then
  echo "Не найдены встроенные зависимости pdf bars для Python $PYTHON_VERSION" >&2
  exit 1
fi

PYTHONPATH="$VERSION_VENDOR:$APP_ROOT/vendor-common:$APP_ROOT/src:${PYTHONPATH:-}" \
  exec "$PYTHON" -m "$MODULE" "$@"
