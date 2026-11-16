import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_public_application_has_no_network_reporting():
    for file in (ROOT/'src/redos_pdf_combiner').glob('*.py'):
        source=file.read_text(encoding='utf-8')
        assert 'telemetry' not in source.lower()
        tree=ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                assert not any(n.name.split('.')[0] in {'requests','http','urllib','socket'} for n in node.names)
    assert not (ROOT/'collector').exists()

def test_public_package_identity():
    from redos_pdf_combiner import __version__
    from redos_pdf_combiner.gui import APP_TITLE
    assert __version__ == '1.7.1'
    assert APP_TITLE == 'pdf bars'
    spec=(ROOT/'packaging/pdf-bars.spec').read_text(encoding='utf-8')
    assert 'Version:        1.7.1' in spec
    assert 'Release:        1' in spec
