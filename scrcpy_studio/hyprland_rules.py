"""Hyprland 0.55+ (HyprLua) window rules and dynamic geometry controller for scrcpy-studio."""

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional


class HyprlandRulesManager:
    APP_CLASS = "io.github.frame.ScrcpyStudio"
    _op_lock = threading.Lock()
    _op_seq = 0
    _tiling_lock = threading.Lock()
    _last_optimized_time: float = 0.0

    _watcher_thread: Optional[threading.Thread] = None
    _watcher_stop_event: threading.Event = threading.Event()
    _target_scrcpy_pid: Optional[int] = None
    _aspect_w: int = 1080
    _aspect_h: int = 2340
    _session_orientation: str = "vertical"
    _tiling_side: str = "left"

    @staticmethod
    def is_hyprland() -> bool:
        """Check if the current session is Hyprland."""
        return (
            os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") is not None
            or os.environ.get("XDG_CURRENT_DESKTOP") == "Hyprland"
        )

    @staticmethod
    def _get_all_clients() -> List[dict]:
        """Fetch all Hyprland client windows as parsed JSON."""
        try:
            res = subprocess.run(
                ["hyprctl", "clients", "-j"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                return json.loads(res.stdout)
        except Exception as e:
            print(f"[HyprlandRules] Error reading clients: {e}")
        return []

    @staticmethod
    def _get_active_window() -> Optional[dict]:
        """Fetch currently focused Hyprland window as parsed JSON."""
        try:
            res = subprocess.run(
                ["hyprctl", "activewindow", "-j"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, dict) and data.get("address"):
                    return data
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_addr(raw_addr: Optional[str]) -> str:
        if not raw_addr:
            return ""
        addr = str(raw_addr).strip()
        if addr and not addr.startswith("0x"):
            addr = f"0x{addr}"
        return addr

    @staticmethod
    def _find_studio_client(
        clients: Optional[List[dict]] = None, retries: int = 0
    ) -> Optional[dict]:
        """Locate the exact Scrcpy Studio window in Hyprland by PID + class first."""
        for attempt in range(retries + 1):
            cur_clients = (
                clients
                if (clients is not None and attempt == 0)
                else HyprlandRulesManager._get_all_clients()
            )
            if cur_clients:
                mapped = [c for c in cur_clients if c.get("mapped", True)] or cur_clients
                my_pid = os.getpid()

                # 1. Exact PID + class match
                for c in mapped:
                    if (
                        c.get("pid") == my_pid
                        and "scrcpystudio" in c.get("class", "").lower()
                    ):
                        return c

                # 2. Exact PID match
                for c in mapped:
                    if c.get("pid") == my_pid:
                        return c

                # 3. Fallback match by class / initialClass / initialTitle / title
                for c in mapped:
                    cls = c.get("class", "").lower()
                    init_cls = c.get("initialClass", "").lower()
                    title = c.get("title", "").lower()
                    init_title = c.get("initialTitle", "").lower()
                    if (
                        "scrcpystudio" in cls
                        or "scrcpystudio" in init_cls
                        or "scrcpy studio" in init_title
                        or "scrcpy studio" in title
                    ):
                        return c

            if attempt < retries:
                time.sleep(0.06)
        return None

    @staticmethod
    def _get_active_monitor_size() -> tuple[int, int]:
        """Return logical (width, height) of the active Hyprland monitor."""
        try:
            res = subprocess.run(
                ["hyprctl", "monitors", "-j"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                monitors = json.loads(res.stdout)
                for m in monitors:
                    if m.get("focused", False) or len(monitors) == 1:
                        scale = float(m.get("scale", 1.0) or 1.0)
                        w = int(round(int(m.get("width", 1920)) / scale))
                        h = int(round(int(m.get("height", 1200)) / scale))
                        return w, h
        except Exception:
            pass
        return 1920, 1200

    @staticmethod
    def _dispatch_lua(lua_expr: str) -> str:
        """Execute a Hyprland 0.55+ Lua dispatcher expression (`return hl.dispatch(<lua_expr>)`)."""
        try:
            res = subprocess.run(
                ["hyprctl", "dispatch", lua_expr],
                capture_output=True,
                text=True,
                timeout=2,
            )
            out = (res.stdout or res.stderr or "").strip()
            if out and out.lower() not in ("ok", "true", "nil"):
                print(f"[HyprlandRules] lua dispatch {lua_expr} -> {out}")
            return out
        except Exception as e:
            print(f"[HyprlandRules] dispatch error ({lua_expr}): {e}")
            return ""

    @staticmethod
    def _dsp_focus(addr: str) -> None:
        HyprlandRulesManager._dispatch_lua(
            f'hl.dsp.focus({{ window = "address:{addr}" }})'
        )

    @staticmethod
    def _dsp_float(action: str, addr: Optional[str] = None) -> None:
        """action: 'on', 'off', or 'toggle'."""
        if addr:
            HyprlandRulesManager._dispatch_lua(
                f'hl.dsp.window.float({{ action = "{action}", window = "address:{addr}" }})'
            )
        else:
            if action == "toggle":
                HyprlandRulesManager._dispatch_lua("hl.dsp.window.float()")
            else:
                HyprlandRulesManager._dispatch_lua(
                    f'hl.dsp.window.float({{ action = "{action}" }})'
                )

    @staticmethod
    def _dsp_resize_exact(width: int, height: int, addr: str) -> None:
        HyprlandRulesManager._dispatch_lua(
            f'hl.dsp.window.resize({{ x = {int(width)}, y = {int(height)}, relative = false, window = "address:{addr}" }})'
        )

    @staticmethod
    def _dsp_resize_relative(dx: int, dy: int) -> None:
        HyprlandRulesManager._dispatch_lua(
            f"hl.dsp.window.resize({{ x = {int(dx)}, y = {int(dy)}, relative = true }})"
        )

    @staticmethod
    def _dsp_move_exact(x: int, y: int, addr: str) -> None:
        HyprlandRulesManager._dispatch_lua(
            f'hl.dsp.window.move({{ x = {int(x)}, y = {int(y)}, relative = false, window = "address:{addr}" }})'
        )

    @staticmethod
    def _dsp_move_dir(direction: str) -> None:
        """direction: 'left', 'right', 'up', 'down'."""
        HyprlandRulesManager._dispatch_lua(
            f'hl.dsp.window.move({{ direction = "{direction}" }})'
        )

    @staticmethod
    def _dsp_togglesplit() -> None:
        HyprlandRulesManager._dispatch_lua('hl.dsp.layout("togglesplit")')

    @staticmethod
    def ensure_window_floating():
        """Ensure the Scrcpy Studio window starts in floating mode in Hyprland."""
        if not HyprlandRulesManager.is_hyprland():
            return

        def _worker():
            client = HyprlandRulesManager._find_studio_client(retries=4)
            if client and not client.get("floating", False):
                addr = HyprlandRulesManager._normalize_addr(client.get("address"))
                if addr:
                    HyprlandRulesManager._dsp_float("on", addr)

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _force_tiled_state(addr: str, target_floating: bool) -> Optional[dict]:
        """Reliably transition window `addr` to `floating == target_floating` via HyprLua."""
        explicit_action = "on" if target_floating else "off"

        for attempt in range(4):
            client = HyprlandRulesManager._find_studio_client(retries=2)
            if not client:
                return None

            addr = HyprlandRulesManager._normalize_addr(client.get("address")) or addr
            cur_floating = bool(client.get("floating", False))
            if cur_floating == target_floating:
                return client

            # Strategy 1: Explicit hl.dsp.window.float({ action = "off"/"on", window = "address:..." })
            HyprlandRulesManager._dsp_float(explicit_action, addr)
            time.sleep(0.05)
            client = HyprlandRulesManager._find_studio_client()
            if client and bool(client.get("floating", False)) == target_floating:
                return client

            # Strategy 2: Toggle by address
            HyprlandRulesManager._dsp_float("toggle", addr)
            time.sleep(0.05)
            client = HyprlandRulesManager._find_studio_client()
            if client and bool(client.get("floating", False)) == target_floating:
                return client

            # Strategy 3: Focus window and invoke active-window hl.dsp.window.float()
            HyprlandRulesManager._dsp_focus(addr)
            time.sleep(0.035)
            active = HyprlandRulesManager._get_active_window()
            active_addr = HyprlandRulesManager._normalize_addr(
                active.get("address") if active else ""
            )
            if active_addr == addr or (active and active.get("pid") == os.getpid()):
                HyprlandRulesManager._dsp_float("toggle", None)
                time.sleep(0.05)
                client = HyprlandRulesManager._find_studio_client()
                if client and bool(client.get("floating", False)) == target_floating:
                    return client

            # Wait for next GTK4 Wayland xdg_toplevel commit before next attempt
            time.sleep(0.08 * (attempt + 1))

        return HyprlandRulesManager._find_studio_client()

    @staticmethod
    def set_window_tiled(
        tiled: bool,
        width: int,
        height: int,
        orientation: str = "vertical",
    ):
        """Asynchronously toggle between Hyprland Tiled (vertical column) and Floating phone mode."""
        if not HyprlandRulesManager.is_hyprland():
            return

        HyprlandRulesManager._op_seq += 1
        seq = HyprlandRulesManager._op_seq

        def _worker():
            # Allow GTK4's GdkFrameClock to commit xdg_toplevel.set_max_size(0, 0) to Wayland first
            time.sleep(0.085 if tiled else 0.05)
            with HyprlandRulesManager._op_lock:
                if seq != HyprlandRulesManager._op_seq:
                    return

                client = HyprlandRulesManager._find_studio_client(retries=4)
                if not client:
                    return
                addr = HyprlandRulesManager._normalize_addr(client.get("address"))
                if not addr:
                    return

                if tiled:
                    # 1. Switch from Floating -> Tiled
                    was_floating = bool(client.get("floating", False))
                    client = HyprlandRulesManager._force_tiled_state(
                        addr, target_floating=False
                    )
                    if not client or bool(client.get("floating", False)):
                        print(
                            f"[HyprlandRules] Warning: window {addr} remained floating after retries"
                        )
                        return

                    if was_floating:
                        print(
                            f"[HyprlandRules] Ventana {addr} acoplada en mosaico Hyprland"
                        )

                    clients = HyprlandRulesManager._get_all_clients()
                    client = HyprlandRulesManager._find_studio_client(clients) or client
                    ws_id = client.get("workspace", {}).get("id")
                    siblings = [
                        c
                        for c in clients
                        if HyprlandRulesManager._normalize_addr(c.get("address")) != addr
                        and not c.get("floating", False)
                        and c.get("workspace", {}).get("id") == ws_id
                    ]
                    if not siblings:
                        return

                    mon_w, mon_h = HyprlandRulesManager._get_active_monitor_size()

                    if orientation == "vertical":
                        # 2. If dwindle placed us in a short top/bottom horizontal split,
                        #    flip the split into a full-height vertical side-by-side column!
                        cur_size = client.get("size", [0, 0])
                        cur_h = int(cur_size[1]) if len(cur_size) >= 2 else 0
                        if cur_h < int(mon_h * 0.72):
                            HyprlandRulesManager._dsp_focus(addr)
                            HyprlandRulesManager._dsp_togglesplit()
                            time.sleep(0.06)
                            clients = HyprlandRulesManager._get_all_clients()
                            client = (
                                HyprlandRulesManager._find_studio_client(clients)
                                or client
                            )

                        # If still short OR squeezed into a tiny <330px sub-column while a wide
                        # sibling (>=680px) exists, move toward the wider sibling and split vertically.
                        cur_size = client.get("size", [0, 0])
                        cur_w = int(cur_size[0]) if len(cur_size) >= 1 else 0
                        cur_h = int(cur_size[1]) if len(cur_size) >= 2 else 0
                        wide_siblings = [
                            s
                            for s in siblings
                            if int(s.get("size", [0, 0])[0]) >= 680
                        ]
                        if cur_h < int(mon_h * 0.72) or (
                            cur_w < 330 and wide_siblings
                        ):
                            my_x = int(client.get("at", [0, 0])[0])
                            target_x = (
                                int(wide_siblings[0].get("at", [mon_w, 0])[0])
                                if wide_siblings
                                else mon_w
                            )
                            move_dir = "right" if target_x >= my_x else "left"
                            HyprlandRulesManager._dsp_focus(addr)
                            HyprlandRulesManager._dsp_move_dir(move_dir)
                            time.sleep(0.06)
                            client = (
                                HyprlandRulesManager._find_studio_client() or client
                            )
                            cur_h = int(client.get("size", [0, 0])[1])
                            if cur_h < int(mon_h * 0.72):
                                HyprlandRulesManager._dsp_focus(addr)
                                HyprlandRulesManager._dsp_togglesplit()
                                time.sleep(0.06)
                                client = (
                                    HyprlandRulesManager._find_studio_client() or client
                                )

                        # 3. If our vertical column is overly wide (e.g. >540px when only 1 terminal
                        #    is open), shrink it cleanly to phone width (~480px) so terminals get
                        #    maximum horizontal workspace without ever squishing narrow columns.
                        cur_size = client.get("size", [0, 0])
                        w0 = int(cur_size[0]) if len(cur_size) >= 1 else 0
                        h0 = int(cur_size[1]) if len(cur_size) >= 2 else mon_h
                        ideal_w = max(width, min(520, int(round((h0 - 47) * 0.4623))))
                        if w0 > ideal_w + 35:
                            HyprlandRulesManager._dsp_focus(addr)
                            err0 = ideal_w - w0  # negative (shrink)
                            HyprlandRulesManager._dsp_resize_relative(err0, 0)
                            time.sleep(0.05)
                            client2 = HyprlandRulesManager._find_studio_client()
                            if client2:
                                w1 = int(client2.get("size", [w0, 0])[0])
                                delta = w1 - w0
                                if delta != 0:
                                    sign = 1 if (delta * err0) > 0 else -1
                                    err1 = ideal_w - w1
                                    if abs(err1) > 16:
                                        HyprlandRulesManager._dsp_resize_relative(
                                            sign * err1, 0
                                        )
                else:
                    # Floating Phone / Settings Mode
                    was_tiled = not bool(client.get("floating", False))
                    if was_tiled:
                        client = HyprlandRulesManager._force_tiled_state(
                            addr, target_floating=True
                        )
                        if client:
                            print(
                                f"[HyprlandRules] Ventana {addr} restaurada a modo flotante"
                            )

                    HyprlandRulesManager._dsp_resize_exact(width, height, addr)
                    time.sleep(0.03)

                    # Keep 100% of the floating window inside monitor bounds
                    client2 = HyprlandRulesManager._find_studio_client()
                    if client2 and client2.get("floating", False):
                        at = client2.get("at", [100, 100])
                        cx, cy = int(at[0]), int(at[1])
                        mon_w, mon_h = HyprlandRulesManager._get_active_monitor_size()
                        margin = 14
                        nx = max(margin, min(cx, mon_w - int(width) - margin))
                        ny = max(margin, min(cy, mon_h - int(height) - margin))
                        if abs(nx - cx) > 4 or abs(ny - cy) > 4:
                            HyprlandRulesManager._dsp_move_exact(nx, ny, addr)

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def ensure_persistent_rules() -> bool:
        """Ensure rules exist in Hyprland config files so they persist across reboots."""
        rules_lua = Path.home() / ".config" / "hypr" / "hyprland" / "rules.lua"
        rules_conf = Path.home() / ".config" / "hypr" / "hyprland.conf"

        if rules_lua.exists():
            try:
                content = rules_lua.read_text(encoding="utf-8")
                if "ScrcpyStudio" not in content and "scrcpy-studio" not in content:
                    snippet = (
                        "\n-- Scrcpy Studio: ventana flotante y opaca\n"
                        'hl.window_rule({ match = { class = "io.github.frame.ScrcpyStudio|scrcpy" }, float = true, opaque = true })\n'
                    )
                    rules_lua.write_text(content + snippet, encoding="utf-8")
                return True
            except Exception as e:
                print(f"[HyprlandRules] Error writing to rules.lua: {e}")

        if rules_conf.exists():
            try:
                content = rules_conf.read_text(encoding="utf-8")
                if "ScrcpyStudio" not in content and "scrcpy-studio" not in content:
                    snippet = (
                        "\n# Scrcpy Studio floating rules\n"
                        "windowrulev2 = float, class:^(io.github.frame.ScrcpyStudio)$\n"
                        "windowrulev2 = opaque, class:^(io.github.frame.ScrcpyStudio)$\n"
                    )
                    rules_conf.write_text(content + snippet, encoding="utf-8")
                return True
            except Exception as e:
                print(f"[HyprlandRules] Error writing to hyprland.conf: {e}")

        return False

    @classmethod
    def start_scrcpy_watcher(
        cls,
        pid: Optional[int] = None,
        aspect_w: int = 1080,
        aspect_h: int = 2340,
        orientation: str = "vertical",
        tiling_side: str = "left",
    ):
        """Monitor Hyprland socket2 for scrcpy window floating/tiling events and auto-format layout."""
        if not cls.is_hyprland():
            return
        cls.stop_scrcpy_watcher()
        cls._target_scrcpy_pid = pid
        cls._aspect_w = aspect_w
        cls._aspect_h = aspect_h
        cls._session_orientation = orientation
        cls._tiling_side = tiling_side
        cls._watcher_stop_event.clear()

        cls._watcher_thread = threading.Thread(
            target=cls._socket2_watcher_loop,
            daemon=True,
            name="ScrcpyHyprlandWatcher",
        )
        cls._watcher_thread.start()
        print(f"[HyprlandRules] Started socket2 watcher for scrcpy (PID {pid}, side: {tiling_side})")

    @classmethod
    def set_tiling_side(cls, side: str):
        """Update preferred tiling side ('left' or 'right') and immediately adjust if window is active."""
        cls._tiling_side = side
        if not cls.is_hyprland():
            return

        def _worker():
            time.sleep(0.04)
            clients = cls._get_all_clients()
            for c in clients:
                c_class = c.get("class", "").lower()
                c_title = c.get("title", "").lower()
                if (c_class == "scrcpy" or "scrcpy studio" in c_title) and not c.get("floating", False):
                    addr = cls._normalize_addr(c.get("address"))
                    at_x = int(c.get("at", [0, 0])[0])
                    if side == "left" and at_x > 250:
                        cls._dsp_focus(addr)
                        cls._dispatch_lua('hl.dsp.layout("swapsplit")')
                    elif side == "right" and at_x < 500:
                        cls._dsp_focus(addr)
                        cls._dispatch_lua('hl.dsp.layout("swapsplit")')
                    break

        threading.Thread(target=_worker, daemon=True).start()

    @classmethod
    def stop_scrcpy_watcher(cls):
        """Stop the socket2 event monitor."""
        cls._watcher_stop_event.set()
        cls._target_scrcpy_pid = None
        cls._watcher_thread = None

    @classmethod
    def _socket2_watcher_loop(cls):
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if not sig:
            return
        sock_path = f"{runtime_dir}/hypr/{sig}/.socket2.sock"
        if not os.path.exists(sock_path):
            return

        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(sock_path)
        except Exception as e:
            print(f"[HyprlandRules] Failed to connect to socket2: {e}")
            return

        buffer = ""
        try:
            while not cls._watcher_stop_event.is_set():
                try:
                    data = s.recv(4096)
                    if not data:
                        break
                    buffer += data.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        cls._handle_socket2_event(line)
                except socket.timeout:
                    continue
                except Exception as e:
                    if not cls._watcher_stop_event.is_set():
                        print(f"[HyprlandRules] socket2 read error: {e}")
                    break
        finally:
            try:
                s.close()
            except Exception:
                pass

    @classmethod
    def _handle_socket2_event(cls, line: str):
        if not line.startswith("changefloatingmode>>"):
            return
        payload = line[len("changefloatingmode>>"):]
        if "," not in payload:
            return
        raw_addr, floating_str = payload.split(",", 1)
        addr = cls._normalize_addr(raw_addr)
        is_floating = floating_str.strip() == "1"

        if not is_floating:
            # Window became TILED! Check if it belongs to our scrcpy session
            clients = cls._get_all_clients()
            scrcpy_client = None
            for c in clients:
                c_addr = cls._normalize_addr(c.get("address"))
                if c_addr.lower() == addr.lower():
                    c_pid = c.get("pid")
                    c_class = c.get("class", "").lower()
                    c_title = c.get("title", "").lower()
                    if (
                        (cls._target_scrcpy_pid and c_pid == cls._target_scrcpy_pid)
                        or (c_class == "scrcpy")
                        or ("scrcpy studio" in c_title)
                    ):
                        scrcpy_client = c
                        break

            if scrcpy_client:
                print(f"[HyprlandRules] Scrcpy window {addr} tiled! Applying vertical layout optimization...")
                threading.Thread(
                    target=cls._optimize_scrcpy_tiling_worker,
                    args=(addr,),
                    daemon=True,
                ).start()

    @classmethod
    def _optimize_scrcpy_tiling_worker(cls, addr: str):
        with cls._tiling_lock:
            now = time.time()
            if now - cls._last_optimized_time < 0.4:
                return
            cls._last_optimized_time = now

            time.sleep(0.06)
            mon_w, mon_h = cls._get_active_monitor_size()

            clients = cls._get_all_clients()
            matches = [c for c in clients if cls._normalize_addr(c.get("address")).lower() == addr.lower()]
            if not matches:
                return
            win = matches[0]
            if win.get("floating", False):
                return

            # 1. Focus scrcpy window so dwindle layout messages apply directly
            cls._dsp_focus(addr)
            time.sleep(0.04)

            # 2. If it was placed in a horizontal split (height < 75% of monitor), flip split to vertical!
            cur_h = int(win.get("size", [0, 0])[1])
            if cur_h < int(mon_h * 0.75):
                cls._dsp_togglesplit()
                time.sleep(0.04)

            # 3. Move to root so it occupies the full-height column of the workspace
            cls._dispatch_lua('hl.dsp.layout("movetoroot")')
            time.sleep(0.04)

            # Re-fetch clients to check placement
            clients = cls._get_all_clients()
            matches = [c for c in clients if cls._normalize_addr(c.get("address")).lower() == addr.lower()]
            if matches:
                win = matches[0]

            # 4. Position on desired side (left or right)
            at_x = int(win.get("at", [0, 0])[0])
            should_swap = False
            if cls._tiling_side == "left" and at_x > 250:
                should_swap = True
            elif cls._tiling_side == "right" and at_x < 500:
                should_swap = True

            if should_swap:
                cls._dispatch_lua('hl.dsp.layout("swapsplit")')
                time.sleep(0.04)
                clients = cls._get_all_clients()
                matches = [c for c in clients if cls._normalize_addr(c.get("address")).lower() == addr.lower()]
                if matches:
                    win = matches[0]

            # 5. Resize to exact phone aspect ratio
            cur_size = win.get("size", [0, mon_h])
            h0 = int(cur_size[1]) if len(cur_size) >= 2 else mon_h
            aspect_ratio = (cls._aspect_w / cls._aspect_h) if cls._aspect_h > 0 else (1080 / 2340)
            target_w = max(360, min(640, int(round(h0 * aspect_ratio))))
            cur_w = int(cur_size[0])
            delta = target_w - cur_w
            if abs(delta) > 12:
                cls._dispatch_lua(
                    f'hl.dsp.window.resize({{ x = {delta}, y = 0, relative = true, window = "address:{addr}" }})'
                )
                print(f"[HyprlandRules] Resized tiled scrcpy column to {target_w}px (delta: {delta:+d})")
