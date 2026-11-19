# pdf bars engineering guide

## Branch Boundary

This checkout is the public `release/1.7` line (`pdf-bars`, version 1.7.2).
Never mark a build here as 1.6 or add corporate reporting, logos or contacts.
Corporate `release/1.6` lives separately at `C:\Project\pdf-bars-1.6`, using
the `redos-pdf-bars` package and its own remote. Do not cross-publish sources.
Public settings/cache use PDFBarsPublic / pdf-bars-public, separate from 1.6.

Public local-only edition. Keep all OCR and documents local. Do not add analytics,
report uploaders, corporate assets or internal addresses. The explicitly approved
developer attribution in About is Mitinskiy A.A., andreymitinskii@gmail.com.
Python 3.8 on RED OS 7.3 and Python 3.11 on RED OS 8 share one universal RPM.
DOCX conversion selects installed Word, R7/ONLYOFFICE or a soffice-compatible
LibreOffice/OpenOffice converter, isolating failed exports before fallback.
Run focused tests, then the full suite before packaging. Do not distribute
Microsoft fonts or proprietary office binaries. Preserve undo, source files
and local recovery. Release binaries belong in GitHub Releases, not Git.

Interface strings use `i18n.tr`; English templates live in `locales/en.json`.
Do not translate document content, paths, font names, regexes or OCR alphabets.
The saved interface language is applied at process startup, independently of OCR.
Legacy tests run in Russian; test_i18n.py starts a separate English UI process.
The Windows installer defaults to all users with administrator approval, but the
application must remain asInvoker. Never weaken Program Files permissions.
