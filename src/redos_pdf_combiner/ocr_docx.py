from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from .ocr import IndexedWord, PageText, build_text_index


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCX_FONT = "Liberation Serif"

ET.register_namespace("w", W_NS)
ET.register_namespace("cp", CP_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("dcterms", DCTERMS_NS)
ET.register_namespace("xsi", XSI_NS)


def pdfs_to_ocr_docx(
    documents: Sequence[str | Path],
    output_dir: str | Path,
    *,
    dpi: int = 200,
    language: str = "rus+eng",
) -> List[Path]:
    if not documents:
        raise ValueError("Нужно выбрать хотя бы один PDF")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    for document in documents:
        source = Path(document)
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"Неподдерживаемый формат документа: {source}")
        if not source.is_file():
            raise FileNotFoundError(str(source))
        output = destination / f"{source.stem}_ocr.docx"
        outputs.append(source_to_ocr_docx(source, output, dpi=dpi, language=language))
    return outputs


def source_to_ocr_docx(
    source: str | Path,
    output: str | Path,
    *,
    text_index: Sequence[PageText] | None = None,
    dpi: int = 200,
    language: str = "rus+eng",
) -> Path:
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(str(source_path))
    output_path = Path(output)
    if output_path.suffix.lower() != ".docx":
        raise ValueError("Выходной файл должен иметь расширение .docx")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pages = (
        list(text_index)
        if text_index is not None
        else build_text_index(source_path, force_ocr=False, dpi=dpi, language=language, enhance=True)
    )
    if not pages:
        raise ValueError("Документ не содержит страниц")
    _write_docx(output_path, pages, source_path.stem)
    return output_path


def page_text_blocks(page: PageText) -> list[list[str]]:
    blocks: dict[int, dict[int, list[IndexedWord]]] = {}
    for word in page.words:
        blocks.setdefault(word.block, {}).setdefault(word.line, []).append(word)

    ordered_blocks = sorted(
        blocks.values(),
        key=lambda lines: _words_origin(word for line in lines.values() for word in line),
    )
    result: list[list[str]] = []
    for lines in ordered_blocks:
        ordered_lines = sorted(lines.values(), key=_words_origin)
        text_lines = []
        for words in ordered_lines:
            words.sort(key=lambda word: (word.order, word.rect[0]))
            value = " ".join(word.text for word in words).strip()
            if value:
                text_lines.append(value)
        if text_lines:
            result.append(text_lines)
    return result


def _words_origin(words: Iterable[IndexedWord]) -> tuple[float, float]:
    values = list(words)
    if not values:
        return float("inf"), float("inf")
    return min(word.rect[1] for word in values), min(word.rect[0] for word in values)


def _write_docx(output: Path, pages: Sequence[PageText], title: str) -> None:
    document = ET.Element(_w("document"))
    body = ET.SubElement(document, _w("body"))
    for page_number, page in enumerate(pages):
        blocks = page_text_blocks(page)
        if not blocks:
            _append_paragraph(body, [""])
        else:
            for block in blocks:
                _append_paragraph(body, block)
        if page_number + 1 < len(pages):
            paragraph = ET.SubElement(body, _w("p"))
            run = ET.SubElement(paragraph, _w("r"))
            ET.SubElement(run, _w("br"), {_w("type"): "page"})
    section = ET.SubElement(body, _w("sectPr"))
    ET.SubElement(section, _w("pgSz"), {_w("w"): "11906", _w("h"): "16838"})
    ET.SubElement(section, _w("pgMar"), {
        _w("top"): "1134",
        _w("right"): "1134",
        _w("bottom"): "1134",
        _w("left"): "1134",
        _w("header"): "708",
        _w("footer"): "708",
        _w("gutter"): "0",
    })

    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types())
        archive.writestr("_rels/.rels", _package_relationships())
        archive.writestr("docProps/core.xml", _core_properties(title))
        archive.writestr("docProps/app.xml", _app_properties())
        archive.writestr("word/document.xml", _xml_bytes(document))
        archive.writestr("word/styles.xml", _styles())
        archive.writestr("word/settings.xml", _settings())
        archive.writestr("word/fontTable.xml", _font_table())
        archive.writestr("word/_rels/document.xml.rels", _document_relationships())


def _append_paragraph(body: ET.Element, lines: Sequence[str]) -> None:
    paragraph = ET.SubElement(body, _w("p"))
    properties = ET.SubElement(paragraph, _w("pPr"))
    ET.SubElement(properties, _w("spacing"), {_w("after"): "120", _w("line"): "276", _w("lineRule"): "auto"})
    run = ET.SubElement(paragraph, _w("r"))
    run_properties = ET.SubElement(run, _w("rPr"))
    ET.SubElement(
        run_properties,
        _w("rFonts"),
        {
            _w("ascii"): DOCX_FONT,
            _w("hAnsi"): DOCX_FONT,
            _w("eastAsia"): DOCX_FONT,
            _w("cs"): DOCX_FONT,
        },
    )
    ET.SubElement(run_properties, _w("sz"), {_w("val"): "24"})
    ET.SubElement(run_properties, _w("szCs"), {_w("val"): "24"})
    ET.SubElement(run_properties, _w("lang"), {_w("val"): "ru-RU"})
    for index, line in enumerate(lines):
        if index:
            ET.SubElement(run, _w("br"))
        text = ET.SubElement(run, _w("t"))
        text.set(f"{{{XML_NS}}}space", "preserve")
        text.text = line


def _w(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def _xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def _content_types() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def _package_relationships() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _document_relationships() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""


def _styles() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr/></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Обычный"/>
    <w:qFormat/>
  </w:style>
</w:styles>"""


def _settings() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{W_NS}"><w:defaultTabStop w:val="708"/><w:characterSpacingControl w:val="doNotCompress"/></w:settings>"""


def _font_table() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:fonts xmlns:w="{W_NS}"><w:font w:name="{DOCX_FONT}"><w:family w:val="roman"/></w:font></w:fonts>"""


def _core_properties(title: str) -> bytes:
    properties = ET.Element(f"{{{CP_NS}}}coreProperties")
    ET.SubElement(properties, f"{{{DC_NS}}}title").text = title
    ET.SubElement(properties, f"{{{DC_NS}}}creator").text = "pdf bars"
    ET.SubElement(properties, f"{{{CP_NS}}}lastModifiedBy").text = "pdf bars"
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    created = ET.SubElement(properties, f"{{{DCTERMS_NS}}}created")
    created.set(f"{{{XSI_NS}}}type", "dcterms:W3CDTF")
    created.text = timestamp
    modified = ET.SubElement(properties, f"{{{DCTERMS_NS}}}modified")
    modified.set(f"{{{XSI_NS}}}type", "dcterms:W3CDTF")
    modified.text = timestamp
    return _xml_bytes(properties)


def _app_properties() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>pdf bars</Application>
</Properties>"""
