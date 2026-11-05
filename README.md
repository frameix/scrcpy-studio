# 🎬 Scrcpy Studio

> **Modern, Wayland-native Studio and Controller for Android via `scrcpy`**  
> Pensado especialmente para Wayland / Hyprland y entornos de escritorio modernos en Linux.

![Status](https://img.shields.io/badge/Status-En%20Desarrollo%20(Beta)-eab308)
![Linux](https://img.shields.io/badge/OS-Linux%20%2F%20Wayland-blue)
![Hyprland](https://img.shields.io/badge/WM-Hyprland%20Ready-00bcd4)
![Python](https://img.shields.io/badge/Python-GTK4%20%2F%20Libadwaita-4caf50)
![Scrcpy](https://img.shields.io/badge/Engine-scrcpy-orange)

> [!IMPORTANT]
> **🚧 Proyecto en Desarrollo Activo (WIP / Beta):** `Scrcpy Studio` se encuentra actualmente en fase de desarrollo activo. Nuevas funciones, mejoras de estabilidad y ajustes en la interfaz se están implementando continuamente.

---

## ✨ Características Principales

* **Relación de Aspecto Perfecta (Cero ventanas cuadradas):**
  A diferencia de otras interfaces que encajonan el celular en mosaicos cuadrados en Hyprland, `scrcpy-studio` gestiona la proporción nativa (20:9 o 16:9) flotante, limpia y alargada.
* **Estudio de Grabación & Cámara en 1 Clic:**
  * Selector de lente en vivo: **Cámara Trasera Principal** o **Frontal (Selfie)**.
  * Selector de resolución: **4K Ultra HD (3840x2160)**, **1080p Full HD** o **720p HD** @ 60 FPS.
  * **Orientación flexible:** Alterna entre **Vertical (9:16)** (para TikTok / Reels) y **Horizontal (16:9)** (para YouTube / Meet / panorámico).
  * Grabación directa por aceleración de hardware a formato MP4 sin compresión visible en `~/Videos/ScrcpyStudio/`.
* **Modo Ahorro de Energía:**
  Opción para **apagar la pantalla física del teléfono** mientras la cámara transmite o graba, evitando que el celular caliente o gaste batería.
* **Barra de Navegación Rápida:**
  Control de Android integrado sin necesidad de tocar la pantalla:
  * ◀ Atrás | ⌂ Inicio | 🗖 Multitarea / Recientes
  * 🔊 Subir / 🔉 Bajar volumen
  * 📸 Captura de pantalla instantánea en la laptop
* **Detección Automática de Dispositivos:**
  Lee dispositivos USB y Wi-Fi sin que tengas que lidiar con puertos dinámicos ni seriales manuales.
* **Multi-entorno:**
  Funciona de forma óptima en **Hyprland** (con reglas automáticas) y en cualquier entorno estándar (**KDE Plasma, GNOME, XFCE**).

---

## 🚀 Instalación

### Opción A: Paquete Nativo en Arch Linux / CachyOS (Instala dependencias automáticamente)

```bash
git clone https://github.com/frameix/scrcpy-studio.git
cd scrcpy-studio
makepkg -si
```

> `pacman` resolverá e instalará automáticamente `scrcpy`, `android-tools` (`adb`), `gtk4`, `libadwaita` y `gstreamer`.

### Opción B: Instalación Rápida Local (Cualquier distribución Linux)

```bash
git clone https://github.com/frameix/scrcpy-studio.git
cd scrcpy-studio
chmod +x install.sh
./install.sh
```

El script de instalación se encarga de:
1. Crear el ejecutable en `~/.local/bin/scrcpy-studio`.
2. Registrar el acceso directo con ícono en tu menú de aplicaciones (`Scrcpy Studio`).
3. Crear las carpetas de grabaciones y capturas.
4. Inyectar automáticamente la regla de ventana flotante con aspect ratio en tu configuración de Hyprland (`rules.lua` o `hyprland.conf`).

---

## 🛠️ Requisitos del Sistema

* Linux con soporte Wayland o X11.
* `scrcpy` (versión 2.0 o superior).
* `android-tools` (`adb`).
* `python-gobject`, `gtk4`, `libadwaita`.

En **Arch Linux / CachyOS**:
```bash
sudo pacman -S scrcpy android-tools gtk4 libadwaita python-gobject
```

---

## 💻 Uso

* **Desde la terminal:** Escribe simplemente `scrcpy-studio`.
* **Desde el menú de aplicaciones:** Presiona la tecla `Super` y busca **"Scrcpy Studio"**.

---

## 📄 Licencia

MIT License. Diseñado para ofrecer máxima calidad visual y comodidad en Linux.
