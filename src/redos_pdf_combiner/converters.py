from __future__ import annotations

from .i18n import tr

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Sequence
from .user_errors import input_path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
DOCX_SUFFIXES = {".docx", ".doc"}
PDF_IMAGE_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "tif": "TIFF", "tiff": "TIFF"}
ConversionProgress = Callable[[int, int, str], None]

R7_FONT_FALLBACKS = {
    "Times New Roman": ("Liberation Serif", "DejaVu Serif", "PT Serif"),
    "Arial": ("Liberation Sans", "DejaVu Sans", "PT Sans"),
    "Calibri": ("Carlito", "Liberation Sans", "DejaVu Sans", "PT Sans"),
    "Cambria": ("Caladea", "Liberation Serif", "DejaVu Serif", "PT Serif"),
    "Courier New": ("Liberation Mono", "DejaVu Sans Mono", "PT Mono"),
    "Tahoma": ("Liberation Sans", "DejaVu Sans", "PT Sans"),
    "Verdana": ("Liberation Sans", "DejaVu Sans", "PT Sans"),
}


def available_output_path(path: os.PathLike[str] | str) -> Path:
    """Return a non-existing path without overwriting an earlier result."""
    candidate = Path(path)
    if not candidate.exists():
        return candidate
    for number in range(2, 10000):
        numbered = candidate.with_name(f"{candidate.stem} ({number}){candidate.suffix}")
        if not numbered.exists():
            return numbered
    raise RuntimeError(tr(f"Не удалось подобрать свободное имя для файла: {candidate.name}"))


def images_to_pdf(
    images: Sequence[os.PathLike[str] | str],
    output: os.PathLike[str] | str,
    dpi: int = 150,
    background: str = "white",
) -> Path:
    """Create one PDF from images, including WEBP."""
    if not images:
        raise ValueError(tr("Нужно выбрать хотя бы одно изображение"))
    if dpi < 36 or dpi > 1200:
        raise ValueError(tr("DPI должен быть в диапазоне 36..1200"))

    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(tr("Не установлена зависимость Pillow. Установите пакет pillow.")) from exc

    output_path = Path(output)
    if output_path.suffix.lower() != ".pdf":
        raise ValueError(tr("Выходной файл должен иметь расширение .pdf"))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pages = []
    try:
        for image_path in images:
            path = input_path(image_path)
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                raise ValueError(tr(f"Неподдерживаемый формат изображения: {path}"))
            with Image.open(path) as original, ImageOps.exif_transpose(original) as image:
                pages.append(_image_to_rgb_page(image, background))
        first, rest = pages[0], pages[1:]
        first.save(str(output_path), "PDF", resolution=dpi, save_all=True, append_images=rest)
    finally:
        for page in pages:
            page.close()
    return output_path


def images_to_individual_pdfs(
    images: Sequence[os.PathLike[str] | str],
    output_dir: os.PathLike[str] | str,
    dpi: int = 150,
) -> List[Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    for image in images:
        output = out_dir / f"{Path(image).stem}.pdf"
        outputs.append(images_to_pdf([image], output, dpi=dpi))
    return outputs


def pdf_to_images(
    input_pdf: os.PathLike[str] | str,
    output_dir: os.PathLike[str] | str,
    image_format: str = "jpg",
    dpi: int = 150,
) -> List[Path]:
    """Render every PDF page to image files. Default output format is JPG."""
    pdf_path = Path(input_pdf)
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(tr("Нужно выбрать PDF-файл"))
    if not pdf_path.exists():
        raise FileNotFoundError(str(pdf_path))
    if dpi < 36 or dpi > 1200:
        raise ValueError(tr("DPI должен быть в диапазоне 36..1200"))

    fmt_key = image_format.lower().lstrip(".")
    if fmt_key == "jpeg":
        fmt_key = "jpg"
    if fmt_key not in PDF_IMAGE_FORMATS:
        raise ValueError(tr("Поддерживаются форматы: JPG, PNG, WEBP, TIFF"))

    try:
        import fitz
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(tr("Не установлены зависимости PyMuPDF/Pillow для преобразования PDF в изображения.")) from exc

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0
    outputs: List[Path] = []

    with fitz.open(str(pdf_path)) as doc:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            output = out_dir / f"{pdf_path.stem}_page_{index:03d}.{fmt_key}"
            save_kwargs = {"quality": 95, "optimize": True} if fmt_key == "jpg" else {}
            image.save(str(output), PDF_IMAGE_FORMATS[fmt_key], **save_kwargs)
            outputs.append(output)
            image.close()
    return outputs


def docx_to_pdf(
    documents: Sequence[os.PathLike[str] | str],
    output_dir: os.PathLike[str] | str,
    soffice_path: Optional[str] = None,
    progress: Optional[ConversionProgress] = None,
) -> List[Path]:
    if not documents:
        raise ValueError(tr("Нужно выбрать хотя бы один DOCX/DOC"))
    inputs = _validate_documents(documents, DOCX_SUFFIXES)
    backends = []
    if sys.platform.startswith("win") and find_microsoft_word():
        backends.append(("Microsoft Word", lambda items, folder, report:
                         _convert_with_microsoft_word(items, folder, ".pdf", report)))
    r7_converter = find_r7_converter()
    if r7_converter:
        backends.append((tr("Р7 / ONLYOFFICE"), lambda items, folder, report:
                         _convert_with_r7(items, folder, ".pdf", r7_converter, report)))
    office = soffice_path or find_libreoffice_converter()
    if office:
        backend = ("LibreOffice / OpenOffice", lambda items, folder, report:
                   _convert_with_libreoffice(items, folder, office, report))
        if soffice_path:
            backends.insert(0, backend)
        else:
            backends.append(backend)
    if not backends:
        raise RuntimeError(
            tr("Не найден офисный конвертер DOCX/DOC в PDF. Установите Microsoft Word "
            "(Windows), Р7-Офис, ONLYOFFICE, LibreOffice или OpenOffice. "
            "Для Р7/ONLYOFFICE один раз откройте документ, чтобы подготовить базу шрифтов.")
        )
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for index, document in enumerate(inputs):
        failures = []
        for label, convert in backends:
            if progress:
                progress(index, len(inputs), f"{label}: {document.name}")
            try:
                # Isolate failed/partial exports, including a backend failure after
                # creating its output. Never expose those files as successful PDFs.
                with tempfile.TemporaryDirectory(prefix="pdf-bars-office-") as folder:
                    generated = convert([document], folder, None)[0]
                    destination = available_output_path(out_dir / f"{document.stem}.pdf")
                    shutil.move(str(generated), str(destination))
                outputs.append(destination)
                if progress:
                    progress(index + 1, len(inputs), tr(f"Готово: {document.name}"))
                break
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{label}: {exc}")
        else:
            raise RuntimeError(tr(f"Не удалось преобразовать {document.name}. ") + "\n".join(failures))
    return outputs


def pdf_to_docx(
    documents: Sequence[os.PathLike[str] | str],
    output_dir: os.PathLike[str] | str,
    converter_path: Optional[str] = None,
    progress: Optional[ConversionProgress] = None,
) -> List[Path]:
    if not documents:
        raise ValueError(tr("Нужно выбрать хотя бы один PDF"))
    inputs = _validate_documents(documents, {".pdf"})
    if sys.platform.startswith("win"):
        word = find_microsoft_word()
        if not word:
            raise RuntimeError(tr("Для преобразования PDF в DOCX требуется установленный Microsoft Word"))
        return _convert_with_microsoft_word(inputs, output_dir, ".docx", progress)
    converter = converter_path or find_r7_converter()
    if not converter:
        raise RuntimeError(tr("Для преобразования PDF в DOCX требуется установленный R7-Office"))
    return _convert_with_r7(inputs, output_dir, ".docx", converter, progress)


def _validate_documents(
    documents: Sequence[os.PathLike[str] | str],
    suffixes: set[str],
) -> List[Path]:
    inputs: List[Path] = []
    for document in documents:
        path = Path(document)
        if path.suffix.lower() not in suffixes:
            raise ValueError(tr(f"Неподдерживаемый формат документа: {path}"))
        if not path.exists():
            raise FileNotFoundError(str(path))
        inputs.append(path)
    return inputs


def _convert_with_r7(
    documents: Sequence[Path],
    output_dir: os.PathLike[str] | str,
    target_suffix: str,
    converter: str,
    progress: Optional[ConversionProgress] = None,
) -> List[Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    total = len(documents)
    r7_font_cache = find_r7_font_cache() if target_suffix == ".pdf" else None
    if target_suffix == ".pdf" and r7_font_cache is None:
        raise RuntimeError(
            tr("R7-Office ещё не подготовил базу шрифтов для пакетного экспорта. "
            "Один раз запустите R7-Office и откройте любой документ, затем повторите преобразование.")
        )
    with _r7_conversion_environment() as (environment, fallback_used), tempfile.TemporaryDirectory(
        prefix="pdf-bars-r7-docx-"
    ) as temporary_directory:
        for index, document in enumerate(documents, start=1):
            conversion_input = document
            font_replacements = {}
            if progress:
                replacements = ", ".join(f"{source} -> {target}" for source, target in font_replacements.items())
                detail = tr(f"; совместимые шрифты: {replacements}") if replacements else ""
                if fallback_used and not detail:
                    detail = tr("; включена системная подстановка шрифтов")
                progress(index - 1, total, tr(f"Преобразование: {document.name}{detail}"))
            output = available_output_path(out_dir / f"{document.stem}{target_suffix}")
            command = [converter, str(conversion_input), str(output)]
            if target_suffix == ".pdf" and r7_font_cache is not None:
                params = Path(temporary_directory) / f"params-{index:04d}.xml"
                _write_r7_pdf_task(params, conversion_input, output, converter, r7_font_cache)
                command = [converter, str(params)]
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                env=environment,
            )
            if completed.returncode != 0 or not output.exists():
                details = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(tr(f"R7-Office не смог преобразовать {document.name}. {details}"))
            outputs.append(output)
            if progress:
                progress(index, total, tr(f"Готово: {document.name}"))
    return outputs


def find_r7_font_cache() -> Path | None:
    """Locate the non-empty font database generated by the R7 desktop editor."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    candidates = (
        data_home / "r7-office" / "editors" / "data" / "fonts" / "AllFonts.js",
        data_home / "R7-Office" / "editors" / "data" / "fonts" / "AllFonts.js",
        data_home / "onlyoffice" / "desktopeditors" / "data" / "fonts" / "AllFonts.js",
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "R7-Office" / "editors" / "data" / "fonts" / "AllFonts.js",
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ONLYOFFICE" / "DesktopEditors" / "data" / "fonts" / "AllFonts.js",
    )
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def _write_r7_pdf_task(
    params_path: Path,
    source: Path,
    output: Path,
    converter: str,
    font_cache: Path,
) -> None:
    desktop_root = Path(converter).resolve().parent.parent
    theme_directory = desktop_root / "editors" / "sdkjs" / "slide" / "themes"
    root = ET.Element("TaskQueueDataConvert")
    for name, value in (
        ("m_sFileFrom", str(source.resolve())),
        ("m_sFileTo", str(output.resolve())),
        ("m_nFormatTo", "513"),
        ("m_sFontDir", str(font_cache.parent.resolve())),
        ("m_sAllFontsPath", str(font_cache.resolve())),
    ):
        ET.SubElement(root, name).text = value
    if theme_directory.is_dir():
        ET.SubElement(root, "m_sThemeDir").text = str(theme_directory)
    ET.SubElement(root, "m_bDontSaveAdditional").text = "true"
    ET.ElementTree(root).write(params_path, encoding="utf-8", xml_declaration=True)


def normalize_docx_fonts_for_r7(
    source: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
) -> dict[str, str]:
    """Replace missing Microsoft fonts in a temporary DOCX used by R7-Office.

    R7's internal converter can select an unrelated font for a missing family,
    which turns Cyrillic text into squares. Rewriting only font family names
    keeps the document structure intact and never modifies the user's source.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.suffix.lower() != ".docx":
        raise ValueError(tr("Подстановка шрифтов поддерживается только для DOCX"))

    replacements: dict[str, str] = {}
    for family, candidates in R7_FONT_FALLBACKS.items():
        if _font_family_installed(family):
            continue
        fallback = next((candidate for candidate in candidates if _font_family_installed(candidate)), None)
        if fallback:
            replacements[family] = fallback

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if not replacements:
        shutil.copy2(source_path, destination_path)
        return replacements

    editable_parts = {
        "word/document.xml",
        "word/styles.xml",
        "word/fontTable.xml",
        "word/theme/theme1.xml",
        "word/numbering.xml",
        "word/settings.xml",
    }
    try:
        with zipfile.ZipFile(source_path, "r") as archive, zipfile.ZipFile(
            destination_path, "w"
        ) as output:
            for info in archive.infolist():
                data = archive.read(info.filename)
                if info.filename in editable_parts:
                    text = data.decode("utf-8")
                    for family, fallback in replacements.items():
                        text = text.replace(family, fallback)
                    data = text.encode("utf-8")
                output.writestr(info, data)
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        destination_path.unlink(missing_ok=True)
        raise RuntimeError(tr(f"Не удалось подготовить совместимые шрифты для {source_path.name}")) from exc
    return replacements


def _convert_with_microsoft_word(
    documents: Sequence[Path],
    output_dir: os.PathLike[str] | str,
    target_suffix: str,
    progress: Optional[ConversionProgress] = None,
) -> List[Path]:
    """Convert documents through the locally installed Microsoft Word COM server."""
    if target_suffix not in {".pdf", ".docx"}:
        raise ValueError(tr(f"Неподдерживаемый формат Microsoft Word: {target_suffix}"))

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    total = len(documents)
    script = r'''param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$Target
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$OutputEncoding = [Console]::OutputEncoding
$word = $null
$document = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.Options.ConfirmConversions = $false
    $word.AutomationSecurity = 3
    $document = $word.Documents.Open($InputPath, $false, $true, $false)
    if ($Target -eq "pdf") {
        $document.SaveAs2($OutputPath, 17)
    } else {
        $document.SaveAs2($OutputPath, 16)
    }
} finally {
    if ($null -ne $document) { $document.Close(0) }
    if ($null -ne $word) { $word.Quit() }
}
'''
    with tempfile.TemporaryDirectory(prefix="pdf-bars-word-") as directory:
        script_path = Path(directory) / "convert.ps1"
        script_path.write_text(script, encoding="utf-8-sig")
        for index, document in enumerate(documents, start=1):
            if progress:
                progress(index - 1, total, f"Microsoft Word: {document.name}")
            output = available_output_path(out_dir / f"{document.stem}{target_suffix}")
            completed = subprocess.run(
                [
                    _powershell_executable(),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    "-InputPath",
                    str(document.resolve()),
                    "-OutputPath",
                    str(output.resolve()),
                    "-Target",
                    target_suffix.lstrip("."),
                ],
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0 or not output.exists():
                details = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(tr(f"Microsoft Word не смог преобразовать {document.name}. {details}"))
            outputs.append(output)
            if progress:
                progress(index, total, tr(f"Готово: {document.name}"))
    return outputs


def _powershell_executable() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def find_microsoft_word() -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        import winreg

        registry_keys = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE"),
        )
        for root, key_name in registry_keys:
            try:
                with winreg.OpenKey(root, key_name) as key:
                    candidate = winreg.QueryValue(key, None)
                if candidate and Path(candidate).is_file():
                    return candidate
            except OSError:
                continue
    except ImportError:
        pass

    candidates = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft Office/root/Office16/WINWORD.EXE",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Office/root/Office16/WINWORD.EXE",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("WINWORD.EXE")


def _font_family_installed(family: str) -> bool:
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return os.name == "nt"
    try:
        output = subprocess.check_output(
            [fc_list, "-f", "%{family}\n"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    requested = family.casefold()
    return any(
        requested == candidate.strip().casefold()
        for line in output.splitlines()
        for candidate in line.split(",")
    )


@contextmanager
def _r7_conversion_environment():
    environment = os.environ.copy()
    if _font_family_installed("Times New Roman"):
        yield environment, False
        return

    with tempfile.TemporaryDirectory(prefix="pdf-bars-fontconfig-") as directory:
        config_path = Path(directory) / "fonts.conf"
        config_path.write_text(
            """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <alias binding="strong">
    <family>Times New Roman</family>
    <prefer>
      <family>Liberation Serif</family>
      <family>DejaVu Serif</family>
    </prefer>
  </alias>
</fontconfig>
""",
            encoding="utf-8",
        )
        environment["FONTCONFIG_FILE"] = str(config_path)
        environment.setdefault("FONTCONFIG_PATH", "/etc/fonts")
        yield environment, True


def _convert_with_libreoffice_legacy(
    documents: Sequence[Path],
    output_dir: os.PathLike[str] | str,
    office: str,
) -> List[Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_outputs = [out_dir / f"{path.stem}.pdf" for path in documents]
    command = [office, "--headless", "--norestore", "--nolockcheck", "--convert-to", "pdf", "--outdir", str(out_dir)]
    command.extend(str(path) for path in documents)
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["SAL_USE_VCLPLUGIN"] = "svp"
    with tempfile.TemporaryDirectory(prefix="pdf-bars-lo-") as profile_dir:
        command.insert(1, f"-env:UserInstallation={Path(profile_dir).as_uri()}")
        completed = subprocess.run(command, text=True, capture_output=True, env=env)
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(tr(f"Резервный конвертер не смог преобразовать DOCX. {details}"))
    missing = [path for path in expected_outputs if not path.exists()]
    if missing:
        raise RuntimeError(tr(f"Конвертер завершился, но не создал: {', '.join(map(str, missing))}"))
    return expected_outputs


def _convert_with_libreoffice(
    documents: Sequence[Path],
    output_dir: os.PathLike[str] | str,
    office: str,
    progress: Optional[ConversionProgress] = None,
) -> List[Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    total = len(documents)
    for index, document in enumerate(documents, start=1):
        if progress:
            progress(index - 1, total, tr(f"Преобразование: {document.name}"))
        output = available_output_path(out_dir / f"{document.stem}.pdf")
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["SAL_USE_VCLPLUGIN"] = "svp"
        with tempfile.TemporaryDirectory(prefix="pdf-bars-lo-") as work_dir:
            work_path = Path(work_dir)
            profile_path = work_path / "profile"
            command = [
                office,
                f"-env:UserInstallation={profile_path.as_uri()}",
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--convert-to",
                "pdf",
                "--outdir",
                str(work_path),
                str(document),
            ]
            completed = subprocess.run(command, text=True, capture_output=True, env=env)
            generated = work_path / f"{document.stem}.pdf"
            if completed.returncode != 0 or not generated.exists():
                details = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(tr(f"Резервный конвертер не смог преобразовать {document.name}. {details}"))
            shutil.move(str(generated), str(output))
        outputs.append(output)
        if progress:
            progress(index, total, tr(f"Готово: {document.name}"))
    return outputs


def find_r7_converter() -> Optional[str]:
    candidates = (
        "/opt/r7-office/desktopeditors/converter/x2t",
        "/opt/r7-office/desktopeditors/x2t",
        "/opt/onlyoffice/desktopeditors/converter/x2t",
        "/opt/onlyoffice/desktopeditors/x2t",
    )
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += tuple(str(Path(base) / vendor / "DesktopEditors" / "converter" / "x2t.exe")
                                for vendor in ("R7-Office", "ONLYOFFICE"))
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("x2t")


def find_libreoffice_converter() -> Optional[str]:
    override = os.environ.get("PDF_BARS_OFFICE_CONVERTER", "").strip()
    if override and Path(override).is_file():
        return override
    for candidate in ("soffice", "soffice.exe", "libreoffice", "lowriter"):
        path = shutil.which(candidate)
        if path:
            return path
    candidates = ["/usr/bin/soffice", "/usr/bin/libreoffice", "/opt/libreoffice/program/soffice"]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.extend(str(Path(base) / office / "program" / executable)
                              for office in ("LibreOffice", "OpenOffice 4")
                              for executable in ("soffice.com", "soffice.exe"))
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def find_office_converter() -> Optional[str]:
    if os.name == "nt":
        return find_microsoft_word() or find_r7_converter() or find_libreoffice_converter()
    return find_r7_converter() or find_libreoffice_converter()


def find_libreoffice() -> Optional[str]:
    return find_office_converter()


def _image_to_rgb_page(image, background: str):
    from PIL import Image
    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        with image.convert("RGBA") as canvas, Image.new("RGBA", image.size, background) as base:
            base.alpha_composite(canvas)
            return base.convert("RGB")
    return image.convert("RGB")
