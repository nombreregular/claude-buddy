"""Protocol core for a Claude Desktop Buddy (the "Hardware Buddy" BLE API).

Pure standard library: no Bluetooth, no D-Bus, no UI. A transport feeds it the raw
bytes the desktop writes to the RX characteristic (`BuddyCore.feed`) and ships
whatever the core hands to `send` out through the TX characteristic.

Behaviour follows REFERENCE.md and the official ESP32 firmware
(anthropics/claude-desktop-buddy), with a few deliberate differences:

* A `turn` event is not a heartbeat. The reference firmware treats every JSON line
  without `cmd`/`time` as a snapshot, so a turn event wipes a pending prompt.
* Every command gets an ack, unknown ones included (`ok:false`).
* Folder push validates paths and sizes, writes into a temp dir and installs the
  pack atomically on `char_end`.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("buddy.core")

# --- wire constants ---------------------------------------------------------------

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # desktop -> device (write)
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> desktop (notify)

LINK_TIMEOUT_S = 30.0  # no traffic for this long = connection dead
TOKENS_PER_LEVEL = 50_000  # the pet levels up every 50K output tokens
FAST_APPROVAL_S = 5.0  # approve faster than this and the pet throws hearts
MAX_LINE_BYTES = 16 * 1024
MAX_PACK_BYTES = int(1.8 * 1024 * 1024)  # "under 1.8MB total" (REFERENCE.md)
MAX_PACK_FILES = 64
PUSH_IDLE_TIMEOUT_S = 20.0  # the desktop gives up after a few seconds without an ack
BUSY_MIN_RUNNING = 1  # the ESP32 firmware uses 3; one running session is enough here

PERSONAS = ("sleep", "idle", "busy", "attention", "celebrate", "dizzy", "heart")
HEARTBEAT_KEYS = frozenset(
    {"total", "running", "waiting", "msg", "entries", "tokens", "tokens_today", "prompt", "completed"}
)


def encode_line(obj: Dict[str, Any]) -> bytes:
    """One JSON object per line, ASCII-only so a notification split can never cut a
    multi-byte character in half."""
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def clean_label(value: str, limit: int) -> str:
    return "".join(ch for ch in value if ch.isprintable()).strip()[:limit]


def system_uptime() -> int:
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return int(time.monotonic())


# --- line framing -----------------------------------------------------------------


class LineBuffer:
    """Reassembles lines from arbitrary byte slices (BLE writes fragment at the MTU).

    Splits on LF or CR like the reference firmware. Decoding happens per complete line,
    so a UTF-8 character split across two writes is fine. A line longer than
    `max_bytes` is dropped whole, including its tail.
    """

    def __init__(self, max_bytes: int = MAX_LINE_BYTES) -> None:
        self._buf = bytearray()
        self._max = max_bytes
        self._skipping = False
        self.dropped = 0

    def reset(self) -> None:
        self._buf.clear()
        self._skipping = False

    def feed(self, data: bytes) -> List[bytes]:
        out: List[bytes] = []
        parts = data.replace(b"\r", b"\n").split(b"\n")
        last_idx = len(parts) - 1
        for i, part in enumerate(parts):
            is_last = i == last_idx  # the last part has no terminator yet
            if self._skipping:
                if not is_last:
                    self._skipping = False
                continue
            self._buf += part
            if len(self._buf) > self._max:
                self.dropped += 1
                self._buf.clear()
                self._skipping = is_last
                continue
            if not is_last:
                if self._buf:
                    out.append(bytes(self._buf))
                self._buf.clear()
        return out


# --- approvals --------------------------------------------------------------------


DECISIONS = ("once", "deny", "always")


@dataclass
class Approval:
    id: str
    tool: str = ""
    hint: str = ""
    source: str = "claude"
    created: float = 0.0
    decision: Optional[str] = None  # "once" | "deny" | "always" once decided
    always: str = ""  # what "always" would allow (a permission rule); empty: the request does not offer it

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


class ApprovalBroker:
    """Holds pending approvals from any source and routes the decision back.

    Claude (over BLE) is one source. A Hermes plugin or an HTTP endpoint can submit
    their own later: whatever UI is attached (terminal now, touch screen next) sees
    one queue and calls `decide(key, "once" | "deny")`.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pending: Dict[str, Tuple[Approval, Callable[[str], None]]] = {}
        self._listeners: List[Callable[[str, Approval], None]] = []

    def subscribe(self, fn: Callable[[str, Approval], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _notify(self, event: str, approval: Approval) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, approval)
            except Exception:  # a broken UI must not break the protocol
                log.exception("approval listener failed")

    def submit(self, approval: Approval, responder: Callable[[str], None]) -> None:
        if not approval.created:
            approval.created = self._clock()
        if approval.key in self._pending:
            return
        self._pending[approval.key] = (approval, responder)
        self._notify("new", approval)

    def pending(self) -> List[Approval]:
        return [a for a, _ in self._pending.values()]

    def current(self) -> Optional[Approval]:
        for approval, _ in self._pending.values():
            return approval
        return None

    def decide(self, key: str, decision: str) -> bool:
        if decision not in DECISIONS:
            raise ValueError("decision must be 'once', 'deny' or 'always'")
        entry = self._pending.get(key)
        if entry is None:
            return False
        approval, responder = entry
        if decision == "always" and not approval.always:  # Claude's own prompts and Hermes never offer it
            raise ValueError("this request does not offer 'always'")
        try:
            responder(decision)
        except Exception:
            log.exception("could not deliver decision for %s", key)
            return False
        self._pending.pop(key, None)
        approval.decision = decision
        self._notify("decided", approval)
        return True

    def withdraw(self, source: str, approval_id: str) -> bool:
        entry = self._pending.pop(f"{source}:{approval_id}", None)
        if entry is None:
            return False
        self._notify("withdrawn", entry[0])
        return True


# --- persistent state -------------------------------------------------------------


@dataclass
class PersistentState:
    name: str = "Buddy"
    owner: str = ""
    species: int = 0xFF  # 0xFF = use the GIF character
    character: str = ""  # directory name of the active character pack
    approvals: int = 0
    denials: int = 0
    velocity: List[int] = field(default_factory=list)  # last 8 seconds-to-respond
    tokens: int = 0  # cumulative output tokens, drives the level
    nap_seconds: int = 0

    @property
    def level(self) -> int:
        return self.tokens // TOKENS_PER_LEVEL

    def median_velocity(self) -> int:
        if not self.velocity:
            return 0
        return sorted(self.velocity)[len(self.velocity) // 2]


class StateStore:
    """state.json with atomic writes. Corrupt files are kept as state.json.bad."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._last_write = float("-inf")
        self.dirty = False
        self.data = PersistentState()
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state is not an object")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("state file unreadable (%s); starting fresh", exc)
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
            return
        base = PersistentState()
        for key, default in asdict(base).items():
            value = raw.get(key, default)
            if not isinstance(value, type(default)):
                continue
            if key == "velocity":
                value = [int(v) for v in value if isinstance(v, int)][-8:]
            setattr(base, key, value)
        self.data = base

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self.data), indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False
        self._last_write = self._clock()

    def touch(self, now: bool = False) -> None:
        self.dirty = True
        if now:
            self.save()

    def flush_if_due(self, min_interval: float = 60.0) -> None:
        if self.dirty and self._clock() - self._last_write >= min_interval:
            self.save()


# --- folder push ------------------------------------------------------------------


class PackError(Exception):
    pass


_DIRNAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_dirname(name: str) -> str:
    return _DIRNAME_RE.sub("_", name).strip("._-")[:48] or "pet"


def validate_filename(path: Any) -> str:
    """The pack is a flat folder, so a file name is a plain name: no separators,
    no dotfiles, no `..`, nothing absolute."""
    if not isinstance(path, str) or not path:
        raise PackError("bad file path")
    if len(path) > 100:
        raise PackError("file name too long")
    if path.startswith("."):
        raise PackError("dotfiles and '..' are not allowed")
    if "/" in path or "\\" in path:
        raise PackError("subdirectories and absolute paths are not allowed")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise PackError("control characters in file name")
    return path


class PackReceiver:
    """Receives a character pack (flat folder) pushed from the desktop."""

    def __init__(
        self,
        root: Path,
        max_total: int = MAX_PACK_BYTES,
        max_files: int = MAX_PACK_FILES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.max_total = max_total
        self.max_files = max_files
        self._clock = clock
        self.active = False
        self.name = ""
        self.declared_total = 0
        self.written_total = 0
        self._tmp: Optional[Path] = None
        self._files: Dict[str, int] = {}
        self._fh: Optional[Any] = None
        self._fname = ""
        self._expected = 0
        self._written = 0
        self._last = 0.0

    def free_bytes(self) -> int:
        probe = self.root if self.root.exists() else self.root.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free

    def idle_for(self) -> float:
        return self._clock() - self._last if self.active else 0.0

    def cleanup_stale(self) -> None:
        if not self.root.is_dir():
            return
        for child in self.root.iterdir():
            if child.is_dir() and child.name.startswith((".incoming-", ".old-")) and child != self._tmp:
                shutil.rmtree(child, ignore_errors=True)

    def begin(self, name: str, total: Any) -> None:
        self.abort()
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise PackError("bad total")
        if total > self.max_total:
            raise PackError(f"pack too big: {total // 1024}K > {self.max_total // 1024}K")
        free = self.free_bytes()
        if total + 4096 > free:
            raise PackError(f"need {total // 1024}K, have {free // 1024}K")
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup_stale()
        self._tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=self.root))
        self.active = True
        self.name = name
        self.declared_total = total
        self.written_total = 0
        self._files = {}
        self._touch()

    def _touch(self) -> None:
        self._last = self._clock()

    def start_file(self, path: Any, size: Any) -> None:
        if not self.active or self._tmp is None:
            raise PackError("no transfer in progress")
        if self._fh is not None:
            raise PackError("previous file not finished")
        fname = validate_filename(path)
        if fname in self._files:
            raise PackError("duplicate file")
        if len(self._files) >= self.max_files:
            raise PackError("too many files")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PackError("bad size")
        if self.written_total + size > self.max_total:
            raise PackError("pack too big")
        target = self._tmp / fname
        if target.resolve().parent != self._tmp.resolve():  # belt and braces
            raise PackError("path escapes the pack folder")
        self._fh = open(target, "wb")
        self._fname = fname
        self._expected = size
        self._written = 0
        self._touch()

    def add_chunk(self, b64: Any) -> int:
        if not self.active or self._fh is None:
            raise PackError("no open file")
        if not isinstance(b64, str):
            raise PackError("missing chunk data")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise PackError("bad base64")
        if self._expected and self._written + len(raw) > self._expected:
            raise PackError("file larger than declared")
        if self.written_total + len(raw) > self.max_total:
            raise PackError("pack too big")
        self._fh.write(raw)
        self._written += len(raw)
        self.written_total += len(raw)
        self._touch()
        return self._written

    def end_file(self) -> int:
        if not self.active or self._fh is None:
            raise PackError("no open file")
        self._fh.close()
        self._fh = None
        self._files[self._fname] = self._written
        self._touch()
        if self._expected and self._written != self._expected:
            raise PackError(f"size mismatch: got {self._written}, expected {self._expected}")
        return self._written

    def finish(self) -> Tuple[Path, Optional[Dict[str, Any]]]:
        """Validate and install. Returns (installed dir, manifest or None)."""
        if not self.active or self._tmp is None:
            raise PackError("no transfer in progress")
        if self._fh is not None:
            raise PackError("file still open")
        manifest = self._read_manifest(self._tmp)
        dest = self.root / safe_dirname(self.name)
        os.chmod(self._tmp, 0o755)
        old: Optional[Path] = None
        if dest.exists():
            old = self.root / f".old-{os.urandom(4).hex()}"
            os.replace(dest, old)
        os.replace(self._tmp, dest)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        self._tmp = None
        self.active = False
        return dest, manifest

    @staticmethod
    def _read_manifest(folder: Path) -> Optional[Dict[str, Any]]:
        mf = folder / "manifest.json"
        if not mf.is_file():
            return None
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise PackError("manifest.json is not valid JSON")
        if not isinstance(data, dict):
            raise PackError("manifest.json must be an object")
        states = data.get("states", {})
        if not isinstance(states, dict):
            raise PackError("manifest 'states' must be an object")
        for value in states.values():
            for fname in value if isinstance(value, list) else [value]:
                if not isinstance(fname, str):
                    raise PackError("manifest has a non-string file name")
                try:
                    validate_filename(fname)
                except PackError:
                    raise PackError(f"manifest references a bad file name: {fname[:40]}")
                if not (folder / fname).is_file():
                    raise PackError(f"missing file: {fname}")
        return data

    def abort(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
        self.active = False


# --- the core ---------------------------------------------------------------------


@dataclass
class Snapshot:
    total: int = 0
    running: int = 0
    waiting: int = 0
    msg: str = ""
    entries: List[str] = field(default_factory=list)
    tokens: int = 0
    tokens_today: int = 0
    completed: bool = False


def _count(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(0, min(value, 2**31 - 1))


# --- what each session is doing ----------------------------------------------------------
#
# The Hardware Buddy protocol only carries totals (total/running/waiting), one message and the last lines
# of the newest session, so the screen cannot say which session does what. The sessions tell the buddy
# themselves: a Claude Code hook on the Mac (tools/context_hook.py) posts to the Hermes listener on every
# tool call and when a turn ends. This board keeps what they said and forgets them when they go quiet.
#
# A row is a thread: a Claude conversation that is alive. It is alive while it works (the hook speaks on
# every tool call) and while it waits for the owner after its turn (it said "idle"); once it stops speaking for
# long enough it is probably closed and leaves. Besides what it does, a row can carry what the Claude desktop
# sidebar calls it: the title of the conversation, the group it sits in and the thread that started it.

TASK_TTL_S = 600.0  # a working thread not heard from in 10 minutes leaves the board (it is probably closed)
TASK_IDLE_TTL_S = 1800.0  # one that finished its turn and waits for the owner stays 30 minutes
TASK_STATES = ("working", "idle", "gone")
MAX_TASKS = 12
MAX_TASK_PROJECT_CHARS, MAX_TASK_TOOL_CHARS, MAX_TASK_WHAT_CHARS = 40, 40, 120
MAX_TASK_TITLE_CHARS, MAX_TASK_GROUP_CHARS, MAX_TASK_PARENT_CHARS = 60, 40, 64  # parent: the length of an id
MAX_TASK_SAID_CHARS = 300  # what the session last said, plain text that may hold line breaks

# The census (tools/census.py) sees what no hook speaks for: headless `claude -p` runs and job scripts.
# See TaskBoard.replace_census.
CENSUS_GROUP = "Corridas"  # the sidebar group of a run that did not name one
CENSUS_TTL_MIN_S, CENSUS_TTL_MAX_S, CENSUS_TTL_DEFAULT_S = 5.0, 120.0, 60.0  # how long a snapshot is believed
MAX_CENSUS_ROWS = 8  # rows in one snapshot; the sender counts the rest in `more`
MAX_CENSUS_MORE = 999
MAX_CENSUS_SECONDS = 31 * 86400  # elapsed and idle are clamped to a month
MAX_CENSUS_SOURCES = 8  # machines that may post at the same time
MAX_CENSUS_NAME_CHARS, MAX_CENSUS_GROUP_CHARS, MAX_CENSUS_PROJECT_CHARS, MAX_CENSUS_WHAT_CHARS = 40, 24, 40, 40
CENSUS_HOOK_FRESH_S = 120.0  # a run keeps saying what its hook session says for this long after the session last spoke
CENSUS_ENDED_HIDE_S = 30.0  # a run whose session ended (SessionEnd) stays hidden this long if the census still lists it


def _switch_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("off", "0", "no")


# The daemon can refuse to keep what sessions said even if a client sends it (CLAUDE_BUDDY_SAID=off in its
# environment, read once at start), the same switch the hook honours on the Mac.
SAID_ENABLED = _switch_on("CLAUDE_BUDDY_SAID")


@dataclass
class Task:
    id: str
    project: str
    tool: str
    what: str
    state: str
    at: float  # clock when last heard from
    said: str = ""  # the last thing the session said, as plain text (may hold line breaks)
    title: str = ""  # the conversation's title in Claude desktop; "" for a terminal session
    group: str = ""  # the sidebar group it sits in; "" when the hook did not say
    parent: str = ""  # the id of the thread that started it, when the hook knows
    source: str = "hook"  # "hook" for a session that spoke; "census" for a run the census saw (tasks() builds those)
    elapsed: int = 0  # census rows: seconds it has been running
    idle: Optional[int] = None  # census rows: seconds since it last wrote anything, when the census knew


# A headless `claude -p` run or a job script has no one to speak for it (its hook may not load, and a killed run says
# nothing at all). tools/census.py, a LaunchAgent on the Mac, looks at the process table instead and posts a whole
# snapshot of what runs there to POST /census every few seconds. The board keeps the last snapshot of each source
# (each machine that posts) beside the sessions, and tasks() merges the two views of a run that is both.


@dataclass
class CensusRow:
    """One run or job the census saw, as its source last said it."""

    id: str  # "run-<name>" or "job-<name>", unique inside its source
    name: str
    group: str
    elapsed: int  # seconds it had been running when the snapshot arrived
    idle: Optional[int] = None  # seconds since it last wrote anything, when the census knew
    session: str = ""  # the hook session id of the run, when the census could tell
    project: str = ""  # its folder name, as the hook reports it
    what: str = ""


@dataclass
class CensusSnapshot:
    """Everything one source said in its last post."""

    rows: List[CensusRow]
    at: float  # clock when it arrived: the running clocks of its rows count from here
    until: float  # clock after which nobody believes it
    more: int  # runs the source saw but did not fit in the snapshot
    seq: Optional[int] = None  # the source's own ordering number, to tell a late post from a new one


class TaskBoard:
    """The live table of sessions, most recent first, and the runs the census saw beside them."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tasks: Dict[str, Task] = {}
        self._census: Dict[str, CensusSnapshot] = {}  # source -> its last snapshot
        # hook session id -> (source, census row id, exact) it was merged into; exact: the row names that very session
        # (the other kind is a guess by folder). Rebuilt on every merge, so it only holds pairs that hold now.
        self._links: Dict[str, Tuple[str, str, bool]] = {}
        self._hidden: Dict[Tuple[str, str], float] = {}  # (source, census row id) -> clock until which it stays hidden

    def update(self, task_id: str, *, project: str = "", tool: str = "", what: str = "", state: str = "working",
               said: Optional[str] = None, title: Optional[str] = None, group: Optional[str] = None,
               parent: Optional[str] = None) -> Optional[Task]:
        """What a session just said about itself. "gone" removes it; returns the row kept, or None.

        said, title, group and parent follow one rule: None (the report did not mention it) keeps what the
        board has, "" clears it, any other text replaces it."""
        if state not in TASK_STATES:
            raise ValueError("state must be working, idle or gone")
        self.prune()
        if state == "gone":
            self._hide_census(task_id)
            self._tasks.pop(task_id, None)
            return None
        now = self._clock()
        previous = self._tasks.get(task_id)
        task = Task(
            id=task_id,
            project=_clean(project, MAX_TASK_PROJECT_CHARS) or (previous.project if previous else ""),
            tool=_clean(tool, MAX_TASK_TOOL_CHARS),
            what=_clean(what, MAX_TASK_WHAT_CHARS),
            state=state,
            at=now,
        )
        if state == "idle" and previous is not None and not task.tool and not task.what:
            task.tool, task.what = previous.tool, previous.what  # "finished" keeps saying what it was doing
        task.said = previous.said if previous else ""
        if said is not None and SAID_ENABLED:
            task.said = _clean(said, MAX_TASK_SAID_CHARS, keep_lines=True)
        task.title = _keep(previous.title if previous else "", title, MAX_TASK_TITLE_CHARS)
        task.group = _keep(previous.group if previous else "", group, MAX_TASK_GROUP_CHARS)
        task.parent = _keep(previous.parent if previous else "", parent, MAX_TASK_PARENT_CHARS)
        if task.parent == task_id:
            task.parent = ""  # nobody is their own parent
        self._tasks[task_id] = task
        if len(self._tasks) > MAX_TASKS:  # the oldest leave first
            for old in sorted(self._tasks.values(), key=lambda t: t.at)[: len(self._tasks) - MAX_TASKS]:
                del self._tasks[old.id]
        return task

    def prune(self) -> None:
        now = self._clock()
        for task in list(self._tasks.values()):
            ttl = TASK_IDLE_TTL_S if task.state == "idle" else TASK_TTL_S
            if now - task.at > ttl:
                del self._tasks[task.id]
        for source, snapshot in list(self._census.items()):
            if now > snapshot.until:  # a source that stopped posting: its rows and its `more` go with it
                self._drop_source(source)
        self._hidden = {key: until for key, until in self._hidden.items() if until > now}

    def tasks(self) -> List[Task]:
        """Everything alive, in the order the screen wants it: working first; among those the census rows (Claude
        runs before jobs, the oldest first) and then the sessions, newest first; then the sessions that wait.

        A census row that is also a session (its hook speaks) is ONE row: the census gives the name and the clock,
        the session its tool, its summary and its words while it spoke in the last CENSUS_HOOK_FRESH_S seconds."""
        self.prune()
        now = self._clock()
        merged = self._merge()
        absorbed = {hook.id for _, _, _, hook in merged if hook is not None}
        taken = {task_id for task_id in self._tasks if task_id not in absorbed}  # a view id must be unique
        census: List[Task] = []
        for source, row, snapshot, hook in merged:
            view_id = row.id
            if view_id in taken:  # the same id from another source (or from a session): the source tells them apart
                view_id = f"{row.id}-{source}"
                while view_id in taken:
                    view_id += "_"
            taken.add(view_id)
            talk = hook if hook is not None and now - hook.at <= CENSUS_HOOK_FRESH_S else None
            running = max(0, int(now - snapshot.at))  # the row keeps counting between posts
            census.append(Task(
                id=view_id, project=row.name, tool=talk.tool if talk else "", what=talk.what if talk else row.what,
                state="working", at=snapshot.at, said=talk.said if talk else "", title=row.name, group=row.group,
                source="census", elapsed=row.elapsed + running, idle=None if row.idle is None else row.idle + running))
        census.sort(key=lambda t: (not t.id.startswith("run-"), -t.elapsed, t.id))
        sessions = sorted((t for t in self._tasks.values() if t.id not in absorbed),
                          key=lambda t: (t.state != "working", -t.at))
        return census + sessions

    def age(self, task: Task) -> int:
        """Seconds since the thread last spoke; for a census row, how long its transcript was silent (0 if unknown)."""
        if task.source == "census":
            return task.idle or 0
        return max(0, int(self._clock() - task.at))

    # -- the census ------------------------------------------------------------------

    def replace_census(self, source: str, rows: List[Dict[str, Any]], ttl: float = CENSUS_TTL_DEFAULT_S,
                       more: int = 0, seq: Optional[int] = None) -> bool:
        """A source (a machine looking at its own processes) says what is running there now.

        The snapshot replaces that source's previous one whole: a row that is not in it leaves at once, and when
        `ttl` seconds pass without another one the source's rows leave by themselves (and its `more` is forgotten).
        Other sources are not touched. `more` counts the runs the source saw but did not fit. Census rows live
        beside the sessions of the hooks, not among them (MAX_TASKS does not count them).

        When the new snapshot no longer has a run whose hook session was merged into a census row, and `more` is 0
        (so nothing was merely cut), the run is over: its session row is deleted now instead of waiting for
        TASK_TTL_S (a killed run never says SessionEnd). A session the census named is gone when no row of the new
        snapshot names it, even if a new run took the row's id; unless a run of the same folder has no session this
        time (the census could not read it), because that may be this one. A session found only by its folder goes
        when its row does. With `more` above 0 nothing is deleted: the run may be just beyond the cut. Nor is anything
        deleted by a farewell, the empty snapshot with the smallest ttl a census posts when it stops: the runs may go on.

        `seq` is the source's own counter: a snapshot with a seq not above the last one it sent is a late post and
        is ignored (False), unless that last one already expired. Without `seq` every snapshot is taken."""
        self.prune()
        previous = self._census.get(source)
        if seq is not None and previous is not None and previous.seq is not None and seq <= previous.seq:
            return False
        self._merge()  # the links as they are now, before the rows they point at change
        fresh: List[CensusRow] = []
        seen = set()
        for raw in rows:
            row = _census_row(raw)
            if row is not None and row.id not in seen:
                seen.add(row.id)
                fresh.append(row)
        more = min(_count(more, 0) + max(0, len(fresh) - MAX_CENSUS_ROWS), MAX_CENSUS_MORE)  # what does not fit
        fresh = fresh[:MAX_CENSUS_ROWS]
        ids = {row.id for row in fresh}
        now = self._clock()
        try:
            life = float(ttl)
        except (TypeError, ValueError):
            life = CENSUS_TTL_DEFAULT_S
        if not math.isfinite(life):
            life = CENSUS_TTL_DEFAULT_S
        life = min(max(life, CENSUS_TTL_MIN_S), CENSUS_TTL_MAX_S)
        if more == 0 and (fresh or life > CENSUS_TTL_MIN_S):  # (an empty snapshot at the smallest ttl is the farewell)
            sessions = {row.session for row in fresh if row.session}
            unread = {row.project for row in fresh if row.project and row.id.startswith("run-") and not row.session}
            for hook_id, (linked_source, census_id, exact) in list(self._links.items()):
                if linked_source != source:
                    continue
                if exact:  # (a run of its folder with no session this time may be it: then nothing says it is gone)
                    hook = self._tasks.get(hook_id)
                    over = hook_id not in sessions and (hook is None or hook.project not in unread)
                else:
                    over = census_id not in ids and hook_id not in sessions
                if over:
                    self._tasks.pop(hook_id, None)
                    del self._links[hook_id]
        self._hidden = {key: until for key, until in self._hidden.items() if key[0] != source or key[1] in ids}
        if previous is None and len(self._census) >= MAX_CENSUS_SOURCES:  # too many machines: the quietest one leaves
            self._drop_source(min(self._census, key=lambda s: self._census[s].at))
        kept = seq if seq is not None else (previous.seq if previous is not None else None)
        self._census[source] = CensusSnapshot(rows=fresh, at=now, until=now + life, more=more, seq=kept)
        return True

    def census_more(self) -> int:
        """Runs the sources saw but could not list, over all the sources that are still believed."""
        self.prune()
        return sum(snapshot.more for snapshot in self._census.values())

    def _drop_source(self, source: str) -> None:
        self._census.pop(source, None)
        self._links = {hook_id: link for hook_id, link in self._links.items() if link[0] != source}
        self._hidden = {key: until for key, until in self._hidden.items() if key[0] != source}

    def _merge(self) -> List[Tuple[str, CensusRow, CensusSnapshot, Optional[Task]]]:
        """Pair every census row with the session row it is, if any, and remember the pairs (`_links`).

        A session is the row whose id is the census row's `session`. A row that names no session (a job, or a run whose
        session the census could not read) may take the only session whose folder is its `project` (two sessions in one
        folder prove nothing, so none is taken); a row that does name its session never does, since a session that is
        not the one it names is somebody else's. A session row belongs to one census row at most. Sources go in name
        order so the pairing does not flap. The links are those of this pass alone: a pairing that no longer holds is
        forgotten, so it can never make a later snapshot delete a session that was not the run's."""
        now = self._clock()
        entries = [(source, row, self._census[source]) for source in sorted(self._census)
                   for row in self._census[source].rows if self._hidden.get((source, row.id), 0.0) <= now]
        found: List[Optional[Task]] = [None] * len(entries)
        claimed = set()
        for n, (_, row, _) in enumerate(entries):  # first the exact ones, so a folder guess never steals them
            hook = self._tasks.get(row.session) if row.session else None
            if hook is not None and hook.id not in claimed:
                found[n] = hook
                claimed.add(hook.id)
        exact = set(claimed)
        folders: Dict[str, List[Task]] = {}
        for task in self._tasks.values():
            folders.setdefault(task.project, []).append(task)
        for n, (_, row, _) in enumerate(entries):
            if found[n] is None and row.project and not row.session:
                same = folders.get(row.project, [])
                if len(same) == 1 and same[0].id not in claimed:
                    found[n] = same[0]
                    claimed.add(same[0].id)
        self._links = {hook.id: (entries[n][0], entries[n][1].id, hook.id in exact)
                       for n, hook in enumerate(found) if hook is not None}
        return [(source, row, snapshot, found[n]) for n, (source, row, snapshot) in enumerate(entries)]

    def _hide_census(self, session_id: str) -> None:
        """The session said SessionEnd: the census row it belongs to is over, even if the process lingers for a
        moment. It stays hidden until the source's next snapshot no longer lists it, or CENSUS_ENDED_HIDE_S pass."""
        if not self._census:
            return
        self._merge()
        until = self._clock() + CENSUS_ENDED_HIDE_S
        link = self._links.pop(session_id, None)
        if link is not None:
            self._hidden[link[:2]] = until
        for source, snapshot in self._census.items():
            for row in snapshot.rows:
                if row.session == session_id:
                    self._hidden[(source, row.id)] = until


def _seconds(value: Any) -> Optional[int]:
    """Whole seconds from a client, clamped to 0..MAX_CENSUS_SECONDS; None when it is not a whole number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, min(value, MAX_CENSUS_SECONDS))


def _census_row(raw: Any) -> Optional[CensusRow]:
    """A row of a census snapshot as the board keeps it: cleaned text, clamped numbers. None if it has no id."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    session = raw.get("session")
    return CensusRow(
        id=raw["id"][:64],
        name=_clean(raw.get("name"), MAX_CENSUS_NAME_CHARS) or _clean(raw["id"], MAX_CENSUS_NAME_CHARS),
        group=_clean(raw.get("group"), MAX_CENSUS_GROUP_CHARS) or CENSUS_GROUP,
        elapsed=_seconds(raw.get("elapsed")) or 0,
        idle=_seconds(raw.get("idle")),
        session=session[:64] if isinstance(session, str) else "",
        project=_clean(raw.get("project"), MAX_CENSUS_PROJECT_CHARS),
        what=_clean(raw.get("what"), MAX_CENSUS_WHAT_CHARS),
    )


def _keep(previous: str, value: Optional[str], limit: int) -> str:
    """None keeps what was there; anything else is the new single-line text ("" clears it)."""
    return previous if value is None else _clean(value, limit)


def _clean(value: Any, limit: int, keep_lines: bool = False) -> str:
    """Plain text from a client: printable characters only, cut to `limit`.

    By default one line (newlines become spaces). keep_lines=True lets line breaks through for text meant to be
    read as a paragraph: CRLF and CR become one break, tabs and every other control or bidi character become
    spaces, trailing spaces go, and three or more breaks in a row become one blank line."""
    text = value if isinstance(value, str) else ""
    if not keep_lines:
        text = "".join(ch if ch.isprintable() or ch == " " else " " for ch in text.replace("\n", " ")).strip()
        return text[:limit]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch if ch == "\n" or ch.isprintable() else " " for ch in text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))  # a blank-looking line is a blank line
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit].rstrip()


# --- notices from Hermes -------------------------------------------------------------------------
#
# Hermes (the owner's always-on assistant on another machine) can leave a short message on the screen: "la tanda terminó",
# "reunión en 10 min". POST /notice on its listener (buddy_hermes.py) puts it on this board; the screen lists the live
# ones above the threads, a tap on "Listo" dismisses one (POST /dismiss on the screen's server) and each leaves by
# itself when its ttl runs out. A notice may also wake the dim night screen, but that is the hub's business
# (UIHub.wake), not the board's. Like the TaskBoard this is in memory only: a daemon restart clears it.

NOTICE_KINDS = ("info", "ok", "warn")
NOTICE_STATES = ("shown", "dismissed", "expired", "withdrawn", "evicted", "unknown")
NOTICE_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
NOTICE_SOURCE = "hermes"  # who a notice comes from when it does not say
MAX_NOTICES = 3  # on the screen at once: a fourth evicts the oldest
NOTICE_TTL_MIN_S, NOTICE_TTL_MAX_S, NOTICE_TTL_DEFAULT_S = 10.0, 3600.0, 300.0  # how long one stays unless dismissed
MAX_NOTICE_TITLE_CHARS, MAX_NOTICE_BODY_CHARS, MAX_NOTICE_SOURCE_CHARS = 60, 240, 24
MAX_NOTICE_BODY_LINES = 4
NOTICE_HISTORY = 50  # how many finished notices are remembered (GET /notice/<id> still says how they ended)...
NOTICE_HISTORY_S = 3600.0  # ...and for how long


@dataclass
class Notice:
    id: str
    title: str  # one line
    body: str  # plain text, up to MAX_NOTICE_BODY_LINES lines; "" when there is none
    kind: str  # one of NOTICE_KINDS
    source: str  # who sent it, shown in the detail
    at: float  # clock when it was (re)sent: its age and its ttl count from here
    ttl: float  # seconds it stays after `at`
    rev: int  # 1 when it first shows up; up by one each time a re-send changes the title, the body or the kind
    # (or brings back an id that had ended)


NOTICE_JOINERS = "\u200c\u200d"  # zero width non-joiner and joiner: format characters, so not printable, yet they hold emoji sequences and some scripts together


def _visible(ch: str) -> bool:
    return ch.isprintable() and not ch.isspace()


def _notice_chars(text: str) -> str:
    """`text` with every character that is not printable turned into a space (control characters, bidi overrides, zero
    width spaces), except a joiner that sits between two visible characters: it is what holds an emoji sequence together."""
    out = []
    for i, ch in enumerate(text):
        if not ch.isprintable() and not (ch in NOTICE_JOINERS and 0 < i < len(text) - 1 and _visible(text[i - 1]) and _visible(text[i + 1])):
            ch = " "
        out.append(ch)
    return "".join(out)


def _notice_line(value: Any, limit: int) -> str:
    """One line of text from a client: unprintable characters become spaces (`_notice_chars`), runs of spaces become one,
    trimmed, cut."""
    text = _notice_chars(value) if isinstance(value, str) else ""
    return " ".join(text.split())[:limit].rstrip(" " + NOTICE_JOINERS)  # (a cut can leave a joiner hanging at the end)


def _notice_body(value: Any, limit: int, lines: int) -> str:
    """The body of a notice: each line cleaned like `_notice_line`, line breaks kept (CRLF and CR count as one), blank
    lines at the ends dropped and a run of them reduced to one, at most `lines` lines and `limit` characters in all.
    tools/notify.py cuts in this same order, so what it sends is what this keeps."""
    rows: List[str] = []
    for row in (value.replace("\r\n", "\n").replace("\r", "\n").split("\n") if isinstance(value, str) else []):
        row = " ".join(_notice_chars(row).split())
        if row or (rows and rows[-1]):  # no blank line first, and never two in a row
            rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[:lines])[:limit].rstrip(" \n" + NOTICE_JOINERS)


def _notice_ttl(value: Any) -> float:
    """Seconds a notice stays: clamped to NOTICE_TTL_MIN_S..NOTICE_TTL_MAX_S; the default for what is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        return NOTICE_TTL_DEFAULT_S
    return float(min(max(value, NOTICE_TTL_MIN_S), NOTICE_TTL_MAX_S))


class NoticeBoard:
    """The notices on the screen: at most MAX_NOTICES, each with a ttl, and a short memory of how the finished ones
    ended (dismissed, expired, withdrawn or evicted) so whoever sent one can ask. Nothing here is ever logged by text.

    Listeners (`subscribe`) hear when a notice arrives, is replaced, dismissed, withdrawn or evicted. Expiry is
    noticed when somebody looks (`active`, `state`...), which the screen does every second, so it tells no one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._active: Dict[str, Notice] = {}  # oldest first: a re-send moves a notice to the end
        self._done: Dict[str, Tuple[str, float, int]] = {}  # id -> (how it ended, clock then, its rev), oldest first
        self._counter = 0  # for the ids nobody chose
        self._listeners: List[Callable[[], None]] = []

    def add(self, *, title: Any, body: Any = "", kind: str = "info", ttl: Any = NOTICE_TTL_DEFAULT_S,
            source: Any = NOTICE_SOURCE, notice_id: Optional[str] = None) -> Notice:
        """Put a notice on the board. The text is cleaned and cut here, `ttl` is clamped; ValueError (a fixed phrase,
        never the text) for an empty title, a kind that is not one of NOTICE_KINDS or an id that is not an id.

        The same id replaces the notice that has it: it counts as new again (ttl and age restart, it goes first) and
        its `rev` goes up only if the title, the body or the kind changed. An id that was already finished comes back
        with a rev above the one it had (while the board still remembers it), so a screen can tell it is back. A new
        notice beyond MAX_NOTICES evicts the oldest."""
        title = _notice_line(title, MAX_NOTICE_TITLE_CHARS)
        if not title:
            raise ValueError("the title needs some text")
        if kind not in NOTICE_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(NOTICE_KINDS)}")
        if notice_id is not None and not (isinstance(notice_id, str) and NOTICE_ID_PATTERN.fullmatch(notice_id)):
            raise ValueError("id must be 1 to 64 characters of letters, digits, _ . : -")
        body = _notice_body(body, MAX_NOTICE_BODY_CHARS, MAX_NOTICE_BODY_LINES)
        source = _notice_line(source, MAX_NOTICE_SOURCE_CHARS) or NOTICE_SOURCE
        self.prune()
        if notice_id is None:
            notice_id = self._fresh_id()
        previous = self._active.pop(notice_id, None)
        finished = self._done.pop(notice_id, None)
        if previous is not None:
            rev = previous.rev + (1 if (title, body, kind) != (previous.title, previous.body, previous.kind) else 0)
        else:
            rev = finished[2] + 1 if finished is not None else 1
        while len(self._active) >= MAX_NOTICES:  # a re-send took its own place out above, so it never evicts
            self._finish(next(iter(self._active)), "evicted")
        notice = Notice(id=notice_id, title=title, body=body, kind=kind, source=source, at=self._clock(),
                        ttl=_notice_ttl(ttl), rev=rev)
        self._active[notice_id] = notice
        self._changed()
        return notice

    def dismiss(self, notice_id: str) -> str:
        """The person tapped "Listo". Returns how the notice stands now (see `state`): a notice that already ended
        keeps the way it ended."""
        return self._end(notice_id, "dismissed")

    def withdraw(self, notice_id: str) -> str:
        """Whoever sent it takes it back. Returns how it stands now, like `dismiss`."""
        return self._end(notice_id, "withdrawn")

    def active(self) -> List[Notice]:
        """The notices on the screen, newest first (the last one sent or re-sent leads)."""
        self.prune()
        return list(self._active.values())[::-1]

    def state(self, notice_id: str) -> str:
        """One of NOTICE_STATES: "shown" while it is on the board, how it ended while the board remembers it, else "unknown"."""
        self.prune()
        if notice_id in self._active:
            return "shown"
        done = self._done.get(notice_id)
        return done[0] if done is not None else "unknown"

    def age(self, notice: Notice) -> int:
        """Whole seconds since the notice was last sent."""
        return max(0, int(self._clock() - notice.at))

    def prune(self) -> None:
        """Expire what ran out of time and forget finished notices after NOTICE_HISTORY_S."""
        now = self._clock()
        for notice in list(self._active.values()):
            if now >= notice.at + notice.ttl:
                self._finish(notice.id, "expired", notice.at + notice.ttl)
        for notice_id, (_, finished, _) in list(self._done.items()):
            if now - finished > NOTICE_HISTORY_S:
                del self._done[notice_id]

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        """`fn()` is called after every change a person or a sender makes. Returns the way to stop."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _end(self, notice_id: str, state: str) -> str:
        self.prune()
        if notice_id in self._active:
            self._finish(notice_id, state)
            self._changed()
        return self.state(notice_id)

    def _finish(self, notice_id: str, state: str, when: Optional[float] = None) -> None:
        notice = self._active.pop(notice_id)
        self._done.pop(notice_id, None)
        self._done[notice_id] = (state, self._clock() if when is None else when, notice.rev)
        while len(self._done) > NOTICE_HISTORY:  # the one remembered the longest goes first
            del self._done[next(iter(self._done))]

    def _fresh_id(self) -> str:
        while True:
            self._counter += 1
            candidate = f"n-{self._counter}"
            if candidate not in self._active and candidate not in self._done:
                return candidate

    def _changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("notice listener failed")


class BuddyCore:
    """Everything the buddy does with the bytes it receives, minus the radio."""

    def __init__(
        self,
        state_dir: Path,
        send: Callable[[Dict[str, Any]], None],
        *,
        broker: Optional[ApprovalBroker] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        unpair: Optional[Callable[[], Any]] = None,
        secure: Optional[Callable[[], bool]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._send = send
        self._clock = clock
        self._wall = wall
        self._unpair_hook = unpair
        self._secure = secure or (lambda: False)
        self.on_event = on_event
        self.broker = broker or ApprovalBroker(clock)
        self.store = StateStore(self.state_dir / "state.json", clock)
        self.snapshot = Snapshot()
        self.packs = PackReceiver(self.state_dir / "characters", clock=clock)
        self.packs.cleanup_stale()
        self.tasks = TaskBoard(clock)  # what each session on the Mac says it is doing (tools/context_hook.py)
        self.notices = NoticeBoard(clock)  # short messages Hermes leaves on the screen (POST /notice)
        self.time_offset: Optional[float] = None  # desktop epoch minus our wall clock
        self.tz_offset = 0
        self._lines = LineBuffer()
        self._link_up = False
        self._last_rx = float("-inf")
        self._last_heartbeat = float("-inf")
        self._started = clock()
        self._prompt_id = ""
        self._prompt_since = 0.0
        self._oneshot = ""
        self._oneshot_until = float("-inf")
        self._tokens_synced = False
        self._last_bridge_tokens = 0
        self._tasks: set = set()
        self._handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {
            "status": self._cmd_status,
            "name": self._cmd_name,
            "owner": self._cmd_owner,
            "species": self._cmd_species,
            "unpair": self._cmd_unpair,
            "char_begin": self._cmd_char_begin,
            "file": self._cmd_file,
            "chunk": self._cmd_chunk,
            "file_end": self._cmd_file_end,
            "char_end": self._cmd_char_end,
        }

    # -- public surface --------------------------------------------------------

    @property
    def state(self) -> PersistentState:
        return self.store.data

    @property
    def connected(self) -> bool:
        return self._link_up and self._clock() - self._last_rx <= LINK_TIMEOUT_S

    def persona(self) -> str:
        """Which of the seven moods the pet should show right now."""
        now = self._clock()
        if self._oneshot_until > now:
            return self._oneshot
        if self.broker.current() is not None:  # a request from any source, Mac linked or not (Hermes does not need it)
            return "attention"
        if not self.connected:
            return "sleep"
        if self.snapshot.waiting > 0:
            return "attention"
        if self.snapshot.completed:
            return "celebrate"
        if self.snapshot.running >= BUSY_MIN_RUNNING:
            return "busy"
        return "idle"

    def feed(self, data: bytes) -> None:
        """Raw bytes from the RX characteristic."""
        for line in self._lines.feed(data):
            try:
                self._handle_line(line)
            except Exception:
                log.exception("error handling a message; ignored")

    def link_down(self, reason: str = "disconnected") -> None:
        """The transport knows the central is gone."""
        self._lines.reset()
        if self._link_up:
            self._link_lost(reason)
        else:
            self.packs.abort()

    def tick(self) -> None:
        """Call about once a second."""
        now = self._clock()
        if self._link_up and now - self._last_rx > LINK_TIMEOUT_S:
            self._link_lost("timeout")
        if self._prompt_id and now - self._last_heartbeat > LINK_TIMEOUT_S:
            self._drop_prompt()
        if self.packs.active and self.packs.idle_for() > PUSH_IDLE_TIMEOUT_S:
            self.packs.abort()
            self._emit("push_failed", error="timeout")
        self.store.flush_if_due()

    def close(self) -> None:
        self.packs.abort()
        if self.store.dirty:
            self.store.save()

    def local_time(self) -> Optional[float]:
        """The desktop's local wall clock (epoch + its tz offset), if it told us."""
        if self.time_offset is None:
            return None
        return self._wall() + self.time_offset + self.tz_offset

    def status_data(self) -> Dict[str, Any]:
        s = self.state
        sys_info: Dict[str, Any] = {"up": system_uptime()}
        try:
            usage = shutil.disk_usage(self.state_dir)
            sys_info["fsFree"], sys_info["fsTotal"] = usage.free, usage.total
        except OSError:
            pass
        return {
            "name": s.name,
            "owner": s.owner,
            "sec": bool(self._secure()),
            "sys": sys_info,
            "stats": {
                "appr": s.approvals,
                "deny": s.denials,
                "vel": s.median_velocity(),
                "nap": s.nap_seconds,
                "lvl": s.level,
            },
        }

    # -- plumbing ----------------------------------------------------------------

    def _emit(self, event: str, **data: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, data)
            except Exception:
                log.exception("event listener failed")

    def _ack(self, cmd: str, ok: bool, n: int = 0, error: Optional[str] = None, data: Any = None) -> None:
        msg: Dict[str, Any] = {"ack": cmd, "ok": ok, "n": n}
        if error:
            msg["error"] = error
        if data is not None:
            msg["data"] = data
        self._send(msg)

    def _mark_alive(self) -> None:
        self._last_rx = self._clock()
        if not self._link_up:
            self._link_up = True
            self._emit("link_up")

    def _link_lost(self, reason: str) -> None:
        self._link_up = False
        self.snapshot = Snapshot()
        self._drop_prompt()
        self.packs.abort()
        self._lines.reset()
        self._emit("link_down", reason=reason)

    def _trigger_oneshot(self, persona: str, seconds: float) -> None:
        self._oneshot = persona
        self._oneshot_until = self._clock() + seconds

    def _handle_line(self, line: bytes) -> None:
        try:
            text = line.decode("utf-8").strip()
        except UnicodeDecodeError:
            log.debug("dropping a line that is not UTF-8")
            return
        if not text.startswith("{"):
            return
        try:
            msg = json.loads(text)
        except ValueError:
            log.debug("dropping a line that is not JSON: %.60r", text)
            return
        if isinstance(msg, dict):
            self._on_message(msg)

    def _on_message(self, msg: Dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        if cmd is not None:
            self._mark_alive()
            if isinstance(cmd, str) and cmd:
                self._on_command(cmd, msg)
        elif "time" in msg:
            self._mark_alive()
            self._on_time(msg["time"])
        elif "evt" in msg:
            self._mark_alive()
            self._emit("evt", msg=msg)
        else:
            self._on_heartbeat(msg)

    # -- time / heartbeat ---------------------------------------------------------

    def _on_time(self, value: Any) -> None:
        if (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
        ):
            self.time_offset = float(value[0]) - self._wall()
            self.tz_offset = int(value[1])
            self._emit("time", epoch=value[0], tz=self.tz_offset)

    def _on_heartbeat(self, msg: Dict[str, Any]) -> None:
        if not HEARTBEAT_KEYS & msg.keys():
            return
        self._mark_alive()
        self._last_heartbeat = self._clock()
        snap = self.snapshot
        snap.total = _count(msg.get("total"), snap.total)
        snap.running = _count(msg.get("running"), snap.running)
        snap.waiting = _count(msg.get("waiting"), snap.waiting)
        snap.tokens_today = _count(msg.get("tokens_today"), snap.tokens_today)
        snap.completed = msg.get("completed") is True
        if isinstance(msg.get("msg"), str):
            snap.msg = msg["msg"][:200]
        if isinstance(msg.get("entries"), list):
            snap.entries = [str(e)[:200] for e in msg["entries"][:8]]
        tokens = msg.get("tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            snap.tokens = tokens
            self._on_bridge_tokens(tokens)
        self._sync_prompt(msg.get("prompt"))
        self._emit("heartbeat", snapshot=snap)

    def _on_bridge_tokens(self, total: int) -> None:
        # The desktop reports tokens since *it* started. Track deltas; latch on first
        # sight so a daemon restart does not re-credit the whole session.
        if not self._tokens_synced:
            self._last_bridge_tokens = total
            self._tokens_synced = True
            return
        if total < self._last_bridge_tokens:  # the desktop app restarted
            self._last_bridge_tokens = total
            return
        delta = total - self._last_bridge_tokens
        self._last_bridge_tokens = total
        if delta == 0:
            return
        before = self.state.level
        self.state.tokens += delta
        after = self.state.level
        self.store.touch(now=after > before)
        if after > before:
            self._trigger_oneshot("celebrate", 3.0)
            self._emit("level_up", level=after)

    def _sync_prompt(self, prompt: Any) -> None:
        if isinstance(prompt, dict) and isinstance(prompt.get("id"), str) and prompt["id"]:
            pid = prompt["id"]
            if pid == self._prompt_id:
                return  # same prompt (keepalive); answered or not, leave it alone
            self._drop_prompt()
            now = self._clock()
            self._prompt_id = pid
            self._prompt_since = now
            approval = Approval(
                id=pid,
                tool=str(prompt.get("tool", ""))[:80],
                hint=str(prompt.get("hint", ""))[:500],
                source="claude",
                created=now,
            )
            self.broker.submit(approval, lambda decision, pid=pid: self._respond(pid, decision))
        else:
            self._drop_prompt()

    def _drop_prompt(self) -> None:
        if self._prompt_id:
            self.broker.withdraw("claude", self._prompt_id)
            self._prompt_id = ""

    def _respond(self, prompt_id: str, decision: str) -> None:
        if prompt_id != self._prompt_id:
            raise RuntimeError("that prompt is gone")
        self._send({"cmd": "permission", "id": prompt_id, "decision": decision})
        took = self._clock() - self._prompt_since
        st = self.state
        if decision == "once":
            st.approvals += 1
            st.velocity = (st.velocity + [int(took)])[-8:]
            if took < FAST_APPROVAL_S:
                self._trigger_oneshot("heart", 2.0)
        else:
            st.denials += 1
        self.store.touch(now=True)
        self._emit("decision", id=prompt_id, decision=decision, seconds=took)

    # -- commands -----------------------------------------------------------------

    def _on_command(self, cmd: str, msg: Dict[str, Any]) -> None:
        if cmd == "permission":  # that one flows device -> desktop; never ours
            return
        handler = self._handlers.get(cmd)
        if handler is None:
            self._ack(cmd, False, error="unknown command")
            return
        try:
            handler(msg)
        except Exception:
            log.exception("command %s failed", cmd)
            self._ack(cmd, False, error="internal error")

    def _cmd_status(self, msg: Dict[str, Any]) -> None:
        self._ack("status", True, data=self.status_data())

    def _cmd_name(self, msg: Dict[str, Any]) -> None:
        value = msg.get("name")
        if not isinstance(value, str):
            self._ack("name", False, error="missing name")
            return
        self.state.name = clean_label(value, 24) or "Buddy"
        self.store.touch(now=True)
        self._emit("name", name=self.state.name)
        self._ack("name", True)

    def _cmd_owner(self, msg: Dict[str, Any]) -> None:
        value = msg.get("name")
        if not isinstance(value, str):
            self._ack("owner", False, error="missing name")
            return
        self.state.owner = clean_label(value, 32)
        self.store.touch(now=True)
        self._emit("owner", owner=self.state.owner)
        self._ack("owner", True)

    def _cmd_species(self, msg: Dict[str, Any]) -> None:
        idx = msg.get("idx", 0xFF)
        self.state.species = idx if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx <= 0xFF else 0xFF
        self.store.touch(now=True)
        self._emit("species", idx=self.state.species)
        self._ack("species", True)

    def _cmd_unpair(self, msg: Dict[str, Any]) -> None:
        hook = self._unpair_hook
        if hook is None:
            self._ack("unpair", True)
            return
        try:
            result = hook()
        except Exception:
            log.exception("unpair hook failed")
            self._ack("unpair", False, error="unpair failed")
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(self._finish_unpair(result))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            self._ack("unpair", result is not False)

    async def _finish_unpair(self, awaitable: Any) -> None:
        try:
            await awaitable
        except Exception:
            log.exception("unpair failed")
            self._ack("unpair", False, error="unpair failed")
            return
        self._emit("unpaired")
        self._ack("unpair", True)

    def _cmd_char_begin(self, msg: Dict[str, Any]) -> None:
        name = msg.get("name")
        name = name if isinstance(name, str) and name else "pet"
        try:
            self.packs.begin(name, msg.get("total", 0))
        except PackError as exc:
            self._ack("char_begin", False, n=min(self.packs.free_bytes(), 2**31 - 1), error=str(exc))
            self._emit("push_failed", error=str(exc))
            return
        self._emit("push_begin", name=name, total=msg.get("total", 0))
        self._ack("char_begin", True)

    def _push_fail(self, cmd: str, exc: PackError) -> None:
        self.packs.abort()
        self._emit("push_failed", error=str(exc))
        self._ack(cmd, False, error=str(exc))

    def _cmd_file(self, msg: Dict[str, Any]) -> None:
        try:
            self.packs.start_file(msg.get("path"), msg.get("size", 0))
        except PackError as exc:
            self._push_fail("file", exc)
            return
        self._ack("file", True)

    def _cmd_chunk(self, msg: Dict[str, Any]) -> None:
        try:
            written = self.packs.add_chunk(msg.get("d"))
        except PackError as exc:
            self._push_fail("chunk", exc)
            return
        self._ack("chunk", True, n=written)

    def _cmd_file_end(self, msg: Dict[str, Any]) -> None:
        try:
            size = self.packs.end_file()
        except PackError as exc:
            self._push_fail("file_end", exc)
            return
        self._ack("file_end", True, n=size)

    def _cmd_char_end(self, msg: Dict[str, Any]) -> None:
        try:
            dest, manifest = self.packs.finish()
        except PackError as exc:
            self._push_fail("char_end", exc)
            return
        if manifest is not None:
            self.state.character = dest.name
            self.store.touch(now=True)
        self._emit("character", path=dest, manifest=manifest)
        self._ack("char_end", True)
