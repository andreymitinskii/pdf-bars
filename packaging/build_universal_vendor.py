from __future__ import annotations

import shutil
import sys
from pathlib import Path
from zipfile import ZipFile


PYTHON_TAGS = ("cp38", "cp311")


def select_wheels(wheelhouse: Path) -> tuple[list[Path], dict[str, Path]]:
    wheels = sorted(wheelhouse.glob("*.whl"))
    common = [
        wheel
        for wheel in wheels
        if not wheel.name.lower().startswith("pillow-")
        and ("-py3-none-any.whl" in wheel.name.lower() or "-abi3-" in wheel.name.lower())
    ]
    pillow = {}
    for tag in PYTHON_TAGS:
        matches = [wheel for wheel in wheels if wheel.name.lower().startswith("pillow-") and f"-{tag}-{tag}-" in wheel.name.lower()]
        if len(matches) != 1:
            raise RuntimeError(f"Ожидался один wheel Pillow для {tag}, найдено: {len(matches)}")
        pillow[tag] = matches[0]

    required = ("pymupdf-", "pypdf-", "tkinterdnd2-", "typing_extensions-")
    names = [wheel.name.lower() for wheel in common]
    missing = [prefix for prefix in required if not any(name.startswith(prefix) for name in names)]
    if missing:
        raise RuntimeError("Не найдены общие зависимости: " + ", ".join(missing))
    return common, pillow


def extract(paths: list[Path], destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for wheel in paths:
        with ZipFile(wheel) as archive:
            archive.extractall(destination)


def build_vendor(wheelhouse: Path, root: Path) -> None:
    common, pillow = select_wheels(wheelhouse)
    extract(common, root / "vendor-common")
    extract([pillow["cp38"]], root / "vendor-py38")
    extract([pillow["cp311"]], root / "vendor-py311")

    tkdnd = root / "vendor-common" / "tkinterdnd2" / "tkdnd"
    if tkdnd.exists():
        for child in tkdnd.iterdir():
            if child.is_dir() and child.name != "linux-x64":
                shutil.rmtree(child)

    print("Common: " + ", ".join(wheel.name for wheel in common))
    print("Python 3.8: " + pillow["cp38"].name)
    print("Python 3.11: " + pillow["cp311"].name)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: build_universal_vendor.py WHEELHOUSE APP_ROOT", file=sys.stderr)
        return 2
    build_vendor(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
