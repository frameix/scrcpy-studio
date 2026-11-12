"""Main Studio Window implementation using GTK4, Libadwaita, and GStreamer."""

import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, GLib, GObject, Gst, Gtk, Pango

try:
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene, Gsk

    _HAS_GSK_TRILINEAR = hasattr(Gsk.ScalingFilter, "TRILINEAR")
except Exception:
    Graphene = None
    Gsk = None
    _HAS_GSK_TRILINEAR = False

from scrcpy_studio.adb_manager import (
    ADBManager,
    AndroidDevice,
    CameraLens,
    UsbOtgDevice,
)
from scrcpy_studio.hyprland_rules import HyprlandRulesManager
from scrcpy_studio.scrcpy_runner import ScrcpyRunner, StudioSessionConfig

# Initialize GStreamer once
Gst.init(None)


class _ScaledPaintable(GObject.Object, Gdk.Paintable):
    """Wraps a full-HD Gdk.MemoryTexture (e.g. 1080x2336) while reporting the target UI
    box dimensions as intrinsic width/height and rendering with GPU Trilinear mipmaps
    so >2x downscaling looks razor-sharp with zero aliasing/pixelation."""

    __gtype_name__ = "ScrcpyStudioScaledPaintable"

    def __init__(self, texture: Gdk.Texture, intrinsic_w: int, intrinsic_h: int):
        super().__init__()
        self._texture = texture
        self._w = intrinsic_w
        self._h = intrinsic_h

    def do_snapshot(self, snapshot: Gdk.Snapshot, width: float, height: float):
        if _HAS_GSK_TRILINEAR:
            try:
                bounds = Graphene.Rect()
                bounds.init(0.0, 0.0, float(width), float(height))
                snapshot.append_scaled_texture(
                    self._texture, Gsk.ScalingFilter.TRILINEAR, bounds
                )
                return
            except Exception:
                pass
        self._texture.snapshot(snapshot, width, height)

    def do_get_intrinsic_width(self) -> int:
        return self._w

    def do_get_intrinsic_height(self) -> int:
        return self._h

    def do_get_intrinsic_aspect_ratio(self) -> float:
        tw = self._texture.get_width()
        th = self._texture.get_height()
        if tw > 0 and th > 0:
            return float(tw) / float(th)
        return float(self._w) / float(max(1, self._h))


class StudioAppWindow(Adw.ApplicationWindow):
    # Settings window dimensions
    SETTINGS_WIDTH = 440
    SETTINGS_HEIGHT = 630

    # Target display span on 1920x1200 screen (1070h fills available height with Waybar cleanly)
    TARGET_PORTRAIT_H = 1070
    TARGET_LANDSCAPE_W = 1200

    HEADER_HEIGHT = 47
    SIDEBAR_WIDTH = 52

    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Scrcpy Studio")
        # In Floating mode, set_resizable(False) + set_default_size(1, 1) makes GTK4's
        # Wayland backend wrap the exact pixel size_request of the active child view.
        self.set_resizable(False)
        self.set_default_size(1, 1)

        self.runner = ScrcpyRunner()
        self.devices: List[AndroidDevice] = []
        self.selected_device: Optional[AndroidDevice] = None
        self.gst_pipeline: Optional[Gst.Pipeline] = None
        self.gst_flip = None
        self.current_flip_direction: int = 0
        self.current_orientation: str = "vertical"  # "vertical" or "horizontal"
        self.current_view: str = "settings"  # "settings" or "phone"
        self.is_tiled_mode: bool = False  # False = Floating phone, True = Docked/Tiled in Hyprland

        # Active session tracking (so live camera/webcam/mirror/otg controls never get confused)
        self.active_session_mode: str = "mirror"  # "mirror", "webcam", "camera", or "otg"
        self.active_camera_facing: str = "back"  # "back" or "front"
        self.active_camera_id: Optional[str] = None
        self.active_requested_res: str = "1920x1080"
        self.active_webcam_rotation: str = "0"
        self.active_record: bool = False
        self.usb_otg_devices: List[UsbOtgDevice] = []

        # Camera lens & dynamic resolution discovery state (`scrcpy --list-camera-sizes`)
        self._cameras_cache: Dict[str, List[CameraLens]] = {}
        self._cameras_loading: set = set()
        self._detected_lenses: List[CameraLens] = []
        self._filtered_exact_lenses: List[Optional[CameraLens]] = [None]
        self._available_resolutions: List[str] = [
            "1920x1080",
            "3840x2160",
            "1280x720",
        ]
        self._updating_lens_combos: bool = False
        self._camera_switch_seq: int = 0
        self._v4l2_label_sync_busy: bool = False

        self.device_screen_size = (1080, 2336)
        self.stream_aspect_w = 1080
        self.stream_aspect_h = 2336
        self._last_video_box = (0, 0)
        self._drag_start_xy = (0.0, 0.0)
        self._received_first_frame = False
        self._latest_frame = None
        self._render_pending = False
        self._adb_scan_busy = False

        # Ensure persistent floating/opaque rules exist in Hyprland config
        HyprlandRulesManager.ensure_persistent_rules()

        self._apply_custom_css()
        self._build_ui()
        self.refresh_devices()

        # NOTE: Only listen to notify::fullscreened (NEVER is_maximized(), because Hyprland
        # sends XDG_TOPLEVEL_STATE_MAXIMIZED on normal windows to suppress GTK CSD shadow margins!).
        self.connect("map", lambda _: GLib.timeout_add(60, self._on_window_mapped))
        self.connect("notify::fullscreened", self._on_fullscreen_changed)
        self.connect("close-request", self._on_close_request)

        # Periodic device polling while idle in settings
        GLib.timeout_add_seconds(3, self._poll_devices)

    @staticmethod
    def _pick_icon(*candidates: str) -> str:
        """Pick the first symbolic icon present in the active GTK4 IconTheme."""
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                theme = Gtk.IconTheme.get_for_display(display)
                for name in candidates:
                    if theme.has_icon(name):
                        return name
        except Exception:
            pass
        return candidates[0] if candidates else "emblem-system-symbolic"

    _WEBCAM_ROT_VALUES = ["0", "180", "flip0", "90", "270"]

    def _selected_mode_key(self) -> str:
        """0 = mirror (default), 1 = webcam (headless), 2 = camera (studio preview), 3 = otg."""
        sel = self.mode_row.get_selected()
        if sel == 1:
            return "webcam"
        if sel == 2:
            return "camera"
        if sel == 3:
            return "otg"
        return "mirror"

    def _is_camera_mode_selected(self) -> bool:
        return self._selected_mode_key() in ("camera", "webcam")

    def _is_webcam_mode_selected(self) -> bool:
        return self._selected_mode_key() == "webcam"

    def _is_otg_mode_selected(self) -> bool:
        return self._selected_mode_key() == "otg"

    def _on_window_mapped(self):
        if not self.is_tiled_mode:
            HyprlandRulesManager.ensure_window_floating()
        self._sync_window_geometry()
        return False

    def _on_fullscreen_changed(self, *_):
        if hasattr(self, "btn_fullscreen"):
            is_fs = self.is_fullscreen()
            self.btn_fullscreen.set_icon_name(
                "view-restore-symbolic" if is_fs else "view-fullscreen-symbolic"
            )
            self._sync_window_geometry()

    def _on_close_request(self, _):
        self._stop_gstreamer()
        if self.runner.is_running:
            self.runner.stop_session()
        return False

    def _apply_custom_css(self):
        css = b"""
        window, .phone-viewport {
            background-color: #050507;
        }
        .control-sidebar {
            background-color: rgba(24, 24, 28, 0.98);
            border-left: 1px solid rgba(255, 255, 255, 0.08);
            padding: 6px 4px;
        }
        .sidebar-btn {
            min-width: 38px;
            min-height: 38px;
            padding: 0px;
            border-radius: 10px;
            font-size: 15px;
        }
        .lens-popover-box {
            padding: 6px;
        }
        .lens-section-title {
            font-size: 11px;
            font-weight: 700;
            opacity: 0.7;
            margin-top: 4px;
            margin-bottom: 2px;
            margin-left: 6px;
        }
        .lens-item-btn {
            padding: 6px 10px;
            border-radius: 8px;
            font-size: 13px;
        }
        .rec-badge {
            background-color: rgba(225, 45, 57, 0.22);
            color: #ff5c67;
            border-radius: 999px;
            padding: 3px 10px;
            font-weight: 700;
            font-size: 12px;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_ui(self):
        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root_box.set_hexpand(True)
        root_box.set_vexpand(True)
        self.set_content(root_box)

        # --- Unified Header Bar ---
        self.header = Adw.HeaderBar()
        root_box.append(self.header)

        # Top-Left: Settings button (toggles between Phone view and Settings view)
        self.btn_settings = Gtk.Button(
            icon_name="emblem-system-symbolic",
            tooltip_text="Ajustes de Estudio",
        )
        self.btn_settings.connect("clicked", self._on_settings_icon_clicked)
        self.header.pack_start(self.btn_settings)

        # Top-Left (next to Settings): Hyprland Tile / Floating Phone ToggleButton
        self.btn_tile_mode = Gtk.ToggleButton(
            icon_name="view-dual-symbolic",
            tooltip_text="Acoplar en mosaico Hyprland (columna vertical igual que una terminal)",
        )
        self.btn_tile_mode.set_active(False)
        self.btn_tile_mode.set_visible(False)
        self.btn_tile_mode.connect("toggled", self._on_tile_mode_toggled)
        self.header.pack_start(self.btn_tile_mode)

        # Top-Left (Settings only): Refresh ADB devices button
        self.btn_refresh = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="Actualizar lista de dispositivos (USB + Wi-Fi)",
        )
        self.btn_refresh.connect("clicked", lambda _: self.refresh_devices(deep=True))
        self.header.pack_start(self.btn_refresh)

        # Center title widget (ellipsized so CenterBox min-width stays ultra-compact)
        self.title_label = Gtk.Label(label="Scrcpy Studio")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.add_css_class("title")
        self.header.set_title_widget(self.title_label)

        # Top-Right: Control Bar ToggleButton (visible in Phone view)
        self.btn_control_bar = Gtk.ToggleButton(
            icon_name="sidebar-show-right-symbolic",
            tooltip_text="Desplegar / Ocultar Barra de Control",
        )
        self.btn_control_bar.set_active(False)
        self.btn_control_bar.set_visible(False)
        self.btn_control_bar.connect("toggled", self._on_control_bar_toggled)
        self.header.pack_end(self.btn_control_bar)

        # Top-Right: Fullscreen button (visible in Phone view)
        self.btn_fullscreen = Gtk.Button(
            icon_name="view-fullscreen-symbolic",
            tooltip_text="Pantalla completa (Centrar teléfono / Ver película)",
        )
        self.btn_fullscreen.set_visible(False)
        self.btn_fullscreen.connect("clicked", self._on_fullscreen_clicked)
        self.header.pack_end(self.btn_fullscreen)

        # Top-Right: General Help & Shortcuts button (visible in Settings view)
        self.btn_help = Gtk.Button(
            icon_name="help-browser-symbolic",
            tooltip_text="Guía Rápida y Atajos de Scrcpy",
        )
        self.btn_help.add_css_class("flat")
        self.btn_help.connect("clicked", self._on_show_general_help)
        self.header.pack_end(self.btn_help)

        # --- Single Active Body Container (unparents inactive view completely) ---
        self.body_bin = Adw.Bin()
        self.body_bin.set_hexpand(True)
        self.body_bin.set_vexpand(True)
        root_box.append(self.body_bin)

        # Build both views
        self._build_settings_view()
        self._build_phone_view()

        # Start in settings view
        self._switch_to_view("settings")

    def _build_settings_view(self):
        self.settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_width(False)
        scroll.set_propagate_natural_height(False)
        scroll.set_vexpand(True)
        self.settings_box.append(scroll)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(420)
        clamp.set_tightening_threshold(360)
        scroll.set_child(clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content_box.set_margin_top(14)
        content_box.set_margin_bottom(14)
        content_box.set_margin_start(14)
        content_box.set_margin_end(14)
        clamp.set_child(content_box)

        # --- Device Group ---
        grp_device = Adw.PreferencesGroup(
            title="Dispositivo Conectado",
            description="Auto-detectado por USB o Wi-Fi emparejado",
        )
        grp_device.set_header_suffix(
            self._create_section_help_button(
                "Dispositivo Conectado",
                [
                    (
                        "Conexión USB (60 FPS)",
                        "Conexión por cable de máxima fluidez y mínima latencia (<15ms). Ideal para trabajar, grabar y jugar.",
                    ),
                    (
                        "Conexión Wi-Fi",
                        "Uso sin cables en la red local. En tu casa se autodetecta por mDNS; en redes públicas o de viaje usa 'Conectar sesión'.",
                    ),
                    (
                        "Botón Refrescar 🔄",
                        "Actualiza la lista de dispositivos conectados si enchufas un nuevo teléfono o cambias de red Wi-Fi.",
                    ),
                ],
            )
        )
        content_box.append(grp_device)

        self.device_combo = Adw.ComboRow(title="Celular")
        self.device_model = Gtk.StringList()
        self.device_combo.set_model(self.device_model)
        self.device_combo.connect("notify::selected", self._on_device_selected)
        grp_device.add(self.device_combo)

        # Graphical Wireless Pairing & Manual Travel/Hotspot Connection (on-demand only!)
        self.wifi_expander = Adw.ExpanderRow(
            title="Conexión Wi-Fi / Otro Lugar",
            subtitle="Vincular con código (1ra vez) o conectar en otra red / Hotspot",
        )
        grp_device.add(self.wifi_expander)

        # Row 1: Shared Phone IP
        ip_row = Adw.ActionRow(title="IP Móvil")
        self.entry_wifi_ip = Gtk.Entry(placeholder_text="Ej: 192.168.100.139")
        self.entry_wifi_ip.set_text(ADBManager.load_saved_wifi_ip())
        self.entry_wifi_ip.set_width_chars(16)
        self.entry_wifi_ip.set_valign(Gtk.Align.CENTER)
        ip_row.add_suffix(self.entry_wifi_ip)
        self.wifi_expander.add_row(ip_row)

        # Row 2: Pair with 6-digit code (`adb pair IP:PORT CODE`)
        pair_row = Adw.ActionRow(
            title="Vincular (1 sola vez)",
            subtitle="Puerto y PIN que muestra la ventana emergente en el celular",
        )
        pair_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pair_box.set_valign(Gtk.Align.CENTER)
        self.entry_pair_port = Gtk.Entry(placeholder_text="Puerto")
        self.entry_pair_port.set_width_chars(6)
        self.entry_pair_port.set_tooltip_text(
            "Puerto temporal que muestra la ventanita 'Vincular con código'"
        )
        self.entry_pair_code = Gtk.Entry(placeholder_text="Código")
        self.entry_pair_code.set_width_chars(6)
        self.entry_pair_code.set_tooltip_text("Código de 6 dígitos")
        self.entry_pair_code.connect("activate", self._on_wifi_pair_clicked)
        self.btn_wifi_pair = Gtk.Button(label="Vincular")
        self.btn_wifi_pair.connect("clicked", self._on_wifi_pair_clicked)
        pair_box.append(self.entry_pair_port)
        pair_box.append(self.entry_pair_code)
        pair_box.append(self.btn_wifi_pair)
        pair_row.add_suffix(pair_box)
        self.wifi_expander.add_row(pair_row)

        # Row 3: Manual Connect (`adb connect IP:PORT`) for University/Cafe/Hotspot Wi-Fi
        conn_row = Adw.ActionRow(
            title="Conectar sesión",
            subtitle="Puerto principal mostrado en 'Depuración inalámbrica'",
        )
        conn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        conn_box.set_valign(Gtk.Align.CENTER)
        self.entry_conn_port = Gtk.Entry(placeholder_text="Puerto")
        self.entry_conn_port.set_width_chars(8)
        self.entry_conn_port.set_tooltip_text(
            "Puerto principal de 'Depuración inalámbrica' (útil en otra red Wi-Fi o Hotspot)"
        )
        self.entry_conn_port.connect("activate", self._on_wifi_connect_clicked)
        self.btn_wifi_conn = Gtk.Button(label="Conectar")
        self.btn_wifi_conn.add_css_class("suggested-action")
        self.btn_wifi_conn.connect("clicked", self._on_wifi_connect_clicked)
        conn_box.append(self.entry_conn_port)
        conn_box.append(self.btn_wifi_conn)
        conn_row.add_suffix(conn_box)
        self.wifi_expander.add_row(conn_row)

        # --- Mode Group (Default: 📱 Espejo Pantalla) ---
        grp_mode = Adw.PreferencesGroup(title="Modo de Operación")
        grp_mode.set_header_suffix(
            self._create_section_help_button(
                "Modos de Operación",
                [
                    (
                        "📱 Espejo Pantalla",
                        "Transmite la pantalla completa a tu PC con control total de teclado y ratón.",
                    ),
                    (
                        "🌐 Webcam Directa (Sin Ventana)",
                        "Envía la cámara a videollamadas (Meet, OBS, Discord) sin abrir ventana local (ahorra batería y GPU).",
                    ),
                    (
                        "🎥 Cámara Estudio (Con Visor)",
                        "Abre un visor flotante de alta fidelidad para encuadrar, alternar lentes físicas y grabar.",
                    ),
                    (
                        "⌨️ Control OTG (Teclado/Ratón USB)",
                        "Emula un teclado y ratón USB físicos conectados al celular sin transmitir video ni gastar batería.",
                    ),
                ],
            )
        )
        content_box.append(grp_mode)

        self.mode_row = Adw.ComboRow(title="Modo")
        self.mode_model = Gtk.StringList.new(
            [
                "📱 Espejo Pantalla",
                "🌐 Webcam Directa (Sin Ventana)",
                "🎥 Cámara Estudio (Con Visor)",
                "⌨️ Control OTG (Teclado/Ratón USB)",
            ]
        )
        self.mode_row.set_model(self.mode_model)
        self.mode_row.set_selected(0)  # Default: Mirror mode
        self.mode_row.connect("notify::selected", self._on_mode_changed)
        grp_mode.add(self.mode_row)

        # --- Virtual Webcam Name & Headless Status Group (visible in Webcam Directa & Cámara Estudio) ---
        self.grp_webcam = Adw.PreferencesGroup(
            title="Webcam Virtual en Linux (Meet / OBS / Discord)",
            description="Nombre con el que las videollamadas reconocen la cámara de tu celular",
        )
        self.grp_webcam.set_header_suffix(
            self._create_section_help_button(
                "Webcam Virtual (v4l2loopback)",
                [
                    (
                        "Cámara en Meet / OBS / Discord",
                        "Crea un dispositivo virtual en Linux (/dev/video) reconocido por navegadores y programas de streaming.",
                    ),
                    (
                        "Renombrar en RAM",
                        "Asigna el nombre de tu teléfono a la webcam en memoria volátil (devtmpfs) sin tocar archivos de disco ni dejar residuos.",
                    ),
                ],
            )
        )
        self.grp_webcam.set_visible(False)
        content_box.append(self.grp_webcam)

        self.webcam_name_row = Adw.ActionRow(
            title=f"🎥 Cámara: {self.runner.v4l2_label}",
            subtitle=f"Dispositivo virtual {self.runner.v4l2_device}",
        )
        self.btn_enable_dyn_name = Gtk.Button(label="Renombrar en RAM")
        self.btn_enable_dyn_name.add_css_class("suggested-action")
        self.btn_enable_dyn_name.set_valign(Gtk.Align.CENTER)
        self.btn_enable_dyn_name.set_tooltip_text(
            "Renombra la cámara virtual temporalmente en memoria RAM (ioctl) sin tocar archivos de arranque ni el disco"
        )
        self.btn_enable_dyn_name.connect(
            "clicked", self._on_enable_dynamic_v4l2_clicked
        )
        self.webcam_name_row.add_suffix(self.btn_enable_dyn_name)
        self.grp_webcam.add(self.webcam_name_row)

        # --- OTG Info & Recommendation Group (visible only when Modo == Control OTG) ---
        self.grp_otg = Adw.PreferencesGroup(
            title="Control OTG Físico (Sin Depuración)",
            description="Simula un teclado y ratón USB reales conectados al celular",
        )
        self.grp_otg.set_header_suffix(
            self._create_section_help_button(
                "Control OTG Físico",
                [
                    (
                        "Solo Cable USB",
                        "Requiere conexión directa por cable USB (el protocolo USB HID no opera a través de Wi-Fi).",
                    ),
                    (
                        "Sin Depuración USB",
                        "Funciona incluso si la depuración está desactivada o la pantalla del móvil está bloqueada.",
                    ),
                    (
                        "Captura de Ratón",
                        "Haz clic en el recuadro para mover el cursor en el celular. Presiona Alt Izquierdo para liberar el ratón a tu PC.",
                    ),
                ],
            )
        )
        self.grp_otg.set_visible(False)
        content_box.append(self.grp_otg)

        self.otg_status_row = Adw.ActionRow(
            title="Estado USB",
            subtitle="Verificando conexión por cable USB...",
        )
        self.grp_otg.add(self.otg_status_row)

        otg_tip_usb = Adw.ActionRow(
            title="🔌 Solo requiere cable USB a la PC",
            subtitle="Funciona incluso con Depuración USB desactivada (no opera por Wi-Fi)",
        )
        self.grp_otg.add(otg_tip_usb)

        otg_tip_pad = Adw.ActionRow(
            title="🖱️ Recuadro Trackpad OTG",
            subtitle="Haz clic en el recuadro de Android para controlar el móvil · Pulsa Alt Izq para soltar el ratón",
        )
        self.grp_otg.add(otg_tip_pad)

        # --- Orientation & Camera Group ---
        self.grp_camera = Adw.PreferencesGroup(title="Formato y Cámara")
        self.grp_camera.set_header_suffix(
            self._create_section_help_button(
                "Formato y Cámara",
                [
                    (
                        "Orientación",
                        "Vertical para formato móvil (TikTok, Shorts, Reels) u Horizontal para clases, videollamadas y streaming.",
                    ),
                    (
                        "Cámara y Lente Exacto",
                        "Alterna entre trasera y frontal. Si tu celular tiene varias lentes (Macro, Gran Angular), puedes fijar el sensor exacto.",
                    ),
                    (
                        "Tamaño / Calidad",
                        "1080p Full HD ofrece nitidez óptima. 4K da máxima definición y 720p minimiza uso de CPU en la PC.",
                    ),
                    (
                        "Rotación en Meet/OBS",
                        "Ajusta el ángulo del flujo de video o activa modo espejo para verte natural como en una selfie.",
                    ),
                ],
            )
        )
        content_box.append(self.grp_camera)

        self.orientation_row = Adw.ComboRow(title="Orientación")
        self.orientation_model = Gtk.StringList.new(
            ["📱 Vertical", "🖥️ Horizontal"]
        )
        self.orientation_row.set_model(self.orientation_model)
        self.orientation_row.connect("notify::selected", self._on_orientation_row_changed)
        self.grp_camera.add(self.orientation_row)

        self.lens_row = Adw.ComboRow(title="Cámara")
        self.lens_model = Gtk.StringList.new(
            ["📷 Trasera", "🤳 Frontal"]
        )
        self.lens_row.set_model(self.lens_model)
        self.lens_row.connect("notify::selected", self._on_facing_row_changed)
        self.lens_row.set_visible(False)  # Hidden while in default Mirror mode
        self.grp_camera.add(self.lens_row)

        self.exact_lens_row = Adw.ComboRow(
            title="Lente Exacto",
            subtitle="Detectando lentes con scrcpy...",
        )
        self.exact_lens_model = Gtk.StringList.new(["Automático (Por defecto)"])
        self.exact_lens_row.set_model(self.exact_lens_model)
        self.exact_lens_row.connect("notify::selected", self._on_exact_lens_row_changed)
        btn_rescan_lenses = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="Re-escanear lentes físicos del dispositivo",
        )
        btn_rescan_lenses.add_css_class("flat")
        btn_rescan_lenses.set_valign(Gtk.Align.CENTER)
        btn_rescan_lenses.connect("clicked", self._on_rescan_lenses_clicked)
        self.exact_lens_row.add_suffix(btn_rescan_lenses)
        self.exact_lens_row.set_visible(False)
        self.grp_camera.add(self.exact_lens_row)

        self.res_row = Adw.ComboRow(title="Tamaño / Calidad")
        self.res_model = Gtk.StringList.new(
            [
                "1920×1080 · 1080p Full HD (Recomendado Meet/OBS)",
                "3840×2160 · 4K Ultra HD (Máxima)",
                "1280×720 · 720p HD (Ligero)",
            ]
        )
        self.res_row.set_model(self.res_model)
        self.res_row.set_selected(0)
        self.res_row.connect("notify::selected", self._on_res_row_changed)
        self.res_row.set_visible(False)
        self.grp_camera.add(self.res_row)

        self.webcam_rot_row = Adw.ComboRow(title="Rotación en Meet/OBS")
        self.webcam_rot_model = Gtk.StringList.new(
            [
                "🖥️ Horizontal Normal (0°)",
                "🔄 Horizontal Invertido (180°)",
                "🪞 Horizontal Espejo (Efecto Selfie)",
                "📱 Vertical Derecha (90°)",
                "📱 Vertical Izquierda (270°)",
            ]
        )
        self.webcam_rot_row.set_model(self.webcam_rot_model)
        self.webcam_rot_row.set_selected(0)
        self.webcam_rot_row.connect("notify::selected", self._on_webcam_rot_changed)
        self.webcam_rot_row.set_visible(False)
        self.grp_camera.add(self.webcam_rot_row)

        self.audio_row = Adw.ComboRow(title="Audio del Micrófono")
        self.audio_model = Gtk.StringList.new(
            [
                "🔇 Mudo (Sin Audio / Máxima Fluidez Wi-Fi)",
                "🎙️ Grabar Micro (Silenciar altavoces PC)",
                "🔊 Grabar Micro y Escuchar en PC",
            ]
        )
        self.audio_row.set_model(self.audio_model)
        self.audio_row.set_selected(0)  # Default: Mute / Silent video
        self.audio_row.set_visible(False)
        self.grp_camera.add(self.audio_row)

        # --- Recording Group (Default: OFF) ---
        self.grp_recording = Adw.PreferencesGroup(title="Grabación")
        self.grp_recording.set_header_suffix(
            self._create_section_help_button(
                "Grabación de Video",
                [
                    (
                        "Grabación a MP4",
                        "Graba directamente la transmisión de pantalla o cámara a un archivo .mp4 de alta definición.",
                    ),
                    (
                        "Codificación por Hardware",
                        "Graba a 16 Mbps sin generar caídas de cuadros (lag) en la previsualización local.",
                    ),
                    (
                        "Carpeta de Destino",
                        "Los videos se guardan automáticamente organizados por fecha y hora en ~/Videos/ScrcpyStudio/.",
                    ),
                ],
            )
        )
        content_box.append(self.grp_recording)

        self.switch_record = Adw.SwitchRow(
            title="🔴 Grabar a MP4",
            subtitle="16 Mbps en ~/Videos/ScrcpyStudio/",
        )
        self.switch_record.set_active(False)  # Default: Recording disabled
        self.grp_recording.add(self.switch_record)

        # --- Window & Tiling Group (Hyprland) ---
        self.grp_tiling = Adw.PreferencesGroup(title="Ventana y Mosaico (Hyprland)")
        self.grp_tiling.set_header_suffix(
            self._create_section_help_button(
                "Ventana y Mosaico",
                [
                    (
                        "Lado al Acoplar",
                        "Elige si la pantalla del teléfono se posiciona en el borde izquierdo o derecho al presionar Super + Alt + Espacio.",
                    ),
                    (
                        "Ancho Matemático Automático",
                        "El sistema calcula el ancho exacto (534 px) para llenar toda la altura de tu pantalla sin deformar la imagen ni dejar franjas negras.",
                    ),
                    (
                        "Mover en Vivo",
                        "Si ya está acoplada, puedes cambiar de lado en cualquier momento con Super + Shift + Flechas.",
                    ),
                ],
            )
        )
        content_box.append(self.grp_tiling)

        self.layout_mode_row = Adw.ComboRow(title="Disposición de Ventana")
        self.layout_mode_row.set_subtitle("Modo al conectar (alterna con Super + Alt + Espacio)")
        self.layout_mode_model = Gtk.StringList.new(
            [
                "📱 Ventana Flotante (Predeterminado)",
                "⬅️ Acoplar a la Izquierda (Mosaico)",
                "➡️ Acoplar a la Derecha (Mosaico)",
            ]
        )
        self.layout_mode_row.set_model(self.layout_mode_model)
        self.layout_mode_row.set_selected(0)
        self.layout_mode_row.connect("notify::selected", self._on_layout_mode_row_changed)
        self.grp_tiling.add(self.layout_mode_row)

        # --- Bottom Action Bar in Settings ---
        action_bar = Gtk.ActionBar()
        self.settings_box.append(action_bar)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_box.set_margin_top(6)
        btn_box.set_margin_bottom(6)

        self.btn_connect = Gtk.Button(label="▶ CONECTAR E INICIAR")
        self.btn_connect.add_css_class("suggested-action")
        self.btn_connect.add_css_class("pill")
        self.btn_connect.set_size_request(220, 44)
        self.btn_connect.connect("clicked", self._on_connect_clicked)
        btn_box.append(self.btn_connect)

        self.btn_disconnect = Gtk.Button(label="⏹ DETENER")
        self.btn_disconnect.add_css_class("destructive-action")
        self.btn_disconnect.add_css_class("pill")
        self.btn_disconnect.set_size_request(130, 44)
        self.btn_disconnect.set_visible(False)
        self.btn_disconnect.connect("clicked", self._on_disconnect_clicked)
        btn_box.append(self.btn_disconnect)

        action_bar.set_center_widget(btn_box)

    def _build_phone_view(self):
        # Horizontal container: [ Phone Video Area ] + [ Collapsible Vertical Control Bar ]
        self.phone_root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.phone_root.set_hexpand(True)
        self.phone_root.set_vexpand(True)

        # 1. Phone Video Area
        self.video_overlay = Gtk.Overlay()
        self.video_overlay.add_css_class("phone-viewport")
        self.video_overlay.set_hexpand(True)
        self.video_overlay.set_vexpand(True)

        self.video_picture = Gtk.Picture()
        self.video_picture.set_can_shrink(True)
        self.video_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.video_picture.set_hexpand(True)
        self.video_picture.set_vexpand(True)
        self.video_picture.set_halign(Gtk.Align.FILL)
        self.video_picture.set_valign(Gtk.Align.FILL)
        self.video_overlay.set_child(self.video_picture)

        # Loading indicator overlay before first frame arrives
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.loading_box.set_valign(Gtk.Align.CENTER)
        self.loading_box.set_halign(Gtk.Align.CENTER)

        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(36, 36)
        self.loading_box.append(self.spinner)

        self.loading_label = Gtk.Label(label="Iniciando transmisión...")
        self.loading_label.add_css_class("dim-label")
        self.loading_box.append(self.loading_label)
        self.video_overlay.add_overlay(self.loading_box)

        # Interactive touch gestures on the phone screen (for Mirror mode)
        click_gesture = Gtk.GestureClick.new()
        click_gesture.set_button(0)  # Listen to Left, Middle, and Right clicks
        click_gesture.connect("released", self._on_video_clicked)
        self.video_overlay.add_controller(click_gesture)

        drag_gesture = Gtk.GestureDrag.new()
        drag_gesture.connect("drag-begin", self._on_video_drag_begin)
        drag_gesture.connect("drag-end", self._on_video_drag_end)
        self.video_overlay.add_controller(drag_gesture)

        # Mouse scroll wheel support (TikTok, Instagram, Web scrolling!)
        scroll_controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll_controller.connect("scroll", self._on_video_scroll)
        self.video_overlay.add_controller(scroll_controller)

        self.phone_root.append(self.video_overlay)

        # 2. Collapsible Vertical Control Sidebar on the right (inside the same window)
        self.sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.sidebar_box.add_css_class("control-sidebar")
        self.sidebar_box.set_size_request(self.SIDEBAR_WIDTH, -1)
        self.sidebar_box.set_hexpand(False)
        self.sidebar_box.set_vexpand(True)
        self.sidebar_box.set_visible(False)

        # Scrollable inner column (propagate_natural_height=False so sidebar never stretches landscape height)
        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_propagate_natural_height(False)
        sidebar_scroll.set_propagate_natural_width(False)
        sidebar_scroll.set_vexpand(True)

        sidebar_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar_inner.set_vexpand(True)
        sidebar_scroll.set_child(sidebar_inner)
        self.sidebar_box.append(sidebar_scroll)

        # Common utility buttons (Rotate, Screenshot, Recordings Folder)
        btn_rotate = self._make_sidebar_btn(
            "object-rotate-right-symbolic",
            "Rotar orientación en vivo (Vertical / Horizontal)",
            lambda _: self._toggle_live_orientation(),
        )
        sidebar_inner.append(btn_rotate)

        btn_shot = self._make_sidebar_btn(
            "camera-photo-symbolic",
            "Tomar captura de pantalla (~/Imágenes/ScrcpyStudio)",
            lambda _: self._take_screenshot(),
        )
        sidebar_inner.append(btn_shot)

        btn_folder = self._make_sidebar_btn(
            "folder-videos-symbolic",
            "Abrir carpeta de grabaciones (~/Videos/ScrcpyStudio)",
            lambda _: self._open_videos_folder(),
        )
        sidebar_inner.append(btn_folder)

        # --- Live Camera Studio Controls (visible when Mode == 🎥 Cámara Estudio) ---
        self.cam_controls_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.cam_controls_box.set_visible(False)
        self.cam_controls_box.append(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        )

        # 1) Quick 1-click Front <-> Back camera toggle
        flip_icon = self._pick_icon(
            "camera-switch-symbolic",
            "object-flip-horizontal-symbolic",
            "media-playlist-repeat-symbolic",
        )
        self.btn_flip_facing = self._make_sidebar_btn(
            flip_icon,
            "Alternar Cámara Trasera ↔ Frontal en vivo",
            lambda _: self._toggle_live_camera_facing(),
        )
        self.cam_controls_box.append(self.btn_flip_facing)

        # 2) Exact Physical Lens Selector Popover (`--camera-id=...`)
        lens_icon = self._pick_icon(
            "camera-video-symbolic",
            "camera-web-symbolic",
            "view-list-symbolic",
        )
        self.btn_lens_menu = Gtk.MenuButton(
            icon_name=lens_icon,
            tooltip_text="Elegir lente exacto en vivo (Traseros y Frontales)",
        )
        self.btn_lens_menu.add_css_class("sidebar-btn")
        self.btn_lens_menu.add_css_class("flat")
        self.btn_lens_menu.set_halign(Gtk.Align.CENTER)

        self.lens_popover = Gtk.Popover()
        self.lens_popover.set_position(Gtk.PositionType.LEFT)
        self.lens_popover.set_has_arrow(True)
        self.lens_popover_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4
        )
        self.lens_popover_content.add_css_class("lens-popover-box")
        self.lens_popover.set_child(self.lens_popover_content)
        self.btn_lens_menu.set_popover(self.lens_popover)
        self.cam_controls_box.append(self.btn_lens_menu)

        sidebar_inner.append(self.cam_controls_box)

        # --- Mirror Hardware & Android Navigation Controls (visible when Mode == 📱 Espejo) ---
        self.mirror_controls_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        self.mirror_controls_box.set_vexpand(True)
        self.mirror_controls_box.append(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        )

        btn_vol_up = self._make_sidebar_btn(
            "audio-volume-high-symbolic",
            "Subir volumen",
            lambda _: self._send_key("vol_up"),
        )
        self.mirror_controls_box.append(btn_vol_up)

        btn_vol_down = self._make_sidebar_btn(
            "audio-volume-low-symbolic",
            "Bajar volumen",
            lambda _: self._send_key("vol_down"),
        )
        self.mirror_controls_box.append(btn_vol_down)

        btn_power = self._make_sidebar_btn(
            "system-shutdown-symbolic",
            "Bloquear / Encender pantalla",
            lambda _: self._send_key("power"),
        )
        self.mirror_controls_box.append(btn_power)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        self.mirror_controls_box.append(spacer)

        self.mirror_controls_box.append(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        )

        btn_recents = self._make_sidebar_btn(
            "view-grid-symbolic",
            "Aplicaciones recientes (Multitarea)",
            lambda _: self._send_key("recents"),
        )
        self.mirror_controls_box.append(btn_recents)

        btn_home = self._make_sidebar_btn(
            "user-home-symbolic",
            "Inicio (Home)",
            lambda _: self._send_key("home"),
        )
        self.mirror_controls_box.append(btn_home)

        btn_back = self._make_sidebar_btn(
            "go-previous-symbolic",
            "Atrás (Back)",
            lambda _: self._send_key("back"),
        )
        self.mirror_controls_box.append(btn_back)

        sidebar_inner.append(self.mirror_controls_box)

        # Pinned Stop / Disconnect button at the very bottom of the sidebar
        self.sidebar_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        btn_stop = self._make_sidebar_btn(
            "media-playback-stop-symbolic",
            "Detener transmisión y volver a ajustes",
            lambda _: self._on_disconnect_clicked(None),
        )
        btn_stop.add_css_class("destructive-action")
        self.sidebar_box.append(btn_stop)

        self.phone_root.append(self.sidebar_box)
        self._rebuild_sidebar_lens_popover()

    def _make_sidebar_btn(self, icon_name: str, tooltip: str, callback) -> Gtk.Button:
        btn = Gtk.Button(icon_name=icon_name, tooltip_text=tooltip)
        btn.add_css_class("sidebar-btn")
        btn.add_css_class("flat")
        btn.set_halign(Gtk.Align.CENTER)
        btn.connect("clicked", callback)
        return btn

    # --- Camera Lens Discovery & Live Lens/Facing Switching ---

    def _ensure_cameras_loaded(self, device: Optional[AndroidDevice], force: bool = False):
        if not device or not device.is_ready:
            return
        cache_key = device.model or device.serial
        if not force and cache_key in self._cameras_cache:
            self._apply_detected_lenses(cache_key, self._cameras_cache[cache_key])
            return
        if cache_key in self._cameras_loading:
            return

        self._cameras_loading.add(cache_key)
        self.exact_lens_row.set_subtitle("Detectando lentes del celular...")

        def _worker():
            try:
                lenses = ADBManager.list_cameras(device.serial)
                GLib.idle_add(self._on_cameras_loaded, cache_key, lenses)
            finally:
                self._cameras_loading.discard(cache_key)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_cameras_loaded(self, cache_key: str, lenses: List[CameraLens]):
        if lenses:
            self._cameras_cache[cache_key] = lenses
        self._apply_detected_lenses(cache_key, lenses)
        return False

    def _apply_detected_lenses(self, cache_key: str, lenses: List[CameraLens]):
        self._detected_lenses = list(lenses)
        if lenses:
            n_back = sum(1 for l in lenses if l.facing == "back")
            n_front = sum(1 for l in lenses if l.facing == "front")
            self.exact_lens_row.set_subtitle(
                f"{len(lenses)} lentes detectados ({n_back} traseros, {n_front} frontales)"
            )
        else:
            self.exact_lens_row.set_subtitle(
                "Sin lista detallada (usando selección automática)"
            )
        self._refresh_exact_lens_combo()
        self._rebuild_sidebar_lens_popover()

    def _on_rescan_lenses_clicked(self, _):
        if self.selected_device and self.selected_device.is_ready:
            self._ensure_cameras_loaded(self.selected_device, force=True)

    def _on_facing_row_changed(self, *_):
        if self._updating_lens_combos:
            return
        self.active_camera_id = None
        self._refresh_exact_lens_combo()
        self._hot_swap_headless_webcam_if_running()

    def _on_exact_lens_row_changed(self, *_):
        if self._updating_lens_combos:
            return
        self._refresh_resolution_combo()
        self._hot_swap_headless_webcam_if_running()

    def _on_res_row_changed(self, *_):
        if self._updating_lens_combos:
            return
        idx = self.res_row.get_selected()
        if 0 <= idx < len(self._available_resolutions):
            self.active_requested_res = self._available_resolutions[idx]
        self._hot_swap_headless_webcam_if_running()

    def _on_webcam_rot_changed(self, *_):
        if self._updating_lens_combos:
            return
        idx = self.webcam_rot_row.get_selected()
        if 0 <= idx < len(self._WEBCAM_ROT_VALUES):
            self.active_webcam_rotation = self._WEBCAM_ROT_VALUES[idx]
        self._hot_swap_headless_webcam_if_running()

    def _refresh_exact_lens_combo(self):
        """Filter `self.exact_lens_row` to show lenses matching the selected Rear/Front side."""
        self._updating_lens_combos = True
        try:
            facing = "back" if self.lens_row.get_selected() == 0 else "front"
            side_name = "Trasera" if facing == "back" else "Frontal"
            matching = [l for l in self._detected_lenses if l.facing == facing]

            self._filtered_exact_lenses = [None] + matching
            labels = [f"Automático ({side_name} por defecto)"] + [
                l.label for l in matching
            ]
            self.exact_lens_model.splice(
                0, self.exact_lens_model.get_n_items(), labels
            )

            # Restore selection if active_camera_id matches one of the filtered lenses
            target_idx = 0
            if self.active_camera_id is not None:
                for idx, lens_obj in enumerate(self._filtered_exact_lenses):
                    if lens_obj and lens_obj.camera_id == self.active_camera_id:
                        target_idx = idx
                        break
            self.exact_lens_row.set_selected(target_idx)
        finally:
            self._updating_lens_combos = False
        self._refresh_resolution_combo()

    def _refresh_resolution_combo(self):
        """Populate `self.res_row` dynamically with all valid video resolutions of the chosen lens."""
        self._updating_lens_combos = True
        try:
            chosen_lens: Optional[CameraLens] = None
            exact_idx = self.exact_lens_row.get_selected()
            if 0 <= exact_idx < len(self._filtered_exact_lenses):
                chosen_lens = self._filtered_exact_lenses[exact_idx]

            if chosen_lens is None:
                facing = "back" if self.lens_row.get_selected() == 0 else "front"
                side_lenses = [
                    l for l in self._detected_lenses if l.facing == facing
                ]
                if side_lenses:
                    chosen_lens = side_lenses[0]

            if chosen_lens is not None:
                pairs = chosen_lens.video_resolutions()
            else:
                pairs = [
                    (
                        "1920x1080",
                        CameraLens.describe_resolution("1920x1080"),
                    ),
                    (
                        "3840x2160",
                        CameraLens.describe_resolution("3840x2160"),
                    ),
                    (
                        "1280x720",
                        CameraLens.describe_resolution("1280x720"),
                    ),
                ]

            self._available_resolutions = [p[0] for p in pairs]
            labels = [p[1] for p in pairs]
            self.res_model.splice(0, self.res_model.get_n_items(), labels)

            # Preserve previously chosen resolution if supported, else prefer 1920x1080 (best Meet/OBS balance), else top
            target_idx = 0
            if self.active_requested_res in self._available_resolutions:
                target_idx = self._available_resolutions.index(
                    self.active_requested_res
                )
            elif "1920x1080" in self._available_resolutions:
                target_idx = self._available_resolutions.index("1920x1080")

            self.res_row.set_selected(target_idx)
            if 0 <= target_idx < len(self._available_resolutions):
                self.active_requested_res = self._available_resolutions[target_idx]
        finally:
            self._updating_lens_combos = False

    def _rebuild_sidebar_lens_popover(self):
        """Populate the live Control Bar Popover with every detected Rear and Front lens."""
        child = self.lens_popover_content.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.lens_popover_content.remove(child)
            child = nxt

        back_lenses = [l for l in self._detected_lenses if l.facing == "back"]
        front_lenses = [l for l in self._detected_lenses if l.facing == "front"]
        other_lenses = [
            l for l in self._detected_lenses if l.facing not in ("back", "front")
        ]

        if not self._detected_lenses:
            lbl = Gtk.Label(label="Detectando lentes con scrcpy...")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            lbl.set_margin_start(8)
            lbl.set_margin_end(8)
            self.lens_popover_content.append(lbl)

            btn_scan = Gtk.Button(label="🔄 Detectar lentes ahora")
            btn_scan.add_css_class("flat")
            btn_scan.connect("clicked", self._on_rescan_lenses_clicked)
            self.lens_popover_content.append(btn_scan)

            # Always provide Rear / Front fallback buttons even before scan completes
            self.lens_popover_content.append(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            )
            for f_key, f_title in (("back", "📷 Cámara Trasera (Auto)"), ("front", "🤳 Cámara Frontal (Auto)")):
                is_cur = (
                    self.active_camera_id is None and self.active_camera_facing == f_key
                )
                b = Gtk.Button(label=f"{'● ' if is_cur else '○ '}{f_title}")
                b.add_css_class("lens-item-btn")
                b.add_css_class("suggested-action" if is_cur else "flat")
                b.set_halign(Gtk.Align.FILL)
                b.connect(
                    "clicked",
                    lambda _, fk=f_key: self._switch_live_camera(facing=fk, lens=None),
                )
                self.lens_popover_content.append(b)
            return

        def _add_group(header_text: str, group_facing: str, items: List[CameraLens]):
            if not items:
                return
            hdr = Gtk.Label(label=header_text, xalign=0.0)
            hdr.add_css_class("lens-section-title")
            self.lens_popover_content.append(hdr)

            for idx, lens in enumerate(items):
                is_active = (
                    self.active_camera_id == lens.camera_id
                    or (
                        self.active_camera_id is None
                        and self.active_camera_facing == group_facing
                        and idx == 0
                    )
                )
                prefix = "● " if is_active else "○ "
                btn = Gtk.Button(label=f"{prefix}{lens.label}")
                btn.add_css_class("lens-item-btn")
                btn.add_css_class("suggested-action" if is_active else "flat")
                btn.set_halign(Gtk.Align.FILL)
                if btn.get_child() and isinstance(btn.get_child(), Gtk.Label):
                    btn.get_child().set_xalign(0.0)
                btn.connect(
                    "clicked",
                    lambda _, l=lens: self._switch_live_camera(
                        facing=l.facing, lens=l
                    ),
                )
                self.lens_popover_content.append(btn)

        _add_group("📷 LENTES TRASEROS", "back", back_lenses)
        if back_lenses and front_lenses:
            self.lens_popover_content.append(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            )
        _add_group("🤳 LENTES FRONTALES", "front", front_lenses)
        if other_lenses:
            self.lens_popover_content.append(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            )
            _add_group("🎥 OTROS LENTES", "external", other_lenses)

    def _toggle_live_camera_facing(self):
        """1-click live switch between Front and Rear camera from the Control Bar."""
        if not self.runner.is_running or self.active_session_mode != "camera":
            return
        new_facing = "front" if self.active_camera_facing == "back" else "back"
        candidates = [l for l in self._detected_lenses if l.facing == new_facing]
        chosen_lens = candidates[0] if candidates else None
        self._switch_live_camera(facing=new_facing, lens=chosen_lens)

    def _switch_live_camera(self, facing: str, lens: Optional[CameraLens]):
        """Hot-swap exact camera lens (`--camera-id`) or facing (`front`/`back`) live inside Phone View."""
        self.lens_popover.popdown()
        if not self.selected_device:
            return

        norm_facing = "front" if facing == "front" else "back"
        new_cam_id = lens.camera_id if lens else None

        # Sync Settings combos cleanly without recursion
        self._updating_lens_combos = True
        try:
            self.lens_row.set_selected(1 if norm_facing == "front" else 0)
        finally:
            self._updating_lens_combos = False

        self.active_camera_facing = norm_facing
        self.active_camera_id = new_cam_id
        self._refresh_exact_lens_combo()
        self._rebuild_sidebar_lens_popover()

        if not self.runner.is_running or self.active_session_mode != "camera":
            return

        effective_res = (
            lens.best_resolution_for(self.active_requested_res)
            if lens
            else self.active_requested_res
        )
        desc = (
            lens.short_label
            if lens
            else ("Cámara Frontal" if norm_facing == "front" else "Cámara Trasera")
        )

        self._camera_switch_seq += 1
        seq = self._camera_switch_seq

        # Release V4L2 reader immediately on main thread and show smooth transition spinner
        self._stop_gstreamer()
        self._received_first_frame = False
        self._latest_frame = None
        self.loading_label.set_label(f"Cambiando a {desc}...")
        self.loading_box.set_visible(True)
        self.spinner.start()
        self._apply_flip_for_current_state()
        self._update_header_title()

        new_config = StudioSessionConfig(
            serial=self.selected_device.serial,
            mode="camera",
            orientation=self.current_orientation,
            camera_facing=norm_facing,
            camera_id=new_cam_id,
            resolution=effective_res,
            mirror_size=self.device_screen_size,
            fps=30,
            record=self.active_record,
            device_label=self.selected_device.webcam_label,
        )

        def _swap_worker():
            if seq != self._camera_switch_seq:
                return
            ok = self.runner.start_session(
                new_config,
                on_stream_ready=lambda v4l2_dev: GLib.idle_add(
                    self._start_gstreamer, v4l2_dev
                ),
                on_stop=lambda: GLib.idle_add(self._on_session_stopped),
            )
            if not ok and seq == self._camera_switch_seq:
                GLib.idle_add(self._on_session_stopped)

        threading.Thread(target=_swap_worker, daemon=True).start()

    # --- View & Dynamic Aspect-Ratio Window Geometry ---

    def _get_header_height(self) -> int:
        _, nat_h, _, _ = self.header.measure(Gtk.Orientation.VERTICAL, -1)
        return nat_h if nat_h > 20 else self.HEADER_HEIGHT

    def _switch_to_view(self, view_name: str):
        self.current_view = view_name
        is_phone = view_name == "phone"

        # Detach inactive child from self.body_bin so its width is 100% removed from widget tree
        if is_phone:
            self.settings_box.set_size_request(-1, -1)
            self.body_bin.set_child(self.phone_root)
            is_cam = self.active_session_mode == "camera"
            self.cam_controls_box.set_visible(is_cam)
            self.mirror_controls_box.set_visible(not is_cam)
        else:
            if self.is_fullscreen():
                self.unfullscreen()
            self.body_bin.set_child(self.settings_box)

        # Header corner icons visibility
        self.btn_refresh.set_visible(not is_phone)
        self.btn_help.set_visible(not is_phone)
        self.btn_control_bar.set_visible(is_phone)
        self.btn_fullscreen.set_visible(is_phone)
        self.btn_tile_mode.set_visible(is_phone)
        self.btn_settings.set_visible(
            is_phone
            or (
                self.runner.is_running
                and self.active_session_mode not in ("otg", "webcam")
            )
        )

        if is_phone:
            self.btn_settings.set_tooltip_text("Abrir Ajustes")
            self._update_header_title()
        else:
            self.btn_settings.set_tooltip_text("Volver al Teléfono")
            self.title_label.set_label("Scrcpy Studio")
            self.title_label.remove_css_class("rec-badge")

        self._sync_window_geometry()

    def _update_header_title(self):
        model_name = (
            self.selected_device.webcam_label if self.selected_device else "Teléfono"
        )
        if self.runner.is_running and self.runner.current_recording_path:
            self.title_label.set_label("● REC")
            self.title_label.set_tooltip_text(f"Grabando {model_name}")
            self.title_label.add_css_class("rec-badge")
        elif self.active_session_mode == "camera" and self.active_camera_id is not None:
            self.title_label.set_label(f"{model_name} · Lente {self.active_camera_id}")
            self.title_label.set_tooltip_text(model_name)
            self.title_label.remove_css_class("rec-badge")
        else:
            self.title_label.set_label(model_name)
            self.title_label.set_tooltip_text(model_name)
            self.title_label.remove_css_class("rec-badge")

    def _compute_exact_video_box_size(self) -> tuple[int, int]:
        """Compute exact pixel width & height matching the true phone/camera aspect ratio."""
        aw = max(1, self.stream_aspect_w)
        ah = max(1, self.stream_aspect_h)
        ratio = min(aw, ah) / max(aw, ah)

        if self.current_orientation == "vertical":
            video_h = self.TARGET_PORTRAIT_H
            video_w = int(round(video_h * ratio))
        else:
            video_w = self.TARGET_LANDSCAPE_W
            video_h = int(round(video_w * ratio))

        return video_w, video_h

    def _sync_window_geometry(self):
        """Enforce exact 1:1 phone aspect ratio in Floating mode, narrow docked column in Tiled mode,
        and centered scaling in Fullscreen mode."""
        header_h = self._get_header_height()

        # 1. Fullscreen Mode (centers vertical phone or fills screen with horizontal movie)
        if self.is_fullscreen():
            self.video_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.video_overlay.set_size_request(200, 200)
            self.video_overlay.set_hexpand(True)
            self.video_overlay.set_vexpand(True)
            self.set_resizable(True)
            self.queue_resize()
            return

        # 2. Settings View
        if self.current_view == "settings":
            self.video_picture.set_paintable(None)
            self._last_video_box = (0, 0)
            target_w = self.SETTINGS_WIDTH
            target_h = self.SETTINGS_HEIGHT
            if self.is_tiled_mode:
                self.settings_box.set_size_request(340, 400)
                self.set_default_size(target_w, target_h)
                self.set_resizable(True)
                self.queue_resize()
                HyprlandRulesManager.set_window_tiled(
                    True, target_w, target_h, orientation="vertical"
                )
            else:
                self.settings_box.set_size_request(
                    target_w, max(420, target_h - header_h)
                )
                self.set_default_size(1, 1)
                self.set_resizable(False)
                self.queue_resize()
                HyprlandRulesManager.set_window_tiled(
                    False, target_w, target_h, orientation="vertical"
                )
            return

        # 3. Phone View
        video_w, video_h = self._compute_exact_video_box_size()
        sidebar_extra = self.SIDEBAR_WIDTH if self.btn_control_bar.get_active() else 0
        target_w = video_w + sidebar_extra
        target_h = video_h + header_h

        # 3A. Docked / Tiled in Hyprland (vertical column alongside terminals without covering them)
        if self.is_tiled_mode:
            self.video_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.video_overlay.set_size_request(200, 200)
            self.video_overlay.set_hexpand(True)
            self.video_overlay.set_vexpand(True)
            self.set_default_size(target_w, target_h)
            self.set_resizable(True)
            self.queue_resize()
            if self.video_picture.get_paintable() is not None:
                self.video_picture.queue_draw()
            HyprlandRulesManager.set_window_tiled(
                True, target_w, target_h, orientation=self.current_orientation
            )
            return

        # 3B. Floating Phone Mode (exact phone aspect ratio 440x952, zero black bars, zero stretching)
        if (video_w, video_h) != self._last_video_box:
            self.video_picture.set_paintable(None)
            self._last_video_box = (video_w, video_h)

        self.video_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.video_overlay.set_hexpand(True)
        self.video_overlay.set_vexpand(True)
        self.video_overlay.set_size_request(video_w, video_h)
        self.set_default_size(1, 1)
        self.set_resizable(False)
        self.queue_resize()
        HyprlandRulesManager.set_window_tiled(
            False, target_w, target_h, orientation=self.current_orientation
        )

    def _on_settings_icon_clicked(self, _):
        """Top-left ⚙️ button toggles between Phone view and Settings view."""
        if self.current_view == "phone":
            self._switch_to_view("settings")
        elif self.runner.is_running and self.active_session_mode not in (
            "otg",
            "webcam",
        ):
            self._switch_to_view("phone")

    def _on_control_bar_toggled(self, toggle_btn: Gtk.ToggleButton):
        """Top-right 🎛️ button unfolds/collapses the vertical control bar (+52px / -52px)."""
        is_open = toggle_btn.get_active()
        self.sidebar_box.set_visible(is_open)
        if is_open and self.active_session_mode == "camera" and self.selected_device:
            self._ensure_cameras_loaded(self.selected_device)
        self._sync_window_geometry()

    def _on_fullscreen_clicked(self, _):
        """Top-right ⛶ button toggles Fullscreen mode."""
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.video_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.video_overlay.set_size_request(200, 200)
            self.set_resizable(True)
            self.queue_resize()
            self.fullscreen()

    def _on_tile_mode_toggled(self, toggle_btn: Gtk.ToggleButton):
        """Top-left 🗔 button docks/undocks the phone into Hyprland's tiling layout as a vertical column."""
        self.is_tiled_mode = toggle_btn.get_active()
        if self.is_tiled_mode and self.is_fullscreen():
            self.unfullscreen()

        if self.is_tiled_mode:
            toggle_btn.set_tooltip_text(
                "Volver a ventana flotante con formato de celular"
            )
        else:
            toggle_btn.set_tooltip_text(
                "Acoplar en mosaico Hyprland (columna vertical igual que una terminal)"
            )
        self._sync_window_geometry()

    def _on_orientation_row_changed(self, combo, _):
        """Dynamically apply orientation changes even when selected inside Settings while running."""
        new_orient = "vertical" if combo.get_selected() == 0 else "horizontal"
        if new_orient != self.current_orientation:
            self.current_orientation = new_orient
            self._apply_flip_for_current_state()
            if self.current_view == "phone":
                self._sync_window_geometry()

    def _toggle_live_orientation(self):
        """Flip between Vertical and Horizontal live without restarting scrcpy."""
        if self.current_orientation == "vertical":
            self.orientation_row.set_selected(1)
        else:
            self.orientation_row.set_selected(0)

    def _apply_flip_for_current_state(self):
        """Calculate and apply GStreamer videoflip direction so image matches Vertical/Horizontal window."""
        if self.runner.is_running:
            is_camera = self.active_session_mode == "camera"
            is_front = self.active_camera_facing == "front"
        else:
            is_camera = self._is_camera_mode_selected()
            is_front = self.lens_row.get_selected() == 1

        if is_camera:
            if self.current_orientation == "vertical":
                self.current_flip_direction = 6 if is_front else 1
            else:
                self.current_flip_direction = 4 if is_front else 0
        else:
            if self.current_orientation == "vertical":
                self.current_flip_direction = 0
            else:
                self.current_flip_direction = 3

        if self.gst_flip is not None:
            try:
                self.gst_flip.set_property(
                    "video-direction", self.current_flip_direction
                )
            except Exception as e:
                print(f"[Studio] Error setting video-direction: {e}")

    # --- Device Discovery, Manual Wi-Fi Pairing/Connect & Session Lifecycle ---

    def _on_wifi_pair_clicked(self, _):
        raw_ip = self.entry_wifi_ip.get_text()
        raw_port = self.entry_pair_port.get_text()
        raw_code = self.entry_pair_code.get_text()
        conn_port = self.entry_conn_port.get_text().strip()

        self.btn_wifi_pair.set_sensitive(False)
        self.wifi_expander.set_subtitle("⏳ Vinculando con el celular...")

        def _worker():
            ok, msg = ADBManager.pair_wifi(raw_ip, raw_port, raw_code)
            devices = ADBManager.get_devices(auto_heal=False)

            # If paired on a public/university Wi-Fi where mDNS is blocked and user filled main port, connect it
            if ok and not any(d.is_ready for d in devices) and conn_port:
                c_ok, c_msg = ADBManager.connect_wifi_manual(raw_ip, conn_port)
                if c_ok:
                    msg = c_msg
                devices = ADBManager.get_devices(auto_heal=False)

            def _done():
                self.btn_wifi_pair.set_sensitive(True)
                self._apply_devices_list(devices)
                has_ready = any(d.is_ready for d in devices)
                if ok and has_ready:
                    self.entry_pair_code.set_text("")
                    self.entry_pair_port.set_text("")
                    self.wifi_expander.set_subtitle("✅ Vinculado y conectado")
                    self.wifi_expander.set_expanded(False)
                elif ok:
                    self.entry_pair_code.set_text("")
                    self.entry_pair_port.set_text("")
                    self.wifi_expander.set_subtitle(
                        "✅ Vinculado — si esta red bloquea auto-detección, pon el puerto abajo en Conectar"
                    )
                else:
                    self.wifi_expander.set_subtitle(f"⚠️ {msg}")
                return False

            GLib.idle_add(_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_wifi_connect_clicked(self, _):
        raw_ip = self.entry_wifi_ip.get_text()
        raw_port = self.entry_conn_port.get_text()

        self.btn_wifi_conn.set_sensitive(False)
        self.wifi_expander.set_subtitle("⏳ Conectando por Wi-Fi...")

        def _worker():
            ok, msg = ADBManager.connect_wifi_manual(raw_ip, raw_port)
            devices = ADBManager.get_devices(auto_heal=False)

            def _done():
                self.btn_wifi_conn.set_sensitive(True)
                self._apply_devices_list(devices)
                if ok:
                    self.wifi_expander.set_subtitle(f"✅ {msg}")
                    self.wifi_expander.set_expanded(False)
                else:
                    self.wifi_expander.set_subtitle(f"⚠️ {msg}")
                return False

            GLib.idle_add(_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _has_physical_usb_phone(self) -> bool:
        return bool(self.usb_otg_devices) or any(
            d.connection_type == "usb" for d in self.devices
        )

    def _sync_v4l2_label_async(self):
        """Pre-rename `/dev/videoN` to the connected phone's name as soon as it connects (when idle)."""
        if (
            self.runner.is_running
            or self._v4l2_label_sync_busy
            or not self.selected_device
            or not self.selected_device.is_ready
            or not self.runner.can_manage_dynamic_label()
        ):
            return
        desired = self.runner.sanitize_card_label(self.selected_device.webcam_label)
        if self.runner.v4l2_label == desired:
            return

        self._v4l2_label_sync_busy = True

        def _worker():
            try:
                self.runner.ensure_v4l2_device_for_phone(desired)
                GLib.idle_add(self._refresh_webcam_ui_state)
            finally:
                self._v4l2_label_sync_busy = False

        threading.Thread(target=_worker, daemon=True).start()

    def _on_enable_dynamic_v4l2_clicked(self, _):
        desired = (
            self.selected_device.webcam_label
            if (self.selected_device and self.selected_device.is_ready)
            else "scrcpy-studio"
        )
        self.btn_enable_dyn_name.set_sensitive(False)
        self.webcam_name_row.set_subtitle("⏳ Solicitando permiso único del sistema (Polkit)...")

        def _worker():
            ok, msg = self.runner.enable_dynamic_v4l2_permissions_pkexec(desired)

            def _done():
                self.btn_enable_dyn_name.set_sensitive(True)
                self._refresh_webcam_ui_state()
                if not ok:
                    self.webcam_name_row.set_subtitle(f"⚠️ {msg}")
                return False

            GLib.idle_add(_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_webcam_ui_state(self):
        dyn_ready = self.runner.can_manage_dynamic_label()
        self.btn_enable_dyn_name.set_visible(not dyn_ready)

        cur_dev = self.runner.v4l2_device
        cur_lbl = self.runner.get_v4l2_device_label(cur_dev)
        self.runner.v4l2_label = cur_lbl

        if self.runner.is_running and self.active_session_mode == "webcam":
            side = (
                f"Lente ID {self.active_camera_id}"
                if self.active_camera_id is not None
                else ("Frontal" if self.active_camera_facing == "front" else "Trasera")
            )
            res_pretty = self.active_requested_res.replace("x", "×")
            self.webcam_name_row.set_title(f"🟢 Webcam Activa: {cur_lbl}")
            self.webcam_name_row.set_subtitle(
                f"{side} · {res_pretty} en {cur_dev} (Elígela así en Google Meet / OBS)"
            )
            return

        if dyn_ready:
            preview_lbl = (
                self.runner.sanitize_card_label(self.selected_device.webcam_label)
                if (self.selected_device and self.selected_device.is_ready)
                else cur_lbl
            )
            self.webcam_name_row.set_title(f"🎥 Nombre en Meet/OBS: {preview_lbl}")
            self.webcam_name_row.set_subtitle(
                f"Cámara virtual {cur_dev} · Cambia automáticamente al nombre del celular conectado"
            )
        else:
            self.webcam_name_row.set_title(f"🎥 Nombre actual en Meet/OBS: {cur_lbl}")
            self.webcam_name_row.set_subtitle(
                f"Pulsa 'Renombrar en RAM' para cambiar '{cur_lbl}' temporalmente en memoria sin tocar archivos de arranque"
            )

    def _refresh_otg_ui_state(self):
        """Update OTG/Webcam status cards and main Connect button sensitivity."""
        if self.runner.is_running and self.active_session_mode == "otg":
            self.otg_status_row.set_title("🟢 Control OTG Activo")
            self.otg_status_row.set_subtitle(
                "Haz clic en 'Trackpad OTG' para usar teclado/ratón · Alt Izq suelta el cursor"
            )
        elif self.usb_otg_devices:
            dev = self.usb_otg_devices[0]
            self.otg_status_row.set_title(f"✅ Cable USB detectado: {dev.display_label}")
            self.otg_status_row.set_subtitle(
                "Listo para iniciar (no necesita Depuración USB activada)"
            )
        elif any(d.connection_type == "usb" for d in self.devices):
            usb_d = next(d for d in self.devices if d.connection_type == "usb")
            self.otg_status_row.set_title(
                f"✅ Cable USB detectado: {usb_d.webcam_label}"
            )
            self.otg_status_row.set_subtitle(
                "Listo para iniciar teclado y ratón físico por USB"
            )
        else:
            self.otg_status_row.set_title("⚠️ Conecta el celular por cable USB a la PC")
            self.otg_status_row.set_subtitle(
                "El Modo OTG simula hardware USB real y no funciona por Wi-Fi"
            )

        self._refresh_webcam_ui_state()

        mode_sel = self._selected_mode_key()
        is_otg = mode_sel == "otg"
        is_webcam = mode_sel == "webcam"
        if not self.runner.is_running:
            if is_otg:
                self.btn_connect.set_label("▶ INICIAR CONTROL OTG")
            elif is_webcam:
                self.btn_connect.set_label("▶ ACTIVAR WEBCAM")
            else:
                self.btn_connect.set_label("▶ CONECTAR E INICIAR")

            if is_otg:
                self.btn_connect.set_sensitive(self._has_physical_usb_phone())
            else:
                self.btn_connect.set_sensitive(
                    bool(self.selected_device and self.selected_device.is_ready)
                )
        else:
            if self.active_session_mode == "otg" and is_otg:
                self.btn_connect.set_label("🔄 REINICIAR OTG")
                self.btn_connect.set_sensitive(self._has_physical_usb_phone())
            elif self.active_session_mode == "webcam" and is_webcam:
                self.btn_connect.set_label("🔄 REINICIAR WEBCAM")
                self.btn_connect.set_sensitive(
                    bool(self.selected_device and self.selected_device.is_ready)
                )
            else:
                self.btn_connect.set_label("🔄 APLICAR CAMBIOS")
                if is_otg:
                    self.btn_connect.set_sensitive(self._has_physical_usb_phone())
                else:
                    self.btn_connect.set_sensitive(
                        bool(self.selected_device and self.selected_device.is_ready)
                    )

    def _apply_devices_list(self, devices: list, usb_otg: Optional[List[UsbOtgDevice]] = None):
        prev_serial = self.selected_device.serial if self.selected_device else None
        self.devices = devices
        if usb_otg is not None:
            self.usb_otg_devices = usb_otg
        else:
            self.usb_otg_devices = ADBManager.detect_usb_otg_devices()

        new_items = [d.display_name for d in self.devices]

        if not new_items:
            if self.usb_otg_devices:
                new_items.append(
                    f"🔌 {self.usb_otg_devices[0].display_label} [Solo USB OTG — Sin Depuración]"
                )
            else:
                new_items.append(
                    "No se detectó celular (Activa Depuración o usa Wi-Fi abajo)"
                )

        self.device_model.splice(0, self.device_model.get_n_items(), new_items)

        ready_devices = [d for d in self.devices if d.is_ready]
        if ready_devices:
            # Preserve user's explicit transport choice (e.g. Wi-Fi vs USB) if still ready, else default to [0] (USB first)
            preserved = next(
                (d for d in ready_devices if d.serial == prev_serial), None
            )
            self.selected_device = preserved if preserved else ready_devices[0]
            sel_idx = self.devices.index(self.selected_device)
            if self.device_combo.get_selected() != sel_idx:
                self.device_combo.set_selected(sel_idx)

            if self.selected_device.ip and not self.selected_device.ip.startswith("["):
                self.entry_wifi_ip.set_text(self.selected_device.ip)
                ADBManager.save_wifi_ip(self.selected_device.ip)
            self._ensure_cameras_loaded(self.selected_device)
            self._sync_v4l2_label_async()
        elif self.devices:
            self.selected_device = self.devices[0]
        else:
            self.selected_device = None

        self._refresh_otg_ui_state()
        return False

    def refresh_devices(self, deep: bool = False):
        """Query connected ADB & physical USB OTG devices asynchronously without resetting `adb`."""
        if self._adb_scan_busy:
            return
        self._adb_scan_busy = True

        def _worker():
            try:
                found = ADBManager.get_devices(auto_heal=deep)
                otg_found = ADBManager.detect_usb_otg_devices()
                GLib.idle_add(self._apply_devices_list, found, otg_found)
            finally:
                self._adb_scan_busy = False

        threading.Thread(target=_worker, daemon=True).start()

    def _poll_devices(self) -> bool:
        if (not self.runner.is_running or self.active_session_mode == "otg") and not self._adb_scan_busy:
            self._adb_scan_busy = True

            def _poll_worker():
                try:
                    devices = ADBManager.get_devices(auto_heal=False)
                    otg_found = ADBManager.detect_usb_otg_devices()
                    new_sig = (
                        [(d.serial, d.state) for d in devices],
                        [(u.serial, u.vendor_id, u.product_id) for u in otg_found],
                    )
                    old_sig = (
                        [(d.serial, d.state) for d in self.devices],
                        [(u.serial, u.vendor_id, u.product_id) for u in self.usb_otg_devices],
                    )
                    if new_sig != old_sig:
                        GLib.idle_add(self._apply_devices_list, devices, otg_found)
                finally:
                    self._adb_scan_busy = False

            threading.Thread(target=_poll_worker, daemon=True).start()
        return True

    def _on_device_selected(self, combo, _):
        idx = combo.get_selected()
        if idx < len(self.devices):
            self.selected_device = self.devices[idx]
            if self.selected_device.is_ready:
                self._ensure_cameras_loaded(self.selected_device)
                self._sync_v4l2_label_async()
        self._refresh_otg_ui_state()

    def _on_mode_changed(self, combo, _):
        mode_key = self._selected_mode_key()
        is_webcam = mode_key == "webcam"
        is_any_cam = mode_key in ("webcam", "camera")
        is_otg = mode_key == "otg"

        self.grp_webcam.set_visible(is_webcam)
        self.grp_otg.set_visible(is_otg)
        self.grp_camera.set_visible(not is_otg)
        self.grp_recording.set_visible(not is_otg)
        self.grp_tiling.set_visible(mode_key in ("mirror", "camera"))

        self.orientation_row.set_visible(mode_key in ("mirror", "camera"))
        self.lens_row.set_visible(is_any_cam)
        self.exact_lens_row.set_visible(is_any_cam)
        self.res_row.set_visible(is_any_cam)
        self.webcam_rot_row.set_visible(is_webcam)
        self.audio_row.set_visible(mode_key == "camera")

        if is_any_cam and self.selected_device and self.selected_device.is_ready:
            self._ensure_cameras_loaded(self.selected_device)
            self._sync_v4l2_label_async()
        self._refresh_otg_ui_state()

    def _build_camera_or_mirror_config(self, mode: str) -> StudioSessionConfig:
        self.current_orientation = (
            "vertical" if self.orientation_row.get_selected() == 0 else "horizontal"
        )
        facing = "back" if self.lens_row.get_selected() == 0 else "front"

        res_idx = self.res_row.get_selected()
        if 0 <= res_idx < len(self._available_resolutions):
            requested_res = self._available_resolutions[res_idx]
        else:
            requested_res = "1920x1080"

        rot_idx = self.webcam_rot_row.get_selected()
        webcam_rot = (
            self._WEBCAM_ROT_VALUES[rot_idx]
            if 0 <= rot_idx < len(self._WEBCAM_ROT_VALUES)
            else "0"
        )
        record = self.switch_record.get_active()

        chosen_lens: Optional[CameraLens] = None
        exact_idx = self.exact_lens_row.get_selected()
        if mode in ("camera", "webcam") and 0 <= exact_idx < len(
            self._filtered_exact_lenses
        ):
            chosen_lens = self._filtered_exact_lenses[exact_idx]

        camera_id = chosen_lens.camera_id if chosen_lens else None
        effective_res = (
            chosen_lens.best_resolution_for(requested_res)
            if (mode in ("camera", "webcam") and chosen_lens)
            else requested_res
        )

        self.active_session_mode = mode
        self.active_camera_facing = facing
        self.active_camera_id = camera_id
        self.active_requested_res = effective_res
        self.active_webcam_rotation = webcam_rot
        self.active_record = record
        self._rebuild_sidebar_lens_popover()

        if mode == "mirror":
            raw_w, raw_h = ADBManager.get_screen_size(self.selected_device.serial)
            self.device_screen_size = (raw_w & ~7, raw_h & ~7)
            self.stream_aspect_w, self.stream_aspect_h = self.device_screen_size
        else:
            try:
                rw, rh = [int(v) for v in effective_res.split("x", 1)]
                self.stream_aspect_w, self.stream_aspect_h = min(rw, rh), max(rw, rh)
            except Exception:
                self.stream_aspect_w, self.stream_aspect_h = 1080, 1920

        layout_idx = self.layout_mode_row.get_selected()
        if layout_idx == 1:
            initial_layout = "tiled_left"
            tiling_side = "left"
        elif layout_idx == 2:
            initial_layout = "tiled_right"
            tiling_side = "right"
        else:
            initial_layout = "float"
            tiling_side = "left"

        audio_idx = self.audio_row.get_selected()
        if audio_idx == 1:
            audio_mode = "record_only"
        elif audio_idx == 2:
            audio_mode = "playback"
        else:
            audio_mode = "mute"

        return StudioSessionConfig(
            serial=self.selected_device.serial,
            mode=mode,
            orientation=self.current_orientation,
            camera_facing=facing,
            camera_id=camera_id,
            resolution=effective_res,
            mirror_size=self.device_screen_size,
            fps=30,
            record=record,
            device_label=self.selected_device.webcam_label,
            webcam_rotation=webcam_rot,
            tiling_side=tiling_side,
            audio_mode=audio_mode,
            initial_layout=initial_layout,
        )

    def _on_layout_mode_row_changed(self, combo, _):
        idx = combo.get_selected()
        side = "right" if idx == 2 else "left"
        HyprlandRulesManager.set_tiling_side(side)

    def _on_headless_webcam_ready(self, _v4l2_dev: str):
        self._refresh_webcam_ui_state()
        return False

    def _hot_swap_headless_webcam_if_running(self):
        """Hot-swap camera side, exact lens, resolution, or rotation live while Webcam Directa is running."""
        if (
            not self.runner.is_running
            or self.active_session_mode != "webcam"
            or self._selected_mode_key() != "webcam"
            or not self.selected_device
            or not self.selected_device.is_ready
        ):
            return

        config = self._build_camera_or_mirror_config("webcam")
        self._camera_switch_seq += 1
        seq = self._camera_switch_seq
        self.webcam_name_row.set_subtitle(
            "⏳ Cambiando lente/calidad de la webcam en vivo..."
        )

        def _swap_worker():
            if seq != self._camera_switch_seq:
                return
            ok = self.runner.start_session(
                config,
                on_stream_ready=lambda dev: GLib.idle_add(
                    self._on_headless_webcam_ready, dev
                ),
                on_stop=lambda: GLib.idle_add(self._on_session_stopped),
            )
            if ok and seq == self._camera_switch_seq:
                GLib.idle_add(self._refresh_otg_ui_state)
            elif not ok and seq == self._camera_switch_seq:
                GLib.idle_add(self._on_session_stopped)

        threading.Thread(target=_swap_worker, daemon=True).start()

    def _on_connect_clicked(self, _):
        """Start Mirror, Headless Direct Webcam, Camera Studio, or USB OTG mode."""
        mode = self._selected_mode_key()

        # --- 1. Dedicated USB OTG Mode (Requires physical USB cable, NO USB Debugging / ADB required) ---
        if mode == "otg":
            self.usb_otg_devices = ADBManager.detect_usb_otg_devices()
            if not self._has_physical_usb_phone():
                self._refresh_otg_ui_state()
                return

            usb_serial: Optional[str] = None
            if len(self.usb_otg_devices) > 1 and self.usb_otg_devices[0].serial:
                usb_serial = self.usb_otg_devices[0].serial
            elif (
                len([d for d in self.devices if d.connection_type == "usb"]) > 1
                and self.selected_device
                and self.selected_device.connection_type == "usb"
            ):
                usb_serial = self.selected_device.serial

            self._camera_switch_seq += 1
            self._stop_gstreamer()
            self.active_session_mode = "otg"
            self._switch_to_view("settings")

            ok = self.runner.start_otg_session(
                usb_serial=usb_serial,
                on_stop=lambda: GLib.idle_add(self._on_session_stopped),
            )
            if ok:
                self.btn_disconnect.set_visible(True)
                self._refresh_otg_ui_state()
            else:
                self._on_session_stopped()
            return

        if not self.selected_device or not self.selected_device.is_ready:
            devices = ADBManager.get_devices(auto_heal=True)
            self._apply_devices_list(devices)
            if not self.selected_device or not self.selected_device.is_ready:
                return

        config = self._build_camera_or_mirror_config(mode)
        self._camera_switch_seq += 1
        self._stop_gstreamer()

        # --- 2. Headless Direct Webcam Mode (Meet / OBS / Discord — Zero extra windows, zero local video CPU) ---
        if mode == "webcam":
            self._switch_to_view("settings")
            self.btn_disconnect.set_visible(True)
            self.webcam_name_row.set_title("⏳ Iniciando Webcam...")
            self.webcam_name_row.set_subtitle(
                "Conectando cámara del celular con /dev/video..."
            )
            seq = self._camera_switch_seq

            def _webcam_worker():
                ok = self.runner.start_session(
                    config,
                    on_stream_ready=lambda dev: GLib.idle_add(
                        self._on_headless_webcam_ready, dev
                    ),
                    on_stop=lambda: GLib.idle_add(self._on_session_stopped),
                )
                if ok and seq == self._camera_switch_seq:
                    GLib.idle_add(self._refresh_otg_ui_state)
                elif not ok and seq == self._camera_switch_seq:
                    GLib.idle_add(self._on_session_stopped)

            threading.Thread(target=_webcam_worker, daemon=True).start()
            return

        # --- 3. Native Ultra-Fast Mirror & Camera Session (Hides Studio Panel -> 1 Single Window) ---
        self._switch_to_view("settings")
        self.btn_connect.set_label("🔄 EN EJECUCIÓN")
        self.btn_disconnect.set_visible(True)
        self.set_visible(False)  # Hide Studio window so only the phone window is on screen
        seq = self._camera_switch_seq

        def _native_session_worker():
            ok = self.runner.start_session(
                config,
                on_stop=lambda: GLib.idle_add(self._on_session_stopped),
            )
            if not ok and seq == self._camera_switch_seq:
                GLib.idle_add(self._on_session_stopped)

        threading.Thread(target=_native_session_worker, daemon=True).start()

    def _on_disconnect_clicked(self, _):
        self._camera_switch_seq += 1
        self._stop_gstreamer()
        if self.runner.is_running:
            self.runner.stop_session()
        self._on_session_stopped()

    def _on_session_stopped(self):
        self._stop_gstreamer()
        self.spinner.stop()
        self.video_picture.set_paintable(None)
        self._latest_frame = None
        self.btn_connect.set_label("▶ CONECTAR E INICIAR")
        self.btn_disconnect.set_visible(False)
        self._switch_to_view("settings")
        self._refresh_otg_ui_state()
        self.set_visible(True)
        self.present()
        return False

    # --- Embedded GStreamer Video Pipeline ---

    def _start_gstreamer(self, v4l2_dev: str):
        """Launch GStreamer pipeline reading full native resolution from v4l2loopback into Gtk.Picture."""
        self._stop_gstreamer()
        self._apply_flip_for_current_state()

        pipeline_desc = (
            f"v4l2src device={v4l2_dev} ! "
            f"videoflip name=flip video-direction={self.current_flip_direction} ! "
            "videoconvert ! "
            "video/x-raw,format=RGB ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        print(f"[Studio] Starting GStreamer pipeline: {pipeline_desc}")

        try:
            self.gst_pipeline = Gst.parse_launch(pipeline_desc)
            self.gst_flip = self.gst_pipeline.get_by_name("flip")
            sink = self.gst_pipeline.get_by_name("sink")
            sink.connect("new-sample", self._on_new_sample)

            bus = self.gst_pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_gst_error, v4l2_dev)

            self.gst_pipeline.set_state(Gst.State.PLAYING)

            # Fallback retry if v4l2 device needed an extra moment
            GLib.timeout_add(1500, self._check_stream_health, v4l2_dev)
        except Exception as e:
            print(f"[Studio] Failed to start GStreamer: {e}")

        return False

    def _check_stream_health(self, v4l2_dev: str):
        if self.runner.is_running and not self._received_first_frame:
            print("[Studio] Retrying GStreamer connection to V4L2 device...")
            self._start_gstreamer(v4l2_dev)
        return False

    def _on_gst_error(self, bus, msg, v4l2_dev: str):
        err, _ = msg.parse_error()
        print(f"[Studio] GStreamer notice: {err.message}")
        if self.runner.is_running and not self._received_first_frame:
            GLib.timeout_add(600, self._start_gstreamer, v4l2_dev)

    def _stop_gstreamer(self):
        if self.gst_pipeline is not None:
            try:
                bus = self.gst_pipeline.get_bus()
                if bus:
                    bus.remove_signal_watch()
                self.gst_pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self.gst_pipeline = None
            self.gst_flip = None

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK

        caps = sample.get_caps()
        if not caps:
            return Gst.FlowReturn.OK

        struct = caps.get_structure(0)
        width = struct.get_value("width")
        height = struct.get_value("height")

        buf = sample.get_buffer()
        success, map_info = buf.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK

        try:
            gbytes = GLib.Bytes.new(map_info.data)
            stride = len(map_info.data) // height if height > 0 else width * 3
            self._latest_frame = (gbytes, width, height, stride)
            if not self._render_pending:
                self._render_pending = True
                GLib.idle_add(self._flush_latest_frame)
        finally:
            buf.unmap(map_info)

        return Gst.FlowReturn.OK

    def _flush_latest_frame(self):
        self._render_pending = False
        frame = self._latest_frame
        if frame is None or self.current_view != "phone":
            return False

        gbytes, width, height, stride = frame

        # Drop any single stale in-flight buffer right across a 90° rotation
        if self.current_orientation == "vertical" and width > height:
            return False
        if self.current_orientation == "horizontal" and height > width:
            return False

        if not self._received_first_frame:
            self._received_first_frame = True
            self.spinner.stop()
            self.loading_box.set_visible(False)

        # Lock onto the 100% real pixel aspect ratio of the decoded stream frame
        live_min = min(width, height)
        live_max = max(width, height)
        cur_min = min(self.stream_aspect_w, self.stream_aspect_h)
        cur_max = max(self.stream_aspect_w, self.stream_aspect_h)
        if live_min > 0 and (abs(live_min - cur_min) > 8 or abs(live_max - cur_max) > 8):
            self.stream_aspect_w, self.stream_aspect_h = live_min, live_max
            self._sync_window_geometry()

        texture = Gdk.MemoryTexture.new(
            width,
            height,
            Gdk.MemoryFormat.R8G8B8,
            gbytes,
            stride,
        )
        video_w, video_h = self._compute_exact_video_box_size()
        paintable = _ScaledPaintable(texture, video_w, video_h)
        self.video_picture.set_paintable(paintable)
        return False

    # --- Interactive Touch & Hardware Controls ---

    def _widget_to_device_coords(self, x: float, y: float):
        w = float(max(1, self.video_overlay.get_width()))
        h = float(max(1, self.video_overlay.get_height()))
        dev_w, dev_h = self.device_screen_size

        stream_w, stream_h = (
            (float(dev_w), float(dev_h))
            if self.current_orientation == "vertical"
            else (float(dev_h), float(dev_w))
        )

        # Account for CONTAIN letterboxing when in Fullscreen or Tiled mode
        if self.video_picture.get_content_fit() == Gtk.ContentFit.CONTAIN:
            scale = min(w / stream_w, h / stream_h)
            draw_w = max(1.0, stream_w * scale)
            draw_h = max(1.0, stream_h * scale)
            offset_x = (w - draw_w) / 2.0
            offset_y = (h - draw_h) / 2.0
            rel_x = (x - offset_x) / draw_w
            rel_y = (y - offset_y) / draw_h
        else:
            rel_x = x / w
            rel_y = y / h

        rel_x = min(1.0, max(0.0, rel_x))
        rel_y = min(1.0, max(0.0, rel_y))

        if self.current_orientation == "vertical":
            dx = int(rel_x * dev_w)
            dy = int(rel_y * dev_h)
        else:
            # video-direction=3 is 90° counter-clockwise (90L): invert back to portrait coords
            dx = int((1.0 - rel_y) * dev_w)
            dy = int(rel_x * dev_h)

        return max(0, min(dev_w - 1, dx)), max(0, min(dev_h - 1, dy))

    def _on_video_drag_begin(self, gesture, start_x: float, start_y: float):
        self._drag_start_xy = (start_x, start_y)

    def _on_video_drag_end(self, gesture, offset_x: float, offset_y: float):
        if not self.selected_device or self.active_session_mode != "mirror":
            return
        if abs(offset_x) < 12 and abs(offset_y) < 12:
            return
        sx, sy = self._drag_start_xy
        x1, y1 = self._widget_to_device_coords(sx, sy)
        x2, y2 = self._widget_to_device_coords(sx + offset_x, sy + offset_y)
        ADBManager.send_swipe(self.selected_device.serial, x1, y1, x2, y2, 220)

    def _on_video_clicked(self, gesture, n_press: int, x: float, y: float):
        if not self.selected_device or self.active_session_mode != "mirror":
            return
        btn = gesture.get_current_button()
        if btn == 3:  # Right-click -> BACK!
            ADBManager.send_back(self.selected_device.serial)
            return
        elif btn == 2:  # Middle-click -> HOME!
            ADBManager.send_home(self.selected_device.serial)
            return
        dx, dy = self._widget_to_device_coords(x, y)
        ADBManager.send_tap(self.selected_device.serial, dx, dy)

    def _on_video_scroll(self, controller, dx: float, dy: float) -> bool:
        if not self.selected_device or self.active_session_mode != "mirror":
            return False
        dev_w, dev_h = self.device_screen_size
        mid_x = dev_w // 2
        # dy > 0: scroll down (swipe up, next video in TikTok/Shorts/feeds)
        # dy < 0: scroll up (swipe down, previous video)
        if dy > 0:
            y1 = int(dev_h * 0.72)
            y2 = int(dev_h * 0.28)
            ADBManager.send_swipe(self.selected_device.serial, mid_x, y1, mid_x, y2, 160)
        elif dy < 0:
            y1 = int(dev_h * 0.28)
            y2 = int(dev_h * 0.72)
            ADBManager.send_swipe(self.selected_device.serial, mid_x, y1, mid_x, y2, 160)
        return True

    def _send_key(self, action: str):
        if not self.selected_device:
            return
        serial = self.selected_device.serial
        if action == "back":
            ADBManager.send_back(serial)
        elif action == "home":
            ADBManager.send_home(serial)
        elif action == "recents":
            ADBManager.send_app_switch(serial)
        elif action == "vol_up":
            ADBManager.send_volume_up(serial)
        elif action == "vol_down":
            ADBManager.send_volume_down(serial)
        elif action == "power":
            ADBManager.send_power(serial)

    def _take_screenshot(self):
        if not self.selected_device:
            return
        save_dir = Path.home() / "Imágenes" / "ScrcpyStudio"
        save_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filepath = save_dir / f"screenshot_{timestamp}.png"
        if ADBManager.take_screenshot(self.selected_device.serial, str(filepath)):
            print(f"[Studio] Captura guardada en: {filepath}")

    def _open_videos_folder(self):
        folder = Path.home() / "Videos" / "ScrcpyStudio"
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(folder)])

    def _create_section_help_button(
        self, title: str, items: List[tuple]
    ) -> Gtk.MenuButton:
        """Create a sleek circular (?) button that triggers a contextual Adw/GTK4 Popover."""
        btn = Gtk.MenuButton(icon_name="help-about-symbolic")
        btn.add_css_class("flat")
        btn.add_css_class("circular")
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text(f"Ayuda: {title}")

        popover = Gtk.Popover()
        popover.set_autohide(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_size_request(290, -1)

        heading = Gtk.Label(
            label=f"<b>{GLib.markup_escape_text(title)}</b>",
            use_markup=True,
            xalign=0,
        )
        box.append(heading)

        for item_title, item_desc in items:
            item_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            lbl_title = Gtk.Label(
                label=f"<b>{GLib.markup_escape_text(item_title)}</b>",
                use_markup=True,
                xalign=0,
            )
            lbl_desc = Gtk.Label(label=item_desc, xalign=0)
            lbl_desc.set_wrap(True)
            lbl_desc.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lbl_desc.add_css_class("dim-label")
            item_box.append(lbl_title)
            item_box.append(lbl_desc)
            box.append(item_box)

        popover.set_child(box)
        btn.set_popover(popover)
        return btn

    def _on_show_general_help(self, _):
        """Open a comprehensive GNOME Adw.PreferencesDialog with shortcuts and full guide."""
        dialog = Adw.PreferencesDialog()
        dialog.set_title("Guía y Atajos de Scrcpy Studio")

        # Page 1: Scrcpy Keyboard Shortcuts & Hyprland Tiling
        page_shortcuts = Adw.PreferencesPage()
        page_shortcuts.set_title("Atajos")
        page_shortcuts.set_icon_name("preferences-desktop-keyboard-shortcuts-symbolic")

        # Hyprland Window Management Group
        grp_hyprland = Adw.PreferencesGroup(title="Ventana y Mosaico en Hyprland")
        grp_hyprland.set_description("Controla la posición y anclaje de la ventana del celular")

        hypr_shortcuts = [
            (
                "Super + Alt + Espacio",
                "Acoplar / Desacoplar: conmuta entre ventana flotante y columna vertical",
                "⭐ Clave",
            ),
            (
                "Super + Shift + ← / →",
                "Mover de lado: cambia la columna del celular entre izquierda y derecha",
                "",
            ),
            (
                "Super + Clic Izquierdo",
                "Arrastrar la ventana flotante libremente por la pantalla",
                "",
            ),
            (
                "Super + Clic Derecho",
                "Redimensionar la ventana flotante con el mouse",
                "",
            ),
            (
                "Super + P",
                "Fijar (Pin): mantiene la ventana visible en todos los escritorios",
                "📌 Pin",
            ),
        ]
        for key, desc, tag in hypr_shortcuts:
            row = Adw.ActionRow(
                title=f"<b>{GLib.markup_escape_text(key)}</b>",
                subtitle=GLib.markup_escape_text(desc),
            )
            row.set_use_markup(True)
            if tag:
                badge = Gtk.Label(label=tag)
                badge.add_css_class("caption")
                badge.add_css_class("dim-label")
                row.add_suffix(badge)
            grp_hyprland.add(row)
        page_shortcuts.add(grp_hyprland)

        # Scrcpy shortcuts
        grp_keys = Adw.PreferencesGroup(title="Control del Teléfono (Atajos Scrcpy)")
        grp_keys.set_description(
            'Nota: "Mod" en Scrcpy es la tecla Alt Izquierdo (junto a la barra espaciadora)'
        )

        shortcuts = [
            ("Alt + O", "Apagar la pantalla física del celular (Ahorra ~80% de batería)", "⚡ Vital"),
            ("Alt + P", "Encender la pantalla física del celular", ""),
            ("Alt + F", "Alternar pantalla completa sin bordes", ""),
            ("Alt + R", "Rotar la pantalla 90°", ""),
            ("Alt + H", "Ir a la pantalla de Inicio (Home)", ""),
            ("Alt + B", "Botón Atrás (Back)", ""),
            ("Alt + S", "Alternar entre aplicaciones recientes", ""),
            ("Alt + V", "Pegar portapapeles de la PC en el teléfono", "📋 Clipboard"),
            ("Alt + Shift + V", "Pegar portapapeles como texto simulado", ""),
            ("Alt + N", "Desplegar panel de notificaciones", ""),
            ("Alt + Shift + N", "Ocultar panel de notificaciones", ""),
            ("Alt + ↑ / ↓", "Subir o bajar volumen multimedia", ""),
        ]
        for key, desc, tag in shortcuts:
            row = Adw.ActionRow(
                title=f"<b>{GLib.markup_escape_text(key)}</b>",
                subtitle=GLib.markup_escape_text(desc),
            )
            row.set_use_markup(True)
            if tag:
                badge = Gtk.Label(label=tag)
                badge.add_css_class("caption")
                badge.add_css_class("dim-label")
                row.add_suffix(badge)
            grp_keys.add(row)
        page_shortcuts.add(grp_keys)
        dialog.add(page_shortcuts)

        # Page 2: Wi-Fi Guide
        page_wifi = Adw.PreferencesPage()
        page_wifi.set_title("Conexión Wi-Fi")
        page_wifi.set_icon_name("network-wireless-symbolic")
        grp_wifi = Adw.PreferencesGroup(title="Cómo Conectar sin Cables")

        wifi_items = [
            (
                "Red de Casa (Con mDNS)",
                "Vincula una sola vez con el código pop-up. Luego tu teléfono se descubrirá y conectará solo.",
            ),
            (
                "Universidad / Hotspot / Café",
                "Las redes públicas bloquean el auto-descubrimiento. Pon la IP y el puerto de 'Depuración inalámbrica' y pulsa 'Conectar sesión'.",
            ),
            (
                "Cable USB (Máxima fluidez)",
                "Para sesiones prolongadas o latencia ultra-baja (<15ms), la conexión USB no requiere configuración de red.",
            ),
        ]
        for w_title, w_desc in wifi_items:
            row = Adw.ActionRow(
                title=f"<b>{GLib.markup_escape_text(w_title)}</b>",
                subtitle=GLib.markup_escape_text(w_desc),
            )
            row.set_use_markup(True)
            grp_wifi.add(row)
        page_wifi.add(grp_wifi)
        dialog.add(page_wifi)

        # Page 3: Meet, OBS & Discord
        page_cam = Adw.PreferencesPage()
        page_cam.set_title("Webcam & Streaming")
        page_cam.set_icon_name("camera-web-symbolic")
        grp_cam = Adw.PreferencesGroup(title="Uso de la Cámara en Linux")

        cam_items = [
            (
                "Webcam Directa (Headless)",
                "Envía la cámara directo a /dev/v4l2loopback sin abrir ventana en tu pantalla. Máxima eficiencia para videollamadas.",
            ),
            (
                "Google Meet / Zoom / Discord",
                "Ve a Ajustes de Vídeo en tu app y selecciona 'scrcpy-studio' como tu cámara de entrada.",
            ),
            (
                "Cámara Estudio (Con Visor)",
                "Abre una ventana local de previsualización para encuadrar, alternar lentes gran angular/macro y grabar.",
            ),
            (
                "Renombrar en RAM",
                "Modifica el nombre en memoria volátil sin escribir archivos en disco ni dejar rastros en el sistema.",
            ),
        ]
        for c_title, c_desc in cam_items:
            row = Adw.ActionRow(
                title=f"<b>{GLib.markup_escape_text(c_title)}</b>",
                subtitle=GLib.markup_escape_text(c_desc),
            )
            row.set_use_markup(True)
            grp_cam.add(row)
        page_cam.add(grp_cam)
        dialog.add(page_cam)

        dialog.present(self)
