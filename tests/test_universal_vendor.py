import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "build_universal_vendor.py"
SPEC = importlib.util.spec_from_file_location("pdf_bars_universal_vendor", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_universal_vendor_selects_both_pillow_versions(tmp_path):
    names = (
        "pillow-10.4.0-cp38-cp38-manylinux2014_x86_64.whl",
        "pillow-10.4.0-cp311-cp311-manylinux2014_x86_64.whl",
        "PyMuPDF-1.24.11-cp38-abi3-manylinux2014_x86_64.whl",
        "pypdf-5.9.0-py3-none-any.whl",
        "tkinterdnd2-0.5.0-py3-none-any.whl",
        "typing_extensions-4.13.2-py3-none-any.whl",
    )
    for name in names:
        (tmp_path / name).touch()

    common, pillow = MODULE.select_wheels(tmp_path)

    assert {path.name for path in common} == set(names[2:])
    assert pillow["cp38"].name == names[0]
    assert pillow["cp311"].name == names[1]
