import hashlib

import fitz

from redos_pdf_combiner.editor import TextEdit, apply_pdf_edits
from redos_pdf_combiner.pdf_standards import declared_pdfa


XMP = '''<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" pdfaid:part="2" pdfaid:conformance="B"/>
<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Original title</dc:title></rdf:Description>
</rdf:RDF></x:xmpmeta>'''


def test_edit_preserves_original_and_does_not_claim_pdfa_compliance(tmp_path):
    source, output = tmp_path / "archive.pdf", tmp_path / "copy.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 50), "Original document")
        pdf.set_xml_metadata(XMP)
        assert declared_pdfa(pdf) == "PDF/A-2B"
        pdf.save(source)
    digest = hashlib.sha256(source.read_bytes()).digest()
    apply_pdf_edits(source, output, [TextEdit(0, (50, 100, 180, 125), "Correction")], [])
    with fitz.open(output) as pdf:
        assert declared_pdfa(pdf) is None
        assert "Original title" in pdf.get_xml_metadata()
        assert "Original document" in pdf[0].get_text()
        assert "Correction" in pdf[0].get_text()
    assert hashlib.sha256(source.read_bytes()).digest() == digest


def test_pdfa_child_elements_are_recognized():
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.set_xml_metadata(XMP.replace('pdfaid:part="2" pdfaid:conformance="B"/>',
            '><pdfaid:part>3</pdfaid:part><pdfaid:conformance>U</pdfaid:conformance></rdf:Description>'))
        assert declared_pdfa(pdf) == "PDF/A-3U"
