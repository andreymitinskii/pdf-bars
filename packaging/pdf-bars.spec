Name:           pdf-bars
Version:        1.7.1
Release:        1
Summary:        pdf bars utility toolkit for RED OS
License:        AGPL-3.0-or-later
URL:            https://github.com/andreymitinskii/pdf-bars
Source0:        %{name}-%{version}.tar.gz
%global debug_package %{nil}
%global __brp_python_hardlink %{nil}
AutoReqProv:    no
ExclusiveArch:  x86_64

Requires:       python3 >= 3.8
Requires:       python3-tkinter
Requires:       ghostscript
Requires:       cups-client
Requires:       python3-gobject-base
Requires:       gtk3
Conflicts:      redos-pdf-bars

%description
Desktop and command-line PDF toolkit for RED OS: arrange and rotate pages,
compress documents, convert images and office documents, make controlled
text corrections and place user-managed signature images. R7-Office x2t is
used when available for DOCX/PDF conversion.

%prep
%setup -q

%build

%install
rm -rf %{buildroot}
install -d %{buildroot}%{_datadir}/%{name}
install -d %{buildroot}%{_bindir}
install -d %{buildroot}%{_libexecdir}
install -d %{buildroot}%{_datadir}/applications
install -d %{buildroot}%{_sysconfdir}/xdg/autostart
install -d %{buildroot}%{_sysconfdir}/pdf-bars
install -d %{buildroot}%{_datadir}/icons
cp -R src docs README.md LICENSE THIRD_PARTY_NOTICES.md requirements.txt %{buildroot}%{_datadir}/%{name}/
if [ -d packaging/icons ]; then
  cp -R packaging/icons/hicolor %{buildroot}%{_datadir}/icons/
fi
if [ -d packaging/wheelhouse ]; then
  python3 packaging/build_universal_vendor.py packaging/wheelhouse %{buildroot}%{_datadir}/%{name}
fi

install -m 0755 packaging/pdf-bars-launcher.sh %{buildroot}%{_libexecdir}/pdf-bars-launch
install -m 0755 packaging/pdf-bars-create-desktop-shortcut.sh %{buildroot}%{_libexecdir}/pdf-bars-create-desktop-shortcut

cat > %{buildroot}%{_bindir}/pdf-bars <<'EOF'
#!/usr/bin/env sh
exec /usr/libexec/pdf-bars-launch redos_pdf_combiner.cli "$@"
EOF

cat > %{buildroot}%{_bindir}/pdf-bars-gui <<'EOF'
#!/usr/bin/env sh
exec /usr/libexec/pdf-bars-launch redos_pdf_combiner.launcher "$@"
EOF

cat > %{buildroot}%{_bindir}/pdf-bars-selfcheck <<'EOF'
#!/usr/bin/env sh
exec /usr/libexec/pdf-bars-launch redos_pdf_combiner.selfcheck "$@"
EOF

chmod 0755 %{buildroot}%{_bindir}/pdf-bars %{buildroot}%{_bindir}/pdf-bars-gui %{buildroot}%{_bindir}/pdf-bars-selfcheck
install -m 0644 packaging/pdf-bars.desktop %{buildroot}%{_datadir}/applications/pdf-bars.desktop
install -m 0644 packaging/pdf-bars-create-shortcut.desktop %{buildroot}%{_sysconfdir}/xdg/autostart/pdf-bars-create-shortcut.desktop

%post
rm -f /usr/local/share/applications/pdf-bars-combiner.desktop
rm -f /usr/local/share/applications/pdf-bars.desktop
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q %{_datadir}/icons/hicolor >/dev/null 2>&1 || true
fi
/usr/bin/getent passwd 2>/dev/null | /usr/bin/awk -F: '$3 >= 1000 && $6 ~ /^\// && $7 !~ /(nologin|false)$/ { print $1 ":" $3 ":" $6 }' | while IFS=: read -r username uid home; do
    [ -n "$username" ] && [ -n "$uid" ] && [ -n "$home" ] || continue
    [ -d "$home" ] || continue
    /usr/sbin/runuser -u "$username" -- /usr/bin/env -u XDG_CONFIG_HOME -u XDG_CACHE_HOME \
        -u DBUS_SESSION_BUS_ADDRESS -u XDG_RUNTIME_DIR HOME="$home" \
        %{_libexecdir}/pdf-bars-create-desktop-shortcut >/dev/null 2>&1 || true
  done

%postun
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || true
fi

%files
%license LICENSE THIRD_PARTY_NOTICES.md
%{_bindir}/pdf-bars
%{_bindir}/pdf-bars-gui
%{_bindir}/pdf-bars-selfcheck
%{_libexecdir}/pdf-bars-launch
%{_libexecdir}/pdf-bars-create-desktop-shortcut
%{_datadir}/%{name}
%{_datadir}/applications/pdf-bars.desktop
%{_sysconfdir}/xdg/autostart/pdf-bars-create-shortcut.desktop
%{_datadir}/icons/hicolor/*/apps/pdf-bars.png

%changelog
* Mon Nov 16 2026 pdf bars contributors - 1.7.1-1
- Add Russian/English interface selection and remove the startup release popup.

* Mon Nov 16 2026 pdf bars contributors - 1.7-1
- Public local-only release without corporate branding or statistics.
