"""The buddy's own screen: a tiny local web server that feeds a kiosk page.

The Pi's 7" touch display runs Chromium full screen on http://127.0.0.1:8765. The page
shows what the buddy knows (Bluetooth state, the pairing code, sessions, the pet's mood)
and has two big buttons for the pending permission request. Pure standard library.

    UIHub       what the screen shows, computed from the core, the broker and the radio.
                No sockets: it produces a JSON-able dict and says when it changed.
                Optionally Buddy wears a costume from the closet (buddy_closet.py), a view-only choice.
    HttpServer  the HTTP/1.1 plumbing (one request per connection, strict parsing, bounded
                sizes). Shared with the Hermes listener in buddy_hermes.py.
    UIServer    HTTP on loopback: the page (GET /), the live state (GET /events, server-sent
                events), a snapshot (GET /state), the answer to a request (POST /decide), a tap
                on the dark screen (POST /wake) and a tap on "Listo" of a notice (POST /dismiss).

Security notes, because anything that can reach POST /decide can approve a command:
* It only listens on loopback. To look at it from another computer use an SSH tunnel.
* A request needs the Host of a loopback name (against DNS rebinding), an Origin that
  matches if the browser sends one, a JSON content type (a plain HTML form cannot send
  it) and the token that is baked into the page when it is served.
* The page writes everything it receives with textContent, never as HTML: the text of a
  permission request comes from a session that may be steered by a hostile web page.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import deque
from pathlib import Path
from stat import S_ISREG
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

from buddy_backlight import Backlight
from buddy_closet import DEFAULT_EVERY_H, Closet, Costume
from buddy_closet import stamp as closet_stamp
from buddy_core import DECISIONS, TOKENS_PER_LEVEL
from buddy_i18n import DEFAULT_LANG, ble_reason, normalize

log = logging.getLogger("buddy.ui")

DEFAULT_PORT = 8765
HEADER_TIMEOUT_S = 10.0
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 4 * 1024
MAX_CLIENTS = 8
PING_S = 10.0
SLOW_CLIENT_S = 5.0
RETRY_MS = 1500  # how soon the page asks again after the stream drops
TOKEN_PLACEHOLDER = "__BUDDY_TOKEN__"
LANG_PLACEHOLDER = "__BUDDY_LANG__"  # where the served page learns its language before the first state arrives
WAKE_RATE_WINDOW_S = 10.0
WAKE_RATE_MAX = 20  # POST /wake per window, counted apart from everything else: a flood of taps cannot starve /decide
DISMISS_RATE_WINDOW_S = 10.0
DISMISS_RATE_MAX = 20  # POST /dismiss per window, its own count as well
DEFAULT_NIGHT = "23:00-07:00"
AWAKE_S = 30.0  # how long a tap or a permission request keeps the screen awake at night
DAY_S = 86400.0

PAGE_DIR = Path(__file__).resolve().parent / "ui"

CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
)
IMAGE_TYPES = {".gif": "image/gif", ".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_NIGHT_RANGE = re.compile(r"\s*([0-9]{1,2}):([0-9]{2})\s*-\s*([0-9]{1,2}):([0-9]{2})\s*")


# --- the night window ------------------------------------------------------------------


def parse_night(text: str) -> Optional[Tuple[int, int]]:
    """`--night`: "HH:MM-HH:MM" as (start, end) in seconds after midnight, or None for "off". The window wraps over
    midnight when the end comes before the start; start == end means it is always night. ValueError for anything else."""
    if text.strip().lower() == "off":
        return None
    found = _NIGHT_RANGE.fullmatch(text)
    edges: List[int] = []
    if found is not None:
        for hour, minute in ((found.group(1), found.group(2)), (found.group(3), found.group(4))):
            if int(hour) > 23 or int(minute) > 59:
                found = None
                break
            edges.append(int(hour) * 3600 + int(minute) * 60)
    if found is None:
        raise ValueError(f"{text!r} no es una franja de noche: usá HH:MM-HH:MM (por ejemplo {DEFAULT_NIGHT}) u off")
    return edges[0], edges[1]


def format_night(window: Tuple[int, int]) -> str:
    """The window the way `--night` writes it."""
    return "%02d:%02d-%02d:%02d" % (window[0] // 3600, window[0] % 3600 // 60, window[1] // 3600, window[1] % 3600 // 60)


def in_night(window: Tuple[int, int], second: float) -> bool:
    """Is `second` (since local midnight) inside the window? The start is inside, the end is not."""
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= second < end
    return second >= start or second < end


def system_second_of_day() -> float:
    """Seconds since midnight on this computer's own clock: where the night is when the Mac has not said what time it is."""
    now = time.time()
    stamp = time.localtime(now)
    return stamp.tm_hour * 3600 + stamp.tm_min * 60 + stamp.tm_sec + now % 1.0


# --- what the screen shows -------------------------------------------------------------


class UIHub:
    """Computes the screen's state and tells listeners when it changes.

    Call `bind` once the core and the transport exist, then `touch()` whenever something
    may have changed (events) and `refresh()` about once a second (time based changes: the
    pet's mood, the clock, the link timeout).

    Night mode (`night`, a window from `parse_night`): inside the window the screen is dim (`view.dim`) unless it is
    awake, which it is for AWAKE_S after `wake()` and while any permission request is pending or a pairing code is on
    screen (plus AWAKE_S after the last one is answered, expires or goes away). The hub owns that state: it moves the
    panel backlight when there is one (`view.backlight` says so, and the page darkens itself only without it) and sets
    a timer for the next moment the state changes, so the screen goes dim at the right second even when nothing else
    happens. `notice` is where the one line per dim/wake transition goes ("noche: pantalla tenue"); without it they go
    to the log, which the service does not show (it runs without -v).

    The closet (`closet`, a buddy_closet.Closet, off by default): Buddy wears one costume at a time or none, picked
    when `bind` is called and again every `closet_every` seconds (0: never again). A costume is a whole character
    pack in the closet, drawn over one base pack; it is worn only while that base is the active pack, and `character()`
    then describes the costume instead (name = its id, plus `costume`, its Spanish name). What the core persists
    (`state.character`) is never touched: the costume is a view-only override, and a closet that is emptied or corrupted
    underneath simply leaves the base pack showing. One timer, like the night one, wakes the hub for the next pick. The
    lines about it ("Closet: ...", "Buddy se puso ...") go where the night ones go.

    The language (`lang`, "es" or "en", Spanish unless told otherwise) is in every view, so the page speaks it; the
    human texts the daemon puts into the view (the reason Bluetooth is not ready) are put into it here. What goes to
    the terminal and the journal stays in Spanish."""

    def __init__(self, *, night: Optional[Tuple[int, int]] = None, backlight: Optional[Backlight] = None,
                 clock: Callable[[], float] = time.monotonic,
                 system_time: Callable[[], float] = system_second_of_day,
                 notice: Optional[Callable[[str], None]] = None,
                 closet: Optional[Closet] = None, closet_every: float = DEFAULT_EVERY_H * 3600.0,
                 lang: str = DEFAULT_LANG) -> None:
        self.lang = normalize(lang)  # what the page speaks (es or en); anything else is Spanish
        self.core: Any = None
        self.transport: Any = None
        self.night = night
        self.backlight = backlight
        self._notice = notice
        self._clock = clock
        self._system_time = system_time  # the Pi's own second of the day, for when the Mac has not told us
        self._passkey: Optional[Dict[str, Any]] = None
        self._listeners: List[Callable[[str], None]] = []
        self.boot = secrets.token_hex(6)  # a new daemon is a new boot: pages opened before it must reload (new token)
        self._last = ""
        self._scheduled = False
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._unsubscribe_notices: Optional[Callable[[], None]] = None
        # per slot ("pack", "costume"): (pack name, manifest (mtime_ns, size), what the manifest says: see _read_manifest)
        self._packs: Dict[str, Tuple[str, Tuple[int, int], Optional[Dict[str, Any]]]] = {}
        self._awake_until = float("-inf")  # on self._clock
        self._wake_reason = "toque"  # why the screen last woke, for the log
        self._dim = False  # the last dim state we acted on (backlight, log)
        self._timer: Optional[asyncio.TimerHandle] = None  # at most one: the next moment dim may change
        self._timer_at = 0.0  # when it fires, on self._clock
        self._closet = closet
        self._closet_every = float(closet_every)  # seconds a look lasts; 0 or less: it is picked once and kept
        self._pick: Optional[Costume] = None  # what the dice chose (worn only over its base pack); None is "sin disfraz"
        self._closet_at = float("inf")  # when the next pick is due, on self._clock
        self._closet_shown = ""  # id of the costume we last said was on, "" for none
        self._closet_skips = ""  # the last "descarté ..." line, so a closet that stays as it is says it once
        self._closet_timer: Optional[asyncio.TimerHandle] = None  # at most one: the next pick
        self._closet_timer_at = 0.0  # when it fires, on self._clock
        self._closed = False

    def bind(self, core: Any, transport: Any = None) -> None:
        self.core, self.transport = core, transport
        self._unsubscribe = core.broker.subscribe(self._on_approval)
        board = getattr(core, "notices", None)
        if board is not None:  # a notice that arrives, is dismissed or is withdrawn reaches the screen at once
            self._unsubscribe_notices = board.subscribe(self.touch)
        self._start_closet()

    def unbind(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_notices is not None:
            self._unsubscribe_notices()
            self._unsubscribe_notices = None

    def close(self) -> None:
        """The daemon is stopping: no more timers, and the panel goes back to its day brightness for good (nothing
        dims it afterwards, even if a page asks for the state again). Never raises."""
        self._closed = True
        self._cancel_timer()
        self._cancel_closet_timer()
        if self.backlight is not None:
            try:
                self.backlight.restore()
            except Exception:  # the backlight does not raise by design; a stopping daemon must not trip on it either
                log.exception("could not restore the backlight")

    # -- inputs ---------------------------------------------------------------------

    def set_passkey(self, code: Any, entered: int = 0) -> None:
        """The pairing code to show (an int or digits), or None once pairing is over. At night a code wakes the screen
        and keeps it awake while it is shown: it is only on screen while the Mac waits for it to be typed. Its end
        (only if one was shown: the radio also says "no code" whenever a link drops) starts the usual countdown."""
        shown = self._passkey is not None
        if code is None:
            self._passkey = None
        else:
            text = str(code).strip()
            if text.isdigit():
                text = text.zfill(6)
            self._passkey = {"code": text[:12], "entered": max(0, min(int(entered or 0), len(text)))}
        if code is not None or shown:
            self.wake("emparejar")  # publishes too
        else:
            self.touch()

    def decide(self, key: str, decision: str) -> bool:
        """The person tapped a button. False when that request is no longer pending; ValueError for a decision
        the request does not take ("always" where it is not offered)."""
        done = self.core.broker.decide(key, decision)
        log.info("screen: %s %s -> %s", key, decision, "sent" if done else "no longer pending")
        return done

    def dismiss(self, notice_id: str) -> str:
        """The person tapped "Listo" on a notice. Returns how the notice stands now (NOTICE_STATES): "dismissed", or what
        it already was ("expired"...) or "unknown" when the board never heard of it. The screen hears about it through
        the board, like every other change. The log gets the state and a count, never the text."""
        board = getattr(self.core, "notices", None)
        if board is None:
            return "unknown"
        state = board.dismiss(notice_id)
        log.info("screen: aviso %s, %d activos", state, len(board.active()))
        return state

    # -- night mode -------------------------------------------------------------------

    def wake(self, reason: str = "toque") -> bool:
        """Wake the screen for AWAKE_S. Everything that wakes it comes through here (a tap on the dark screen, a
        permission request, a pairing code, a notice from Hermes that asks for it). Returns whether the screen is still dim:
        False, unless night mode is off or has not started, in which case it never was."""
        self._awake_until = max(self._awake_until, self._clock() + AWAKE_S)
        self._wake_reason = reason
        self.touch()
        return self._dim_now()

    def _on_approval(self, event: str, approval: Any) -> None:
        # A new request wakes the screen; an answer or an expiry keeps it awake AWAKE_S more, so the last one to go
        # starts the countdown (while any is pending the screen is awake whatever the countdown says).
        self.wake("permiso")

    def _second_of_day(self) -> float:
        local = self.core.local_time() if self.core is not None else None  # the same clock as the one on the screen
        return local % DAY_S if local is not None else self._system_time()

    def _is_night(self) -> bool:
        return self.night is not None and in_night(self.night, self._second_of_day())

    def _is_awake(self, now: float) -> bool:
        pending = self.core.broker.pending() if self.core is not None else []
        return bool(pending) or self._passkey is not None or self._awake_until > now

    def _dim_now(self) -> bool:
        return self._is_night() and not self._is_awake(self._clock())

    def _controls_backlight(self) -> bool:
        return self.night is not None and self.backlight is not None and self.backlight.active

    def _sync_night(self) -> None:
        """Act on the dim state: log a change, move the backlight (it writes only when the level changes) and set the
        timer for the next change. Called by refresh(), so it rides the same coalescing as everything else."""
        if self.night is None or self._closed:
            return
        dim = self._dim_now()
        if dim != self._dim:
            self._dim = dim
            if dim:
                self._say("noche: pantalla tenue")
            elif self._is_night():
                self._say("noche: despierta (%s)" % self._wake_reason)
            else:
                self._say("noche: terminó, pantalla normal")
        if self.backlight is not None:
            try:
                self.backlight.set_dim(dim)
            except Exception:  # it does not raise by design; if it ever does, the page darkens itself instead
                log.exception("backlight failed; leaving it alone")
                self.backlight = None
        self._arm_timer()

    def _say(self, text: str) -> None:
        """One line about a dim/wake transition: to the notice if there is one, else (or if that fails) to the log."""
        if self._notice is not None:
            try:
                self._notice(text)
                return
            except Exception:  # a closed stdout must not stop the screen from updating
                pass
        log.info("%s", text)

    def _next_change(self, now: float) -> Optional[float]:
        """Seconds until dim may change by itself: the awake time running out, or the night window starting or ending."""
        delays: List[float] = []
        if self.core is not None and not self.core.broker.pending() and self._passkey is None and self._awake_until > now:
            delays.append(self._awake_until - now)
        if self.night is not None and self.night[0] != self.night[1]:
            second = self._second_of_day()
            delays.extend((edge - second) % DAY_S or DAY_S for edge in self.night)
        return min(delays) if delays else None

    def _arm_timer(self) -> None:
        """One timer, for the next change. Kept when it is already right (this runs on every refresh), replaced
        when the moment moved (a wake, an answered request, the Mac's clock arriving)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop, no timer: whoever refreshes by hand sees the state anyway
        now = self._clock()
        delay = self._next_change(now)
        if delay is None:
            self._cancel_timer()
            return
        delay += 0.05  # a hair late, never early: the callback must find the state already changed
        if self._timer is not None and abs(self._timer_at - (now + delay)) < 0.02:
            return
        self._cancel_timer()
        self._timer_at = now + delay
        self._timer = loop.call_later(delay, self._on_timer)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timer(self) -> None:
        self._timer = None
        self.touch()  # publishes the new state (and re-arms) through the usual path

    # -- the closet ---------------------------------------------------------------------

    def _start_closet(self) -> None:
        """Called by bind(): read the closet, say what is in it and pick the first look."""
        if self._closet is None or self._closed:
            return
        self._closet.scan()
        count = len(self._closet.costumes)
        if not count:
            self._say("Closet: sin disfraces todavía")
        elif self._closet_every > 0:
            self._say("Closet: %d %s, cambia cada %g h" % (count, "disfraz" if count == 1 else "disfraces",
                                                            self._closet_every / 3600))
        else:
            self._say("Closet: %d %s, elige uno solo al arrancar" % (count, "disfraz" if count == 1 else "disfraces"))
        self._say_skips()
        self._take_pick(self._clock())
        self._report_costume()
        self._arm_closet_timer()

    def drop_closet(self) -> bool:
        """No screen to dress Buddy for (the page never opened): forget the closet and its timer. Whether there was one."""
        had = self._closet is not None
        self._cancel_closet_timer()
        self._closet, self._pick, self._closet_shown = None, None, ""
        return had

    def _take_pick(self, now: float) -> None:
        assert self._closet is not None
        self._pick = self._closet.pick()
        self._closet_at = now + self._closet_every if self._closet_every > 0 else float("inf")

    def _say_skips(self) -> None:
        """What the last scan left out, once: the same leftovers on the next scan are not news."""
        text = self._closet.skipped_text() if self._closet is not None else ""
        if text != self._closet_skips:
            self._closet_skips = text
            if text:
                self._say("Closet: " + text)

    def _wearing(self) -> Optional[Costume]:
        """The costume Buddy has on right now: the one picked, if the active pack is the one it was drawn over and it
        is still sound on disk. None otherwise (no closet, "sin disfraz", another pack or none at all, or a closet
        emptied or corrupted since: the pack underneath shows then, and a costume that went bad is dropped)."""
        pick, closet = self._pick, self._closet
        if closet is None or pick is None:
            return None
        active = str(getattr(getattr(self.core, "state", None), "character", "") or "")
        if not active or active != pick.base:
            return None
        try:
            sound = closet.recheck(pick)
        except Exception:  # recheck does not raise by design; a closet we cannot look at is a closet without costumes
            log.exception("could not look at the closet")
            sound = None
        self._pick = sound
        return sound

    def _report_costume(self) -> None:
        """One line each time the costume Buddy actually wears changes (on, off or swapped for another)."""
        worn = self._wearing()
        shown = worn.id if worn is not None else ""
        if shown == self._closet_shown:
            return
        self._closet_shown = shown
        self._say(("Buddy se puso " + worn.name) if worn is not None else "Buddy se sacó el disfraz")

    def _sync_closet(self) -> None:
        """Pick again when the time is up, say what changed and set the timer for the next pick. Called by refresh()."""
        if self._closet is None or self._closed:
            return
        now = self._clock()
        if now >= self._closet_at:  # inf when picked once and kept
            self._closet.scan()  # the closet grows: a costume copied in since is in the dice now
            self._say_skips()
            self._take_pick(now)
        self._report_costume()
        self._arm_closet_timer()

    def _arm_closet_timer(self) -> None:
        """One timer, for the next pick: kept while it is right (this runs on every refresh), replaced when the moment moved."""
        if self._closet is None or self._closed or self._closet_every <= 0:
            self._cancel_closet_timer()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop, no timer: whoever refreshes by hand gets the pick when it is due
        now = self._clock()
        delay = max(0.0, self._closet_at - now) + 0.05  # a hair late, never early: the callback must find it due
        if self._closet_timer is not None and abs(self._closet_timer_at - (now + delay)) < 0.02:
            return
        self._cancel_closet_timer()
        self._closet_timer_at = now + delay
        self._closet_timer = loop.call_later(delay, self._on_closet_timer)

    def _cancel_closet_timer(self) -> None:
        if self._closet_timer is not None:
            self._closet_timer.cancel()
            self._closet_timer = None

    def _on_closet_timer(self) -> None:
        self._closet_timer = None
        self.touch()  # publishes the new look (and re-arms) through the usual path

    # -- output ---------------------------------------------------------------------

    def character(self) -> Optional[Dict[str, Any]]:
        """The character the page draws: its name, its colors (only #rrggbb), the image files per state and `rev`,
        12 hex digits that stand for the manifest and for every file it lists (name, size, modification time). None
        without a pack: the page then draws its own buddy. The manifest is parsed again only when it changes on disk,
        but the files are looked at on every call (one stat each, so it stays cheap once a second): a GIF replaced under
        the same name changes `rev`, and the page loads its images again.

        That is the active pack, unless Buddy is wearing a costume (see the closet in UIHub): then it is the costume's
        pack, computed the same way, named by the costume's id and with `costume`, its Spanish name, added. Without a
        closet, or while nothing is worn, the result is exactly what it always was."""
        worn = self._wearing()
        if worn is not None:
            found = self._describe(worn.id, worn.folder, "costume", closet_stamp)
            if found is not None:
                found["costume"] = worn.name
                return found
        core = self.core
        name = str(getattr(getattr(core, "state", None), "character", "") or "")
        packs = getattr(core, "packs", None)
        if not name or packs is None:
            return None
        return self._describe(name, Path(packs.root) / name, "pack", self._stamp)

    def character_bytes(self, pack: Dict[str, Any], fname: str) -> Optional[bytes]:
        """The bytes of an image of `pack`, as character() just described it; None when it cannot be read. A costume's
        files come through the closet (plain files only, never through a link); the active pack's from its folder."""
        if "costume" in pack:
            worn = self._wearing()
            if worn is None or worn.id != pack["name"] or self._closet is None:
                return None
            return self._closet.read(worn, fname)
        try:
            return (Path(self.core.packs.root) / pack["name"] / fname).read_bytes()
        except OSError:
            return None

    def _describe(self, name: str, folder: Path, slot: str,
                  stamp: Callable[[Path], Optional[Tuple[int, int]]]) -> Optional[Dict[str, Any]]:
        """The pack in `folder` as the page needs it (see character()). `slot` is where its parsed manifest is kept."""
        manifest = folder / "manifest.json"
        try:
            info = manifest.stat()
        except OSError:
            return None
        key = (info.st_mtime_ns, info.st_size)
        cached_name, cached_key, parsed = self._packs.get(slot, ("", (0, 0), None))
        if cached_name != name or cached_key != key:
            parsed = self._read_manifest(manifest)
            self._packs[slot] = (name, key, parsed)
        if parsed is None:
            return None
        stamps: Dict[str, Optional[Tuple[int, int]]] = {}  # file name -> (size, mtime_ns); None when it is not there
        files: Dict[str, List[str]] = {}
        for state, names in parsed["states"].items():
            for n in names:
                if n not in stamps:
                    stamps[n] = stamp(folder / n)
            good = [n for n in names if stamps[n] is not None]
            if good:
                files[state] = good
        if not files:
            return None
        digest = hashlib.sha1(parsed["digest"], usedforsecurity=False)
        for n, got in sorted(stamps.items()):
            if got is not None:
                digest.update(json.dumps([n, *got]).encode("ascii") + b"\n")  # json: no encoding surprises in a name
        return {
            "name": name,
            "colors": parsed["colors"],
            "accents": parsed["accents"],
            "render": parsed["render"],
            "states": files,
            "rev": digest.hexdigest()[:12],
        }

    @staticmethod
    def _read_manifest(manifest: Path) -> Optional[Dict[str, Any]]:
        """What a pack's manifest says, ready for `character`: the hash of its bytes, the colors, the accents, how to
        draw it and the file names each state points at (names that could be a file here, not yet checked on disk).
        None when it cannot be read or is not an object."""
        try:
            blob = manifest.read_bytes()
            raw = json.loads(blob.decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        states = raw.get("states")
        colors = raw.get("colors")
        accents = raw.get("accents")  # our own extension: one accent per state, so pills follow the mood
        listed: Dict[str, List[str]] = {}
        for state, value in (states.items() if isinstance(states, dict) else []):
            names = value if isinstance(value, list) else [value]
            good = [n for n in names if isinstance(n, str) and "/" not in n and "\\" not in n and not n.startswith(".")]
            if good and isinstance(state, str):
                listed[state] = good
        return {
            "digest": hashlib.sha1(blob, usedforsecurity=False).digest(),
            "colors": {k: v for k, v in colors.items() if isinstance(k, str) and isinstance(v, str)
                       and _HEX_COLOR.fullmatch(v)} if isinstance(colors, dict) else {},
            "accents": {k: v for k, v in accents.items() if isinstance(k, str) and isinstance(v, str)
                        and _HEX_COLOR.fullmatch(v)} if isinstance(accents, dict) else {},
            "render": "pixel" if raw.get("render") == "pixel" else "smooth",  # pixel art wants crisp upscaling
            "states": listed,
        }

    @staticmethod
    def _stamp(path: Path) -> Optional[Tuple[int, int]]:
        """(size, mtime_ns) of a regular file, None when it is not one or cannot be looked at."""
        try:
            info = path.stat()
        except (OSError, ValueError):  # ValueError: a name the OS cannot even be asked about
            return None
        return (info.st_size, info.st_mtime_ns) if S_ISREG(info.st_mode) else None

    def view(self) -> Dict[str, Any]:
        core, radio = self.core, self.transport
        ready = bool(getattr(radio, "ready", False))
        snap, st = core.snapshot, core.state
        tasks, totals = self._tasks_view()
        return {
            "v": 1,
            "boot": self.boot,
            "lang": self.lang,
            "ble": {
                "ready": ready,
                "name": str(getattr(radio, "device_name", "") or ""),
                "reason": "" if ready else ble_reason(str(getattr(radio, "not_ready_reason", "") or ""), self.lang),
                "secure": bool(getattr(radio, "secure", True)),
            },
            "linked": bool(core.connected),
            "pairing": dict(self._passkey) if self._passkey else None,
            "mood": core.persona(),
            "pet": {
                "name": st.name,
                "owner": st.owner,
                "level": st.level,
                "xp": round((st.tokens % TOKENS_PER_LEVEL) / TOKENS_PER_LEVEL, 3),
                "approvals": st.approvals,
                "denials": st.denials,
            },
            "sessions": {
                "total": snap.total,
                "running": snap.running,
                "waiting": snap.waiting,
                "msg": snap.msg,
                "entries": list(snap.entries[:4]),
                "tokens_today": snap.tokens_today,
            },
            "approvals": [
                {"key": a.key, "tool": a.tool, "hint": a.hint, "source": a.source, "always": a.always}
                for a in core.broker.pending()
            ],
            "tasks": tasks,
            **totals,
            "notices": self._notices_view(),
            "clock": self._clock_text(),
            "character": self.character(),
            "night": self._is_night(),  # inside the night window, awake or not: the page keeps it awake while it is touched
            "dim": self._dim_now(),  # night and nobody has woken it: the page covers the stage (see UIHub)
            "backlight": self._controls_backlight(),  # True: the panel itself dims, the page must not darken twice
        }

    MAX_TASKS_SHOWN = 8  # the board holds 12 sessions and up to 8 census rows per source; the screen draws 8
    # Of those 8, at most 5 are census rows (runs and jobs) and, when there are that many sessions, at least 3 are sessions.
    # The 5 is a hard cap even when the sessions leave slots free (spare slots go from the runs to the sessions, not the other
    # way): the page draws at most 5 runs too, so a sixth would only hide behind the "+K" line.
    MAX_CENSUS_SHOWN = 5
    MIN_HOOK_SHOWN = 3

    def _tasks_view(self) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """The live threads and runs on the Mac and what each says it is doing (see buddy_core.TaskBoard), and the
        totals of the whole board: ([], zeros) without the hook.

        What is drawn is cut to MAX_TASKS_SHOWN, but the totals are taken before the cut, so the screen can say how many
        work and how many are left out: working_total (rows that work), tasks_total (all rows), census_total (runs and
        jobs alive, plus the ones their sender could not list) and tasks_more (tasks_total minus the rows drawn). The runs a
        sender could not list (its "more") are in all four, so the header, the group count and the "+K" line agree.

        title, group, parent and said are in a thread's entry only when it has them; source says where the row comes
        from ("hook" for a session, "census" for a run or job), and a census row also carries elapsed (seconds it has
        been running) and, when known, idle (seconds since it last wrote anything; its age is that, or 0)."""
        board = getattr(self.core, "tasks", None)
        if board is None:
            return [], {"working_total": 0, "tasks_total": 0, "census_total": 0, "tasks_more": 0}
        merged = board.tasks()  # census rows first (all of them work), then the sessions in the board's order
        census = [t for t in merged if t.source == "census"]
        hooks = [t for t in merged if t.source != "census"]
        room = self.MAX_TASKS_SHOWN
        take = min(len(census), self.MAX_CENSUS_SHOWN, max(0, room - min(len(hooks), self.MIN_HOOK_SHOWN)))
        shown = census[:take] + hooks[: max(0, room - take)]
        view = []
        for t in shown:
            entry: Dict[str, Any] = {"id": t.id, "project": t.project, "tool": t.tool, "what": t.what,
                                     "state": t.state, "age": board.age(t), "source": t.source}
            for key in ("title", "group", "parent", "said"):
                if getattr(t, key):
                    entry[key] = getattr(t, key)
            if t.source == "census":
                entry["elapsed"] = t.elapsed
                if t.idle is not None:
                    entry["idle"] = t.idle
            view.append(entry)
        unlisted = board.census_more()  # runs their sender could not list: they work, so they count in every total
        total = len(merged) + unlisted
        totals = {"working_total": sum(1 for t in merged if t.state == "working") + unlisted, "tasks_total": total,
                  "census_total": len(census) + unlisted, "tasks_more": total - len(shown)}
        return view, totals

    def _notices_view(self) -> List[Dict[str, Any]]:
        """The notices Hermes left (see buddy_core.NoticeBoard), newest first, at most MAX_NOTICES: each is
        {id, title, kind, source, age, rev} plus `body` only when it has one. `age` is whole seconds since it was last
        sent; `rev` goes up when a re-send changed it. [] without a board."""
        board = getattr(self.core, "notices", None)
        if board is None:
            return []
        view = []
        for n in board.active():
            entry: Dict[str, Any] = {"id": n.id, "title": n.title, "kind": n.kind, "source": n.source,
                                     "age": board.age(n), "rev": n.rev}
            if n.body:
                entry["body"] = n.body
            view.append(entry)
        return view

    def _clock_text(self) -> Optional[str]:
        local = self.core.local_time()
        if local is None:
            return None
        stamp = time.gmtime(local)  # the desktop already added its own UTC offset
        return "%02d:%02d" % (stamp.tm_hour, stamp.tm_min)

    def current(self) -> str:
        self.refresh()
        return self._last

    def subscribe(self, fn: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def refresh(self) -> bool:
        """Recompute; tell the listeners if the screen would look different."""
        if self.core is None:
            return False
        self._sync_night()  # first: the view below must say whether the backlight is still ours
        self._sync_closet()  # and who is wearing what
        text = json.dumps(self.view(), separators=(",", ":"), sort_keys=True)  # ASCII only
        if text == self._last:
            return False
        self._last = text
        for fn in list(self._listeners):
            try:
                fn(text)
            except Exception:
                log.exception("screen listener failed")
        return True

    def touch(self) -> None:
        """Something may have changed. Coalesces bursts into one refresh."""
        if self._scheduled or self.core is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.refresh()
            return
        self._scheduled = True
        loop.call_soon(self._flush)

    def _flush(self) -> None:
        self._scheduled = False
        self.refresh()


# --- HTTP ------------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: int, text: str = "") -> None:
        super().__init__(text or str(status))
        self.status = status
        self.text = text


REASONS = {
    200: "OK", 202: "Accepted", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large", 500: "Internal Server Error", 503: "Service Unavailable",
}


class Request:
    def __init__(self, method: str, path: str, headers: Dict[str, str], body: bytes, query: str = "") -> None:
        self.method, self.path, self.headers, self.body, self.query = method, path, headers, body, query


class HttpServer:
    """A small HTTP/1.1 server on asyncio streams: one request per connection, strict parsing, bounded headers
    and bodies, JSON errors. Subclasses route (`_route`) and name themselves (`name`, `log`). The screen
    (`UIServer`, loopback only) and the Hermes listener (`buddy_hermes.HermesServer`, LAN or Tailscale) are both
    built on it, so they read requests the same way; what each one accepts is its own business."""

    name = "http"
    log = log
    max_body = MAX_BODY_BYTES

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: Set["asyncio.Future[Any]"] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port, limit=MAX_HEADER_BYTES)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    # -- connection ------------------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            try:
                try:
                    request = await asyncio.wait_for(self._read_request(reader), HEADER_TIMEOUT_S)
                    await self._route(request, reader, writer)
                except HttpError as exc:
                    await self._send(writer, exc.status,
                                     {"ok": False, "error": exc.text or REASONS.get(exc.status, "")})
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                pass  # a client that never finished, or left
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("%s request failed", self.name)
                try:
                    await self._send(writer, 500, {"ok": False, "error": "internal error"})
                except Exception:
                    pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> Request:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            raise HttpError(431) from None
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise HttpError(400, "bad request line")
        method, target = parts[0], parts[1]
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep or not name.strip():
                raise HttpError(400, "bad header")
            headers[name.strip().lower()] = value.strip()
        if "transfer-encoding" in headers:
            raise HttpError(400, "chunked bodies are not supported")
        body = b""
        if "content-length" in headers:
            try:
                length = int(headers["content-length"])
            except ValueError:
                raise HttpError(400, "bad content-length") from None
            if length < 0:
                raise HttpError(400, "bad content-length")
            if length > self.max_body:
                raise HttpError(413)
            body = await reader.readexactly(length)
        path, _, query = target.partition("?")
        return Request(method, path, headers, body, query)

    async def _send(self, writer: asyncio.StreamWriter, status: int, body: Any = b"",
                    content_type: str = "application/json", extra: Optional[List[Tuple[str, str]]] = None) -> None:
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, separators=(",", ":")).encode("ascii")
        lines = [
            f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
            "Referrer-Policy: no-referrer",
            "Connection: close",
        ]
        for name, value in extra or []:
            lines.append(f"{name}: {value}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + bytes(body))
        await writer.drain()

    async def _route(self, request: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raise NotImplementedError

    @staticmethod
    def _need(method: str, allowed: str) -> None:
        if method != allowed:
            raise HttpError(405)


class UIServer(HttpServer):
    name = "screen"

    def __init__(self, hub: UIHub, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 page: Optional[Path] = None, clock: Callable[[], float] = time.monotonic) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the screen only listens on loopback")
        super().__init__(host, port)
        self.hub = hub
        self.page = Path(page) if page is not None else PAGE_DIR / "index.html"
        self.token = secrets.token_urlsafe(24)
        self._clock = clock
        self._streams = 0
        self._hosts: Set[str] = set()
        self._wake_hits: Deque[float] = deque()  # when the last POST /wake calls were accepted
        self._dismiss_hits: Deque[float] = deque()  # and the last POST /dismiss calls

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        await super().start()
        self._hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        log.info("screen server on %s", self.url)

    # -- routes ----------------------------------------------------------------------

    def _check_origin(self, request: Request) -> None:
        if request.headers.get("host", "").lower() not in self._hosts:
            raise HttpError(403, "unexpected host")
        origin = request.headers.get("origin")
        if origin is not None and origin.lower() not in {"http://" + h for h in self._hosts}:
            raise HttpError(403, "unexpected origin")

    async def _route(self, request: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._check_origin(request)
        method, path = request.method, request.path
        if path in ("/", "/index.html"):
            self._need(method, "GET")
            await self._page(writer)
        elif path == "/events":
            self._need(method, "GET")
            await self._events(reader, writer)
        elif path == "/state":
            self._need(method, "GET")
            await self._send(writer, 200, self.hub.current().encode("ascii"))
        elif path == "/decide":
            self._need(method, "POST")
            await self._decide(request, writer)
        elif path == "/wake":
            self._need(method, "POST")
            await self._wake(request, writer)
        elif path == "/dismiss":
            self._need(method, "POST")
            await self._dismiss(request, writer)
        elif path == "/healthz":
            self._need(method, "GET")
            await self._send(writer, 200, b"ok", "text/plain")
        elif path.startswith("/character/"):
            self._need(method, "GET")
            await self._character_file(path, writer)
        elif path == "/favicon.ico":
            await self._send(writer, 204)
        else:
            raise HttpError(404)

    async def _page(self, writer: asyncio.StreamWriter) -> None:
        try:
            html = self.page.read_text(encoding="utf-8")
        except OSError as exc:
            log.error("cannot read the page %s: %s", self.page, exc)
            raise HttpError(500, "the page file is missing") from exc
        html = html.replace(TOKEN_PLACEHOLDER, self.token).replace(LANG_PLACEHOLDER, normalize(getattr(self.hub, "lang", DEFAULT_LANG)))
        await self._send(writer, 200, html.encode("utf-8"), "text/html; charset=utf-8",
                         [("Content-Security-Policy", CSP)])

    async def _character_file(self, path: str, writer: asyncio.StreamWriter) -> None:
        """An image of the active pack, or of the costume Buddy wears: GET /character/<pack>/<file>. Only files the
        manifest points at are served, so the page cannot ask for anything else on the disk (no manifest, no other pack
        or costume, no `..`)."""
        parts = path.split("/")
        if len(parts) != 4 or not parts[2] or not parts[3]:
            raise HttpError(404)
        name, fname = unquote(parts[2]), unquote(parts[3])
        pack = self.hub.character()
        if pack is None or name != pack["name"] or not any(fname in files for files in pack["states"].values()):
            raise HttpError(404)
        content_type = IMAGE_TYPES.get(Path(fname).suffix.lower())
        if content_type is None:
            raise HttpError(404)
        data = self.hub.character_bytes(pack, fname)
        if data is None:
            raise HttpError(404)
        await self._send(writer, 200, data, content_type)

    def _check_token(self, request: Request) -> None:
        sent = request.headers.get("x-buddy-token", "").encode("latin-1")
        if not hmac.compare_digest(sent, self.token.encode("ascii")):
            raise HttpError(403, "bad token")

    async def _decide(self, request: Request, writer: asyncio.StreamWriter) -> None:
        self._check_token(request)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HttpError(415)
        try:
            data = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "not json") from None
        key = data.get("key") if isinstance(data, dict) else None
        decision = data.get("decision") if isinstance(data, dict) else None
        if not isinstance(key, str) or decision not in DECISIONS:
            raise HttpError(400, "need key and decision (once, deny or always)")
        try:
            done = self.hub.decide(key, decision)
        except ValueError:
            raise HttpError(400, "that request does not offer always") from None
        if done:
            await self._send(writer, 200, {"ok": True})
        else:
            await self._send(writer, 409, {"ok": False, "error": "no longer pending"})

    async def _wake(self, request: Request, writer: asyncio.StreamWriter) -> None:
        """A tap on the dark screen: POST /wake wakes it for AWAKE_S. Same token, Host and Origin rules as /decide, and
        it can do nothing else: it never answers a request or changes any state but the screen's brightness. The body is
        optional ({} from the page) and ignored; if there is one, or a content type, it has to be JSON, so a plain
        HTML form cannot send it either."""
        self._check_token(request)
        ctype = request.headers.get("content-type")
        if (ctype is not None or request.body) and (ctype or "").split(";")[0].strip().lower() != "application/json":
            raise HttpError(415)
        # its own budget, taken after the token check: a flood of bad requests spends none of it
        self._spend(self._wake_hits, WAKE_RATE_WINDOW_S, WAKE_RATE_MAX)
        await self._send(writer, 200, {"ok": True, "dim": self.hub.wake("toque")})

    async def _dismiss(self, request: Request, writer: asyncio.StreamWriter) -> None:
        """A tap on "Listo" of a notice: POST /dismiss {"id": "<notice id>"} takes it off the screen and answers
        {"ok": true, "state": ...} (the board's word for how it stands: "dismissed", or "unknown" for an id it does not
        have). Same token, Host and Origin rules as /decide, a JSON body, and its own budget like /wake's, taken after
        the token check. It can do nothing else: it never answers a request, wakes the screen or changes anything but
        the one notice."""
        self._check_token(request)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HttpError(415)
        self._spend(self._dismiss_hits, DISMISS_RATE_WINDOW_S, DISMISS_RATE_MAX)
        try:
            data = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "not json") from None
        notice_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(notice_id, str):
            raise HttpError(400, "need the id of a notice")
        await self._send(writer, 200, {"ok": True, "state": self.hub.dismiss(notice_id)})

    def _spend(self, hits: Deque[float], window: float, limit: int) -> None:
        """Take one of the `limit` calls a route may have per `window` seconds; 429 when they are gone."""
        now = self._clock()
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            raise HttpError(429, "too many requests")
        hits.append(now)

    # -- server-sent events ------------------------------------------------------------

    async def _events(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._streams >= MAX_CLIENTS:
            raise HttpError(503, "too many screens")
        self._streams += 1
        queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=1)

        def push(text: str) -> None:
            if queue.full():
                queue.get_nowait()  # a slow screen only ever needs the latest state
            queue.put_nowait(text)

        initial = self.hub.current()  # no await until subscribed, so no change can slip in between
        unsubscribe = self.hub.subscribe(push)
        closed = asyncio.ensure_future(reader.read(1))  # returns when the page goes away
        getter: Optional["asyncio.Future[str]"] = None
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                b"X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n"
                + f"retry: {RETRY_MS}\n\n".encode("ascii") + self._frame(initial)
            )
            await asyncio.wait_for(writer.drain(), SLOW_CLIENT_S)
            while True:
                if getter is None:
                    getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, closed}, timeout=PING_S, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    return
                if getter in done:
                    writer.write(self._frame(getter.result()))
                    getter = None
                else:
                    writer.write(b"event: ping\ndata: 1\n\n")
                await asyncio.wait_for(writer.drain(), SLOW_CLIENT_S)
        except (asyncio.TimeoutError, ConnectionError):
            return
        finally:
            unsubscribe()
            self._streams -= 1
            if closed.done() and not closed.cancelled():
                closed.exception()  # a reset connection is not worth a warning
            closed.cancel()
            if getter is not None:
                getter.cancel()

    @staticmethod
    def _frame(text: str) -> bytes:
        return f"event: state\ndata: {text}\n\n".encode("ascii")
