"""Handle archival declarations without claiming unverified conformance."""
from __future__ import annotations

from xml.etree import ElementTree as ET

PDFA_ID = "http://www.aiim.org/pdfa/ns/id/"


def _metadata(document):
    try:
        xml = document.get_xml_metadata()
        return ET.fromstring(xml) if xml else None
    except (ET.ParseError, ValueError, RuntimeError):
        return None


def declared_pdfa(document) -> str | None:
    root = _metadata(document)
    if root is None:
        return None
    values = {}
    for element in root.iter():
        for name in ("part", "conformance"):
            key = "{%s}%s" % (PDFA_ID, name)
            if element.tag == key:
                values[name] = element.text or ""
            if key in element.attrib:
                values[name] = element.attrib[key]
    part = values.get("part", "").strip()
    level = values.get("conformance", "").strip().upper()
    return "PDF/A-%s%s" % (part, level) if part in {"1", "2", "3", "4"} else None


def remove_pdfa_declaration(document) -> None:
    root = _metadata(document)
    if root is None:
        return
    prefix = "{%s}" % PDFA_ID
    changed = False
    for element in list(root.iter()):
        for key in list(element.attrib):
            if key.startswith(prefix):
                del element.attrib[key]
                changed = True
        for child in list(element):
            if child.tag.startswith(prefix):
                element.remove(child)
                changed = True
    if changed:
        document.set_xml_metadata(ET.tostring(root, encoding="unicode"))
