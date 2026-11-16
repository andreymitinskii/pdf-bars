from __future__ import annotations

import sys

from . import __version__
from .ocr import ocr_models_available, tessdata_path


def main() -> int:
    import fitz
    import pypdf
    import tkinter
    import tkinterdnd2
    import typing_extensions
    import PIL
    from PIL import _imaging

    print(f"pdf bars {__version__}")
    print(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    print(f"Pillow {PIL.__version__}: {_imaging.__file__}")
    print(f"PyMuPDF {fitz.VersionBind}")
    print(f"pypdf {pypdf.__version__}")
    print(f"Tk {tkinter.TkVersion}")
    print(f"TkDnD {tkinterdnd2.__file__}")
    print(f"typing_extensions {typing_extensions.__file__}")
    print(f"OCR rus+eng: {'OK' if ocr_models_available() else 'models missing'} ({tessdata_path()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
