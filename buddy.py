#!/usr/bin/env python3
"""Claude Buddy for Raspberry Pi.

Speaks the Hardware Buddy BLE protocol of Claude desktop (macOS and Windows) so a
Pi can act as the little desk pet: it shows up in Developer > Open Hardware Buddy,
pairs with a passkey, receives session status and permission prompts, and answers
approve or deny.

    python3 buddy.py              run the daemon (keyboard: a approve, d deny; touch screen on :8765)
    python3 buddy.py check        diagnose Bluetooth and try the BlueZ registration
    python3 buddy.py forget-all   erase the pairings made with the buddy (other Bluetooth devices stay)
"""
import argparse
import asyncio
import getpass
import logging
import os
import signal
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from buddy_bluez import BlueZTransport, TransportError, diagnose, forget_all_devices
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Falta una dependencia ({exc.name}). Instalala con: pip install -r requirements.txt")

from buddy_backlight import DEFAULT_NIGHT_LEVEL, FLOOR, Backlight
from buddy_closet import CLOSET_DIRNAME, DEFAULT_EVERY_H, MAX_EVERY_H, MIN_EVERY_H, Closet
from buddy_core import Approval, ApprovalBroker, BuddyCore
from buddy_hermes import DEFAULT_PORT as HERMES_DEFAULT_PORT
from buddy_hermes import MIN_TOKEN_CHARS, TOKEN_ENV, HermesServer
from buddy_i18n import DEFAULT_LANG, parse_lang
from buddy_i18n import ENV_VAR as LANG_ENV
from buddy_ui import DEFAULT_NIGHT
from buddy_ui import DEFAULT_PORT as UI_DEFAULT_PORT
from buddy_ui import UIHub, UIServer, format_night, parse_night

__version__ = "0.3.0"

HELP_KEYS = "a aprobar | d denegar | s estado | u olvidar emparejamientos del buddy | q salir"
PEERS_FILE = "peers.json"


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-buddy"


def emit(text: str) -> None:
    print(text, flush=True)  # flush: under systemd stdout is a pipe and would otherwise sit in a buffer


class Terminal:
    """The screenless UI: prints what happens and lets you answer prompts from the keyboard."""

    def __init__(self, broker: ApprovalBroker, out=emit, color: bool = False) -> None:
        self.broker = broker
        self._out = out
        self._color = color
        self.core: Optional[BuddyCore] = None
        self.transport: Optional[BlueZTransport] = None
        self.request_quit = False
        self._last_beat: Any = None
        self._not_ready: Optional[str] = None
        self._passkey_up = False
        self._passkey_shown = ""
        self._passkey_entered = 0
        self._confirm_forget = False

    # -- output ----------------------------------------------------------------------

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._color else text

    def say(self, text: str, style: str = "") -> None:
        if not text:
            self._out("")
            return
        stamp = time.strftime("%H:%M:%S")
        self._out(f"{self._paint(stamp, '2')} {self._paint(text, style) if style else text}")

    # -- events from the core --------------------------------------------------------

    def on_core_event(self, event: str, data: Dict[str, Any]) -> None:
        if event == "link_up":
            self.say("Claude desktop conectado", "32")
        elif event == "link_down":
            why = "sin señal por 30 s" if data.get("reason") == "timeout" else "se cortó el enlace"
            self.say(f"Claude desktop desconectado ({why})", "33")
            self._last_beat = None
        elif event == "heartbeat":
            snap = data["snapshot"]
            beat = (snap.total, snap.running, snap.waiting, snap.msg)
            if beat != self._last_beat:
                self._last_beat = beat
                self.say(f"sesiones: {snap.total} | corriendo: {snap.running} | esperando: {snap.waiting} | {snap.msg}")
        elif event == "decision":
            word = "APROBADO" if data["decision"] == "once" else "DENEGADO"
            self.say(f"{word} en {data['seconds']:.1f}s", "32" if data["decision"] == "once" else "31")
        elif event == "level_up":
            self.say(f"Subió de nivel: {data['level']}", "35")
        elif event == "name":
            self.say(f"Nombre del buddy: {data['name']}")
        elif event == "owner":
            self.say(f"Dueño: {data['owner']}")
        elif event == "push_begin":
            self.say(f"Recibiendo personaje '{data['name']}' ({data['total'] // 1024} KB)")
        elif event == "character":
            self.say(f"Personaje instalado en {data['path']}", "32")
        elif event == "push_failed":
            self.say(f"Falló la transferencia: {data['error']}", "31")
        elif event == "unpaired":
            self.say("Emparejamientos borrados por pedido de Claude desktop", "33")

    # -- events from the Bluetooth side ---------------------------------------------

    def on_transport_event(self, event: str, data: Dict[str, Any]) -> None:
        if event == "ready":
            self._not_ready = None
            mode = "seguro" if data["secure"] else "INSEGURO (sin emparejar, solo para pruebas)"
            self.say(f"Bluetooth listo. Anunciando como {data['name']} ({mode}, anuncio {data['adv_mode']})", "32")
            self.say(f"En el Mac: Developer > Open Hardware Buddy > Connect > {data['name']}")
            if data["secure"]:
                self.say("Va a pedir un código de 6 dígitos: aparece acá")
            self.say(HELP_KEYS, "2")
        elif event == "not_ready":
            if data["reason"] != self._not_ready:
                self._not_ready = data["reason"]
                self.say(f"Bluetooth no está listo: {data['reason']} (sigo intentando)", "33")
        elif event == "device_connected":
            self.say(f"Dispositivo conectado: {data['label']}")
        elif event == "device_disconnected":
            self.say(f"Dispositivo desconectado: {data['label']}")
        elif event == "paired":
            self.say(f"Emparejado con {data['label']}", "32")
        elif event == "notify":
            self.say("Claude desktop " + ("activó" if data["enabled"] else "desactivó") + " las notificaciones")

    def on_passkey(self, passkey: Optional[str], entered: int) -> None:
        if passkey is None:
            if self._passkey_up:
                self._passkey_up = False
                self._passkey_shown, self._passkey_entered = "", 0
                self.say("Emparejamiento terminado")
            return
        self._passkey_up = True
        if passkey != self._passkey_shown:
            self._passkey_shown, self._passkey_entered = passkey, 0
            self.say("")
            self.say(f"   CÓDIGO DE EMPAREJAMIENTO:  {self._paint(passkey[:3] + ' ' + passkey[3:], '1;33')}"
                     "   (escribilo en el Mac)")
        if entered and entered != self._passkey_entered:
            self._passkey_entered = entered
            self.say(f"   dígitos ingresados: {entered} de 6", "2")

    def on_approval(self, event: str, approval: Approval) -> None:
        who = "" if approval.source == "claude" else f"[{approval.source}] "
        if event == "new":
            extra = len(self.broker.pending()) - 1
            more = f" (+{extra} en cola)" if extra else ""
            self.say("")
            self.say(f"¿APROBAR?  {who}{approval.tool}: {approval.hint}{more}", "1;33")
            self.say("           [a] aprobar   [d] denegar", "33")
        elif event == "withdrawn":
            self.say(f"(el pedido {who}{approval.tool} ya no está pendiente)", "2")
        elif event == "decided":
            if approval.source != "claude":  # Claude's own decisions are reported by the core, with the timing
                word = {"once": "APROBADO", "always": "APROBADO SIEMPRE"}.get(approval.decision or "", "DENEGADO")
                self.say(f"{word} {who}{approval.tool}", "31" if approval.decision == "deny" else "32")
            nxt = self.broker.current()
            if nxt is not None:
                self.say(f"Sigue: {'' if nxt.source == 'claude' else f'[{nxt.source}] '}{nxt.tool}: {nxt.hint}", "33")

    # -- keyboard ---------------------------------------------------------------------

    def on_key(self, key: str) -> None:
        if self._confirm_forget:
            self._confirm_forget = False
            if key.lower() == "y":
                asyncio.ensure_future(self._forget())
            else:
                self.say("Cancelado")
            return
        key = key.lower()
        if key in ("a", "d"):
            current = self.broker.current()
            if current is None:
                self.say("No hay nada pendiente", "2")
            else:
                self.broker.decide(current.key, "once" if key == "a" else "deny")
        elif key == "s":
            self.print_status()
        elif key == "u":
            self._confirm_forget = True
            self.say("¿Borrar los emparejamientos hechos con el buddy? Escribí y para confirmar, cualquier otra tecla cancela", "33")
        elif key in ("q", "\x03", "\x04"):
            self.request_quit = True
        elif key in ("?", "h"):
            self.say(HELP_KEYS)

    async def _forget(self) -> None:
        if self.transport is None:
            return
        try:
            count = await self.transport.forget_all()
        except Exception as exc:
            self.say(f"No pude borrar los emparejamientos: {exc}", "31")
            return
        self.say(f"Emparejamientos del buddy borrados: {count}", "32")

    def print_status(self) -> None:
        core, transport = self.core, self.transport
        if core is None or transport is None:
            return
        snap, st = core.snapshot, core.state
        self.say(f"estado: {core.persona()} | Claude desktop: {'conectado' if core.connected else 'sin conexión'}"
                 f" | sesiones: {snap.total} (corriendo {snap.running}, esperando {snap.waiting})")
        self.say(f"nivel: {st.level} | aprobadas: {st.approvals} | denegadas: {st.denials} | nombre: {st.name}"
                 f" | dueño: {st.owner or '-'}")
        ble = f"{transport.device_name} ({transport.address})" if transport.ready else f"no listo: {transport.not_ready_reason}"
        self.say(f"bluetooth: {ble} | cifrado: {'sí' if transport.is_secure() else 'no'}")


# --- commands ---------------------------------------------------------------------------


def make_transport(args: argparse.Namespace, **kw: Any) -> BlueZTransport:
    return BlueZTransport(
        on_data=kw.pop("on_data", lambda data: None),
        on_link_down=kw.pop("on_link_down", lambda: None),
        adapter=args.adapter,
        bus_address=args.bus,
        secure=not args.insecure,
        adv_mode=args.adv_mode,
        peers_path=args.state_dir / PEERS_FILE,
        **kw,
    )


async def start_screen(args: argparse.Namespace, hub: UIHub, term: Terminal) -> Optional[UIServer]:
    """Open the local page for the touch screen. The daemon works without it."""
    if args.no_ui:
        return None
    server = UIServer(hub, port=args.ui_port)
    try:
        await server.start()
    except OSError as exc:
        term.say(f"No pude abrir la pantalla en el puerto {args.ui_port}: {exc}. Sigo sin pantalla.", "33")
        return None
    term.say(f"Pantalla en {server.url} (en el Pi: ./kiosk.sh)")
    try:
        who = f"{getpass.getuser()}@{socket.gethostname()}.local"
    except Exception:  # an odd uid or hostname must not stop the daemon
        who = "usuario@buddy.local"
    term.say(f"Para verla desde otra compu: ssh -L {server.port}:localhost:{server.port} {who}  y abrí http://localhost:{server.port}", "2")
    return server


def make_backlight(args: argparse.Namespace) -> Optional[Backlight]:
    """The panel backlight night mode may move, or None: no night mode, `--backlight off`, or none this user can write
    (then the page darkens itself). Without night mode the daemon never touches the brightness."""
    if args.night is None or args.backlight == "off":
        return None
    return Backlight.find(args.backlight, args.night_level)


def describe_night(args: argparse.Namespace, backlight: Optional[Backlight]) -> Optional[Tuple[str, str]]:
    """What to tell the operator about night mode when the daemon starts: (text, style), None when it is off."""
    if args.night is None:
        return None
    window = format_night(args.night)
    if backlight is None:
        return f"Modo noche {window}: sin luz de pantalla que mover, la página se oscurece sola", "2"
    return (f"Modo noche {window}: la luz baja a {backlight.night} de {backlight.day} ({backlight.directory}) "
            "y un toque o un permiso la despiertan", "2")


def make_closet(args: argparse.Namespace, term: Terminal) -> Optional[Closet]:
    """The closet of costumes Buddy may wear, or None: `--closet off` (the default, nothing is read), no screen to wear
    one on, or a folder that cannot be read (one warning, and the closet stays off: never a reason to stop)."""
    spec = args.closet
    if spec == "off":
        return None
    if args.no_ui:
        term.say("Closet apagado: sin pantalla no hay quién vea los disfraces", "2")
        return None
    root = args.state_dir / CLOSET_DIRNAME if spec == "auto" else Path(spec).expanduser()
    problem = Closet.problem(root)
    if problem:
        term.say(f"Closet apagado: {problem}", "33")
        return None
    return Closet(root)


def load_hermes_token(args: argparse.Namespace) -> Tuple[Optional[str], str]:
    """The Hermes token, from `--hermes-token-file` or `$BUDDY_HERMES_TOKEN`: (token, complaint).
    Read once at start; never printed, never logged. A readable-by-everyone file is a warning, not a stop."""
    if args.hermes_token_file:
        path = args.hermes_token_file.expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"no pude leer el token de Hermes en {path}: {exc.strerror or exc}"
        token = text.strip().splitlines()[0].strip() if text.strip() else ""
        try:
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                emit(f"[ojo]   {path} lo puede leer cualquier usuario del Pi: chmod 600 {path}")
        except OSError:
            pass
    else:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            return None, f"falta el token de Hermes (--hermes-token-file RUTA o ${TOKEN_ENV})"
    if len(token) < MIN_TOKEN_CHARS:
        return None, f"el token de Hermes necesita al menos {MIN_TOKEN_CHARS} caracteres"
    return token, ""


async def start_hermes(args: argparse.Namespace, broker: ApprovalBroker, term: Terminal,
                       hub: Optional[UIHub] = None) -> Optional[HermesServer]:
    """Open the listener Hermes talks to, if asked for (`--hermes-bind`). The daemon works without it. `hub` is how a
    notice that asks for it wakes the dim night screen (the same `UIHub.wake` as a tap or a permission)."""
    if not args.hermes_bind:
        return None
    token, problem = load_hermes_token(args)
    if token is None:
        term.say(f"Hermes apagado: {problem}", "33")
        return None
    try:
        name = socket.gethostname()
        names = {name, f"{name}.local"}
    except Exception:
        names = set()
    tasks = term.core.tasks if term.core is not None else None  # so sessions on the Mac can report what they do
    notices = term.core.notices if term.core is not None else None  # so Hermes can leave a notice on the screen
    server = HermesServer(broker, token, host=args.hermes_bind, port=args.hermes_port, hosts=names, tasks=tasks,
                          notices=notices, wake=hub.wake if hub is not None else None)
    try:
        await server.start()
    except OSError as exc:
        term.say(f"No pude abrir el puerto de Hermes en {args.hermes_bind}:{args.hermes_port}: {exc}. Sigo sin Hermes.", "33")
        return None
    term.say(f"Hermes en {server.url}/approval (token propio; los pedidos salen en la pantalla como hermes · herramienta)")
    return server


async def run_daemon(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    use_keys = sys.stdin.isatty() and not args.no_keys
    term = Terminal(ApprovalBroker(), color=sys.stdout.isatty())
    term.say(f"Claude Buddy {__version__} | datos en {args.state_dir}")
    # The service runs without -v, so the dim/wake lines are printed like everything else it says.
    backlight = make_backlight(args)
    hub = UIHub(night=args.night, backlight=backlight, notice=lambda text: term.say(text, "2"),
                closet=make_closet(args, term), closet_every=args.closet_every * 3600.0, lang=args.lang)

    # The terminal and the screen both follow the same events.
    def on_core_event(event: str, data: Dict[str, Any]) -> None:
        term.on_core_event(event, data)
        hub.touch()

    def on_transport_event(event: str, data: Dict[str, Any]) -> None:
        term.on_transport_event(event, data)
        hub.touch()

    def on_passkey(passkey: Optional[str], entered: int) -> None:
        term.on_passkey(passkey, entered)
        hub.set_passkey(passkey, entered)

    transport = make_transport(
        args,
        on_data=lambda data: core.feed(data),
        on_link_down=lambda: core.link_down(),
        on_passkey=on_passkey,
        on_event=on_transport_event,
    )
    core = BuddyCore(
        args.state_dir,
        transport.send,
        broker=term.broker,
        unpair=transport.unpair,
        secure=transport.is_secure,
        on_event=on_core_event,
    )
    term.core, term.transport = core, transport
    term.broker.subscribe(term.on_approval)
    hub.bind(core, transport)

    stop = asyncio.Event()
    signals = [signal.SIGINT, signal.SIGTERM]
    if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:  # nohup ignores it on purpose; a closed terminal or ssh session stops us
        signals.append(signal.SIGHUP)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)

    restore_tty = _enable_keys(loop, term, stop) if use_keys else None
    screen: Optional[UIServer] = None
    hermes: Optional[HermesServer] = None
    try:
        screen = await start_screen(args, hub, term)
        if screen is None:  # no page (--no-ui, or its port is taken): nothing to dim and nothing to tap the panel awake with
            if hub.backlight is not None:
                hub.backlight.restore()  # not dimmed yet (that is the touch below): this just makes sure it never is
            hub.backlight = hub.night = None
            if hub.drop_closet():  # and nobody to dress Buddy for either
                term.say("Closet apagado: sin pantalla no hay quién vea los disfraces", "2")
        night = describe_night(args, hub.backlight) if screen is not None else None
        if night is not None:
            term.say(*night)
        hub.touch()  # night mode: set the panel's brightness now, not at the first tick
        hermes = await start_hermes(args, term.broker, term, hub)
        if not use_keys:
            if screen is not None:
                term.say("Sin teclado: los pedidos de aprobación se responden en la pantalla táctil", "2")
            else:
                term.say("Sin teclado ni pantalla: los pedidos de aprobación se verán pero no se pueden responder", "33")
        try:
            await transport.start()
        except TransportError as exc:
            term.say(str(exc), "31")
            return 1

        async def ticker() -> None:
            while True:
                await asyncio.sleep(1.0)
                try:
                    core.tick()
                    hub.refresh()
                except Exception:
                    logging.getLogger("buddy").exception("tick failed")
                if term.request_quit:
                    stop.set()

        tick_task = asyncio.ensure_future(ticker())
        try:
            await stop.wait()
        finally:
            hub.close()  # before anything awaits: stopping Bluetooth can take its time, and the screen must not stay dim meanwhile
            term.say("Cerrando...")
            tick_task.cancel()
            try:
                await transport.stop()
            finally:
                core.close()
    finally:
        hub.close()  # the same, for the ways out that never get to the stop above (it is safe to call twice)
        hub.unbind()
        if hermes is not None:
            await hermes.stop()
        if screen is not None:
            await screen.stop()
        if restore_tty:
            restore_tty()
    return 0


def _enable_keys(loop: asyncio.AbstractEventLoop, term: Terminal, stop: asyncio.Event):
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    def on_readable() -> None:
        try:
            data = os.read(fd, 16).decode(errors="ignore")
        except OSError:
            return
        # One keypress per read. Arrow keys arrive as "ESC [ A" and a paste as many characters:
        # neither may ever count as "a" (approve) or "d" (deny).
        if len(data) == 1:
            term.on_key(data)
        if term.request_quit:
            stop.set()

    loop.add_reader(fd, on_readable)

    def restore() -> None:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    return restore


def _print_findings(findings) -> int:
    tags = {"ok": "[ok]    ", "warn": "[ojo]   ", "fail": "[ERROR] "}
    for level, text in findings:
        print(f"{tags[level]}{text}")
    return 1 if any(level == "fail" for level, _ in findings) else 0


async def run_check(args: argparse.Namespace) -> int:
    findings, facts = await diagnose(args.adapter, args.bus, args.state_dir / PEERS_FILE)
    if _print_findings(findings):
        print("\nArreglá lo marcado como ERROR y repetí: python3 buddy.py check")
        return 1
    if facts.get("active_instances"):
        print("\nNo hago el ensayo de registro porque ya hay un anuncio activo.")
        return 0
    print("\nEnsayo: registro el servicio GATT, el agente de emparejamiento y el anuncio en BlueZ...")
    transport = make_transport(args)
    try:
        await transport.start()
        if transport.ready:
            mode = "seguro" if transport.secure else "INSEGURO"
            print(f"[ok]    BlueZ aceptó todo. Anuncio como {transport.device_name} (modo {transport.effective_adv_mode}, {mode})")
            return 0
        print(f"[ERROR] BlueZ no aceptó el registro: {transport.not_ready_reason}")
        return 1
    except TransportError as exc:
        print(f"[ERROR] {exc}")
        return 1
    finally:
        await transport.stop()


async def run_forget_all(args: argparse.Namespace) -> int:
    try:
        forgotten, left = await forget_all_devices(args.adapter, args.bus, args.state_dir / PEERS_FILE)
    except TransportError as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(f"Emparejamientos del buddy borrados: {forgotten}")
    if left:
        print(f"No toqué {left} dispositivo(s) más que BlueZ recuerda (teclados, audífonos...). "
              "Esos se quitan con bluetoothctl remove <MAC>")
    return 0


def night_window(text: str) -> Optional[Tuple[int, int]]:
    """argparse type for --night: HH:MM-HH:MM or off."""
    try:
        return parse_night(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def backlight_spec(text: str) -> str:
    """argparse type for --backlight: auto, off or a directory (a missing one is a warning at start, not an error:
    the screen still works, it just darkens itself)."""
    if not text.strip():
        raise argparse.ArgumentTypeError("usá auto, off o la carpeta de la luz (por ejemplo /sys/class/backlight/10-0045)")
    return text.strip()


def lang_choice(text: str) -> str:
    """argparse type for --lang: es or en."""
    try:
        return parse_lang(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def closet_spec(text: str) -> str:
    """argparse type for --closet: off, auto or a directory (one that cannot be read is a warning at start, not an error)."""
    spec = text.strip()
    if not spec:
        raise argparse.ArgumentTypeError("usá off, auto o la carpeta del closet (por ejemplo ~/disfraces)")
    return spec.lower() if spec.lower() in ("off", "auto") else spec


def closet_hours(text: str) -> float:
    """argparse type for --closet-every: hours between one look and the next, MIN_EVERY_H to MAX_EVERY_H, or 0 for "once"."""
    try:
        hours = float(text)
    except ValueError:
        hours = float("nan")
    if hours != hours or not (hours == 0 or MIN_EVERY_H <= hours <= MAX_EVERY_H):
        raise argparse.ArgumentTypeError(f"{text!r} no sirve: usá un número de horas entre {MIN_EVERY_H:g} y {MAX_EVERY_H:g}, "
                                         "o 0 para elegir un disfraz solo al arrancar")
    return hours + 0.0  # no negative zero


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="buddy.py",
        description="Claude Buddy para Raspberry Pi (protocolo Hardware Buddy de Claude desktop).",
    )
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "forget-all"],
                        help="run (default), check (diagnóstico) o forget-all (borrar emparejamientos)")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(), help="dónde guardar estado y personajes")
    parser.add_argument("--adapter", default="hci0", help="adaptador Bluetooth (default hci0)")
    parser.add_argument("--adv-mode", choices=["name", "kernel"], default="name",
                        help="cómo se anuncia el nombre BLE: name (default, nombre completo) o kernel "
                             "(plan B: el kernel lo pone y lo corta a 10 letras)")
    parser.add_argument("--insecure", action="store_true",
                        help="SOLO PRUEBAS: sin emparejamiento ni cifrado (los textos de tus sesiones viajan en claro)")
    parser.add_argument("--no-keys", action="store_true", help="no leer el teclado (para correr como servicio)")
    parser.add_argument("--ui-port", type=int, default=UI_DEFAULT_PORT,
                        help=f"puerto local de la pantalla táctil (default {UI_DEFAULT_PORT}; solo escucha en 127.0.0.1)")
    parser.add_argument("--no-ui", action="store_true", help="no abrir la pantalla táctil (solo terminal)")
    parser.add_argument("--night", type=night_window, default=DEFAULT_NIGHT, metavar="HH:MM-HH:MM|off",
                        help=f"modo noche: en esta franja (hora local del Mac; si no la sabe, la del Pi) la pantalla queda tenue, "
                             f"y un toque, un permiso o un aviso de Hermes con wake la despiertan 30 s (default {DEFAULT_NIGHT}; "
                             "inicio igual a fin: siempre de noche; off: sin modo noche, y el daemon no toca la luz)")
    parser.add_argument("--night-level", type=int, default=DEFAULT_NIGHT_LEVEL, metavar="N",
                        help=f"luz de la pantalla de noche (default {DEFAULT_NIGHT_LEVEL}; se corta entre {FLOOR} y el máximo del panel: "
                             "nunca se apaga del todo). De día va al máximo")
    parser.add_argument("--backlight", type=backlight_spec, default="auto", metavar="auto|off|RUTA",
                        help="la luz del panel que mueve el modo noche: auto (la primera de /sys/class/backlight que se pueda escribir), "
                             "off (la página se oscurece sola) o la carpeta de una (con brightness y max_brightness)")
    parser.add_argument("--closet", type=closet_spec, default="off", metavar="off|auto|CARPETA",
                        help="el closet de disfraces: a veces Buddy sale con uno, elegido al azar entre los que hay (y a veces sin). "
                             f"off (default: no se lee nada), auto (la carpeta {CLOSET_DIRNAME} del state-dir) o la carpeta de uno. "
                             "Los disfraces se hacen con tools/make_closet.py y se copian ahí (nunca van a git); Buddy se los pone "
                             "solo sobre el personaje para el que se dibujaron")
    parser.add_argument("--closet-every", type=closet_hours, default=DEFAULT_EVERY_H, metavar="HORAS",
                        help=f"cada cuántas horas Buddy elige otro disfraz (default {DEFAULT_EVERY_H:g}; de {MIN_EVERY_H:g} a {MAX_EVERY_H:g}; "
                             "0: elige uno al arrancar y no cambia más)")
    parser.add_argument("--hermes-bind", metavar="IP",
                        help="recibir pedidos de aprobación y avisos de Hermes Agent en esta IP (la de Tailscale del Pi, o "
                             "0.0.0.0 para toda la LAN). Sin esto Hermes queda apagado. Necesita un token")
    parser.add_argument("--hermes-port", type=int, default=HERMES_DEFAULT_PORT,
                        help=f"puerto para Hermes (default {HERMES_DEFAULT_PORT}; aparte del de la pantalla)")
    parser.add_argument("--hermes-token-file", type=Path, metavar="RUTA",
                        help=f"archivo con el token que Hermes manda (Authorization: Bearer). Si no, ${TOKEN_ENV}. "
                             f"Mínimo {MIN_TOKEN_CHARS} caracteres; nunca se imprime ni se loguea")
    parser.add_argument("--lang", type=lang_choice, default=None, metavar="es|en",
                        help=f"idioma de la pantalla: es (default, español) o en (inglés). Sin la opción se lee ${LANG_ENV}. "
                             "La terminal y el registro siguen en español")
    parser.add_argument("--bus", help=argparse.SUPPRESS)  # private D-Bus address, used by the tests
    parser.add_argument("-v", "--verbose", action="count", default=0, help="más logs (-v, -vv)")
    parser.add_argument("--log-file", type=Path, help="escribir los logs en este archivo (para mandarlos sin copiar de la terminal)")
    args = parser.parse_args(argv)
    args.state_dir = args.state_dir.expanduser()
    if args.lang is None:  # the flag wins; the environment is only the fallback, and a bad value there is as wrong as one here
        from_env = os.environ.get(LANG_ENV)
        if from_env is None or not from_env.strip():
            args.lang = DEFAULT_LANG
        else:
            try:
                args.lang = parse_lang(from_env)
            except ValueError:
                parser.error(f"${LANG_ENV}={from_env!r} no es un idioma: usá es o en")
    return args


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # a C locale must never crash the printing
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    level = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    handlers = None
    if args.log_file:
        try:
            handlers = [logging.FileHandler(args.log_file.expanduser(), encoding="utf-8")]
        except OSError as exc:
            sys.exit(f"No puedo escribir el log en {args.log_file}: {exc}")
    logging.basicConfig(level=level, handlers=handlers, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("buddy").info("buddy %s, python %s, %s", __version__, sys.version.split()[0], sys.platform)
    runner = {"run": run_daemon, "check": run_check, "forget-all": run_forget_all}[args.command]
    try:
        return asyncio.run(runner(args))
    except TransportError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
