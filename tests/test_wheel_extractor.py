import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "extract_wheels.py"
SPEC = importlib.util.spec_from_file_location("pdf_bars_extract_wheels", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_wheel_selection_for_redos_python_versions():
    pure = Path("pypdf-5.9.0-py3-none-any.whl")
    pillow38 = Path("pillow-10.4.0-cp38-cp38-manylinux2014_x86_64.whl")
    pillow311 = Path("pillow-10.4.0-cp311-cp311-manylinux2014_x86_64.whl")
    pymupdf_abi3 = Path("PyMuPDF-1.24.11-cp38-abi3-manylinux2014_x86_64.whl")

    assert MODULE.wheel_is_compatible(pure, (3, 8), "cpython")
    assert MODULE.wheel_is_compatible(pure, (3, 11), "cpython")
    assert MODULE.wheel_is_compatible(pillow38, (3, 8), "cpython")
    assert not MODULE.wheel_is_compatible(pillow38, (3, 11), "cpython")
    assert MODULE.wheel_is_compatible(pillow311, (3, 11), "cpython")
    assert not MODULE.wheel_is_compatible(pillow311, (3, 8), "cpython")
    assert MODULE.wheel_is_compatible(pymupdf_abi3, (3, 8), "cpython")
    assert MODULE.wheel_is_compatible(pymupdf_abi3, (3, 11), "cpython")
    assert not MODULE.wheel_is_compatible(pymupdf_abi3, (3, 7), "cpython")
