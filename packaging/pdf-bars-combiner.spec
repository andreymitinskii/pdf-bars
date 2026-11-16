Name:           pdf-bars-combiner
Version:        0.5.2
Release:        1%{?dist}
Summary:        pdf bars utility toolkit for RED OS
License:        MIT
URL:            https://local/pdf-bars-combiner
Source0:        %{name}-%{version}.tar.gz
%global debug_package %{nil}
AutoReqProv:    no

Requires:       python3 >= 3.8
Requires:       python3-tkinter
Requires:       ghostscript

%description
Desktop and command-line PDF toolkit: merge, split, extract/delete/rotate
pages, compress, encrypt/decrypt, add text watermarks, create PDFs from
images, export PDF pages to images and batch convert DOCX/DOC to PDF when
a compatible office converter is already installed.

%prep
%setup -q

%build

%install
rm -rf %{buildroot}
install -d %{buildroot}%{_datadir}/%{name}
install -d %{buildroot}%{_bindir}
install -d %{buildroot}%{_datadir}/applications
install -d %{buildroot}%{_datadir}/icons
cp -R src README.md requirements.txt %{buildroot}%{_datadir}/%{name}/
if [ -d packaging/icons ]; then
  cp -R packaging/icons/hicolor %{buildroot}%{_datadir}/icons/
fi
if [ -d packaging/wheelhouse ]; then
  cp -R packaging/wheelhouse %{buildroot}%{_datadir}/%{name}/
  install -d %{buildroot}%{_datadir}/%{name}/vendor
  python3 - <<'PY'
from pathlib import Path
from zipfile import ZipFile
import shutil

root = Path("%{buildroot}%{_datadir}/%{name}")
for wheel in sorted((root / "wheelhouse").glob("*.whl")):
    with ZipFile(wheel) as archive:
        archive.extractall(root / "vendor")
tkdnd = root / "vendor" / "tkinterdnd2" / "tkdnd"
if tkdnd.exists():
    for child in tkdnd.iterdir():
        if child.is_dir() and child.name != "linux-x64":
            shutil.rmtree(child)
PY
fi

cat > %{buildroot}%{_bindir}/pdf-bars <<'EOF'
#!/usr/bin/env sh
PYTHONPATH="/usr/share/pdf-bars-combiner/vendor:/usr/share/pdf-bars-combiner/src:${PYTHONPATH:-}" exec python3 -m redos_pdf_combiner.cli "$@"
EOF

cat > %{buildroot}%{_bindir}/pdf-bars-gui <<'EOF'
#!/usr/bin/env sh
PYTHONPATH="/usr/share/pdf-bars-combiner/vendor:/usr/share/pdf-bars-combiner/src:${PYTHONPATH:-}" exec python3 -m redos_pdf_combiner.gui "$@"
EOF

chmod 0755 %{buildroot}%{_bindir}/pdf-bars %{buildroot}%{_bindir}/pdf-bars-gui
install -m 0644 packaging/pdf-bars-combiner.desktop %{buildroot}%{_datadir}/applications/pdf-bars-combiner.desktop

%post
for launcher in /usr/local/bin/pdf-bars /usr/local/bin/pdf-bars-gui; do
  if [ -f "$launcher" ] && grep -q '/opt/pdf-bars-combiner' "$launcher" 2>/dev/null; then
    rm -f "$launcher"
  fi
done
for desktop in /usr/local/share/applications/pdf-bars-combiner.desktop /usr/local/share/applications/pdf-bars.desktop; do
  if [ -f "$desktop" ] && grep -Eq 'PDF Комбайн|RED OS PDF Комбайн|/opt/pdf-bars-combiner|pdf-bars-gui' "$desktop" 2>/dev/null; then
    rm -f "$desktop"
  fi
done
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q %{_datadir}/icons/hicolor >/dev/null 2>&1 || true
fi

%files
%license README.md
%{_bindir}/pdf-bars
%{_bindir}/pdf-bars-gui
%{_datadir}/%{name}
%{_datadir}/applications/pdf-bars-combiner.desktop
%{_datadir}/icons/hicolor/*/apps/pdf-bars.png

%changelog
* Thu Jun 18 2026 RED OS PDF Combiner <local@localhost> - 0.5.2-1
- Replace the application icon artwork.
- Show the application artwork in the About tab.
- Reset multi-page selection to a single clicked page when Shift is not held.

* Thu Jun 18 2026 RED OS PDF Combiner <local@localhost> - 0.5.1-1
- Add pdf bars application icon and install it into the desktop icon theme.
- Add Shift-click range selection for PDF page thumbnails.

* Wed Jun 17 2026 RED OS PDF Combiner <local@localhost> - 0.5.0-1
- Add page rotation and drag-and-drop page reordering in the PDF workspace.
- Allow mixing PDF pages and images in one assembled PDF document.
- Add batch PDF compression.

* Wed Jun 17 2026 RED OS PDF Combiner <local@localhost> - 0.4.5-1
- Show desktop application as pdf bars in the application menu.
- Replace percent compression control with maximum, medium and lossless minimum presets.
- Add lossless qpdf-based compression path when qpdf is available.

* Wed Jun 17 2026 RED OS PDF Combiner <local@localhost> - 0.4-1
- Rename desktop application to pdf bars, add PDF to images conversion.
- Vendor typing_extensions for older RED OS 7.3 Python installations.
- Do not require LibreOffice package to avoid changing office file associations.

* Wed Jun 10 2026 RED OS PDF Combiner <local@localhost> - 0.3.5-1
- Improve save dialogs, compression drag-and-drop and user hints.
