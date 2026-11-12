"""ADB connection, manual Wi-Fi pairing/connect, and scrcpy camera lens discovery."""

import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class AndroidDevice:
    serial: str
    model: str
    connection_type: str  # "usb" or "tcpip"
    state: str  # "device", "unauthorized", "offline"
    ip: Optional[str] = None
    port: Optional[int] = None
    friendly_name: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "device"

    @property
    def webcam_label(self) -> str:
        """Dynamic V4L2 camera name shown in Google Meet / OBS / Discord for this phone."""
        base = (self.friendly_name or self.model or "Scrcpy Studio").strip()
        return base if base else "Scrcpy Studio"

    @property
    def display_name(self) -> str:
        name = (self.friendly_name or self.model or "Android").strip()
        if self.state == "unauthorized":
            return f"⚠️ {name} [Acepta el permiso USB/ADB en el celular]"
        if self.state == "offline":
            return f"⏳ {name} [Offline - Reconectando...]"

        if self.connection_type == "usb":
            conn = f"⚡ Cable USB (Baja latencia · {self.serial})"
        elif self.ip and self.port:
            conn = f"📶 Wi-Fi ({self.ip}:{self.port})"
        else:
            conn = "📶 Wi-Fi (Auto)"
        return f"{name} [{conn}]"


@dataclass
class UsbOtgDevice:
    """Physical USB Android device detected via Linux sysfs (requires NO USB Debugging / ADB)."""

    serial: str
    vendor_id: str
    product_id: str
    name: str

    @property
    def display_label(self) -> str:
        if self.serial:
            return f"{self.name} ({self.serial})"
        return f"{self.name} ({self.vendor_id}:{self.product_id})"


@dataclass
class CameraLens:
    camera_id: str
    facing: str  # "back", "front", "external"
    max_size: str  # e.g. "4080x3060"
    max_w: int
    max_h: int
    fps_list: List[int] = field(default_factory=list)
    supported_sizes: List[str] = field(default_factory=list)
    focal_mm: Optional[float] = None
    role_hint: str = ""

    @property
    def label(self) -> str:
        pretty_size = self.max_size.replace("x", "×")
        parts = [f"ID {self.camera_id}"]
        if self.role_hint:
            parts.append(self.role_hint)
        if self.focal_mm:
            parts.append(f"{self.focal_mm:.1f}mm")
        return f"{' · '.join(parts)} ({pretty_size})"

    @property
    def short_label(self) -> str:
        role = self.role_hint or ("Frontal" if self.facing == "front" else "Trasera")
        return f"ID {self.camera_id} · {role}"

    def best_resolution_for(self, requested: str) -> str:
        """Return `requested` (e.g. '1920x1080') if supported by this physical lens,
        otherwise fall back to the closest valid resolution so scrcpy never fails."""
        try:
            req_w, req_h = [int(v) for v in requested.split("x", 1)]
        except Exception:
            req_w, req_h = 1920, 1080
        req_pixels = req_w * req_h

        if self.supported_sizes:
            if requested in self.supported_sizes:
                return requested

            # Prefer standard 16:9 sizes <= requested pixel count if available
            preferred_16_9 = ["3840x2160", "2560x1440", "1920x1080", "1280x720"]
            for cand in preferred_16_9:
                if cand in self.supported_sizes:
                    cw, ch = [int(v) for v in cand.split("x", 1)]
                    if cw * ch <= req_pixels:
                        return cand

            # Otherwise pick the largest supported size <= requested pixels (or largest overall)
            parsed: List[Tuple[int, int, str]] = []
            for s in self.supported_sizes:
                if "x" in s:
                    try:
                        sw, sh = [int(v) for v in s.split("x", 1)]
                        parsed.append((sw * sh, sw, s))
                    except ValueError:
                        pass
            if parsed:
                parsed.sort(reverse=True)
                for px, _, s in parsed:
                    if px <= req_pixels:
                        return s
                return parsed[-1][2]

        # Fallback if --list-cameras was used without per-size list
        max_dim = max(self.max_w, self.max_h)
        min_dim = min(self.max_w, self.max_h)
        for cand_w, cand_h in ((req_w, req_h), (1920, 1080), (1280, 720)):
            if max_dim >= max(cand_w, cand_h) and min_dim >= min(cand_w, cand_h):
                return f"{cand_w}x{cand_h}"
        return "1280x720"

    @staticmethod
    def describe_resolution(res: str, is_top_for_lens: bool = False, is_vertical: bool = False) -> str:
        """Human-friendly label for a camera resolution (e.g. '1920x1080')."""
        try:
            w, h = [int(v) for v in res.split("x", 1)]
            disp_w, disp_h = (h, w) if is_vertical else (w, h)
        except Exception:
            disp_w, disp_h = 1920, 1080

        known = {
            "3840x2160": f"{disp_w}×{disp_h} · 4K Ultra HD" + (" (Vertical 9:16)" if is_vertical else " (Máxima)"),
            "2560x1440": f"{disp_w}×{disp_h} · 2K QHD" + (" (Vertical 9:16)" if is_vertical else " (16:9)"),
            "1920x1440": f"{disp_w}×{disp_h} · Full HD+" + (" (Vertical 3:4)" if is_vertical else " (4:3)"),
            "1920x1080": f"{disp_w}×{disp_h} · 1080p Full HD" + (" (Vertical 9:16)" if is_vertical else " (Recomendado Meet/OBS)"),
            "1600x1200": f"{disp_w}×{disp_h} · UXGA" + (" (Vertical 3:4)" if is_vertical else " (4:3)"),
            "1440x1080": f"{disp_w}×{disp_h} · Full HD" + (" (Vertical 3:4)" if is_vertical else " (4:3)"),
            "1280x960": f"{disp_w}×{disp_h} · HD+" + (" (Vertical 3:4)" if is_vertical else " (4:3)"),
            "1280x720": f"{disp_w}×{disp_h} · 720p HD" + (" (Vertical 9:16)" if is_vertical else " (Ligero)"),
            "960x720": f"{disp_w}×{disp_h} · qHD" + (" (Vertical 3:4)" if is_vertical else " (4:3)"),
        }
        if res in known:
            lbl = known[res]
            if res == "1920x1080" and is_top_for_lens and not is_vertical:
                return "1920×1080 · 1080p Full HD (Máxima del lente)"
            return lbl
        try:
            ratio = w / h if h else 1.77
            if is_vertical:
                ar = "9:16" if abs(ratio - 16 / 9) < 0.04 else ("3:4" if abs(ratio - 4 / 3) < 0.04 else f"1:{ratio:.2f}")
            else:
                ar = "16:9" if abs(ratio - 16 / 9) < 0.04 else ("4:3" if abs(ratio - 4 / 3) < 0.04 else f"{ratio:.2f}:1")
            suffix = " · Máxima" if is_top_for_lens else ""
            return f"{disp_w}×{disp_h} ({ar}{suffix})"
        except Exception:
            return res

    def video_resolutions(self, is_vertical: bool = False) -> List[Tuple[str, str]]:
        """Return hardware-encoder-safe video resolutions supported by this lens, sorted highest first."""
        valid: List[Tuple[int, int, str]] = []
        for s in self.supported_sizes:
            if "x" not in s:
                continue
            try:
                w, h = [int(v) for v in s.split("x", 1)]
            except ValueError:
                continue
            # Hardware MediaCodec AVC/HEVC + v4l2loopback require multiples of 8 up to 4K (3840x2160)
            if (
                960 <= w <= 3840
                and 540 <= h <= 2160
                and (w % 8 == 0)
                and (h % 8 == 0)
                and w >= h
            ):
                valid.append((w * h, w, s))

        if not valid:
            # Fallback from lens max_w/max_h
            max_d = max(self.max_w, self.max_h)
            min_d = min(self.max_w, self.max_h)
            for cw, ch in ((3840, 2160), (2560, 1440), (1920, 1080), (1280, 720)):
                if max_d >= cw and min_d >= ch:
                    valid.append((cw * ch, cw, f"{cw}x{ch}"))
            if not valid:
                valid.append((1280 * 720, 1280, "1280x720"))

        valid.sort(reverse=True)
        seen = set()
        ordered: List[str] = []
        for _, _, s in valid:
            if s not in seen:
                seen.add(s)
                ordered.append(s)

        return [
            (s, CameraLens.describe_resolution(s, is_top_for_lens=(idx == 0), is_vertical=is_vertical))
            for idx, s in enumerate(ordered)
        ]


class ADBManager:
    _WIFI_IP_FILE = Path.home() / ".config" / "scrcpy-studio" / "wifi_ip.txt"
    _FRIENDLY_NAME_CACHE: Dict[str, str] = {}

    @staticmethod
    def _cache_key_for_serial(serial: str) -> str:
        m = re.match(r"^adb-([^-]+)-", serial)
        return m.group(1) if m else serial

    @staticmethod
    def get_friendly_device_name(serial: str, fallback_model: str = "") -> str:
        """Query the phone's real user/commercial name (e.g. 'A57 de Marco' or 'Samsung Galaxy A57') once and cache it."""
        key = ADBManager._cache_key_for_serial(serial)
        if key in ADBManager._FRIENDLY_NAME_CACHE:
            return ADBManager._FRIENDLY_NAME_CACHE[key]

        chosen = ""
        try:
            res = subprocess.run(
                [
                    "adb",
                    "-s",
                    serial,
                    "shell",
                    (
                        "settings get global device_name; "
                        "echo '---SEP---'; "
                        "getprop ro.product.marketname; "
                        "echo '---SEP---'; "
                        "getprop ro.config.marketing_name; "
                        "echo '---SEP---'; "
                        "getprop ro.product.manufacturer; "
                        "echo '---SEP---'; "
                        "getprop ro.product.model"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and res.stdout:
                chunks = [c.strip() for c in res.stdout.split("---SEP---")]
                dev_name = chunks[0] if len(chunks) > 0 else ""
                market = chunks[1] if len(chunks) > 1 else ""
                alt_market = chunks[2] if len(chunks) > 2 else ""
                manuf = chunks[3] if len(chunks) > 3 else ""
                model = chunks[4] if len(chunks) > 4 else ""

                for cand in (dev_name, market, alt_market):
                    if cand and cand.lower() not in ("null", "none", "unknown"):
                        chosen = cand
                        break

                if not chosen:
                    if manuf and model:
                        if manuf.lower() in model.lower():
                            chosen = model
                        else:
                            chosen = f"{manuf.title()} {model}"
                    elif model:
                        chosen = model
        except Exception:
            pass

        if not chosen:
            chosen = fallback_model or "Android"

        # Collapse whitespace cleanly
        chosen = " ".join(chosen.split()).strip()
        if chosen and chosen != fallback_model:
            ADBManager._FRIENDLY_NAME_CACHE[key] = chosen
        elif chosen and key:
            ADBManager._FRIENDLY_NAME_CACHE[key] = chosen
        return chosen

    @staticmethod
    def _detect_local_subnet_prefix() -> str:
        """Detect laptop's current LAN IPv4 prefix (e.g. '192.168.100.') for travel/new Wi-Fi."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.4)
            s.connect(("1.1.1.1", 80))
            local_ip = s.getsockname()[0]
            s.close()
            parts = local_ip.split(".")
            if len(parts) == 4 and not local_ip.startswith("127."):
                return f"{parts[0]}.{parts[1]}.{parts[2]}."
        except Exception:
            pass
        return ""

    @staticmethod
    def load_saved_wifi_ip() -> str:
        """Return saved phone IP if still on the same Wi-Fi subnet, or pre-fill new LAN prefix when traveling."""
        prefix = ADBManager._detect_local_subnet_prefix()
        saved = ""
        try:
            if ADBManager._WIFI_IP_FILE.exists():
                saved = ADBManager._WIFI_IP_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass

        if saved:
            # If we are on the same Wi-Fi subnet (or offline/hotspot without WAN route), keep saved IP
            if not prefix or saved.startswith(prefix):
                return saved
            # Laptop moved to a different Wi-Fi network! Pre-fill the new network's prefix
            return prefix

        return prefix or "192.168.100.139"

    @staticmethod
    def save_wifi_ip(ip: str) -> None:
        clean = ip.strip()
        if not clean or clean.endswith("."):
            return
        try:
            ADBManager._WIFI_IP_FILE.parent.mkdir(parents=True, exist_ok=True)
            ADBManager._WIFI_IP_FILE.write_text(clean, encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _normalize_ip_and_port(raw_ip: str, raw_port: str) -> Tuple[str, str]:
        """Allow typing IP and Port separately OR pasting 'IP:Puerto' directly into either box."""
        ip = raw_ip.strip()
        port = raw_port.strip()

        if ":" in port and not port.startswith("["):
            p_ip, p_port = port.rsplit(":", 1)
            if p_ip and p_port.isdigit():
                ip = p_ip.strip()
                port = p_port.strip()

        if ":" in ip and not ip.startswith("["):
            i_ip, i_port = ip.rsplit(":", 1)
            if i_port.isdigit():
                ip = i_ip.strip()
                if not port:
                    port = i_port.strip()

        return ip, port

    @staticmethod
    def pair_wifi(raw_ip: str, raw_port: str, raw_code: str) -> Tuple[bool, str]:
        """Run `adb pair IP:PORT CODE` on user demand without killing or disturbing the `adb` daemon."""
        ip, port = ADBManager._normalize_ip_and_port(raw_ip, raw_port)
        code = raw_code.strip()

        if not ip or ip.endswith("."):
            return False, "Ingresa la IP del celular (ej. 192.168.100.139)"
        if not port or not port.isdigit():
            return False, "Ingresa el puerto de la ventana de vinculación"
        if not code:
            return False, "Ingresa el código de vinculación de 6 dígitos"

        endpoint = f"{ip}:{port}"
        try:
            res = subprocess.run(
                ["adb", "pair", endpoint, code],
                capture_output=True,
                text=True,
                timeout=8,
            )
            out = (res.stdout or res.stderr or "").strip()
            print(f"[ADBManager] adb pair {endpoint} -> {out}")
            if "successfully paired" in out.lower():
                ADBManager.save_wifi_ip(ip)
                # Give native `adb` mDNS TLS auto-connect ~1.2s to attach automatically
                time.sleep(1.2)
                return True, f"Vinculado con éxito ({ip})"
            return False, out or "Código o puerto de vinculación incorrecto"
        except subprocess.TimeoutExpired:
            return (
                False,
                f"Tiempo agotado hacia {endpoint} (verifica que estén en la misma red Wi-Fi o Hotspot)",
            )
        except Exception as e:
            return False, str(e)

    @staticmethod
    def connect_wifi_manual(raw_ip: str, raw_port: str) -> Tuple[bool, str]:
        """Run `adb connect IP:PORT` on user demand (ideal for public/university Wi-Fi or Hotspot)."""
        ip, port = ADBManager._normalize_ip_and_port(raw_ip, raw_port)

        if not ip or ip.endswith("."):
            return False, "Ingresa la IP del celular"
        if not port or not port.isdigit():
            return False, "Ingresa el puerto principal de Depuración inalámbrica"

        endpoint = f"{ip}:{port}"
        try:
            res = subprocess.run(
                ["adb", "connect", endpoint],
                capture_output=True,
                text=True,
                timeout=6,
            )
            out = (res.stdout or res.stderr or "").strip()
            print(f"[ADBManager] adb connect {endpoint} -> {out}")
            ok = (
                ("connected to" in out.lower() or "already connected" in out.lower())
                and "cannot" not in out.lower()
                and "failed" not in out.lower()
            )
            if ok:
                ADBManager.save_wifi_ip(ip)
                return True, f"Conectado a {endpoint}"
            return False, out or f"No se pudo conectar a {endpoint}"
        except subprocess.TimeoutExpired:
            return (
                False,
                f"Sin respuesta de {endpoint} (si el Wi-Fi público aísla clientes, usa el Hotspot del celular)",
            )
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _parse_adb_devices() -> Tuple[List[AndroidDevice], bool]:
        """Run `adb devices -l` cleanly without disturbing the native `adb` mDNS daemon."""
        devices: List[AndroidDevice] = []
        has_offline = False

        try:
            res = subprocess.run(
                ["adb", "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=4,
            )
        except Exception:
            return [], False

        if res.returncode != 0:
            return [], False

        lines = res.stdout.strip().split("\n")[1:]  # skip header
        for line in lines:
            line = line.strip()
            if not line or line.startswith("*"):
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            serial = parts[0]
            state = parts[1]

            if state == "offline":
                has_offline = True
            if state not in ("device", "unauthorized"):
                continue

            model_match = re.search(r"model:([^\s]+)", line)
            product_match = re.search(r"product:([^\s]+)", line)
            device_model = (
                model_match.group(1)
                if model_match
                else (product_match.group(1) if product_match else serial)
            )
            device_model = device_model.replace("_", " ")

            ip = None
            port = None
            if ":" in serial and not serial.startswith("adb-"):
                conn_type = "tcpip"
                try:
                    ip, port_str = serial.rsplit(":", 1)
                    port = int(port_str)
                except ValueError:
                    pass
            elif serial.startswith("adb-"):
                conn_type = "tcpip"
            else:
                conn_type = "usb"

            friendly = (
                ADBManager.get_friendly_device_name(serial, device_model)
                if state == "device"
                else device_model
            )

            devices.append(
                AndroidDevice(
                    serial=serial,
                    model=device_model,
                    connection_type=conn_type,
                    state=state,
                    ip=ip,
                    port=port,
                    friendly_name=friendly,
                )
            )

        # Prioritize ready devices first, and USB before Wi-Fi when both are active
        devices.sort(
            key=lambda d: (
                0 if d.is_ready else 1,
                0 if d.connection_type == "usb" else 1,
                d.serial,
            )
        )
        return devices, has_offline

    @staticmethod
    def get_devices(auto_heal: bool = False) -> List[AndroidDevice]:
        """List connected ADB devices cleanly, preserving native `adb` mDNS TLS auto-connect."""
        devices, has_offline = ADBManager._parse_adb_devices()
        if has_offline and auto_heal:
            try:
                subprocess.run(
                    ["adb", "reconnect", "offline"],
                    capture_output=True,
                    timeout=3,
                )
                devices, _ = ADBManager._parse_adb_devices()
            except Exception:
                pass
        return devices

    _PHONE_USB_VENDORS = {
        "04e8": "Samsung",
        "18d1": "Google / Android",
        "2717": "Xiaomi",
        "22b8": "Motorola",
        "2a70": "OnePlus",
        "22d9": "OPPO / Realme",
        "2d95": "vivo",
        "12d1": "Huawei / Honor",
        "0fce": "Sony",
        "2b4c": "Nothing",
        "1004": "LG",
        "0bb4": "HTC",
        "19d2": "ZTE / Nubia",
        "2916": "Fairphone",
    }

    @staticmethod
    def detect_usb_otg_devices() -> List[UsbOtgDevice]:
        """Detect physical USB-connected Android phones directly from `/sys/bus/usb/devices`
        so `scrcpy --otg` works even when Developer Options & USB Debugging are OFF."""
        found: List[UsbOtgDevice] = []
        sys_usb = Path("/sys/bus/usb/devices")
        if not sys_usb.exists():
            return found

        def _read_sys(p: Path) -> str:
            try:
                return p.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                return ""

        seen_keys = set()
        for dev_dir in sorted(sys_usb.iterdir()):
            vid_file = dev_dir / "idVendor"
            pid_file = dev_dir / "idProduct"
            if not vid_file.exists() or not pid_file.exists():
                continue

            vid = _read_sys(vid_file).lower()
            pid = _read_sys(pid_file).lower()
            if not vid or vid == "1d6b":  # Skip Linux root USB hubs
                continue

            dev_class = _read_sys(dev_dir / "bDeviceClass").lower()
            if dev_class in ("09", "e0"):  # Skip USB hubs (09) and Bluetooth controllers (e0)
                continue

            manufacturer = _read_sys(dev_dir / "manufacturer")
            product = _read_sys(dev_dir / "product")
            serial = _read_sys(dev_dir / "serial")

            combined_lower = f"{manufacturer} {product}".lower()
            if any(
                skip in combined_lower
                for skip in (
                    "bluetooth",
                    "integrated camera",
                    "webcam",
                    "receiver",
                    "fingerprint",
                    "hub",
                )
            ):
                continue

            is_android_product = "android" in combined_lower
            is_phone_vendor = vid in ADBManager._PHONE_USB_VENDORS and bool(serial)
            if not (is_android_product or is_phone_vendor):
                continue

            key = serial or f"{vid}:{pid}:{dev_dir.name}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            brand = manufacturer or ADBManager._PHONE_USB_VENDORS.get(vid, "Android")
            prod_clean = product.replace("_", " ").strip()
            if prod_clean and brand.lower() not in prod_clean.lower():
                friendly = f"{brand} {prod_clean}".strip()
            else:
                friendly = prod_clean or brand

            found.append(
                UsbOtgDevice(
                    serial=serial,
                    vendor_id=vid,
                    product_id=pid,
                    name=friendly,
                )
            )
        return found

    @staticmethod
    def _query_camera_focal_lengths(serial: str) -> Dict[str, float]:
        """Best-effort query of physical lens focal lengths (mm) from Android CameraService."""
        focals: Dict[str, float] = {}
        try:
            res = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "media.camera"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode != 0 or not res.stdout:
                return focals

            current_id: Optional[str] = None
            lines = res.stdout.splitlines()
            for idx, line in enumerate(lines):
                id_match = re.search(
                    r"(?:Camera ID:\s*|== Camera (?:HAL )?device [^\n]*?(?:id |device ))([0-9A-Za-z_]+)",
                    line,
                )
                if id_match:
                    current_id = id_match.group(1)
                    continue

                if current_id and "android.lens.info.availableFocalLengths" in line:
                    # Value is typically on the next line inside `[ 4.60000000 ]`
                    window = " ".join(lines[idx : min(len(lines), idx + 3)])
                    f_match = re.search(r"\[\s*([0-9]+(?:\.[0-9]+)?)", window)
                    if f_match and current_id not in focals:
                        val = float(f_match.group(1))
                        if 0.5 <= val <= 50.0:
                            focals[current_id] = val
        except Exception:
            pass
        return focals

    @staticmethod
    def list_cameras(serial: str) -> List[CameraLens]:
        """Detect all physical/logical camera lenses and their supported resolutions via `scrcpy`."""
        lenses: List[CameraLens] = []
        raw_out = ""

        for flag in ("--list-camera-sizes", "--list-cameras"):
            try:
                res = subprocess.run(
                    ["scrcpy", "-s", serial, flag],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                combined = f"{res.stdout or ''}\n{res.stderr or ''}"
                if "--camera-id=" in combined:
                    raw_out = combined
                    break
            except Exception as e:
                print(f"[ADBManager] Notice running scrcpy {flag}: {e}")

        if not raw_out:
            return []

        current_lens: Optional[CameraLens] = None
        cam_pattern = re.compile(
            r"--camera-id=([^\s]+)\s+\(([^,)]+)(?:,\s*(\d+)x(\d+))?(?:,\s*fps=\[([^\]]*)\])?"
        )
        size_pattern = re.compile(r"^\s*-\s*(\d+x\d+)")

        for line in raw_out.splitlines():
            m_cam = cam_pattern.search(line)
            if m_cam:
                cam_id = m_cam.group(1).strip()
                facing = m_cam.group(2).strip().lower()
                max_w = int(m_cam.group(3)) if m_cam.group(3) else 1920
                max_h = int(m_cam.group(4)) if m_cam.group(4) else 1080
                fps_raw = m_cam.group(5) or ""
                fps_list = [
                    int(x.strip())
                    for x in fps_raw.split(",")
                    if x.strip().isdigit()
                ]
                current_lens = CameraLens(
                    camera_id=cam_id,
                    facing=facing,
                    max_size=f"{max_w}x{max_h}",
                    max_w=max_w,
                    max_h=max_h,
                    fps_list=fps_list,
                )
                lenses.append(current_lens)
                continue

            if current_lens is not None:
                m_size = size_pattern.match(line)
                if m_size:
                    sz = m_size.group(1)
                    if sz not in current_lens.supported_sizes:
                        current_lens.supported_sizes.append(sz)

        # Enrich with human-readable lens role hints (Main 1x, Ultrawide, Telephoto, Macro, Wide Selfie)
        focals = ADBManager._query_camera_focal_lengths(serial)
        for lens in lenses:
            if lens.camera_id in focals:
                lens.focal_mm = focals[lens.camera_id]

        back_lenses = [l for l in lenses if l.facing == "back"]
        front_lenses = [l for l in lenses if l.facing == "front"]

        main_back_focal = next(
            (l.focal_mm for l in back_lenses if l.camera_id == "0" and l.focal_mm),
            None,
        )
        for idx, l in enumerate(back_lenses):
            if l.camera_id == "0" or idx == 0:
                l.role_hint = "Principal 1x"
            elif l.focal_mm and main_back_focal:
                if l.focal_mm < main_back_focal * 0.72 and max(l.max_w, l.max_h) >= 2800:
                    l.role_hint = "Ultra Gran Angular"
                elif l.focal_mm > main_back_focal * 1.35:
                    l.role_hint = "Telefoto"
                elif max(l.max_w, l.max_h) <= 2650:
                    l.role_hint = "Macro / Aux"
                else:
                    l.role_hint = "Secundario"
            else:
                if max(l.max_w, l.max_h) <= 2650:
                    l.role_hint = "Macro / Aux"
                else:
                    l.role_hint = "Gran Angular / Aux"

        if front_lenses:
            primary_front_px = front_lenses[0].max_w * front_lenses[0].max_h
            for idx, l in enumerate(front_lenses):
                if idx == 0:
                    l.role_hint = (
                        "Frontal Normal" if len(front_lenses) > 1 else "Frontal"
                    )
                else:
                    px = l.max_w * l.max_h
                    l.role_hint = (
                        "Frontal Amplio" if px >= primary_front_px else "Frontal Aux"
                    )

        for l in lenses:
            if not l.role_hint:
                l.role_hint = "Externa" if l.facing == "external" else l.facing.title()

        print(
            "[ADBManager] Lentes detectados por scrcpy: "
            + ", ".join(f"{l.label} [{l.facing}]" for l in lenses)
        )
        return lenses

    @staticmethod
    def get_screen_size(serial: str) -> Tuple[int, int]:
        """Return physical screen width and height (e.g. 1080, 2340)."""
        try:
            res = subprocess.run(
                ["adb", "-s", serial, "shell", "wm", "size"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            match = re.search(r"(\d+)x(\d+)", res.stdout)
            if match:
                return int(match.group(1)), int(match.group(2))
        except Exception:
            pass
        return 1080, 2340

    @staticmethod
    def send_key(serial: str, keycode: int) -> bool:
        """Send keyevent to device."""
        cmd = ["adb", "-s", serial, "shell", "input", "keyevent", str(keycode)]
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
        except Exception:
            return False

    @staticmethod
    def send_tap(serial: str, x: int, y: int) -> bool:
        """Send touch tap at device coordinates (x, y)."""
        cmd = [
            "adb",
            "-s",
            serial,
            "shell",
            "input",
            "tap",
            str(int(x)),
            str(int(y)),
        ]
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
        except Exception:
            return False

    @staticmethod
    def send_swipe(
        serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 250
    ) -> bool:
        """Send touch swipe gesture."""
        cmd = [
            "adb",
            "-s",
            serial,
            "shell",
            "input",
            "swipe",
            str(int(x1)),
            str(int(y1)),
            str(int(x2)),
            str(int(y2)),
            str(int(duration_ms)),
        ]
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
        except Exception:
            return False

    @staticmethod
    def take_screenshot(serial: str, save_path: str) -> bool:
        """Take screenshot and save to laptop."""
        cmd = ["adb", "-s", serial, "exec-out", "screencap", "-p"]
        try:
            with open(save_path, "wb") as f:
                res = subprocess.run(cmd, stdout=f, timeout=10)
                return res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def send_back(serial: str) -> bool:
        return ADBManager.send_key(serial, 4)  # KEYCODE_BACK

    @staticmethod
    def send_home(serial: str) -> bool:
        return ADBManager.send_key(serial, 3)  # KEYCODE_HOME

    @staticmethod
    def send_app_switch(serial: str) -> bool:
        return ADBManager.send_key(serial, 187)  # KEYCODE_APP_SWITCH

    @staticmethod
    def send_volume_up(serial: str) -> bool:
        return ADBManager.send_key(serial, 24)  # KEYCODE_VOLUME_UP

    @staticmethod
    def send_volume_down(serial: str) -> bool:
        return ADBManager.send_key(serial, 25)  # KEYCODE_VOLUME_DOWN

    @staticmethod
    def send_power(serial: str) -> bool:
        return ADBManager.send_key(serial, 26)  # KEYCODE_POWER
