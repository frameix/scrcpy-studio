"""Scrcpy process runner and dynamic V4L2 loopback stream supervisor."""

import ctypes
import errno
import fcntl
import os
import pty
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from scrcpy_studio.hyprland_rules import HyprlandRulesManager


class _V4l2LoopbackConfig(ctypes.Structure):
    """Exact ABI match for `struct v4l2_loopback_config` in `v4l2loopback.h` (0.15.x, 72 bytes)."""

    _fields_ = [
        ("output_nr", ctypes.c_int32),
        ("unused", ctypes.c_int32),
        ("card_label", ctypes.c_char * 32),
        ("min_width", ctypes.c_uint32),
        ("max_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32),
        ("max_buffers", ctypes.c_int32),
        ("max_openers", ctypes.c_int32),
        ("debug", ctypes.c_int32),
        ("announce_all_caps", ctypes.c_int32),
    ]


_IOC_WRITE = 1
_V4L2LOOPBACK_MAGIC = ord("~")  # 0x7E
_V4L2LOOPBACK_CTL_ADD = (
    (_IOC_WRITE << 30)
    | (ctypes.sizeof(_V4l2LoopbackConfig) << 16)
    | (_V4L2LOOPBACK_MAGIC << 8)
    | 1
)
_V4L2LOOPBACK_CTL_REMOVE = (
    (_IOC_WRITE << 30) | (4 << 16) | (_V4L2LOOPBACK_MAGIC << 8) | 2
)


@dataclass
class StudioSessionConfig:
    serial: str
    mode: str  # "mirror", "webcam", or "camera"
    orientation: str  # "vertical" or "horizontal"
    camera_facing: str = "back"  # "back" or "front"
    camera_id: Optional[str] = None  # Explicit physical/logical camera ID from `scrcpy --list-cameras`
    resolution: str = "1920x1080"
    mirror_size: Tuple[int, int] = (1080, 2340)
    fps: int = 30
    record: bool = False
    turn_screen_off: bool = False
    device_label: str = "scrcpy-studio"
    webcam_rotation: str = "0"  # "0", "90", "180", "270", "flip0"
    recordings_dir: str = str(Path.home() / "Videos" / "ScrcpyStudio")
    tiling_side: str = "left"  # "left" or "right"
    audio_mode: str = "mute"  # "mute", "record_only", "playback"
    initial_layout: str = "float"  # "float", "tiled_left", "tiled_right"


class ScrcpyRunner:
    _CTL_PATH = "/dev/v4l2loopback"

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.current_recording_path: Optional[str] = None
        self.session_config: Optional[StudioSessionConfig] = None
        self.v4l2_device: str = self.find_v4l2_loopback()
        self.v4l2_label: str = self.get_v4l2_device_label(self.v4l2_device)
        self._on_stream_ready: Optional[Callable[[str], None]] = None
        self._on_stop_callback: Optional[Callable[[], None]] = None
        self._stream_notified: bool = False

    @staticmethod
    def _list_virtual_v4l2_nodes() -> List[Tuple[int, str, str]]:
        """Return sorted list of `(video_nr, '/dev/videoN', card_label)` for virtual v4l2loopback nodes."""
        nodes: List[Tuple[int, str, str]] = []
        virtual_v4l2 = Path("/sys/devices/virtual/video4linux")
        if not virtual_v4l2.exists():
            return nodes
        for d in virtual_v4l2.glob("video*"):
            m = re.match(r"^video(\d+)$", d.name)
            if not m:
                continue
            nr = int(m.group(1))
            dev_path = f"/dev/video{nr}"
            if not os.path.exists(dev_path):
                continue
            try:
                label = (d / "name").read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                label = ""
            nodes.append((nr, dev_path, label))
        nodes.sort(key=lambda x: x[0])
        return nodes

    @staticmethod
    def find_v4l2_loopback() -> str:
        """Auto-detect v4l2loopback virtual video device (e.g. /dev/video4)."""
        nodes = ScrcpyRunner._list_virtual_v4l2_nodes()
        if nodes:
            return nodes[0][1]
        return "/dev/video4"

    @staticmethod
    def get_v4l2_device_label(dev_path: str) -> str:
        """Read current kernel `card_label` for `/dev/videoN` (what Google Meet / OBS displays)."""
        name = Path(dev_path).name
        sys_name = Path("/sys/devices/virtual/video4linux") / name / "name"
        try:
            if sys_name.exists():
                return sys_name.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            pass
        return "Webcam Virtual"

    @staticmethod
    def sanitize_card_label(raw_name: str) -> str:
        """Trim label cleanly to <= 31 UTF-8 bytes (`char card_label[32]` in kernel driver)."""
        clean = " ".join((raw_name or "").split()).strip()
        if not clean:
            clean = "Scrcpy Studio"
        raw_bytes = clean.encode("utf-8")
        if len(raw_bytes) <= 31:
            return clean
        return raw_bytes[:31].decode("utf-8", errors="ignore").strip() or "Scrcpy Studio"

    @classmethod
    def can_manage_dynamic_label(cls) -> bool:
        """True when `/dev/v4l2loopback` exists and is writable by the user (`video` group)."""
        return os.path.exists(cls._CTL_PATH) and os.access(
            cls._CTL_PATH, os.R_OK | os.W_OK
        )

    def ensure_v4l2_device_for_phone(self, desired_label: str) -> Tuple[str, str]:
        """Dynamically assign the connected phone's name to the virtual V4L2 camera via `/dev/v4l2loopback`.
        Idempotent when the label already matches so active Google Meet / OBS streams are never interrupted."""
        target_label = self.sanitize_card_label(desired_label)
        nodes = self._list_virtual_v4l2_nodes()

        # 1. Fast path: reuse existing virtual camera if it already carries this phone's exact name
        for nr, dev_path, cur_label in nodes:
            if cur_label == target_label:
                self.v4l2_device = dev_path
                self.v4l2_label = cur_label
                # Opportunistically clean up extra idle dynamic nodes if more than 1 exist
                if len(nodes) > 1 and self.can_manage_dynamic_label():
                    self._cleanup_extra_idle_nodes(keep_nr=nr)
                return dev_path, cur_label

        # 2. Dynamic kernel ioctl rename/create when `/dev/v4l2loopback` is accessible
        if self.can_manage_dynamic_label():
            pref_nr = nodes[0][0] if nodes else 4
            ctl_fd = -1
            try:
                ctl_fd = os.open(self._CTL_PATH, os.O_RDWR)
                target_nr = pref_nr

                # Try removing the old idle node (e.g. 'Iriun Webcam' on /dev/video4) to rename it in-place
                if any(n[0] == pref_nr for n in nodes):
                    try:
                        fcntl.ioctl(ctl_fd, _V4L2LOOPBACK_CTL_REMOVE, int(pref_nr))
                    except OSError as rm_err:
                        if rm_err.errno == errno.EBUSY:
                            # An external app currently holds /dev/video4 open; allocate a fresh slot
                            target_nr = -1
                        elif rm_err.errno != errno.ENODEV:
                            target_nr = -1

                cfg = _V4l2LoopbackConfig(
                    output_nr=target_nr,
                    unused=target_nr,
                    card_label=target_label.encode("utf-8"),
                    min_width=0,
                    max_width=0,
                    min_height=0,
                    max_height=0,
                    max_buffers=2,
                    max_openers=10,
                    debug=0,
                    announce_all_caps=0,  # exclusive_caps=1 required by Chrome / Meet / WebRTC
                )
                created_nr = fcntl.ioctl(ctl_fd, _V4L2LOOPBACK_CTL_ADD, cfg)
                new_dev = f"/dev/video{created_nr}"

                # Wait briefly (~15-150ms) for systemd-udevd to apply `video` group permissions
                for _ in range(24):
                    if os.path.exists(new_dev) and os.access(
                        new_dev, os.R_OK | os.W_OK
                    ):
                        break
                    time.sleep(0.015)

                self._cleanup_extra_idle_nodes(keep_nr=created_nr, ctl_fd=ctl_fd)
                self.v4l2_device = new_dev
                self.v4l2_label = self.get_v4l2_device_label(new_dev) or target_label
                print(
                    f"[ScrcpyRunner] Cámara virtual dinámica lista: {self.v4l2_device} -> '{self.v4l2_label}'"
                )
                return self.v4l2_device, self.v4l2_label
            except Exception as e:
                print(f"[ScrcpyRunner] Notice updating dynamic V4L2 label: {e}")
            finally:
                if ctl_fd >= 0:
                    try:
                        os.close(ctl_fd)
                    except OSError:
                        pass

        # 3. Fallback when udev rule hasn't been enabled yet
        self.v4l2_device = self.find_v4l2_loopback()
        self.v4l2_label = self.get_v4l2_device_label(self.v4l2_device)
        return self.v4l2_device, self.v4l2_label

    def _cleanup_extra_idle_nodes(
        self, keep_nr: int, ctl_fd: Optional[int] = None
    ) -> None:
        own_fd = ctl_fd is None
        fd = ctl_fd if ctl_fd is not None else -1
        try:
            if own_fd:
                fd = os.open(self._CTL_PATH, os.O_RDWR)
            for nr, _, _ in self._list_virtual_v4l2_nodes():
                if nr != keep_nr:
                    try:
                        fcntl.ioctl(fd, _V4L2LOOPBACK_CTL_REMOVE, int(nr))
                    except OSError:
                        pass
        except Exception:
            pass
        finally:
            if own_fd and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def enable_dynamic_v4l2_permissions_pkexec(
        self, initial_label: str = "scrcpy-studio"
    ) -> Tuple[bool, str]:
        """Volatile in-RAM permission grant for `/dev/v4l2loopback` without touching boot or disk config files."""
        script = (
            "set -e; "
            "if [ -e /dev/v4l2loopback ]; then "
            "  chgrp video /dev/v4l2loopback && chmod 0660 /dev/v4l2loopback; "
            "fi"
        )
        try:
            res = subprocess.run(
                ["pkexec", "sh", "-c", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if res.returncode == 0 and self.can_manage_dynamic_label():
                dev, lbl = self.ensure_v4l2_device_for_phone(initial_label)
                return True, f"Activado: {lbl} ({dev})"
            err = (res.stderr or res.stdout or "").strip()
            return False, err or "No se completó la autorización Polkit"
        except Exception as e:
            return False, str(e)

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _prepare_v4l2_sink(self, config: StudioSessionConfig) -> None:
        """Pre-configure v4l2loopback width, height, and stride (bytesperline) to match stream."""
        if not os.path.exists(self.v4l2_device):
            return
        try:
            if config.mode in ("camera", "webcam") and "x" in config.resolution:
                parts = config.resolution.split("x")
                w, h = int(parts[0]), int(parts[1])
                if config.mode == "webcam" and config.webcam_rotation in (
                    "90",
                    "270",
                    "flip90",
                    "flip270",
                ):
                    w, h = h, w
            else:
                w, h = config.mirror_size

            # scrcpy rounds both width and height down to multiples of 8 (& ~7)
            w = w & ~7
            h = h & ~7

            subprocess.run(
                [
                    "v4l2-ctl",
                    "-d",
                    self.v4l2_device,
                    "--set-fmt-video-out",
                    f"width={w},height={h},pixelformat=YU12,bytesperline={w}",
                ],
                capture_output=True,
                timeout=2,
            )
        except Exception as e:
            print(f"[ScrcpyRunner] Warning preparing v4l2 sink: {e}")

    def start_session(
        self,
        config: StudioSessionConfig,
        on_stream_ready: Optional[Callable[[str], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Start a headless scrcpy session feeding V4L2 (for Meet/OBS directly or embedded GTK4 player)."""
        if self.is_running:
            self.stop_session()

        self.session_config = config
        self._on_stream_ready = on_stream_ready
        self._on_stop_callback = on_stop
        self._stream_notified = False
        self.v4l2_device, self.v4l2_label = self.ensure_v4l2_device_for_phone(
            config.device_label
        )

        self._prepare_v4l2_sink(config)

        bitrate = "16M"
        if config.mode in ("camera", "webcam") and "x" in config.resolution:
            try:
                if int(config.resolution.split("x", 1)[0]) >= 2560:
                    bitrate = "24M"
            except ValueError:
                pass

        if config.mode == "mirror":
            label = config.device_label or "Teléfono"
            cmd: List[str] = [
                "scrcpy",
                "-s",
                config.serial,
                "--video-codec=h265",
                "--max-size=1920",
                "--max-fps=60",
                "--no-audio",
                "--keyboard=uhid",
                f"--window-title=Scrcpy Studio · {label}",
            ]
            if config.orientation == "horizontal":
                cmd.append("--capture-orientation=90")
            elif config.orientation == "vertical":
                cmd.append("--capture-orientation=@0")

            is_wireless = ":" in config.serial or "tcp" in config.serial.lower() or "." in config.serial
            if is_wireless:
                cmd.append("--video-buffer=50")

            if config.record:
                save_dir = Path(config.recordings_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                filename = f"scrcpy_mirror_{timestamp}.mp4"
                filepath = save_dir / filename
                self.current_recording_path = str(filepath)
                cmd.extend(["--record", str(filepath)])
            else:
                self.current_recording_path = None

        elif config.mode == "camera":
            safe_fps = 30 if config.fps > 30 else config.fps
            label = config.device_label or "Teléfono"
            facing_label = "Frontal" if config.camera_facing == "front" else "Trasera"
            cmd = [
                "scrcpy",
                "-s",
                config.serial,
                "--video-source=camera",
                "--video-codec=h265",
                f"--camera-size={config.resolution}",
                f"--camera-fps={safe_fps}",
                "--keyboard=uhid",
                f"--window-title=Scrcpy Studio · Cámara {facing_label} ({label})",
            ]
            if config.camera_id is not None and str(config.camera_id).strip() != "":
                cmd.append(f"--camera-id={config.camera_id}")
            else:
                cmd.append(f"--camera-facing={config.camera_facing}")

            # Correct sensor landscape mounting: back camera is +90°, front is +270° in portrait
            if config.webcam_rotation and config.webcam_rotation != "0":
                cmd.append(f"--capture-orientation={config.webcam_rotation}")
            elif config.orientation == "vertical":
                rot = "270" if config.camera_facing == "front" else "90"
                cmd.append(f"--capture-orientation={rot}")
            elif config.orientation == "horizontal":
                cmd.append("--capture-orientation=0")

            # Audio mode: mute (no sound / best wireless performance), record_only (clean mic recording without PC echo), or playback
            if config.audio_mode == "mute":
                cmd.append("--no-audio")
            elif config.audio_mode == "record_only":
                cmd.extend(["--audio-source=mic", "--no-audio-playback"])
            elif config.audio_mode == "playback":
                cmd.append("--audio-source=mic")

            # Buffer 50ms over Wi-Fi/TCP to smooth out jitter and eliminate occasional lag spikes
            is_wireless = ":" in config.serial or "tcp" in config.serial.lower() or "." in config.serial
            if is_wireless:
                cmd.append("--video-buffer=50")

            if config.record:
                save_dir = Path(config.recordings_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                filename = f"scrcpy_camera_{timestamp}.mp4"
                filepath = save_dir / filename
                self.current_recording_path = str(filepath)
                cmd.extend(["--record", str(filepath)])
            else:
                self.current_recording_path = None

        elif config.mode == "webcam":
            self.v4l2_device, self.v4l2_label = self.ensure_v4l2_device_for_phone(
                config.device_label
            )
            self._prepare_v4l2_sink(config)
            cmd = [
                "scrcpy",
                "-s",
                config.serial,
                "--no-window",
                "--no-playback",
                "--no-audio",
                "--video-source=camera",
                f"--camera-size={config.resolution}",
                f"--camera-fps=30",
                f"--video-bit-rate={bitrate}",
                f"--v4l2-sink={self.v4l2_device}",
            ]
            if config.camera_id is not None and str(config.camera_id).strip() != "":
                cmd.append(f"--camera-id={config.camera_id}")
            else:
                cmd.append(f"--camera-facing={config.camera_facing}")

            # Correct sensor landscape mounting: back camera is +90°, front is +270° in portrait
            if config.webcam_rotation and config.webcam_rotation != "0":
                cmd.append(f"--capture-orientation={config.webcam_rotation}")

        print(f"[ScrcpyRunner] Executing: {' '.join(cmd)}")

        try:
            # Use a PTY so scrcpy flushes stdout lines immediately in real time
            master_fd, slave_fd = pty.openpty()
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)

            monitor_thread = threading.Thread(
                target=self._monitor_pty_stdout,
                args=(self.process, master_fd),
                daemon=True,
            )
            monitor_thread.start()

            if config.mode in ("mirror", "camera"):
                if config.mode == "camera" and config.resolution:
                    try:
                        rw, rh = [int(x) for x in config.resolution.split("x")]
                        if config.orientation == "vertical":
                            aspect_w, aspect_h = min(rw, rh), max(rw, rh)
                        else:
                            aspect_w, aspect_h = max(rw, rh), min(rw, rh)
                    except Exception:
                        aspect_w, aspect_h = (1080, 1920) if config.orientation == "vertical" else (1920, 1080)
                else:
                    aspect_w, aspect_h = (
                        config.mirror_size
                        if (config.mirror_size and len(config.mirror_size) == 2)
                        else (1080, 2340)
                    )
                HyprlandRulesManager.start_scrcpy_watcher(
                    pid=self.process.pid,
                    aspect_w=aspect_w,
                    aspect_h=aspect_h,
                    orientation=config.orientation,
                    tiling_side=config.tiling_side,
                    initial_layout=config.initial_layout,
                )

            return True
        except Exception as e:
            print(f"[ScrcpyRunner] Failed to launch scrcpy: {e}")
            return False

    def start_otg_session(
        self,
        usb_serial: Optional[str] = None,
        on_stop: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Start `scrcpy --otg` (physical USB HID keyboard & mouse simulation without ADB/debugging)."""
        if self.is_running:
            self.stop_session()

        self.session_config = None
        self.current_recording_path = None
        self._on_stream_ready = None
        self._on_stop_callback = on_stop
        self._stream_notified = True

        cmd: List[str] = [
            "scrcpy",
            "--otg",
            "--window-title=Trackpad OTG (Clic = Controlar móvil | Alt Izq = Soltar)",
            "--window-width=320",
            "--window-height=220",
        ]
        if usb_serial:
            cmd.extend(["-s", usb_serial])

        print(f"[ScrcpyRunner] Executing OTG: {' '.join(cmd)}")

        try:
            master_fd, slave_fd = pty.openpty()
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)

            monitor_thread = threading.Thread(
                target=self._monitor_pty_stdout,
                args=(self.process, master_fd),
                daemon=True,
            )
            monitor_thread.start()
            return True
        except Exception as e:
            print(f"[ScrcpyRunner] Failed to launch scrcpy --otg: {e}")
            return False

    def _notify_stream_ready(self, proc: subprocess.Popen) -> None:
        if self._on_stream_ready and proc.poll() is None:
            self._on_stream_ready(self.v4l2_device)

    def _monitor_pty_stdout(self, proc: subprocess.Popen, master_fd: int) -> None:
        """Read unbuffered PTY output from scrcpy to detect exact V4L2 sink start."""
        try:
            with os.fdopen(
                master_fd, "r", encoding="utf-8", errors="replace", buffering=1
            ) as stdout_file:
                for line in stdout_file:
                    line_clean = line.strip()
                    if not line_clean:
                        continue
                    print(f"[scrcpy] {line_clean}")

                    if not self._stream_notified and (
                        "v4l2 sink started" in line_clean.lower()
                        or "texture" in line_clean.lower()
                        or "renderer" in line_clean.lower()
                    ):
                        self._stream_notified = True
                        threading.Timer(
                            0.2,
                            self._notify_stream_ready,
                            args=(proc,),
                        ).start()
        except OSError:
            pass
        except Exception as e:
            print(f"[ScrcpyRunner] Monitor error: {e}")
        finally:
            HyprlandRulesManager.stop_scrcpy_watcher()
            proc.wait()
            if self.process == proc:
                self.process = None
                if self._on_stop_callback:
                    try:
                        self._on_stop_callback()
                    except Exception:
                        pass

    def stop_session(self) -> None:
        """Safely terminate scrcpy so recordings finalize without corrupting MP4 headers."""
        HyprlandRulesManager.stop_scrcpy_watcher()
        proc = self.process
        self.process = None
        if proc and proc.poll() is None:
            print("[ScrcpyRunner] Sending SIGINT to cleanly finalize session...")
            try:
                proc.send_signal(signal.SIGINT)
                for _ in range(30):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                else:
                    proc.terminate()
            except Exception as e:
                print(f"[ScrcpyRunner] Error terminating process: {e}")
