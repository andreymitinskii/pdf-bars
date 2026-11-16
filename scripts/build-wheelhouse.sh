#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WHEELHOUSE="$ROOT_DIR/packaging/wheelhouse"
PYTHON="${PYTHON:-python3}"

mkdir -p "$WHEELHOUSE"
"$PYTHON" -m pip download \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 38 \
  --implementation cp \
  --abi cp38 \
  --dest "$WHEELHOUSE" \
  -r "$ROOT_DIR/requirements.txt"

# RED OS 8 uses CPython 3.11. Other dependencies are pure Python or ABI3,
# so Pillow is the only additional interpreter-specific wheel required.
"$PYTHON" -m pip download \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 311 \
  --implementation cp \
  --abi cp311 \
  --dest "$WHEELHOUSE" \
  "Pillow==10.4.0"

echo "$WHEELHOUSE"
