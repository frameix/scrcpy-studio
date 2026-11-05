#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
VIDEOS_DIR="${HOME}/Videos/ScrcpyStudio"
PICTURES_DIR="${HOME}/Imágenes/ScrcpyStudio"

echo "=============================================="
echo "    Instalando Scrcpy Studio en tu sistema    "
echo "=============================================="

# 1. Ensure target directories exist
mkdir -p "${BIN_DIR}" "${DESKTOP_DIR}" "${VIDEOS_DIR}" "${PICTURES_DIR}"

# 2. Install launcher
echo "[1/4] Creando ejecutable en ~/.local/bin/scrcpy-studio..."
cat << EOF > "${BIN_DIR}/scrcpy-studio"
#!/usr/bin/env bash
PROJECT_DIR="${SCRIPT_DIR}"
export PYTHONPATH="\${PROJECT_DIR}:\${PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

pkill -f "scrcpy_studio/main.py" 2>/dev/null || true

exec python3 -B "\${PROJECT_DIR}/scrcpy_studio/main.py" "\$@"
EOF
chmod +x "${BIN_DIR}/scrcpy-studio"

# 3. Install desktop launcher
echo "[2/4] Creando acceso directo en el menú de aplicaciones..."
sed "s|^Exec=.*|Exec=${BIN_DIR}/scrcpy-studio|g" "${SCRIPT_DIR}/assets/scrcpy-studio.desktop" > "${DESKTOP_DIR}/scrcpy-studio.desktop"
chmod +x "${DESKTOP_DIR}/scrcpy-studio.desktop"

# 4. Configure Hyprland rules if present
echo "[3/4] Comprobando reglas de ventana para Hyprland..."
HYPR_RULES_LUA="${HOME}/.config/hypr/hyprland/rules.lua"
HYPR_CONF="${HOME}/.config/hypr/hyprland.conf"

if [ -f "${HYPR_RULES_LUA}" ]; then
    if ! grep -q "ScrcpyStudio" "${HYPR_RULES_LUA}"; then
        echo "Añadiendo reglas de aspecto flotante a rules.lua..."
        cat << 'EOF' >> "${HYPR_RULES_LUA}"

-- Scrcpy Studio: ventana flotante y opaca
hl.window_rule({ match = { class = "io.github.frame.ScrcpyStudio|scrcpy" }, float = true, opaque = true })
EOF
    fi
elif [ -f "${HYPR_CONF}" ]; then
    if ! grep -q "ScrcpyStudio" "${HYPR_CONF}"; then
        echo "Añadiendo reglas de aspecto flotante a hyprland.conf..."
        cat << 'EOF' >> "${HYPR_CONF}"

# Scrcpy Studio: mantén proporción alargada del teléfono y ventana flotante
windowrulev2 = float, class:^(io.github.frame.ScrcpyStudio)$
windowrulev2 = opaque, class:^(io.github.frame.ScrcpyStudio)$
EOF
    fi
fi

# 5. Check dependencies
echo "[4/4] Verificando dependencias..."
if ! command -v scrcpy &> /dev/null; then
    echo "AVISO: scrcpy no está instalado. Ejecuta: sudo pacman -S scrcpy"
fi

if ! command -v adb &> /dev/null; then
    echo "AVISO: adb no está instalado. Ejecuta: sudo pacman -S android-tools"
fi

echo ""
echo "¡Instalación completada con éxito!"
echo "Puedes abrir la aplicación:"
echo "  1. Escribiendo 'scrcpy-studio' en cualquier terminal."
echo "  2. Buscando 'Scrcpy Studio' en tu lanzador de aplicaciones (Super/Rofi/Wofi)."
