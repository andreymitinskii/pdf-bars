#!/usr/bin/env sh
set -eu

HOST="${1:-}"
if [ -z "$HOST" ]; then
  echo "Usage: sh scripts/verify-redos-ssh.sh user@host" >&2
  exit 2
fi

APP_NAME="pdf-bars"
VERSION="1.2"
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ARCHIVE="$ROOT_DIR/dist/${APP_NAME}-${VERSION}.tar.gz"

[ -f "$ARCHIVE" ] || sh "$ROOT_DIR/scripts/build-portable-tar.sh"

REMOTE_DIR="/tmp/${APP_NAME}-verify"
ssh "$HOST" "rm -rf '$REMOTE_DIR' && mkdir -p '$REMOTE_DIR'"
scp "$ARCHIVE" "$HOST:$REMOTE_DIR/"
ssh "$HOST" "cd '$REMOTE_DIR' && tar -xzf '${APP_NAME}-${VERSION}.tar.gz' && cd '${APP_NAME}-${VERSION}' && sudo sh packaging/install.sh"
ssh "$HOST" "pdf-bars --version && pdf-bars-selfcheck && PY_TAG=\$(python3 -c 'import sys; print(f\"py{sys.version_info.major}{sys.version_info.minor}\")') && PYTHONPATH=/opt/pdf-bars/vendor-\$PY_TAG:/opt/pdf-bars/vendor-common:/opt/pdf-bars/src python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from PIL import Image
from pypdf import PdfReader, PdfWriter
from redos_pdf_combiner.converters import find_r7_converter, images_to_pdf, pdf_to_images
from redos_pdf_combiner.core import compress_pdf_preset, merge_pdfs

with TemporaryDirectory() as td:
    td = Path(td)
    img = td / 'sample.webp'
    Image.new('RGB', (200, 120), 'white').save(img, 'WEBP')
    pdf = images_to_pdf([img], td / 'image.pdf')
    assert len(PdfReader(str(pdf)).pages) == 1
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    base = td / 'base.pdf'
    with base.open('wb') as handle:
        writer.write(handle)
    merged = merge_pdfs([base, pdf], td / 'merged.pdf')
    assert len(PdfReader(str(merged)).pages) == 2
    images = pdf_to_images(merged, td / 'out-images', 'jpg', 100)
    assert len(images) == 2
    compressed = compress_pdf_preset(merged, td / 'compressed.pdf', 'minimum')
    assert compressed.exists()
    converter = find_r7_converter()
print('REDOS_VERIFY_OK', 'R7=' + (str(converter) if converter else 'not-installed'))
PY"
ssh "$HOST" "grep -E '^Name(\\[ru(_RU)?\\])?=pdf bars$' /usr/share/applications/pdf-bars.desktop"
