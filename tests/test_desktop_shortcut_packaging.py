from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_shortcut_is_packaged_for_install_and_first_login():
    helper = (ROOT / "packaging" / "pdf-bars-create-desktop-shortcut.sh").read_text(encoding="utf-8")
    autostart = (ROOT / "packaging" / "pdf-bars-create-shortcut.desktop").read_text(encoding="utf-8")
    spec = (ROOT / "packaging" / "pdf-bars.spec").read_text(encoding="utf-8")

    assert "xdg-user-dir DESKTOP" in helper
    assert "pdf bars.desktop" in helper
    assert "metadata::trusted true" in helper
    assert "desktop-shortcut-1.7" in helper
    assert "Exec=/usr/libexec/pdf-bars-create-desktop-shortcut" in autostart
    assert "NoDisplay=true" in autostart
    assert "getent passwd" in spec
    assert "install -d -m 0755" in spec
    assert '"$desktop_dir/pdf bars.desktop"' in spec
    assert "%{_sysconfdir}/xdg/autostart/pdf-bars-create-shortcut.desktop" in spec




def test_application_version_is_consistent():
    assert '__version__ = "1.7"' in (ROOT / "src" / "redos_pdf_combiner" / "__init__.py").read_text(encoding="utf-8")
    assert 'version = "1.7"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")




def test_rpm_build_uses_the_requested_isolated_topdir():
    build_script = (ROOT / "scripts" / "build-redos-rpm.sh").read_text(encoding="utf-8")
    assert 'rpmbuild --define "_topdir $BUILD_ROOT"' in build_script


def test_desktop_entry_registers_supported_files():
    desktop = (ROOT / "packaging" / "pdf-bars.desktop").read_text(encoding="utf-8")
    assert "Exec=pdf-bars-gui %F" in desktop
    assert "StartupNotify=false" in desktop
    assert "MimeType=application/pdf;" in desktop
    assert "image/webp;" in desktop


def test_pdf_toolbar_is_compact_and_page_actions_are_in_context_menu():
    source = (ROOT / "src" / "redos_pdf_combiner" / "gui.py").read_text(encoding="utf-8")
    editor = source.split("    def _editor_tab(", 1)[1].split("    def _images_tab(", 1)[0]
    context_menu = source.split("    def _show_editor_thumbnail_context_menu(", 1)[1].split("    def _toggle_editor_grid(", 1)[0]

    assert 'self.pdf_tab = ' not in source
    assert 'text="Все страницы"' in editor
    assert 'text="Закрыть документ"' in editor
    assert 'text="Печать"' in editor
    assert 'label="Сохранить выделенные страницы…"' in context_menu
    assert 'label="Сохранить как…"' in context_menu
    assert 'label="Дублировать выбранные"' in context_menu
    assert 'label="Повернуть влево"' in context_menu
    assert 'label="Повернуть вправо"' in context_menu
    assert 'label="Вставить пустую страницу"' in context_menu
    assert 'label="Удалить выбранные"' in context_menu


def test_ocr_docx_is_available_in_editor_documents_and_cli():
    gui = (ROOT / "src" / "redos_pdf_combiner" / "gui.py").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "redos_pdf_combiner" / "cli.py").read_text(encoding="utf-8")

    assert '("Word DOCX с распознанным текстом", "*.docx")' in gui
    assert 'text="Предварительно распознать текст"' in gui
    assert "if self.pdf_docx_pre_ocr.get():" in gui
    assert "По умолчанию создаётся чистый DOCX" in gui
    assert 'text="Сохранить графику"' in gui
    assert "pdfs_to_layout_ocr_docx(" in gui
    assert "source_to_layout_ocr_docx(" in gui
    assert 'pdf_docx.add_argument("--ocr"' in cli
    assert "preserve_graphics=args.ocr_graphics" in cli


def test_gui_launcher_uses_immediate_splash_screen():
    spec = (ROOT / "packaging" / "pdf-bars.spec").read_text(encoding="utf-8")
    launcher = (ROOT / "src" / "redos_pdf_combiner" / "launcher.py").read_text(encoding="utf-8")
    assert "redos_pdf_combiner.launcher" in spec
    assert 'text="pdf bars"' in launcher
    assert 'value="Загрузка компонентов..."' in launcher
    assert '"--listactivemonitors"' in launcher
    assert "def _active_monitor_geometry" in launcher


def test_modern_interface_uses_flat_tabs_and_original_brand_assets():
    source = (ROOT / "src" / "redos_pdf_combiner" / "gui.py").read_text(encoding="utf-8")
    assert "class TabWorkspace" in source
    assert 'button.configure(bg=BLUE, fg="white"' in source
    assert 'APP_ICON_PATH = Path(__file__).with_name("assets") / "pdf-bars-transparent.png"' in source
    assert 'DROP_ZONE_ICON_PATH = Path(__file__).with_name("assets") / "pdf-drop-zone.png"' in source
    assert 'text="Перетащите PDF или изображения"' in source
    assert 'dash=(6, 5)' in source
    assert "def _render_custom_header" in source
    assert '"window_minimize"' in source
    assert '"window_close"' in source
    assert "self.overrideredirect(True)" in source
    assert "self.configure(menu=menu_bar)" not in source
    assert "class RoundedPanel" in source
    assert "def _apply_window_rounding" in source
    assert "def _bind_canvas_wheel" in source


def test_all_file_drop_states_match_pdf_editor_drop_zone():
    source = (ROOT / "src" / "redos_pdf_combiner" / "gui.py").read_text(encoding="utf-8")
    drop_state = source.split("    def _create_file_drop_state(", 1)[1].split("    def _sync_file_drop_states", 1)[0]

    assert "create_rounded_card(" in drop_state
    assert "fill=DROP_ZONE_BG" in drop_state
    assert 'border="#BCD2F1"' in drop_state
    assert "radius=22" in drop_state
    assert "dash=(6, 5)" in drop_state


def test_new_application_logo_has_real_transparency():
    from PIL import Image

    logo = ROOT / "src" / "redos_pdf_combiner" / "assets" / "pdf-bars-transparent.png"
    with Image.open(logo) as image:
        assert image.mode == "RGBA"
        alpha = image.getchannel("A")
        assert alpha.getextrema() == (0, 255)
        assert alpha.getpixel((0, 0)) == 0


def test_modern_tabs_use_inset_rounded_card_shells():
    source = (ROOT / "src" / "redos_pdf_combiner" / "gui.py").read_text(encoding="utf-8")

    assert "def create_rounded_card(" in source
    assert 'body.pack(fill="both", expand=True, padx=padding, pady=padding)' in source
    assert "radius = 0 if self._window_maximized else 8" in source
    assert "DwmSetWindowAttribute" in source
    assert "preference = ctypes.c_int(1 if radius <= 0 else 2)" in source
    for method in ("_images_tab", "_pdf_images_tab", "_documents_tab", "_compress_tab"):
        section = source.split(f"    def {method}(", 1)[1].split("\n    def ", 1)[0]
        assert "create_rounded_card(" in section


def test_launcher_parses_primary_monitor_geometry():
    from redos_pdf_combiner.launcher import _parse_xrandr_monitors

    output = """Monitors: 2
 0: +HDMI-1 1920/510x1080/290+0+0  HDMI-1
 1: +*DP-1 2560/600x1440/340+1920+0  DP-1
"""
    assert _parse_xrandr_monitors(output) == [
        (0, 0, 1920, 1080, False),
        (1920, 0, 2560, 1440, True),
    ]


def test_launcher_formats_negative_monitor_offsets():
    from unittest.mock import patch

    from redos_pdf_combiner import launcher

    with patch.object(launcher, "_active_monitor_geometry", return_value=(-1920, 0, 1920, 1080)):
        assert launcher._center_geometry(object(), 470, 190) == "470x190-1195+445"


def test_maximize_selects_monitor_containing_window_center():
    from redos_pdf_combiner.launcher import _select_monitor_geometry

    monitors = [
        (0, 0, 1920, 1080, False),
        (1920, 0, 2560, 1440, True),
    ]
    assert _select_monitor_geometry(monitors, 960, 540) == (0, 0, 1920, 1080)
    assert _select_monitor_geometry(monitors, 3000, 700) == (1920, 0, 2560, 1440)
    assert _select_monitor_geometry(monitors, 960, 540, prefer_primary=True) == (1920, 0, 2560, 1440)


def test_custom_maximize_does_not_use_virtual_desktop_width_directly():
    source = (ROOT / "src" / "redos_pdf_combiner" / "gui.py").read_text(encoding="utf-8")
    maximize = source.split("    def _toggle_maximize_window(", 1)[1].split("\n    def ", 1)[0]

    assert "_monitor_geometry_for_point" in maximize
    assert "winfo_screenwidth" not in maximize
    assert 'f"{width}x{height}{x:+d}{y:+d}"' in maximize
