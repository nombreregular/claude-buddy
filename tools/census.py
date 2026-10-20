#!/usr/bin/env python3
"""What runs on the Mac, told to the Claude Buddy screen: the headless Claude runs and the job scripts you name.

tools/context_hook.py only speaks when a Claude session does something, so the screen misses a `claude -p` run that
stays quiet for minutes and every script that is not a Claude session at all. This program looks at the process table
every `interval_s` (15 s) and posts a snapshot of what it finds to the buddy's Hermes listener (`POST /census`, same
token as the hook). The screen lists the rows in the group "Corridas" with their name and how long each has been
running, and the snapshot replaces the previous one whole: a run that ended is gone from the next one.

    python3 tools/census.py run              # the loop (what the LaunchAgent runs, see tools/census_agent.sh)
    python3 tools/census.py once --print     # one pass, print the snapshot, send nothing

What leaves the Mac, and nothing else: a name (the run's folder name, or the one your config gives a job), the seconds
it has been running, the seconds since its transcript last changed, the hook's session id (so the daemon shows a run
once) and the folder name. Command lines are read in memory to classify a process and dropped at once: no argument,
environment, path, prompt or output is kept, logged, written to disk or sent. `ps` runs without the `e` modifier and
the environment of another process is never read. Standard library only, and the Python 3.9 that ships with macOS.

A headless run is a process that is ALL of: its program is called `claude`; the word after it is a flag; `-p` or
`--print` is among its first 80 words; no `--sdk-url` among them; nothing above it lives inside Claude.app or is a
headless run itself (a nested `claude -p` belongs to the outer run); and it is older than `min_age_s`. Wrappers
(`bash -c ... claude -p`, `caffeinate -i claude -p`, `ssh host claude -p`) are never runs, their child is.

Options as environment variables (launchd does not forward the shell's: set them in the plist with `install --url`):

    CLAUDE_BUDDY_URL             default http://buddy:8766 (http only; this Mac, the tailnet or `insecure_url_ok`;
                                 the token is also held back if the name leads to any other address)
    CLAUDE_BUDDY_TOKEN_FILE      default ~/.config/claude-buddy/hermes.token, must be 0600 and yours
    CLAUDE_BUDDY_CENSUS=off      do nothing (exit 0 at once)
    CLAUDE_BUDDY_CENSUS_CONFIG   default ~/.config/claude-buddy/census.json (see tools/census.example.json)
    CLAUDE_CONFIG_DIR            Claude Code's own folder (default ~/.claude): its live-process registry and transcripts
"""
import calendar
import ctypes
import http.client
import ipaddress
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

DEFAULT_URL = "http://buddy:8766"
DEFAULT_TOKEN_FILE = "~/.config/claude-buddy/hermes.token"
DEFAULT_CONFIG = "~/.config/claude-buddy/census.json"
OFF = ("off", "0", "no")
CONNECT_TIMEOUT_S = 1.5
REPLY_TIMEOUT_S = 3.0  # per wait on the socket; the real bound on a whole post is the join below
POST_JOIN_S = 4.5  # a post is abandoned after this (a resolver that ignores socket timeouts must not freeze the loop)
STOP_SLICE_S = 0.5  # how long SIGTERM can wait for the main thread to notice it (launchd allows ExitTimeOut, 5 s)
FAREWELL_JOIN_S = 1.5  # SIGTERM: launchd gives the process ExitTimeOut (5 s) before it kills it
PS_TIMEOUT_S = 30.0  # a busy Mac makes `ps` slow, and a pass that gives up flickers the group
MIN_TOKEN_CHARS = 16
MAX_POST_BYTES = 3900  # the listener refuses bodies over 4096 bytes; the same margin the hook keeps
INTERVAL_S, INTERVAL_RANGE = 15.0, (5.0, 30.0)
TTL_FACTOR, TTL_RANGE = 4, (5, 120)  # one lost snapshot must not blink the group
FAREWELL_TTL_S = 5
BACKOFF_S = (15.0, 30.0, 60.0)  # after a network error or a 5xx
REFUSED_WAIT_S = {403: 600.0, 404: 300.0}  # a bad token also feeds the lockout the hooks share: do not hammer
TOKEN_NOTE_EVERY_S = 3600.0
MAX_ROWS, MAX_MORE = 8, 999
MIN_AGE_S = 20  # a younger process is not listed (git hooks, one-shot helpers)
MAX_ELAPSED_S = 2678400  # what the listener accepts (31 days); older processes are clamped, not dropped
GROUP, SOURCE = "Corridas", "mac"
NAME_CHARS = 40
LABEL_CHARS = 80  # a program or script name is cut to this before anything compares it: a pattern may be slow on a long one
HEAD_TOKENS = 80  # `-p` and `--sdk-url` count only among the first words after the program: a prompt may say anything
MIN_PROCS_SANE = 20  # fewer rows than this means `ps` failed, not that nothing runs
JOB_PROCS_MAX = 10  # a job that matches more processes than this is a loose pattern
MAX_JOBS = MAX_PREFIXES = 20
CONFIG_MAX_BYTES = 64 * 1024
REGISTRY_MAX_BYTES = 16 * 1024
MAX_SUBAGENT_FILES = 200

SHELLS = frozenset(("bash", "sh", "zsh", "dash"))
INTERPRETERS = SHELLS | {"python", "node", "ruby", "perl"}
VALUE_FLAGS = {"node": frozenset(("--require", "-r", "--import", "--loader", "--experimental-loader")),
               "python": frozenset(("-W", "-X"))}  # flags whose next word is their value, not the script
CODE_FLAGS = frozenset(("-c", "-e", "-m", "-p", "--eval", "--print", "--command"))  # the next word is code
SCRIPT_EXT = re.compile(r"\.(sh|bash|zsh|py|js|mjs|cjs|ts|mts|rb|pl)$", re.I)
SHELL_CODE_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")  # bash -c, -lc, -ic
SAFE = re.compile(r"[^A-Za-z0-9._-]+")
ID_OK = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SOURCE_OK = re.compile(r"[a-z0-9_-]{1,24}")
GROUP_OK = re.compile(r"[!-~](?:[ -~]{0,22}[!-~])?")  # printable ASCII, 1 to 24, no space at either end
ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)\s+(.*)$")  # pid ppid [[dd-]hh:]mm:ss command
LSTART = re.compile(r"^[A-Za-z]{3} ([A-Za-z]{3})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})$")  # `ps -o lstart`
MONTHS = {name: n for n, name in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
TAILNET_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]*")  # `buddy`; a number such as 3232235777 or 0xc0a80101 is an IPv4 address in disguise
HOME = os.path.expanduser("~")


class Proc(NamedTuple):
    """All a process is allowed to be here: no command line survives parse_ps, so none can leak."""
    pid: int
    ppid: int
    elapsed: int
    prog: str  # basename of the program (the first word of the command), never an argument
    script: str  # interpreters only: basename of the script file, never another argument
    headless: bool  # looks like `claude -p`: program, a flag first, -p/--print early, no --sdk-url
    managed: bool  # the program lives inside Claude.app (the desktop app, its helpers and its `disclaimer` wrapper)


# --- reading the process table ---------------------------------------------------------------------------

def interpreter_of(prog: str) -> str:
    """The family of an interpreter: macOS's `ps` shows Python.app's binary as `Python`, and `python3.13` is python."""
    family = re.sub(r"[0-9.]+$", "", prog.lower())
    return family if family in INTERPRETERS else ""


def script_of(family: str, words: List[str]) -> str:
    """The script an interpreter runs: its first word that is not a flag (or a flag's value), if that is a file with a
    known extension. A word after it is an argument and is never looked at; code given on the command line is no script."""
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in VALUE_FLAGS.get(family, ()):
            skip = True
        elif family in SHELLS and (SHELL_CODE_FLAG.match(word) or word == "--command"):
            return ""
        elif family not in SHELLS and word in CODE_FLAGS:
            return ""
        elif not word.startswith("-"):
            leaf = os.path.basename(word)
            return leaf[:LABEL_CHARS] if SCRIPT_EXT.search(leaf) else ""
    return ""


def parse_ps(text: str) -> List[Proc]:
    """`ps -ww -o pid=,ppid=,etime=,command=` to Procs. Each line is reduced at once; the text is the caller's to drop."""
    out: List[Proc] = []
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        elapsed = ((int(m.group(3) or 0) * 24 + int(m.group(4) or 0)) * 60 + int(m.group(5))) * 60 + int(m.group(6))
        cmd = m.group(7)
        if cmd.endswith("<defunct>"):  # a zombie is not running anything
            out.append(Proc(int(m.group(1)), int(m.group(2)), elapsed, "", "", False, False))
            continue
        words = cmd.split(None, HEAD_TOKENS + 1)  # the program, the words that count, the rest in one piece (dropped here)
        prog = os.path.basename(words[0])[:LABEL_CHARS] if words else ""
        rest = words[1:HEAD_TOKENS + 1]
        headless = (prog == "claude" and bool(rest) and rest[0].startswith("-") and ("-p" in rest or "--print" in rest)
                    and not any(w == "--sdk-url" or w.startswith("--sdk-url=") for w in rest))
        family = interpreter_of(prog)
        managed = "/claude.app/" in cmd.split(" -", 1)[0].lower()  # a path with spaces ends where the flags begin
        out.append(Proc(int(m.group(1)), int(m.group(2)), elapsed, prog, script_of(family, rest) if family else "",
                        headless, managed))
    return out


def ancestors(table: Dict[int, Proc], pid: int) -> List[Proc]:
    chain: List[Proc] = []
    seen = {pid}
    proc = table.get(pid)
    while proc is not None and proc.ppid not in seen:
        seen.add(proc.ppid)
        proc = table.get(proc.ppid)
        if proc is not None:
            chain.append(proc)
    return chain


def is_headless_run(proc: Proc, table: Dict[int, Proc]) -> bool:
    """A `claude -p` that is a run of its own: not Claude desktop's, not an SDK child, and not inside another run."""
    if not proc.headless or proc.managed:
        return False
    return not any(a.managed or a.headless for a in ancestors(table, proc.pid))


# --- what we may know about a process without opening anything of its own --------------------------------

_libproc: Any = None
VNODE_INFO_BYTES, CWD_OFFSET, PATH_BYTES, PROC_PIDVNODEPATHINFO = 2352, 152, 1024, 9


def cwd_of(pid: int) -> Optional[str]:
    """The current directory through proc_pidinfo(PROC_PIDVNODEPATHINFO): no lsof, no file opened, no TCC prompt."""
    global _libproc
    try:
        if _libproc is None:
            try:
                lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
                lib.proc_pidinfo.restype = ctypes.c_int
                _libproc = lib
            except (OSError, AttributeError):  # not macOS
                _libproc = False
        if not _libproc:
            return None
        buf = ctypes.create_string_buffer(VNODE_INFO_BYTES)
        if _libproc.proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, buf, VNODE_INFO_BYTES) < VNODE_INFO_BYTES:
            return None
        return buf.raw[CWD_OFFSET:CWD_OFFSET + PATH_BYTES].split(b"\0", 1)[0].decode("utf-8", "replace") or None
    except Exception:  # a process that just died, a changed layout: the caller has a fallback name
        return None


def parse_lstart(text: str) -> Optional[float]:
    """`Sun Oct 18 14:54:26 2026` (UTC, the way Claude Code writes `procStart`) to epoch seconds."""
    m = LSTART.match(text.strip())
    if not m or m.group(1) not in MONTHS or not 1 <= int(m.group(2)) <= 31:
        return None
    try:
        return float(calendar.timegm((int(m.group(6)), MONTHS[m.group(1)], int(m.group(2)),
                                      int(m.group(3)), int(m.group(4)), int(m.group(5)), 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def finite(value: Any) -> bool:
    """math.isfinite, but an integer too big for a float is simply not a number to use (math.isfinite raises on it)."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def registry_session(claude_dir: str, pid: int, elapsed: int, now: float) -> Optional[str]:
    """The hook's session id for this pid from Claude Code's own list of live processes (<dir>/sessions/<pid>.json),
    if the file is this process's and not a stale one of an earlier process with the same pid. Only sessionId and the
    start time are looked at; the *.key files next to it are never touched."""
    path = os.path.join(claude_dir, "sessions", "%d.json" % pid)
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > REGISTRY_MAX_BYTES:
            return None
        with open(path, "rb") as f:
            data = json.loads(f.read(REGISTRY_MAX_BYTES))
    except (OSError, ValueError, RecursionError):  # (a file nested too deep for the parser is no one's session)
        return None
    if not isinstance(data, dict) or str(data.get("pid")) != str(pid):
        return None
    session = data.get("sessionId")
    if not isinstance(session, str) or not ID_OK.fullmatch(session):
        return None
    began = now - elapsed
    started = parse_lstart(data["procStart"]) if isinstance(data.get("procStart"), str) else None
    if started is not None:
        return session if abs(started - began) <= 3 else None
    ms = data.get("startedAt")  # the older field: coarser (it was off by 77 s once), so a wider window
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not finite(ms):
        return None
    return session if abs(ms / 1000.0 - began) <= 20 else None


class Transcripts:
    """When a session last wrote, from the modification times of its transcript and its subagents' transcripts.
    stat only: a transcript is located and measured, never opened."""

    def __init__(self, claude_dir: str) -> None:
        self.base = os.path.join(claude_dir, "projects")
        self.paths: Dict[str, str] = {}

    def find(self, session: str) -> Optional[str]:
        try:
            for folder in os.listdir(self.base):
                candidate = os.path.join(self.base, folder, session + ".jsonl")
                if os.path.isfile(candidate):
                    if len(self.paths) >= 64:
                        self.paths.clear()
                    self.paths[session] = candidate
                    return candidate
        except OSError:
            pass
        return None

    def idle(self, session: str, now: float) -> Optional[int]:
        path = self.paths.get(session) or self.find(session)
        if path is None:
            return None
        try:
            newest = os.stat(path).st_mtime
        except OSError:
            self.paths.pop(session, None)
            return None
        try:
            with os.scandir(os.path.join(os.path.dirname(path), session, "subagents")) as entries:
                for n, entry in enumerate(entries):
                    if n >= MAX_SUBAGENT_FILES:
                        break
                    if entry.name.endswith(".jsonl"):
                        try:
                            newest = max(newest, entry.stat().st_mtime)
                        except OSError:
                            pass
        except OSError:
            pass
        return max(0, int(now - newest))


# --- config: ~/.config/claude-buddy/census.json (yours, not in git) -------------------------------------

class Job(NamedTuple):
    match: str
    regex: Optional["re.Pattern[str]"]
    name: str
    min_age: int
    group: str


class Config(NamedTuple):
    url: str
    token_file: str
    source: str
    interval: float
    group: str
    max_rows: int
    min_age: int
    claude_runs: bool
    strip_prefix: Tuple[str, ...]
    jobs: Tuple[Job, ...]
    ignore: Tuple[Job, ...]
    insecure_url_ok: bool
    warnings: Tuple[str, ...]

    @property
    def ttl(self) -> int:
        return int(round(min(TTL_RANGE[1], max(TTL_RANGE[0], TTL_FACTOR * self.interval))))


def _job(raw: Any, group: str, min_age: int, warn: List[str], where: str) -> Optional[Job]:
    if not isinstance(raw, dict) or not isinstance(raw.get("match"), str) or not 1 <= len(raw["match"]) <= 120:
        warn.append("%s: tiene que traer un match de 1 a 120 caracteres" % where)
        return None
    rx = None
    if raw.get("regex") is True:
        try:
            rx = re.compile(raw["match"])
        except re.error:
            warn.append("%s: la regex no compila" % where)
            return None
    elif "regex" in raw and raw["regex"] is not False:
        warn.append("%s: regex tiene que ser true o false" % where)
    name, age, other = raw.get("name", "{script}"), raw.get("min_age_s", min_age), raw.get("group", group)
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        warn.append("%s: name no sirve, uso {script}" % where)
        name = "{script}"
    if isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= 3600:
        warn.append("%s: min_age_s no sirve, uso %d" % (where, min_age))
        age = min_age
    if not isinstance(other, str) or not GROUP_OK.fullmatch(other):
        warn.append("%s: group no sirve, uso %s" % (where, group))
        other = group
    return Job(raw["match"], rx, name, age, other)


def load_config(path: str, env: Dict[str, str]) -> Config:
    """Tolerant: anything wrong falls back to the safe default (the headless Claude runs, nothing else) and says why in
    `warnings`, as fixed phrases and indexes, never the text of the file."""
    warn: List[str] = []
    raw: Dict[str, Any] = {}
    try:
        info = os.stat(os.path.expanduser(path))
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            warn.append("ignoro el archivo: tiene que ser tuyo y que otros no puedan escribirlo")
        elif info.st_size > CONFIG_MAX_BYTES:
            warn.append("ignoro el archivo: pesa mas de 64 KB")
        else:
            with open(os.path.expanduser(path), "rb") as f:
                loaded = json.loads(f.read(CONFIG_MAX_BYTES))
            if isinstance(loaded, dict):
                raw = loaded
            else:
                warn.append("ignoro el archivo: tiene que ser un objeto JSON")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError):  # (a file nested too deep for the parser is not JSON we can use)
        warn.append("ignoro el archivo: no se pudo leer como JSON")
    if raw.get("version", 1) != 1:
        warn.append("version desconocida, leo solo lo que entiendo")

    def num(key: str, default: float, lo: float, hi: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite(value):
            if key in raw:
                warn.append("%s: tiene que ser un numero, uso %g" % (key, default))
            return default
        if not lo <= value <= hi:
            warn.append("%s: fuera de %g a %g, uso %g" % (key, lo, hi, min(hi, max(lo, value))))
        return min(hi, max(lo, value))

    def text(key: str, ok: "re.Pattern[str]", default: str) -> str:
        value = raw.get(key, default)
        if isinstance(value, str) and ok.fullmatch(value):
            return value
        if key in raw:
            warn.append("%s: no sirve, uso %s" % (key, default))
        return default

    def many(key: str) -> List[Any]:
        value = raw.get(key, [])
        if not isinstance(value, list):
            warn.append("%s: tiene que ser una lista" % key)
            return []
        if len(value) > MAX_JOBS:
            warn.append("%s: uso solo los primeros %d" % (key, MAX_JOBS))
        return value[:MAX_JOBS]

    group = text("group", GROUP_OK, GROUP)
    min_age = int(num("min_age_s", MIN_AGE_S, 0, 3600))
    jobs = tuple(j for i, r in enumerate(many("jobs")) for j in [_job(r, group, min_age, warn, "jobs[%d]" % i)] if j)
    ignore = tuple(j for i, r in enumerate(many("ignore"))
                   for j in [_job(r if isinstance(r, dict) else {"match": r}, group, min_age, warn, "ignore[%d]" % i)] if j)
    prefixes = raw.get("strip_prefix", [])
    if not isinstance(prefixes, list):
        warn.append("strip_prefix: tiene que ser una lista")
        prefixes = []
    url = env.get("CLAUDE_BUDDY_URL") or (raw["url"] if isinstance(raw.get("url"), str) and raw["url"] else DEFAULT_URL)
    token_file = env.get("CLAUDE_BUDDY_TOKEN_FILE") or (
        raw["token_file"] if isinstance(raw.get("token_file"), str) and raw["token_file"] else DEFAULT_TOKEN_FILE)
    return Config(
        url=url, token_file=token_file, source=text("source", SOURCE_OK, SOURCE),
        interval=float(num("interval_s", INTERVAL_S, *INTERVAL_RANGE)), group=group,
        max_rows=int(num("max_rows", MAX_ROWS, 1, MAX_ROWS)), min_age=min_age,
        claude_runs=raw.get("claude_runs", True) is not False,
        strip_prefix=tuple(x for x in prefixes if isinstance(x, str) and 0 < len(x) <= 20)[:MAX_PREFIXES],
        jobs=jobs, ignore=ignore, insecure_url_ok=raw.get("insecure_url_ok") is True, warnings=tuple(warn))


def job_hit(job: Job, label: str) -> bool:
    """A job is compared with the LABEL of a process only (its script's or its program's name), never with an argument:
    arguments are not kept, so no pattern can be written against them."""
    return bool(job.regex.search(label)) if job.regex is not None else job.match in label


# --- one pass over the process table -----------------------------------------------------------------------

def clean_name(text: str) -> str:
    """A folder or script name as the listener takes it: ASCII letters, digits and . _ - only, 40 at most."""
    return SAFE.sub("-", text).strip("-")[:NAME_CHARS].strip("-")


def unique_id(rid: str, pid: int, used: set) -> str:
    """The lowest pid keeps the id; the others of the same name get -p<pid> (a repeated id is a 400 for the whole snapshot)."""
    if rid in used:
        rid = "%s-p%d" % (rid, pid)
    while rid in used:  # only a name that already looks like a pid suffix gets here
        rid += "-"
    used.add(rid)
    return rid


def scan(procs: List[Proc], cfg: Config, now: float, cwd: Callable[[int], Optional[str]] = cwd_of,
         session: Optional[Callable[[int, int, float], Optional[str]]] = None,
         idle: Optional[Callable[[str, float], Optional[int]]] = None, home: Optional[str] = None,
         notes: Optional[List[str]] = None) -> Dict[str, Any]:
    """The snapshot to post. `notes` collects the (count-only) warnings the caller may want to log."""
    table = {p.pid: p for p in procs}
    homes = {(home or HOME).rstrip("/"), os.path.realpath(home or HOME).rstrip("/")}

    def folder(pid: int) -> str:  # the last component of the cwd, "" when it says nothing (unknown, /, the home folder)
        path = (cwd(pid) or "").rstrip("/")
        return "" if not path or path in homes else os.path.basename(path)

    def short(name: str) -> str:
        for prefix in cfg.strip_prefix:
            if name.startswith(prefix) and len(name) > len(prefix):
                return name[len(prefix):]
        return name

    def row(rid: str, name: str, group: str, p: Proc) -> Dict[str, Any]:
        return {"id": rid, "name": name, "group": group, "state": "working", "elapsed": min(p.elapsed, MAX_ELAPSED_S)}

    used: set = set()
    runs: List[Dict[str, Any]] = []
    if cfg.claude_runs:
        for p in sorted(procs, key=lambda q: q.pid):
            if p.elapsed < cfg.min_age or not is_headless_run(p, table):
                continue
            where = folder(p.pid)
            name = clean_name(short(where)) or "claude-%d" % p.pid
            item = row(unique_id("run-" + name, p.pid, used), name, cfg.group, p)
            try:
                sid = session(p.pid, p.elapsed, now) if session else None
            except Exception:  # one odd file costs this run its session; it must not cost the pass its other rows
                sid = None
            if sid:
                item["session"] = sid
                try:
                    gap = idle(sid, now) if idle else None
                except Exception:
                    gap = None
                if gap is not None:
                    item["idle"] = min(gap, MAX_ELAPSED_S)
            if clean_name(where):
                item["project"] = clean_name(where)
            runs.append(item)

    jobs: List[Dict[str, Any]] = []
    if cfg.jobs:
        hits: Dict[int, List[Proc]] = {}
        for p in procs:
            label = p.script or p.prog
            if p.headless or not label or any(job_hit(i, label) for i in cfg.ignore):  # a headless claude is a run, never a job
                continue
            n = next((n for n, j in enumerate(cfg.jobs) if job_hit(j, label)), None)
            if n is None or p.elapsed < cfg.jobs[n].min_age:
                continue
            parent = table.get(p.ppid)
            if parent is not None and job_hit(cfg.jobs[n], parent.script or parent.prog):
                continue  # a subshell or a child of the same job: the root stands for it
            hits.setdefault(n, []).append(p)
        kept: List[Tuple[Proc, Job]] = []
        for n in sorted(hits):
            found = sorted(hits[n], key=lambda q: (-q.elapsed, q.pid))
            if len(found) > JOB_PROCS_MAX:
                if notes is not None:
                    notes.append("el trabajo #%d coincide con %d procesos: muestro los %d mas viejos"
                                 % (n + 1, len(found), JOB_PROCS_MAX))
                found = found[:JOB_PROCS_MAX]
            kept.extend((p, cfg.jobs[n]) for p in found)
        for p, job in sorted(kept, key=lambda t: t[0].pid):
            leaf = SCRIPT_EXT.sub("", p.script) if p.script else p.prog
            label = job.name.replace("{script}", leaf).replace("{prog}", p.prog).replace("{cwd}", short(folder(p.pid)) or leaf)
            name = clean_name(label) or "proc-%d" % p.pid
            jobs.append(row(unique_id("job-" + name, p.pid, used), name, job.group, p))

    def oldest_first(r: Dict[str, Any]) -> Tuple[int, str]:
        return -r["elapsed"], r["id"]

    rows = sorted(runs, key=oldest_first) + sorted(jobs, key=oldest_first)  # runs before jobs
    shown = rows[:cfg.max_rows]
    return {"v": 1, "source": cfg.source, "seq": int(now * 1000), "ttl": cfg.ttl,
            "more": min(MAX_MORE, len(rows) - len(shown)), "rows": shown}


def encode(body: Dict[str, Any]) -> bytes:
    """The request body: ASCII only (json escapes the rest), and small enough for the listener, which refuses over
    4096 bytes and would drop the whole snapshot. If it ever does not fit, the last rows are left out and counted."""
    body = dict(body)
    rows = list(body["rows"])
    while True:
        data = json.dumps(dict(body, rows=rows), separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(data) <= MAX_POST_BYTES or not rows:
            return data
        rows.pop()
        body["more"] = min(MAX_MORE, body["more"] + 1)


# --- talking to the buddy -------------------------------------------------------------------------------

class Stop:
    """A threading.Event without its lock. A signal handler runs on the main thread between two bytecodes, and an
    Event.set() that lands while that thread is entering Event.wait() would wait for a lock the thread itself holds
    (SIGTERM would hang the process until launchd kills it). Here set() is one assignment, and wait() sleeps in slices."""

    def __init__(self) -> None:
        self.flag = False

    def set(self) -> None:
        self.flag = True

    def is_set(self) -> bool:
        return self.flag

    def wait(self, timeout: float = 0.0) -> bool:
        end = time.monotonic() + timeout
        while not self.flag and time.monotonic() < end:
            time.sleep(min(STOP_SLICE_S, max(0.0, end - time.monotonic())))
        return self.flag


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
    """Why the token must not be sent to this URL, or None. It travels in plain http, so only this Mac or the tailnet."""
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
    return ("la URL del buddy no es de esta Mac ni de la tailnet y el token viajaria en claro: no mando nada "
            "(insecure_url_ok si de verdad queres)")


class PeerRefused(OSError):
    """The name led to an address that is neither this Mac nor the tailnet, so the token was not sent."""


def peer_may_hear_token(address: Any) -> bool:
    """The same rule as the hook's guard for `said` (tools/context_hook.py): this Mac, or 100.64.0.0/10 and fd7a:115c:a1e0::/48."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])  # IPv6 peers can carry a %scope
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is not "loopback" to Python 3.9 until unwrapped
        return bool(ip.is_loopback or ip in (TAILNET_V4 if ip.version == 4 else TAILNET_V6))
    except ValueError:
        return False


def post(url: str, token: str, data: bytes) -> int:
    """POST the body to /census and return the HTTP status. Raises OSError / HTTPException when the buddy is not there.

    url_problem judges the URL by its text; here the address the name really led to is checked too, before the token is
    written. A URL the text rule refuses is only here because the config said insecure_url_ok, and that is that person's call."""
    parts = urlsplit(url)
    strict = url_problem(url, False) is None
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=CONNECT_TIMEOUT_S)
    try:
        conn.connect()
        if strict and not peer_may_hear_token(conn.sock.getpeername()[0]):
            raise PeerRefused("peer")
        conn.sock.settimeout(REPLY_TIMEOUT_S)
        conn.request("POST", "/census", body=data, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + token, "Host": parts.netloc})
        response = conn.getresponse()
        response.read(1024)
        return response.status
    finally:
        conn.close()


def post_bounded(url: str, token: str, data: bytes, stop: Optional[Stop] = None, join_s: float = POST_JOIN_S) -> int:
    """post() in a daemon thread that is abandoned after join_s (or when `stop` is set): a name that never resolves or a
    reply that trickles in must not freeze the loop, and must not keep the process from ending on SIGTERM."""
    box: List[Any] = []

    def work() -> None:
        try:
            box.append(post(url, token, data))
        except Exception as exc:
            box.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    deadline = time.monotonic() + join_s
    while thread.is_alive() and not (stop is not None and stop.is_set()):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        thread.join(min(0.1, left))
    if not box:
        raise TimeoutError("post")
    if isinstance(box[0], Exception):
        raise box[0]
    return box[0]


def read_ps(stop: Optional[Stop] = None, timeout: float = PS_TIMEOUT_S) -> Optional[str]:
    """The process table of this user: one call with a fixed argument list and no shell, and no `e`, so the environment of
    another process is never even asked for. None when ps fails or takes longer than `timeout`."""
    argv = ["/bin/ps", "-U", str(os.getuid()), "-ww", "-o", "pid=,ppid=,etime=,command="]
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, close_fds=True)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, _ = child.communicate(timeout=0.25)  # in slices, so SIGTERM does not wait for a slow ps
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() > deadline or (stop is not None and stop.is_set()):
                child.kill()
                child.communicate()
                return None
    return out.decode("utf-8", "replace") if child.returncode == 0 else None


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)  # fixed phrases, counts, ids, error TYPES


PEER_NOTE = ("el nombre del buddy lleva a una direccion que no es de esta Mac ni de la tailnet: no le mando el token "
             "(insecure_url_ok si de verdad queres)")
REFUSALS = {
    400: "buddy no acepto el formato de la foto (400): sigo con el ritmo normal",
    403: "buddy rechazo el token (403): reintento en 10 min",
    404: "este buddy no conoce /census (daemon viejo): reintento en 5 min",
    413: "buddy dice que la foto es muy grande (413): sigo con el ritmo normal",
    429: "buddy dice que son demasiadas (429): reintento enseguida",
}


class State:
    def __init__(self) -> None:
        self.failures = 0  # passes in a row that the buddy did not answer
        self.note = ""  # the last answer that was a refusal, logged once
        self.ids: Optional[List[str]] = None
        self.more = 0
        self.ps_ok = True
        self.crash = ""
        self.token_note_at: Optional[float] = None
        self.warned: set = set()


class Census:
    """The loop's body. Everything that touches the machine or the network is a parameter, so a test can fake it."""

    def __init__(self, cfg: Config, *, claude_dir: Optional[str] = None, stop: Optional[Stop] = None,
                 read_ps: Optional[Callable[..., Optional[str]]] = None, post: Optional[Callable[..., int]] = None,
                 token: Optional[Callable[[str], Optional[str]]] = None, cwd: Optional[Callable[[int], Optional[str]]] = None,
                 session: Optional[Callable[[int, int, float], Optional[str]]] = None,
                 idle: Optional[Callable[[str, float], Optional[int]]] = None, log: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Optional[Callable[[float], bool]] = None, out: Any = None) -> None:
        here = sys.modules[__name__]  # the defaults are looked up now, so a test can swap the module's functions
        self.cfg = cfg
        self.stop = stop if stop is not None else Stop()
        self.read_ps, self.post, self.token = read_ps or here.read_ps, post or here.post_bounded, token or here.read_token
        self.cwd, self.log = cwd or here.cwd_of, log or here.log
        self.clock, self.monotonic, self.out = clock, monotonic, out
        self.sleep = sleep if sleep is not None else self.stop.wait  # True when asked to stop
        directory = claude_dir or os.path.join(HOME, ".claude")
        transcripts = Transcripts(directory)
        self.session = session if session is not None else (
            lambda pid, elapsed, now: registry_session(directory, pid, elapsed, now))
        self.idle = idle if idle is not None else transcripts.idle
        self.st = State()

    def snapshot(self, now: float) -> Optional[Dict[str, Any]]:
        text = self.read_ps(self.stop)
        if text is None:
            return None
        procs = parse_ps(text)
        del text  # the command lines go away here
        if len(procs) < MIN_PROCS_SANE:
            return None
        notes: List[str] = []
        # ps measured how long each process has run when it ANSWERED, and the registry's start times are compared with
        # that moment, not with the start of the pass (on a busy Mac they are seconds apart). The seq stays at the start.
        snap = scan(procs, self.cfg, max(now, self.clock()), self.cwd, self.session, self.idle, notes=notes)
        snap["seq"] = int(now * 1000)
        for note in notes:
            if note not in self.st.warned:
                self.st.warned.add(note)
                self.log(note)
        return snap

    def cycle(self, dry: bool = False) -> float:
        """One pass: look, then tell. Returns how long after the START of this pass the next one is due."""
        cfg, st = self.cfg, self.st
        snap = self.snapshot(self.clock())
        if snap is None:  # nothing is posted: a pass that cannot see must not wipe the screen
            if st.ps_ok:
                self.log("no pude leer la lista de procesos: no mando nada")
            st.ps_ok = False
            return cfg.interval
        if not st.ps_ok:
            self.log("la lista de procesos volvio")
        st.ps_ok = True
        ids = [r["id"] for r in snap["rows"]]
        if ids != st.ids or snap["more"] != st.more:
            self.log("corridas: %d (+%d)%s" % (len(ids), snap["more"], "".join(" " + i for i in ids)))
            st.ids, st.more = ids, snap["more"]
        if dry:
            print(json.dumps(snap, indent=1), file=self.out)
            return cfg.interval
        token = self.token(cfg.token_file)
        if token is None:
            if st.token_note_at is None or self.monotonic() - st.token_note_at >= TOKEN_NOTE_EVERY_S:
                self.log("sin token usable (tiene que ser un archivo tuyo, 0600, de 16+ caracteres): no mando nada")
                st.token_note_at = self.monotonic()
            return cfg.interval
        try:
            status = self.post(cfg.url, token, encode(snap), stop=self.stop)
        except Exception as exc:  # the TYPE only: the text of an error can carry data
            return cfg.interval if self.stop.is_set() else self.unanswered(type(exc).__name__)
        return self.answered(status)

    def answered(self, status: int) -> float:
        cfg, st = self.cfg, self.st
        if status == 200:
            if st.failures or st.note:
                self.log("buddy volvio")
            st.failures, st.note = 0, ""
            return cfg.interval
        if status not in REFUSALS:  # a 5xx or anything unexpected: as if it did not answer
            return self.unanswered("http %d" % status)
        st.failures = 0  # it answers, it just does not take this: not a network problem
        if st.note != str(status):
            st.note = str(status)
            self.log(REFUSALS[status])
        if status in REFUSED_WAIT_S:
            return REFUSED_WAIT_S[status]
        return cfg.ttl / 3.0 if status == 429 else cfg.interval  # 400 and 413: each pass rebuilds the body

    def unanswered(self, what: str) -> float:
        st = self.st
        st.failures += 1
        if st.failures == 1:
            self.log(PEER_NOTE if what == PeerRefused.__name__ else "buddy no responde (%s)" % what)
        return max(self.cfg.interval, BACKOFF_S[min(st.failures, len(BACKOFF_S)) - 1])

    def farewell(self) -> None:
        """SIGTERM: empty the group at once instead of letting the rows run out their ttl. Gives up after 1.5 s."""
        cfg = self.cfg
        if self.st.note in ("403", "404"):  # nothing of ours is there, and one more 403 would feed the lockout
            return
        token = self.token(cfg.token_file)
        if token is None:
            return
        data = encode({"v": 1, "source": cfg.source, "ttl": FAREWELL_TTL_S, "more": 0, "rows": []})
        try:
            self.post(cfg.url, token, data, join_s=FAREWELL_JOIN_S)
        except Exception:
            pass


def run(env: Dict[str, str], once: bool = False, dry: bool = False, **parts: Any) -> int:
    """`parts` replaces pieces of Census (tests only)."""
    if env.get("CLAUDE_BUDDY_CENSUS", "").strip().lower() in OFF:
        return 0
    cfg = load_config(env.get("CLAUDE_BUDDY_CENSUS_CONFIG") or DEFAULT_CONFIG, env)
    stop = parts.pop("stop", None) or Stop()
    census = Census(cfg, stop=stop, claude_dir=env.get("CLAUDE_CONFIG_DIR") or None, **parts)
    for warning in cfg.warnings:
        census.log("config: " + warning)
    if not dry:
        problem = url_problem(cfg.url, cfg.insecure_url_ok)
        if problem:
            census.log(problem)
            return 0
    census.log("census arranca: cada %g s, %d trabajos, corridas de Claude %s"
               % (cfg.interval, len(cfg.jobs), "si" if cfg.claude_runs else "no"))
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            saved[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            started = census.monotonic()
            try:
                wait = census.cycle(dry)
            except Exception as exc:  # a bug must not turn into a crash loop; the TYPE is logged, and once per kind
                if once:
                    raise
                if census.st.crash != type(exc).__name__:
                    census.st.crash = type(exc).__name__
                    census.log("fallo inesperado (%s): sigo" % census.st.crash)
                wait = cfg.interval
            if once:
                return 0
            if census.sleep(max(0.0, started + wait - census.monotonic())):  # from the START of this pass, not its end
                break
        if not dry:
            census.farewell()
        return 0
    finally:
        for sig, handler in saved.items():
            if handler is not None:
                signal.signal(sig, handler)


def main(argv: List[str]) -> int:
    args = argv[1:]
    if args not in (["run"], ["once"], ["once", "--print"]):
        print("usage: census.py run | once [--print]", file=sys.stderr)
        return 2
    return run(dict(os.environ), once=args[0] == "once", dry="--print" in args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
