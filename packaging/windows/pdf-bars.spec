# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files


project = Path(SPECPATH).parents[1]
source = project / "src"
assets = source / "redos_pdf_combiner" / "assets"

datas = [
    (str(assets), "redos_pdf_combiner/assets"),
    (str(project / "docs" / "PDF_Bars_User_Guide.pdf"), "docs"),
    (str(project / "docs" / "PDF_Bars_User_Guide_en.pdf"), "docs"),
]
datas += collect_data_files("tkinterdnd2")
datas += [(str(source / "redos_pdf_combiner" / "locales"), "redos_pdf_combiner/locales")]
datas += [(str(project / "LICENSE"), "."), (str(project / "THIRD_PARTY_NOTICES.md"), ".")]

a = Analysis(
    [str(project / "packaging" / "windows" / "pdf_bars_windows.py")],
    pathex=[str(source)],
    binaries=[],
    datas=datas,
    hiddenimports=["tkinterdnd2", "redos_pdf_combiner.gui"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pandas", "scipy", "torch", "matplotlib", "IPython"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pdf-bars",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project / "packaging" / "windows" / "pdf-bars.ico"),
    version=str(project / "packaging" / "windows" / "pdf-bars-version.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="pdf-bars",
)
