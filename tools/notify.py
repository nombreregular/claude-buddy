#!/usr/bin/env python3
"""Leave a short notice on the Claude Buddy screen: "la tanda termino", "llego un correo", "reunion en 10 min".

Hermes (or any script) runs this as a shell command. It posts to the buddy's Hermes listener (`POST /notice`, the same
port and token as the hook and the census) and the notice shows at the top of the thread list, goes away with a tap,
expires by itself and, with --wake, wakes the dim night screen. Like /session and /census it can never approve or
deny anything.

    notify.py --title "La tanda termino" --body "37 de 40 pasaron" --kind ok --ttl 600 --id tanda-21 --wake
    echo "detalle largo" | notify.py --title "Aviso" --body -
    notify.py --status tanda-21         # {"id":"tanda-21","state":"shown"}, or dismissed, expired, withdrawn, evicted, unknown
    notify.py --withdraw tanda-21       # takes it off the screen; an id the buddy does not know is fine too

The same --id replaces the notice that has it (its time starts over). The title is cut to 60 characters, the body to 4
lines and 240 characters, the source (shown as "de <source>", the buddy says "hermes" without one) to 24; control
characters and runs of spaces are cleaned the way the buddy cleans them (it cleans again on its side, and only there do
bidi marks and zero width spaces go). `--body -` reads the text from stdin, which has to close (`echo texto | notify.py
...`): a stdin that stays open until the 3 seconds are up is a failure, exit 1.

Exit codes: 0 the buddy answered (the answer is printed on stdout, one line of JSON), 1 the notice could not be
delivered (no token, a URL the token must not go to, the network, any 4xx or 5xx; the reason is one fixed phrase on
stderr, with the name of the exception or the HTTP status and nothing else: never the token and never the text), 2 usage.
The whole call takes 3 seconds at most, reading stdin included, and is never retried: a wrong token feeds the lockout the
hooks share.

Options as environment variables:

    CLAUDE_BUDDY_URL             default http://buddy:8766 (http only; this machine or the tailnet, or --insecure-url-ok;
                                 the token is also held back if the name leads to any other address, and that flag
                                 lifts this check as well)
    CLAUDE_BUDDY_TOKEN_FILE      default ~/.config/claude-buddy/hermes.token, must be a regular file, yours, 0600 and
                                 16 or more characters, the same rule as tools/context_hook.py

Standard library only, and the Python 3.9 that ships with macOS; imports nothing from this repo, so it also runs on the
Pi and on the machine where Hermes lives.
"""
import http.client
import ipaddress
import json
import math
import os
import re
import stat
import sys
import threading
import time
from typing import Any, BinaryIO, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

DEFAULT_URL = "http://buddy:8766"
DEFAULT_TOKEN_FILE = "~/.config/claude-buddy/hermes.token"
TOTAL_TIMEOUT_S = 3.0  # stdin, name lookup, connection and answer together: a name that never resolves must not hang Hermes
MIN_TOKEN_CHARS = 16
MAX_STDIN = 16 * 1024  # `--body -` reads this much at most: a pipe that never ends must not hold the call
MAX_REPLY_BYTES = 8192
MAX_POST_BYTES = 3900  # the listener refuses bodies over 4096 bytes; the same margin the hook keeps
TITLE_CHARS, BODY_CHARS, BODY_LINES, SOURCE_CHARS = 60, 240, 4, 24  # what the daemon keeps
KINDS = ("info", "ok", "warn")
ID_OK = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
TAILNET_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]*")  # `buddy`; a number such as 3232235777 or 0xc0a80101 is an IPv4 address in disguise

VALUE_OPTIONS = ("--title", "--body", "--kind", "--ttl", "--id", "--source", "--status", "--withdraw")
FLAG_OPTIONS = ("--wake", "--insecure-url-ok", "--help")
SEND_OPTIONS = ("--title", "--body", "--kind", "--ttl", "--id", "--source", "--wake")

USAGE = """uso: notify.py --title TITULO [--body TEXTO | --body -] [--kind info|ok|warn] [--ttl SEGUNDOS]
                [--wake] [--id ID] [--source FUENTE] [--insecure-url-ok]
     notify.py --status ID
     notify.py --withdraw ID
  --body -    lee el texto de la entrada estandar, que tiene que cerrarse (se corta a 240 caracteres)
  --ttl       segundos en pantalla, de 10 a 3600 (por defecto 300)
  --wake      despierta la pantalla tenue de noche
  --id        el mismo id reemplaza al aviso que lo tiene
  --status    en que estado esta un aviso; --withdraw lo saca de la pantalla
URL en CLAUDE_BUDDY_URL (por defecto http://buddy:8766), token en CLAUDE_BUDDY_TOKEN_FILE
salida: 0 entregado, 1 no se pudo entregar, 2 uso incorrecto
"""

REFUSALS = {
    400: "buddy no acepto el aviso (400): el titulo, el tipo, el ttl o el id no valen",
    403: "buddy rechazo el token o el Host (403)",
    404: "este buddy no conoce /notice (daemon viejo o sin tablero de avisos)",
    413: "buddy dice que el aviso es muy grande (413)",
    415: "buddy no acepto el tipo de contenido (415)",
    429: "buddy dice que son demasiados avisos (429): esperar un minuto",
}


class Usage(Exception):
    """The command line is wrong. The message is a fixed phrase and may name an option, never a value."""


class PeerRefused(OSError):
    """The name led to an address that is neither this machine nor the tailnet, so the token was not sent."""


class StdinStalled(Exception):
    """`--body -` and a stdin that did not end (nor fill MAX_STDIN) before the deadline."""


# --- the command line ------------------------------------------------------------------------------------------

def parse(argv: List[str]) -> Dict[str, Any]:
    """Options as `--name value`, `--name=value` and bare flags. A value is whatever follows its option, even `-` or
    something that starts with a dash, unless that word is itself one of our options (a forgotten value)."""
    found: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        word = argv[i]
        i += 1
        name, equals, value = word.partition("=") if word.startswith("--") else (word, "", "")
        if word in ("-h", "--help"):
            found["--help"] = True
        elif name in FLAG_OPTIONS and not equals:
            if name in found:
                raise Usage("la opcion %s esta repetida" % name)
            found[name] = True
        elif name in VALUE_OPTIONS:
            if name in found:
                raise Usage("la opcion %s esta repetida" % name)
            if not equals:
                if i >= len(argv) or argv[i] in VALUE_OPTIONS or argv[i] in FLAG_OPTIONS:
                    raise Usage("a la opcion %s le falta el valor" % name)
                value = argv[i]
                i += 1
            found[name] = value
        elif name in FLAG_OPTIONS:
            raise Usage("la opcion %s no lleva valor" % name)
        elif word.startswith("-"):
            raise Usage("hay una opcion que no existe")
        else:
            raise Usage("hay palabras sueltas: el valor de cada opcion va entre comillas")
    return found


def plain(text: str) -> str:
    """Text that survives the wire: a byte that is not UTF-8 (a lone surrogate from argv) becomes `?`."""
    return text.encode("utf-8", "replace").decode("utf-8")


def one_line(text: str, limit: int) -> str:
    """Control characters become spaces, runs of whitespace one space, the ends are trimmed, then it is cut."""
    return " ".join(CONTROL.sub(" ", plain(text)).split())[:limit].rstrip()


def body_text(text: str) -> str:
    """Lines are kept (CR removed, each one cleaned like a title); blank lines at the ends go and a run of them becomes one,
    and only then come at most BODY_LINES of them and BODY_CHARS characters: the buddy's own order, so a run of blank lines
    never costs the text that follows it (the buddy cannot give back what is cut here)."""
    rows: List[str] = []
    for line in plain(text).replace("\r", "").split("\n"):
        line = " ".join(CONTROL.sub(" ", line).split())
        if line or (rows and rows[-1]):
            rows.append(line)
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[:BODY_LINES])[:BODY_CHARS].rstrip()


def read_stdin(stream: Optional[Any], deadline: float) -> str:
    """Up to MAX_STDIN bytes of `stream`, decoded leniently; "" when there is no stream or it cannot be read. The read runs
    in a daemon thread that is abandoned at `deadline` (a time.monotonic() value): a read returns only at EOF or MAX_STDIN, so
    a stdin that stays open would hold the call far past its 3 seconds. StdinStalled then."""
    if stream is None:
        return ""
    box: List[Any] = []

    def work() -> None:
        try:
            box.append(stream.read(MAX_STDIN))
        except Exception as exc:
            box.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    if not box:
        raise StdinStalled("stdin")
    data = box[0]
    if isinstance(data, (OSError, ValueError)):
        return ""
    if isinstance(data, Exception):
        raise data
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)


def notice_id(raw: str, option: str) -> str:
    if not ID_OK.fullmatch(raw):
        raise Usage("%s tiene que ser de 1 a 64 letras, numeros o _ . : -" % option)
    return raw


def request_of(found: Mapping[str, Any], stdin: Optional[Any], deadline: float) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """(method, path, body) for what the command line asks. `deadline` is how long (time.monotonic()) stdin may be waited for."""
    modes = [o for o in ("--status", "--withdraw") if o in found]
    sending = [o for o in SEND_OPTIONS if o in found]
    if len(modes) > 1 or (modes and sending):
        raise Usage("--status, --withdraw y el envio de un aviso no se mezclan")
    if modes:
        return ("GET" if modes[0] == "--status" else "DELETE"), "/notice/" + notice_id(found[modes[0]], modes[0]), None
    title = one_line(found.get("--title", ""), TITLE_CHARS)
    if not title:
        raise Usage("falta --title" if "--title" not in found else "--title esta vacio")
    notice: Dict[str, Any] = {"title": title}
    if "--body" in found:
        body = body_text(read_stdin(stdin, deadline) if found["--body"] == "-" else found["--body"])
        if body:
            notice["body"] = body
    if "--kind" in found:
        if found["--kind"] not in KINDS:
            raise Usage("--kind tiene que ser info, ok o warn")
        notice["kind"] = found["--kind"]
    if "--ttl" in found:
        notice["ttl"] = seconds(found["--ttl"])
    if "--wake" in found:
        notice["wake"] = True
    if "--id" in found:
        notice["id"] = notice_id(found["--id"], "--id")
    if "--source" in found:
        source = one_line(found["--source"], SOURCE_CHARS)
        if source:
            notice["source"] = source
    return "POST", "/notice", notice


def seconds(raw: str) -> Any:
    """A finite number; the buddy clamps it to 10..3600 itself, and a NaN or an infinity it would refuse."""
    text = raw.strip()
    value = float(text) if NUMBER.fullmatch(text) else math.nan
    if not math.isfinite(value):
        raise Usage("--ttl tiene que ser un numero de segundos")
    return int(value) if value.is_integer() and abs(value) < 1e9 else value


# --- where the token may go ------------------------------------------------------------------------------------

def read_token(path_text: str) -> Optional[str]:
    """The same rule as tools/context_hook.py: the token only from a file that is ours and unreadable to others."""
    path = os.path.expanduser(path_text)
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except (OSError, ValueError):
        return None
    return token if len(token) >= MIN_TOKEN_CHARS else None


def url_problem(url: str, insecure_ok: bool) -> Optional[str]:
    """Why the token must not be sent to this URL, or None. It travels in plain http, so only this machine or the tailnet.
    The same rule as tools/census.py."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "la URL del buddy no se entiende: no mando nada"
    host = parts.hostname or ""
    if port is not None and port == 0:
        return "la URL del buddy no se entiende: no mando nada"
    if parts.scheme != "http" or not host or parts.username is not None or parts.password is not None:
        return "la URL del buddy tiene que ser http://nombre:puerto: no mando nada"
    if insecure_ok:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        ok = ip.is_loopback or (ip.version == 4 and ip in TAILNET_V4)
    else:
        ok = host == "localhost" or host.endswith(".ts.net") or TAILNET_NAME.fullmatch(host) is not None  # `buddy`: a Tailscale name
    if ok:
        return None
    return ("la URL del buddy no es de esta maquina ni de la tailnet y el token viajaria en claro: no mando nada "
            "(--insecure-url-ok si de verdad queres)")


def peer_may_hear_token(address: Any) -> bool:
    """The same rule as tools/census.py and the hook's guard for `said`: this machine, or 100.64.0.0/10 and fd7a:115c:a1e0::/48."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])  # IPv6 peers can carry a %scope
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is not "loopback" to Python 3.9 until unwrapped
        return bool(ip.is_loopback or ip in (TAILNET_V4 if ip.version == 4 else TAILNET_V6))
    except ValueError:
        return False


# --- talking to the buddy --------------------------------------------------------------------------------------

def encode(body: Dict[str, Any]) -> bytes:
    """The request body: ASCII (json escapes the rest) unless that does not fit the listener's limit, which only text that
    is nearly all emoji can do: UTF-8 is shorter than the \\u escapes of a surrogate pair."""
    data = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(data) > MAX_POST_BYTES:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return data


def request(url: str, token: str, method: str, path: str, body: Optional[Dict[str, Any]], strict: bool) -> Tuple[int, bytes]:
    """The HTTP status and the first bytes of the answer. Raises OSError / HTTPException when the buddy is not there.

    url_problem judges the URL by its text; `strict` (the URL passed it) also checks the address the name really led to,
    before the token is written. A URL the text rule refuses is only here because of --insecure-url-ok: that is the
    caller's call. Redirects are never followed."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=TOTAL_TIMEOUT_S)
    try:
        conn.connect()
        if strict and not peer_may_hear_token(conn.sock.getpeername()[0]):
            raise PeerRefused("peer")
        headers = {"Authorization": "Bearer " + token, "Host": parts.netloc, "Accept": "application/json",
                   "Connection": "close"}
        data = None
        if body is not None:
            data = encode(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, response.read(MAX_REPLY_BYTES + 1)
    finally:
        conn.close()


def send(url: str, token: str, method: str, path: str, body: Optional[Dict[str, Any]], strict: bool,
         budget: Optional[float] = None) -> Tuple[int, bytes]:
    """request() in a daemon thread that is abandoned after `budget` seconds (TOTAL_TIMEOUT_S without one; `run` hands over
    what is left of it after stdin): a name lookup that ignores socket timeouts or an answer that trickles in must not
    hold Hermes."""
    box: List[Any] = []

    def work() -> None:
        try:
            box.append(request(url, token, method, path, body, strict))
        except Exception as exc:
            box.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(TOTAL_TIMEOUT_S if budget is None else max(0.0, budget))
    if not box:
        raise TimeoutError("send")
    if isinstance(box[0], Exception):
        raise box[0]
    return box[0]


def say(message: str) -> None:
    sys.stderr.write("notify: " + message + "\n")
    sys.stderr.flush()


def answer_line(raw: bytes) -> Optional[str]:
    """The buddy's answer as one line of JSON, or None when it is not a JSON object (some other server on that port)."""
    if len(raw) > MAX_REPLY_BYTES:
        return None
    try:
        answer = json.loads(raw.decode("utf-8"))
        return json.dumps(answer, separators=(",", ":"), allow_nan=False) if isinstance(answer, dict) else None
    except (ValueError, RecursionError):
        return None


def main(argv: List[str], env: Optional[Mapping[str, str]] = None, stdin: Optional[BinaryIO] = None) -> int:
    """`env` and `stdin` are the process's own unless given (tests)."""
    env = os.environ if env is None else env
    try:
        return run(argv, env, stdin)
    except KeyboardInterrupt:
        say("interrumpido")
        return 1
    except Exception as exc:  # whatever it is, one phrase with its TYPE: never its text, which could hold the token
        say("fallo inesperado (%s)" % type(exc).__name__)
        return 1


def run(argv: List[str], env: Mapping[str, str], stdin: Optional[BinaryIO]) -> int:
    deadline = time.monotonic() + TOTAL_TIMEOUT_S  # one budget for stdin and the network together
    try:
        found = parse(argv)
        if "--help" in found:
            sys.stdout.write(USAGE)
            return 0
        method, path, body = request_of(found, stdin if stdin is not None else getattr(sys.stdin, "buffer", None), deadline)
    except Usage as exc:
        say(str(exc))
        sys.stderr.write(USAGE)
        return 2
    except StdinStalled:
        say("la entrada estandar no se cerro a tiempo: con --body - el texto tiene que llegar por una tuberia que termine "
            "(echo texto | notify.py ...)")
        return 1
    insecure = "--insecure-url-ok" in found
    url = (env.get("CLAUDE_BUDDY_URL") or "").strip() or DEFAULT_URL
    problem = url_problem(url, insecure)
    if problem:
        say(problem)
        return 1
    token = read_token(env.get("CLAUDE_BUDDY_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    if token is None:
        say("no hay un token que se pueda usar: el archivo tiene que ser un archivo tuyo, 0600 y de 16 o mas caracteres")
        return 1
    try:
        # the address check is for a URL that passed the text rule on its own merits; the flag lifts it, as it lifts the text rule
        status, raw = send(url, token, method, path, body, not insecure and url_problem(url, False) is None,
                           deadline - time.monotonic())
    except PeerRefused:
        say("el nombre del buddy lleva a una direccion que no es de esta maquina ni de la tailnet: no le mando el token "
            "(--insecure-url-ok si de verdad queres)")
        return 1
    except (OSError, ValueError, http.client.HTTPException) as exc:  # a ValueError: a token http.client will not put in a header
        say("no se pudo entregar (%s)" % type(exc).__name__)
        return 1
    if not 200 <= status < 300:
        say(REFUSALS.get(status) or "buddy contesto %d" % status)
        return 1
    line = answer_line(raw)
    if line is None:
        say("la respuesta del buddy no se entiende (%d): no se sabe si llego" % status)
        return 1
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)  # a lookup abandoned after the timeout is still running in its thread: do not wait for it
