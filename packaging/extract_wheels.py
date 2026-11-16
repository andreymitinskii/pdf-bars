from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile


def wheel_is_compatible(
    path: Path,
    runtime: tuple[int, int] | None = None,
    implementation: str | None = None,
) -> bool:
    name = path.name.lower()
    if "-py3-none-any.whl" in name:
        return True

    runtime = runtime or (sys.version_info.major, sys.version_info.minor)
    implementation = implementation or sys.implementation.name
    exact_tag = f"cp{runtime[0]}{runtime[1]}"
    if f"-{exact_tag}-{exact_tag}-" in name:
        return True

    abi3_match = re.search(r"-cp(\d)(\d+)-abi3-", name)
    if abi3_match:
        minimum = (int(abi3_match.group(1)), int(abi3_match.group(2)))
        return implementation == "cpython" and runtime >= minimum
    return False


def extract_wheels(wheelhouse: Path, vendor: Path) -> list[Path]:
    vendor.mkdir(parents=True, exist_ok=True)
    selected = [wheel for wheel in sorted(wheelhouse.glob("*.whl")) if wheel_is_compatible(wheel)]
    if not selected:
        raise RuntimeError(f"Не найдены зависимости для Python {sys.version_info.major}.{sys.version_info.minor}")

    for wheel in selected:
        with ZipFile(wheel) as archive:
            archive.extractall(vendor)

    tkdnd = vendor / "tkinterdnd2" / "tkdnd"
    if tkdnd.exists():
        for child in tkdnd.iterdir():
            if child.is_dir() and child.name != "linux-x64":
                shutil.rmtree(child)
    return selected


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: extract_wheels.py WHEELHOUSE VENDOR", file=sys.stderr)
        return 2
    selected = extract_wheels(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Python {sys.version_info.major}.{sys.version_info.minor}: " + ", ".join(path.name for path in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
