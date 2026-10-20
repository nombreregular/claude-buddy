#!/bin/bash
# Abre la pantalla del buddy en Chromium, a pantalla completa (modo kiosco).
#
#   ./kiosk.sh            espera al daemon y abre el kiosco
#   ./kiosk.sh --window   lo abre en una ventana común (para probar y ajustar el diseño)
#
# Se puede lanzar desde SSH: busca solo la pantalla del Pi.
# Variables opcionales: BUDDY_PORT (default 8765), BUDDY_URL, BUDDY_PROFILE, BUDDY_WAIT (segundos
# esperando al daemon, default 90).

set -u

PORT="${BUDDY_PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
URL="${BUDDY_URL:-${BASE}/?kiosk=1}"
MODE=kiosk
if [ "${1:-}" = "--window" ]; then
    MODE=window
    URL="${BUDDY_URL:-${BASE}/}"
fi

say() { echo "kiosk: $*" >&2; }

# 1. Chromium (en Pi OS Trixie se llama chromium; en Bookworm también existe chromium-browser)
BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
    say "no encuentro Chromium (busqué chromium y chromium-browser). Instalalo: sudo apt install chromium"
    exit 1
fi

# 2. Qué pantalla usar. Desde SSH no hay variables de sesión: las buscamos.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    for sock in "$RUNTIME"/wayland-*; do
        case "$sock" in *.lock) continue ;; esac
        if [ -S "$sock" ]; then
            export XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$(basename "$sock")"
            break
        fi
    done
fi
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ] && [ -S /tmp/.X11-unix/X0 ]; then
    export DISPLAY=:0
fi
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    say "no encuentro la pantalla del Pi. ¿Está iniciado el escritorio (con la pantalla conectada)?"
    exit 1
fi

# 3. Esperar al daemon: sin él Chromium mostraría una página de error y no reintenta solo.
if ! python3 - "${BASE}/healthz" "${BUDDY_WAIT:-90}" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1]
end = time.time() + float(sys.argv[2])
while time.time() < end:
    try:
        urllib.request.urlopen(url, timeout=2).read()
        sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit(1)
PY
then
    say "el daemon no contesta en ${BASE}. ¿Está corriendo? (python buddy.py, o el servicio claude-buddy)"
    exit 1
fi

# 4. Chromium, con un perfil propio para no mezclarse con el tuyo.
#    Chromium ofrece traducir la página (una barra encima del kiosco) según preferencias que guarda en el perfil;
#    en Chromium 154 ni --disable-features=Translate ni --lang alcanzan. Se apaga en las preferencias del perfil
#    antes de abrirlo. --password-store=basic: que no pida desbloquear el llavero de GNOME en la pantalla.
PROFILE="${BUDDY_PROFILE:-$HOME/.cache/claude-buddy-kiosk}"
mkdir -p "$PROFILE/Default"
python3 - "$PROFILE/Default/Preferences" <<'PY' || say "no pude apagar el traductor en el perfil (sigo igual)"
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        prefs = json.load(f)
except (OSError, ValueError):
    prefs = {}
prefs.setdefault("translate", {})["enabled"] = False
prefs["translate_blocked_languages"] = sorted(set(prefs.get("translate_blocked_languages", [])) | {"es"})
with open(path, "w", encoding="utf-8") as f:
    json.dump(prefs, f)
PY
FLAGS=(
    "--user-data-dir=$PROFILE"
    --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble
    --password-store=basic --disable-features=Translate --lang=es
    --disable-pinch --overscroll-history-navigation=0
    --check-for-update-interval=31536000
)
# Desde SSH Chromium no sabe si la sesión es Wayland o X11 y por defecto busca X: decírselo.
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    FLAGS+=(--ozone-platform=wayland)
else
    FLAGS+=(--ozone-platform=x11)
fi
if [ "$MODE" = kiosk ]; then
    FLAGS+=(--kiosk)
else
    FLAGS+=(--window-size=800,520)
fi

say "abriendo ${URL} (${MODE})"
exec "$BROWSER" "${FLAGS[@]}" "$URL"
