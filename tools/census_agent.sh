#!/bin/bash
# Instala, quita y revisa el LaunchAgent del censo (tools/census.py) en el dominio de TU usuario: sin root.
#
#   tools/census_agent.sh install [-n] [--url URL]  escribe el plist, lo carga y arranca (idempotente; -n solo muestra el plan)
#   tools/census_agent.sh uninstall [-n] [--purge]  lo descarga y borra el plist (config y token se quedan; --purge borra el log)
#   tools/census_agent.sh status [--ping]           si está cargado, pid, último código de salida, token, config y log;
#                                                   no toca la red salvo con --ping (un GET /healthz a la pantalla)
#   tools/census_agent.sh restart                   kickstart -k (tras un git pull de census.py)
#   tools/census_agent.sh print [--url URL]         solo imprime el plist que escribiría
#
# launchd no hereda el entorno de tu terminal: CLAUDE_BUDDY_URL solo llega al censo si instalás con --url (queda en el
# plist; sin --url el censo usa census.json o http://buddy:8766). El token no va en el plist: el censo lo lee de
# ~/.config/claude-buddy/hermes.token, igual que el hook, y este script nunca lo abre (solo mira dueño, permisos y largo).
# Variables (pruebas o instalaciones raras): CLAUDE_BUDDY_PYTHON (otro intérprete, ruta absoluta), CLAUDE_BUDDY_LAUNCHCTL.

set -eu

LABEL="com.claude-buddy.census"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
CENSUS="$HERE/census.py"
PYTHON="${CLAUDE_BUDDY_PYTHON:-/usr/bin/python3}"
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
LOGFILE="$HOME/Library/Logs/claude-buddy-census.log"
CONFIG="$HOME/.config/claude-buddy/census.json"
TOKEN="$HOME/.config/claude-buddy/hermes.token"
DEFAULT_URL="http://buddy:8766"
LAUNCHCTL="${CLAUDE_BUDDY_LAUNCHCTL:-/bin/launchctl}"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,${/^#/!q;s/^# \{0,1\}//;p;}' "$0"; }
bad() { printf 'error: %s\n' "$*" >&2; printf 'Uso: %s {install|uninstall|status|restart|print} (--help muestra las opciones)\n' "$0" >&2; exit 2; }
xml() { printf '%s' "$1" | LC_ALL=C sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
loaded() { "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
# stat con dueño y permisos de verdad (sigue enlaces, como el censo); GNU primero, BSD después
mode() { stat -L -c %a "$1" 2>/dev/null || stat -L -f %Lp "$1" 2>/dev/null || true; }
size() { stat -L -c %s "$1" 2>/dev/null || stat -L -f %z "$1" 2>/dev/null || true; }

TMPPLIST=""
trap 'if [ -n "$TMPPLIST" ]; then rm -f "$TMPPLIST"; fi' EXIT

# --- argumentos --------------------------------------------------------------------------------------------------------

CMD="${1:-}"
if [ $# -gt 0 ]; then shift; fi
DRY=0; PURGE=0; PING=0; HAVE_URL=0; URL_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    -n|--dry-run) DRY=1 ;;
    --purge) PURGE=1 ;;
    --ping) PING=1 ;;
    --url) [ $# -ge 2 ] || bad "--url necesita una dirección (http://buddy:8766)"; HAVE_URL=1; URL_ARG="$2"; shift ;;
    --url=*) HAVE_URL=1; URL_ARG="${1#--url=}" ;;
    *) bad "opción desconocida: $1" ;;
  esac
  shift
done
case "$CMD" in
  install) [ "$PURGE$PING" = 00 ] || bad "install solo acepta -n y --url" ;;
  uninstall) [ "$PING$HAVE_URL" = 00 ] || bad "uninstall solo acepta -n y --purge" ;;
  status) [ "$DRY$PURGE$HAVE_URL" = 000 ] || bad "status solo acepta --ping" ;;
  restart) [ "$DRY$PURGE$PING$HAVE_URL" = 0000 ] || bad "restart no lleva opciones" ;;
  print) [ "$DRY$PURGE$PING" = 000 ] || bad "print solo acepta --url" ;;
esac

# --- el destino --------------------------------------------------------------------------------------------------------

# La misma regla que el cliente, porque el token viaja en claro por http: la propia Mac (127.0.0.0/8, localhost, ::1),
# un nombre de Tailscale (sin puntos, como buddy, o algo.ts.net) o una IP 100.64.0.0/10. Deja el motivo en URL_WHY.
url_check() {
  local u="$1" hostport host port re o1 o2 o3 o4 o
  case "$u" in
    http://*) ;;
    *) URL_WHY="tiene que empezar con http://"; return 1 ;;
  esac
  hostport="${u#http://}"
  hostport="${hostport%/}"
  re='^(\[::1\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?$'
  if ! [[ $hostport =~ $re ]]; then
    URL_WHY="esperaba http://destino o http://destino:puerto, sin usuario, ruta ni parámetros"; return 1
  fi
  host="$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:upper:]' '[:lower:]')"
  port="${BASH_REMATCH[2]#:}"
  if [ -n "$port" ] && { [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; }; then
    URL_WHY="el puerto tiene que estar entre 1 y 65535"; return 1
  fi
  URL_WHY="el destino tiene que ser esta Mac (127.0.0.1, localhost), un nombre de Tailscale (buddy, algo.ts.net) o una IP 100.64.0.0/10: el token viaja sin cifrar. Para otro destino, ponelo en census.json con insecure_url_ok bajo tu riesgo"
  case "$host" in
    "[::1]"|localhost) return 0 ;;
  esac
  re='^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$'
  if [[ $host =~ $re ]]; then
    o1="${BASH_REMATCH[1]}"; o2="${BASH_REMATCH[2]}"; o3="${BASH_REMATCH[3]}"; o4="${BASH_REMATCH[4]}"
    for o in "$o1" "$o2" "$o3" "$o4"; do
      case "$o" in
        0|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) [ "$o" -le 255 ] || { URL_WHY="esa IP no existe"; return 1; } ;;
        *) URL_WHY="escribí la IP con números sin ceros a la izquierda"; return 1 ;;
      esac
    done
    if [ "$o1" -eq 127 ]; then return 0; fi
    if [ "$o1" -eq 100 ] && [ "$o2" -ge 64 ] && [ "$o2" -le 127 ]; then return 0; fi
    return 1
  fi
  re='^[a-z][a-z0-9-]*$'          # un nombre sin puntos tiene que empezar con letra: 2130706433 es una IP escondida
  if [[ $host =~ $re ]]; then return 0; fi
  re='^[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net$'
  if [[ $host =~ $re ]]; then return 0; fi
  return 1
}

if [ "$HAVE_URL" -eq 1 ]; then
  url_check "$URL_ARG" || bad "--url: $URL_WHY"
  URL_ARG="${URL_ARG%/}"
fi

# La url que ya fija el plist instalado, si es una que el censo aceptaría (la escribimos nosotros, una línea bajo la llave).
plist_url() {
  local found
  [ -f "$PLIST" ] || return 0
  found="$(LC_ALL=C sed -n '/<key>CLAUDE_BUDDY_URL<\/key>/{n;s/.*<string>\(.*\)<\/string>.*/\1/p;}' "$PLIST" | head -n 1)"
  if [ -n "$found" ] && url_check "$found"; then printf '%s' "$found"; fi
}

# --- el plist ----------------------------------------------------------------------------------------------------------

render_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$(xml "$LABEL")</string>
	<key>ProgramArguments</key>
	<array>
		<string>$(xml "$PYTHON")</string>
		<string>-I</string>
		<string>-u</string>
		<string>$(xml "$CENSUS")</string>
		<string>run</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>ThrottleInterval</key>
	<integer>30</integer>
	<key>ExitTimeOut</key>
	<integer>5</integer>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>StandardOutPath</key>
	<string>$(xml "$LOGFILE")</string>
	<key>StandardErrorPath</key>
	<string>$(xml "$LOGFILE")</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>PATH</key>
		<string>/usr/bin:/bin</string>
EOF
  if [ -n "$URL_ARG" ]; then
    cat <<EOF
		<key>CLAUDE_BUDDY_URL</key>
		<string>$(xml "$URL_ARG")</string>
EOF
  fi
  cat <<EOF
	</dict>
</dict>
</plist>
EOF
}

# --- lo que se puede mirar sin tocar nada ------------------------------------------------------------------------------

# El token, sin abrirlo: la regla del hook (archivo común, tuyo, sin permisos para grupo ni otros, 16 o más caracteres).
token_state() {
  local m s
  if [ ! -e "$TOKEN" ]; then say "falta"; return 0; fi
  if [ ! -f "$TOKEN" ]; then say "no es un archivo común"; return 0; fi
  if [ ! -O "$TOKEN" ]; then say "no es tuyo"; return 0; fi
  m="$(mode "$TOKEN")"
  case "$m" in
    0|*00) ;;
    *) say "permisos $m (tiene que ser 600: chmod 600)"; return 0 ;;
  esac
  if [ ! -r "$TOKEN" ]; then say "tuyo pero sin permiso de lectura"; return 0; fi
  s="$(size "$TOKEN")"
  if [ "${s:-0}" -lt 16 ]; then say "demasiado corto (menos de 16 caracteres)"; return 0; fi
  say "ok"
}

plist_state() {
  if [ ! -f "$PLIST" ]; then say "nuevo"
  elif render_plist | cmp -s - "$PLIST"; then say "igual"
  else say "cambia"; fi
}

plan() {
  local est t
  est="$(plist_state)"
  t="$(token_state)"
  say "El censo mira los procesos de esta Mac y le avisa a la pantalla del buddy qué corridas largas hay."
  say "Va a hacer (nada necesita root):"
  say "  plist     $PLIST  ($est)"
  say "  correr    $PYTHON -I -u $CENSUS run"
  say "  cargar    launchctl bootstrap $DOMAIN $PLIST   (arranca solo cada vez que iniciás sesión)"
  if [ -n "$URL_ARG" ]; then
    say "  destino   $URL_ARG  (fijado en el plist con --url)"
  else
    say "  destino   el de census.json, o $DEFAULT_URL si no hay (no se fija en el plist)"
  fi
  say "  log       $LOGFILE  (solo cuentas, nombres y tipos de error: ningún argumento ni entorno)"
  if [ -f "$CONFIG" ]; then say "  config    $CONFIG  (existe)"; else say "  config    $CONFIG  (no existe: solo corridas headless de Claude)"; fi
  if [ "$t" = ok ]; then say "  token     $TOKEN  (ok)"; else say "  token     $TOKEN  $t: sin un token usable no manda nada"; fi
  if [ "$est" != nuevo ] && [ -z "$URL_ARG" ] && [ -n "$(plist_url)" ]; then
    say "  ojo       el plist instalado fija la url a $(plist_url) y sin --url se quita"
  fi
  if [ -z "$URL_ARG" ] && [ -n "${CLAUDE_BUDDY_URL:-}" ]; then
    say "  ojo       CLAUDE_BUDDY_URL de esta terminal no llega al agente (launchd no hereda el entorno): usá --url"
  fi
  say "  aviso     macOS puede avisarte de un ítem de inicio nuevo: dejalo prendido (Ajustes del Sistema > General)"
  say "Para deshacerlo: $0 uninstall"
}

# --- los comandos ------------------------------------------------------------------------------------------------------

guard() {
  local h home_p
  [ -x "$LAUNCHCTL" ] || die "no encuentro launchctl ($LAUNCHCTL): esto es para macOS"
  [ -f "$CENSUS" ] || die "no encuentro $CENSUS: este script tiene que quedar junto a census.py"
  home_p="$(cd "$HOME" 2>/dev/null && pwd -P)" || home_p="$HOME"
  for h in "$HOME" "$home_p"; do
    case "$HERE/" in
      "$h"/Documents/*|"$h"/Desktop/*|"$h"/Downloads/*|"$h"/Library/Mobile\ Documents/*|"$h"/Library/CloudStorage/*)
        die "census.py está en una carpeta que macOS protege ($HERE): launchd no puede leerla sin un permiso. Cloná el repo fuera de Documents, Desktop, Downloads y iCloud (por ejemplo en ~/Projects)." ;;
    esac
  done
  case "$PYTHON" in
    /*) ;;
    *) die "CLAUDE_BUDDY_PYTHON tiene que ser una ruta absoluta" ;;
  esac
  [ -x "$PYTHON" ] || die "no encuentro $PYTHON"
  if [ "$PYTHON" = /usr/bin/python3 ] && command -v xcode-select >/dev/null 2>&1; then
    xcode-select -p >/dev/null 2>&1 || die "/usr/bin/python3 necesita las Command Line Tools: corré xcode-select --install"
  fi
  "$PYTHON" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "$PYTHON no sirve: hace falta Python 3.9 o más nuevo"
}

# bootout vuelve antes de que el servicio se haya ido: sin esperar, el bootstrap que sigue falla con "5: Input/output error"
wait_gone() { local i=0; while loaded && [ "$i" -lt 20 ]; do sleep 0.5; i=$((i + 1)); done; }

cmd_install() {
  local same=0 n=0 err
  guard
  plan
  if [ "$DRY" -eq 1 ]; then say "(-n: no cambié nada)"; return 0; fi
  if [ -f "$PLIST" ] && render_plist | cmp -s - "$PLIST"; then same=1; fi
  if [ "$same" -eq 1 ] && loaded; then
    say "Ya está instalado y cargado, igual a lo que escribiría: nada que cambiar."
    return 0
  fi
  mkdir -p "$AGENTS" "$(dirname "$LOGFILE")"
  ( umask 077; : >> "$LOGFILE" )        # el log lleva nombres de carpetas: que sea solo tuyo
  if [ "$same" -eq 0 ]; then
    TMPPLIST="$(mktemp "$AGENTS/.$LABEL.XXXXXX")"
    render_plist > "$TMPPLIST"
    if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$TMPPLIST" >/dev/null 2>&1; then
      die "el plist generado no es válido: no escribí nada"
    fi
    chmod 644 "$TMPPLIST"
  fi
  if loaded; then "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true; wait_gone; fi
  if [ "$same" -eq 0 ]; then mv -f "$TMPPLIST" "$PLIST"; TMPPLIST=""; fi
  "$LAUNCHCTL" enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true      # si alguna vez lo apagaste, bootstrap no lo carga
  until err="$("$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST" 2>&1)"; do
    n=$((n + 1))
    if [ "$n" -ge 4 ]; then
      die "launchctl bootstrap falló $n veces (${err:-sin mensaje}). Si apagaste el ítem en Ajustes del Sistema > General, prendelo y repetí."
    fi
    sleep 1
  done
  say "Instalado y cargado."
  sleep 2
  cmd_status
}

cmd_uninstall() {
  say "Va a descargar $DOMAIN/$LABEL y borrar $PLIST (el config y el token se quedan; --purge borra también el log)."
  if [ "$DRY" -eq 1 ]; then say "(-n: no cambié nada)"; return 0; fi
  if loaded; then
    "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    wait_gone
    if loaded; then say "ojo: launchd lo sigue mostrando cargado; se va al cerrar sesión"; else say "descargado"; fi
  else
    say "no estaba cargado"
  fi
  if [ -f "$PLIST" ]; then rm -f "$PLIST"; say "plist borrado"; else say "no había plist"; fi
  if [ "$PURGE" -eq 1 ]; then rm -f "$LOGFILE"; say "log borrado"; fi
}

# Pregunta una vez GET /healthz (sin token). Es lo único que sale a la red, y solo con --ping.
ping_buddy() {
  local u src reply code body
  if [ -n "$URL_ARG" ]; then u="$URL_ARG"; src="del plist"
  elif [ -n "${CLAUDE_BUDDY_URL:-}" ]; then u="${CLAUDE_BUDDY_URL%/}"; src="de CLAUDE_BUDDY_URL de esta terminal"
  else u="$DEFAULT_URL"; src="de fábrica"; fi
  if ! url_check "$u"; then say "buddy     no le hablo a ese destino ($src): $URL_WHY"; return 0; fi
  if ! command -v curl >/dev/null 2>&1; then say "buddy     no hay curl para preguntar"; return 0; fi
  reply="$(curl -s -m 2 --noproxy '*' -w '\n%{http_code}' "$u/healthz" 2>/dev/null || true)"
  code="${reply##*$'\n'}"
  body="${reply%$'\n'*}"
  if [ "$code" = 200 ] && [ "$body" = ok ]; then say "buddy     responde ($u, $src)"
  elif [ -z "$code" ] || [ "$code" = 000 ]; then say "buddy     no responde ($u, $src)"
  else say "buddy     contestó HTTP $code ($u, $src): ¿es el puerto de Hermes?"; fi
}

cmd_status() {
  local out
  URL_ARG="$(plist_url)"                # lo que sabe el plist instalado, para comparar y para el ping
  say "etiqueta  $LABEL"
  if [ ! -f "$PLIST" ]; then say "plist     no está instalado ($0 install)"
  elif render_plist | cmp -s - "$PLIST"; then say "plist     $PLIST  (igual a lo que escribiría este repo)"
  else say "plist     $PLIST  (distinto de lo que escribiría este repo: corré $0 install)"; fi
  if out="$("$LAUNCHCTL" print "$DOMAIN/$LABEL" 2>/dev/null)"; then
    printf '%s\n' "$out" \
      | sed -En 's/^[[:space:]]*(state|pid|runs|last exit code) = (.*)$/\1 = \2/p' \
      | awk -F' = ' '!seen[$1]++ { print "cargado   " $0 }'
  else
    say "cargado   no"
  fi
  say "token     $(token_state)  ($TOKEN)"
  if [ -f "$CONFIG" ]; then say "config    $CONFIG"; else say "config    ninguna (solo corridas headless de Claude)"; fi
  if [ "$PING" -eq 1 ]; then
    ping_buddy
  else
    say "buddy     sin contactar al Pi, usá --ping"
  fi
  if [ -f "$LOGFILE" ]; then
    say "log       $LOGFILE"
    tail -n 5 "$LOGFILE" | LC_ALL=C tr -d '\000-\010\013-\037\177' | sed 's/^/          /'
  fi
}

cmd_restart() {
  loaded || die "no está cargado: $0 install"
  "$LAUNCHCTL" kickstart -k "$DOMAIN/$LABEL"
  say "reiniciado"
}

case "$CMD" in
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  restart) cmd_restart ;;
  print) render_plist ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
