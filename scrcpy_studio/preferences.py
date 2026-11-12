"""
Scrcpy Studio - Preferences & 'Ajustes para Mantener' (One UI Camera style)
Persists user configuration and toggles for settings retention across application launches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Dict, Any

PREFERENCES_DIR = Path.home() / ".config" / "scrcpy-studio"
PREFERENCES_FILE = PREFERENCES_DIR / "preferences.json"


@dataclass
class StudioPreferences:
    # --- Interruptores: Qué ajustes mantener (Estilo Samsung One UI) ---
    keep_mode: bool = True          # Mantener Modo de Operación (Espejo, Cámara Estudio, Webcam, etc.)
    keep_camera: bool = True        # Mantener Lente y Cámara (Trasera/Frontal / Lente específico)
    keep_orientation: bool = True   # Mantener Orientación (Vertical/Horizontal)
    keep_resolution: bool = True    # Mantener Resolución/Calidad (1080p, 4K, 720p)
    keep_layout: bool = True        # Mantener Disposición de Ventana (Flotante / Izquierda / Derecha)
    keep_audio: bool = True         # Mantener Modo de Audio (Mudo / Grabar / Retorno)
    keep_record: bool = False       # Mantener Estado de Grabación a MP4 (Por seguridad default False)

    # --- Valores guardados (Últimos utilizados por el usuario) ---
    last_mode_index: int = 0
    last_orientation_index: int = 0
    last_facing_index: int = 0
    last_exact_lens_id: Optional[str] = None
    last_res_index: int = 0
    last_res_value: str = "1920x1080"
    last_layout_index: int = 0
    last_audio_index: int = 0
    last_record_active: bool = False

    # --- Rutas de almacenamiento ---
    recordings_dir: str = str(Path.home() / "Videos" / "ScrcpyStudio")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StudioPreferences:
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class PreferencesManager:
    _instance: Optional[PreferencesManager] = None

    def __init__(self):
        self.prefs: StudioPreferences = self.load()

    @classmethod
    def get(cls) -> PreferencesManager:
        if cls._instance is None:
            cls._instance = PreferencesManager()
        return cls._instance

    def load(self) -> StudioPreferences:
        if not PREFERENCES_FILE.exists():
            return StudioPreferences()
        try:
            with open(PREFERENCES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return StudioPreferences.from_dict(data)
        except Exception as e:
            print(f"[Preferences] Warning: Could not load preferences: {e}")
            return StudioPreferences()

    def save(self):
        try:
            PREFERENCES_DIR.mkdir(parents=True, exist_ok=True)
            temp_file = PREFERENCES_FILE.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.prefs.to_dict(), f, indent=2, ensure_ascii=False)
            temp_file.replace(PREFERENCES_FILE)
        except Exception as e:
            print(f"[Preferences] Warning: Could not save preferences: {e}")

    def reset_to_defaults(self) -> StudioPreferences:
        self.prefs = StudioPreferences()
        self.save()
        return self.prefs
