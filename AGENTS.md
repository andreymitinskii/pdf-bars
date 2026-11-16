# pdf bars engineering guide

Public local-only edition. Keep all OCR and documents local. Do not add analytics,
report uploaders, corporate assets, internal addresses or support contacts.
Python 3.8 on RED OS 7.3 and Python 3.11 on RED OS 8 share one universal RPM.
DOCX conversion selects installed Word, R7/ONLYOFFICE or a soffice-compatible
LibreOffice/OpenOffice converter, isolating failed exports before fallback.
Run focused tests, then the full suite before packaging. Do not distribute
Microsoft fonts or proprietary office binaries. Preserve undo, source files
and local recovery. Release binaries belong in GitHub Releases, not Git.
