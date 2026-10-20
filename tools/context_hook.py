#!/usr/bin/env python3
"""Each Claude Code session on the Mac tells the Claude Buddy screen what it is doing.

The Hardware Buddy protocol only carries totals (how many sessions, how many running) and the last
lines of the newest session, so with several sessions the screen could only say "3 trabajan". This
hook runs in every session (the terminal ones and the ones inside Claude desktop, which read the same
settings.json) and posts one line per event to the buddy's Hermes listener (`POST /session`, same
token as tools/cli_hook.py): the project (the folder's name), the tool about to run and a short
summary of it, or "idle" when the turn ends. The daemon keeps a board of live sessions and the page
lists them: "claude-buddy · Bash pytest -q · 0:12".

    {"hooks": {
        "PreToolUse":       [{"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}],
        "Stop":             [{"hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}],
        "SessionEnd":       [{"hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}]}}

Keep the `2>/dev/null; exit 0`: Python exits with code 2 when the script file does not exist (a moved folder, a
checkout without tools/, a restored backup) and Claude Code reads 2 as "block": PreToolUse would refuse every tool
call of every session and UserPromptSubmit would erase the prompt. The script itself always exits 0, but only the
command line can cover a script that never starts.

Besides what the session does, every report can carry what the Claude desktop sidebar calls it, so the
screen can show WHICH conversations are alive instead of folder names (most sessions run in the home
folder, so the folder says nothing):

  title   the conversation's title (at most 60 characters)
  group   the sidebar group it sits in, "Sesiones" when it sits in none (at most 40)
  parent  the id of the thread that started it, when that thread is known

Terminal sessions that Claude desktop does not know get none of the three (the screen falls back to the
folder's name and the group "Terminal"). They are found by the hook's session id, which is the
`cliSessionId` of the desktop's session file, and nothing else is guessed.

On Stop the report also carries `said`: up to 300 characters of the reply the session just ended its turn
with (the start and the end of it, markdown flattened, code blocks and anything that looks like a secret
hidden; the secret filter is best effort, not a boundary). Without `last_assistant_message` in the hook
input the hook reads the end of the session's transcript. A failure to find the text sends NO `said`
(the screen keeps what it has); `said` is "" only for a final message that verifiably has no text.

It never changes what Claude Code does: it prints nothing and exits 0 whatever happens (buddy off, no
token, timeout), and it gives up fast so a slow network cannot slow a session down: 1.5 s to connect, 2 s to
wait for a reply and, whatever the network does (a name that never resolves, a reply that trickles in), a
watchdog that ends the process at 2.5 s (Claude Code's own limit is 5). After a failed attempt the buddy is
left alone for 30 seconds: events that say "working" (every tool call) do not even try, so a Pi that is off or
off the tailnet costs a session one slow call in thirty seconds and not one per tool call. "Stop" and
"SessionEnd" always try, because a lost "finished" would leave a thread showing "working". Options as
environment variables:

    CLAUDE_BUDDY_URL             default http://buddy:8766
    CLAUDE_BUDDY_TOKEN_FILE      default ~/.config/claude-buddy/hermes.token, must be 0600 and yours
    CLAUDE_BUDDY_CONTEXT=off     disable the hook
    CLAUDE_BUDDY_THREADS=off     no title, group or parent (the hook then reads nothing of Claude desktop's)
    CLAUDE_BUDDY_SAID=off        never send `said` and never open the transcript
    CLAUDE_BUDDY_SAID_EXCLUDE    folder names, separated by commas, whose sessions never send `said`
                                 (the hook is global, so scheduled and headless sessions send it too)

Testing and relocation only (they point the hook at other folders, so they are as trusted as
CLAUDE_BUDDY_URL, which already decides where the token goes): CLAUDE_BUDDY_DESKTOP_DIR (default
~/Library/Application Support/Claude), CLAUDE_BUDDY_CACHE_DIR (default ~/.cache/claude-buddy) and
CLAUDE_CONFIG_DIR (default ~/.claude, the variable Claude Code itself honours).

What it reads for the thread fields, all read-only and all local to this Mac:
  - claude-code-sessions/<org>/<org>/local_<uuid>.json in the desktop folder: the session's `title`,
    `cliSessionId` and `spawnedFrom.sessionId`. These files also hold the connectors' tool schemas, so the
    hook looks at the first keys and at `spawnedFrom` and parses nothing else of them unless it must.
  - claude_desktop_config.json: ONLY `preferences.epitaxyPrefs["dframe-group-scopes"][<org>/<org>]`, its
    `groups` and `assignments`. The rest of that file (connector settings, trusted folders) is never
    looked at, cached, logged or sent.
  - ~/.claude/sessions/<pid>.json: Claude Code's own list of live processes, only to learn which desktop
    session a hook id belongs to (`sessionId`, `hostSessionId`).
The only things it writes are two small files in ~/.cache/claude-buddy (folder 0700, files 0600, ours only):
threads.json, the index cache (hook id -> desktop id, title and parent, kept 30 seconds before it is checked
again; it holds no group names and nothing else from the config) and down_until, the time until which the
buddy is not tried after a failure. Titles and group names reach the Pi the way folder names and command
summaries already do: over the plain HTTP of the token's listener.

Standard library only; runs on the Python 3.9 that ships with macOS (so no `X | Y` annotations, no match).
"""
import http.client
import json
import os
import re
import stat
import string
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_hook import summarize  # noqa: E402  (the same one-line summary the permission hook shows)

DEFAULT_URL = "http://buddy:8766"
DEFAULT_TOKEN_FILE = "~/.config/claude-buddy/hermes.token"
CONNECT_TIMEOUT_S = 1.5
REPLY_TIMEOUT_S = 2.0  # per wait on the socket; the real bound on the whole run is DEADLINE_S
DEADLINE_S = 2.5  # the watchdog: the process is gone by then, whatever the network or the disk is doing
DOWN_S = 30.0  # after a failed post, reports that say "working" skip the buddy for this long
MIN_TOKEN_CHARS = 16
MAX_STDIN = 1 << 20
MAX_PROJECT_CHARS, MAX_TOOL_CHARS, MAX_WHAT_CHARS = 40, 40, 120  # what the daemon keeps; the body stays small
MAX_POST_BYTES = 3900  # the listener refuses bodies over 4096 bytes
OFF = ("off", "0", "no")
# hook event -> what the board should know
STATES = {"PreToolUse": "working", "UserPromptSubmit": "working", "Stop": "idle", "SubagentStop": None,
          "SessionEnd": "gone"}


def _off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in OFF


def project_name(cwd: Any) -> str:
    """The folder's name, which is how the owner knows their sessions apart ("claude-buddy", "relay")."""
    if not isinstance(cwd, str) or not cwd:
        return ""
    path = Path(cwd)
    if path == Path.home():
        return "~"
    return (path.name or str(path))[:MAX_PROJECT_CHARS]


# -- what the session said ---------------------------------------------------------------------------------

MAX_SAID_CHARS = 300
SAID_HEAD, SAID_GAP, SAID_TAIL = 200, " … ", 97  # 200 + 3 + 97 = 300: the headline first, the question last
RAW_HEAD, RAW_TAIL = 4000, 2000  # of a reply only this much is ever looked at: every step below is linear in it
REDACTED = "[oculto]"
TAIL_WINDOWS = (64 << 10, 256 << 10, 1 << 20, 4 << 20)  # transcript lines of 1.2 MB exist: grow the window

_LINK = re.compile(r"\[([^\]\n]{1,200})\]\([^)\s]{1,500}\)")
_PEM = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----.*?(?:-----END [A-Z ]{0,40}PRIVATE KEY-----|\Z)", re.S)
_PIECES = re.compile(r"[/.:=,;@?&]+")
_WORD = frozenset(string.ascii_letters + string.digits + "_-")
_TOKENISH = _WORD | frozenset("._~+/=")
_BASE64 = frozenset(string.ascii_letters + string.digits + "/+")
_EDGE = "\"'()[]{}<>,;:!?*"  # what wraps a word in prose
_SECRET_PREFIXES = (("sk-", 16), ("pk-", 16), ("rk-", 16), ("ghp_", 12), ("gho_", 12), ("ghs_", 12), ("ghu_", 12),
                    ("github_pat_", 12), ("glpat-", 12), ("xoxa-", 12), ("xoxb-", 12), ("xoxp-", 12),
                    ("xoxr-", 12), ("xoxs-", 12), ("AKIA", 12), ("ASIA", 12))
_SECRET_NAMES = ("password", "passwd", "passphrase", "contrase", "secret", "token", "api_key", "apikey", "access_key",
                 "private_key", "credential")
_SECRET_WORDS = ("bearer", "token", "authorization", "password", "passwd", "passphrase", "contraseña", "contrasena",
                 "secret", "apikey", "api_key")


def _drop_fences(text: str) -> str:
    """A code block is not something to read across a room: "(código)" stands for it."""
    out: List[str] = []
    pos = 0
    while True:
        start = text.find("```", pos)
        if start < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:start])
        out.append(" (código) ")
        end = text.find("```", start + 3)
        if end < 0:
            break  # an open block runs to the end
        pos = end + 3
    return "".join(out)


def _flatten(line: str) -> str:
    """One line of markdown as plain words: no heading marks, quote marks or bold; bullets become a dot.
    Only real emphasis goes (`**`); a bare `__init__.py` keeps its underscores."""
    line = " ".join("".join(ch if ch.isprintable() else " " for ch in line).split())
    if line.startswith("#"):
        marks = len(line) - len(line.lstrip("#"))
        if marks <= 6 and line[marks:marks + 1] == " ":
            line = line[marks:].lstrip()
    elif line.startswith(("- ", "* ", "+ ")):
        line = "• " + line[2:].lstrip()
    elif line.startswith("> "):
        line = line[2:].lstrip()
    return line.replace("**", "").replace("`", "")


def _is_slug(piece: str) -> bool:
    """words_joined_like_this, Project-Names-With-2025 or MAX_SIZE_300: identifiers, not keys."""
    parts = [p for p in piece.replace("-", "_").split("_") if p]
    return len(parts) >= 3 and all(p.isdigit() or (p.isalpha() and (p.islower() or p.istitle() or p.isupper())) for p in parts)


def _mixed_run(piece: str) -> bool:
    """32 or more letters and digits together (a hash, a key), not a word and not an identifier."""
    if len(piece) < 32 or not all(c in _WORD for c in piece):
        return False
    return any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece) and not _is_slug(piece)


def _credential(value: str) -> bool:
    """The value of a password=... / token: ... pair, as opposed to a path, a variable or a placeholder."""
    if len(value) < 8 or value.startswith(("/", "~", "$", "<", "{", "(", "[", "http")):
        return False
    return (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)) or len(value) >= 16


def _scrub_token(token: str) -> str:
    core = token.strip(_EDGE)
    if len(core) < 10:
        return token
    if "://" in core and "@" in core:  # https://user:password@host
        authority = core.partition("://")[2].split("/", 1)[0]
        userinfo = authority.rpartition("@")[0]
        if ":" in userinfo:
            return token.replace(userinfo, REDACTED, 1)
    for sep in ("=", ":"):  # name=value / name:value, where the name says it is a secret
        name, found, value = core.partition(sep)
        norm = name.lower().replace("-", "_").lstrip("_$")
        if found and value and (any(w in norm for w in _SECRET_NAMES) or norm in ("pass", "pwd", "auth")):
            if _credential(value.strip("\"'")):
                return token.replace(value, REDACTED, 1)
    for prefix, room in _SECRET_PREFIXES:
        rest = core[len(prefix):]
        if core.startswith(prefix) and len(rest) >= room and all(c in _WORD for c in rest):
            return token.replace(core, REDACTED, 1)
    if core.startswith("eyJ") and core.count(".") == 2:  # a JWT
        if all(len(p) >= 8 and all(c in _WORD for c in p) for p in core.split(".")):
            return token.replace(core, REDACTED, 1)
    if (len(core) == 40 and all(c in _BASE64 for c in core) and ("/" in core or "+" in core)
            and any(c.isdigit() for c in core) and any(c.isupper() for c in core) and any(c.islower() for c in core)):
        return token.replace(core, REDACTED, 1)  # an AWS secret key
    for piece in _PIECES.split(core):  # a hash or key inside a path, a url or a sentence
        if _mixed_run(piece):
            token = token.replace(piece, REDACTED, 1)
    return token


def _scrub_line(line: str) -> str:
    tokens = [_scrub_token(t) for t in line.split(" ")]
    for i in range(len(tokens) - 1):  # "password: hunter2hunter2", "Bearer abc...", "Authorization: Bearer abc..."
        word = tokens[i].strip("\"'()[]{}<>,;*")
        marked = word.endswith((":", "="))
        word = word.rstrip(":=").lower()
        if word not in _SECRET_WORDS:
            continue
        j = i + 1
        if tokens[j].lower() in ("bearer", "basic") and j + 1 < len(tokens):
            j += 1
        shown = tokens[j].strip(_EDGE)
        looks_long = len(shown) >= 16 and all(c in _TOKENISH for c in shown) and any(c.isdigit() for c in shown)
        if (_credential(shown) if marked else looks_long) and not shown.startswith("["):
            tokens[j] = tokens[j].replace(shown, REDACTED, 1)
    return " ".join(tokens)


def scrub(text: str) -> str:
    """Hide what looks like a secret. Best effort, linear in the text, and no boundary: it also fires on
    UUID-shaped ids. The real controls are CLAUDE_BUDDY_SAID=off and the peer check in post()."""
    text = _PEM.sub(REDACTED, text)
    return "\n".join(_scrub_line(line) for line in text.split("\n"))


def shorten(text: str) -> str:
    """At most MAX_SAID_CHARS: the first part and the last part, each cut at a word."""
    if len(text) <= MAX_SAID_CHARS:
        return text
    head, tail = text[:SAID_HEAD], text[-SAID_TAIL:]
    cut = max(head.rfind(" "), head.rfind("\n"))
    head = head[:cut] if cut > SAID_HEAD // 2 else head  # never end the head in the middle of a word
    gaps = [i for i in (tail.find(" "), tail.find("\n")) if i >= 0]
    cut = min(gaps) if gaps else -1
    tail = tail[cut + 1:] if 0 <= cut < SAID_TAIL // 2 else tail  # nor start the tail in the middle of one
    return head.rstrip() + SAID_GAP + tail.lstrip()


def _tidy(part: str) -> str:
    text = _LINK.sub(r"\1", _drop_fences(part.replace("\r\n", "\n").replace("\r", "\n")))
    lines: List[str] = []
    for line in text.split("\n"):
        line = _flatten(line)
        if line or (lines and lines[-1]):  # no blank lines at the start, at most one in a row
            lines.append(line)
    return scrub("\n".join(lines).strip())


def clean_said(raw: Any) -> str:
    """A reply as the screen shows it: bounded first (nothing below looks at more than RAW_HEAD + RAW_TAIL
    characters), then markdown flattened, secrets hidden, and cut to MAX_SAID_CHARS."""
    if not isinstance(raw, str):
        return ""
    if len(raw) > RAW_HEAD + RAW_TAIL:
        text = _tidy(raw[:RAW_HEAD]) + SAID_GAP + _tidy(raw[-RAW_TAIL:])
    else:
        text = _tidy(raw)
    return shorten(text.strip())


# The end of the transcript, for a Stop input without `last_assistant_message`.
_MORE = object()


def _is_turn_boundary(rec: Dict[str, Any]) -> bool:
    """A user record that is not just tool results starts a turn (a typed prompt, a task notification, a /command)."""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return True


def _texts(rec: Dict[str, Any]) -> List[str]:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    out: List[str] = []
    for block in content if isinstance(content, list) else []:
        # thinking and tool_use blocks are never said out loud
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) \
                and block["text"].strip():
            out.append(block["text"])
    return out


def _done(texts: List[List[str]], seen: bool, stop_reason: Optional[str]) -> Optional[str]:
    """The text of the final message, "" when that message verifiably has none, None when we cannot tell.
    A message that stopped to call a tool is not the end of the turn: with the transcript lagging behind the
    hook (the docs warn about it) it would be an earlier message of the same turn."""
    if not seen or stop_reason == "tool_use":
        return None
    return "\n".join(t for part in reversed(texts) for t in part).strip()


def _scan(lines: List[bytes], cut_at_start: bool) -> Any:
    """Walk back from the end. The final message is the last real assistant message (the rule Claude Code
    uses for `last_assistant_message`): its text blocks joined with a newline. _MORE: the window ran out."""
    group_id: Optional[str] = None
    group_stop: Optional[str] = None
    seen = False
    texts: List[List[str]] = []  # newest line first
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue  # a half-written last line (the transcript is written asynchronously) or a cut one
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue  # subagents live in their own files, but never trust that
        kind = rec.get("type")
        if kind == "user":
            if seen or _is_turn_boundary(rec):
                return _done(texts, seen, group_stop)
            continue  # a tool_result between the lines of the same turn
        if kind != "assistant" or rec.get("isApiErrorMessage"):
            continue  # attachments, system, queue-operation, last-prompt... are noise between the lines that matter
        message = rec.get("message") or {}
        if message.get("model") == "<synthetic>":
            continue  # "No response requested.", API error placeholders
        mid = message.get("id")
        if not seen:
            seen, group_id, group_stop = True, mid or "", message.get("stop_reason")
        elif mid != group_id:
            return _done(texts, seen, group_stop)  # an earlier message: the final one is complete
        texts.append(_texts(rec))
    return _MORE if cut_at_start else _done(texts, seen, group_stop)


def last_said_from_transcript(path_text: Any) -> Optional[str]:
    """What the last assistant message of the last turn said; "" if it verifiably said nothing; None when
    it cannot be known (never raises)."""
    try:
        path = Path(os.path.expanduser(path_text))
        info = path.stat()
        if path.suffix != ".jsonl" or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        size = info.st_size
        for window in TAIL_WINDOWS:
            start = max(0, size - window)
            with open(str(path), "rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]  # the first line is most likely cut in half
            found = _scan(lines, cut_at_start=start > 0)
            if found is not _MORE:
                return found
            if start == 0:
                break
    except Exception:
        pass
    return None


def said_for(hook: Dict[str, Any], project: str) -> Optional[str]:
    """What to post as `said` for a Stop event, or None to post none (the screen then keeps what it has)."""
    if _off("CLAUDE_BUDDY_SAID"):
        return None
    names = os.environ.get("CLAUDE_BUDDY_SAID_EXCLUDE", "").split(",")
    if project and project in {n.strip()[:MAX_PROJECT_CHARS] for n in names if n.strip()}:
        return None
    raw = hook.get("last_assistant_message")
    if not (isinstance(raw, str) and raw.strip()):
        raw = last_said_from_transcript(hook.get("transcript_path"))
        if raw is None:
            return None
    return clean_said(raw)


def build_report(hook: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What to post for this hook event, or None when the event says nothing worth posting."""
    event = hook.get("hook_event_name")
    state = STATES.get(event if isinstance(event, str) else "")
    session = hook.get("session_id")
    if state is None or not isinstance(session, str) or not session:
        return None
    report: Dict[str, Any] = {"id": session[:64], "project": project_name(hook.get("cwd")), "state": state}
    if event == "PreToolUse":
        tool = hook.get("tool_name")
        tool = tool if isinstance(tool, str) else ""
        report["tool"] = tool[:MAX_TOOL_CHARS]
        what = summarize(tool, hook.get("tool_input"), None)
        report["what"] = what.split("\n", 1)[0][:MAX_WHAT_CHARS]
    elif event == "UserPromptSubmit":
        report["tool"], report["what"] = "", "nuevo pedido"
    elif event == "Stop":
        said = said_for(hook, report["project"])
        if said is not None:
            report["said"] = said
    return report


# -- which conversation it is: title, group and parent from Claude desktop's own files ---------------------

CACHE_TTL_S = 30.0  # an index entry younger than this is used as is; older is checked against the file
MISS_TTL_S = 10.0  # an id no desktop session claims is not searched for again inside this time
HEAD_BYTES = 4096  # cliSessionId sits in the first bytes of every desktop session file
HEAD_PARSE_BYTES = 16384  # the top-level keys we want sit in this much of the start
SPAWNED_BYTES = 8192  # room for the small object after "spawnedFrom"
MAX_FILE_BYTES = 8 << 20  # desktop session files are about 0.8 MB; refuse anything absurd
MAX_CONFIG_BYTES = 1 << 20
MAX_CACHE_BYTES = 1 << 18
MAX_REGISTRY_FILES = 128
MAX_ANCESTORS = 4
MAX_CACHE_ENTRIES = 64
CACHE_KEEP_S = 86400.0
MISS_KEEP_S = 3600.0
TITLE_CHARS, GROUP_CHARS = 60, 40
NO_GROUP = "Sesiones"  # a desktop session the sidebar shows outside any custom group
ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # the hook's session ids and the board's (same as the daemon)
LOCAL_RE = re.compile(r"local_[0-9a-fA-F-]{36}")  # the desktop's own session ids
SCOPE_PART = r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}"  # a folder of the desktop's sessions: no dots-only names
SCOPE_RE = re.compile(SCOPE_PART + "/" + SCOPE_PART)
SCOPE_PART_RE = re.compile(SCOPE_PART)


def _root() -> Path:
    env = os.environ.get("CLAUDE_BUDDY_DESKTOP_DIR")
    return Path(env) if env else Path.home() / "Library" / "Application Support" / "Claude"


def _cache_dir() -> Path:
    env = os.environ.get("CLAUDE_BUDDY_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "claude-buddy"


def _registry_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env) if env else Path.home() / ".claude") / "sessions"


def _one_line(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scope_dirs(root: Path) -> List[Tuple[str, Path]]:
    """("orgA/orgB", folder) for every <orgA>/<orgB> under claude-code-sessions (exactly two levels)."""
    out: List[Tuple[str, Path]] = []
    try:
        with os.scandir(str(root / "claude-code-sessions")) as outer:
            for a in sorted(outer, key=lambda e: e.name):  # sorted: the same answer on every filesystem
                if not SCOPE_PART_RE.fullmatch(a.name) or not a.is_dir():
                    continue
                with os.scandir(a.path) as inner:
                    for b in sorted(inner, key=lambda e: e.name):
                        if SCOPE_PART_RE.fullmatch(b.name) and b.is_dir():
                            out.append((a.name + "/" + b.name, Path(b.path)))
    except OSError:
        pass
    return out


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _head_fields(data: bytes) -> Dict[str, str]:
    """sessionId, cliSessionId and title: the string members of the top-level object, read one member at a
    time from the start as far as the start is complete. Values that run past the start end the walk, so a
    huge member never gets parsed."""
    found: Dict[str, str] = {}
    text = data[:HEAD_PARSE_BYTES].decode("utf-8", "ignore")
    decoder = json.JSONDecoder()
    pos = _skip_space(text, 1 if text[:1] == "\ufeff" else 0)
    if text[pos:pos + 1] != "{":
        return found
    pos += 1
    while True:
        pos = _skip_space(text, pos)
        if text[pos:pos + 1] == ",":
            pos = _skip_space(text, pos + 1)
        try:
            key, pos = decoder.raw_decode(text, pos)
            pos = _skip_space(text, pos)
            if not isinstance(key, str) or text[pos:pos + 1] != ":":
                break
            value, pos = decoder.raw_decode(text, _skip_space(text, pos + 1))
        except ValueError:
            break
        if key in ("sessionId", "cliSessionId", "title") and isinstance(value, str):
            found[key] = value
    return found


def _spawned_from(data: bytes) -> Optional[str]:
    """The desktop id of the thread that started this one: "" when the file says it has none, None when the
    file has the key but it cannot be read the quick way. The key sits near the END of the file, in a small
    object; decoding just that object spares the parse of the connector schemas that fill the rest."""
    marker = data.rfind(b'"spawnedFrom"')
    if marker < 0:
        return ""
    text = data[marker + len(b'"spawnedFrom"'):marker + SPAWNED_BYTES].decode("utf-8", "ignore")
    pos = _skip_space(text, 0)
    if text[pos:pos + 1] != ":":
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, _skip_space(text, pos + 1))
    except ValueError:
        return None
    if value is None:
        return ""
    sid = value.get("sessionId") if isinstance(value, dict) else None
    return sid if isinstance(sid, str) and LOCAL_RE.fullmatch(sid) else None


def _read_session(path: Path) -> Optional[Dict[str, Any]]:
    """{"sid", "cli", "title", "spawned", "key"} of one desktop session file, or None. The key is
    (mtime_ns, size) taken from the SAME open file as the bytes (the desktop replaces files by rename, so a
    read is never torn). The quick way first; a full parse only when the quick way did not find what it
    needs (a new session's small file, or a layout that moved)."""
    try:
        with open(str(path), "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                return None
            data = fh.read(MAX_FILE_BYTES + 1)
        key = (info.st_mtime_ns, info.st_size)
        head = _head_fields(data)
        spawned = _spawned_from(data)
        if "sessionId" in head and "title" in head and spawned is not None:
            fields = head
        else:  # the quick way came up short: parse the whole file, once
            whole = json.loads(data)
            if not isinstance(whole, dict):
                return None
            fields = {k: whole[k] for k in ("sessionId", "cliSessionId", "title") if isinstance(whole.get(k), str)}
            sp = whole.get("spawnedFrom")
            sid = sp.get("sessionId") if isinstance(sp, dict) else None
            spawned = sid if isinstance(sid, str) and LOCAL_RE.fullmatch(sid) else ""
        if not LOCAL_RE.fullmatch(fields.get("sessionId", "")):
            return None
        return {"sid": fields["sessionId"], "cli": fields.get("cliSessionId", ""),
                "title": _one_line(fields.get("title"), TITLE_CHARS), "spawned": spawned, "key": key}
    except Exception:
        return None


def _small_json(path: Path, limit: int) -> Optional[Dict[str, Any]]:
    try:
        with open(str(path), "rb") as fh:
            if os.fstat(fh.fileno()).st_size > limit:
                return None
            data = json.loads(fh.read(limit + 1))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _registry_host(session_id: str) -> Optional[str]:
    """~/.claude/sessions/<pid>.json is Claude Code's own list of live processes: `sessionId` is the hook's
    session id and `hostSessionId` the desktop's "local_<uuid>" (absent in terminal sessions). This finds a
    brand-new session before the desktop has written its cliSessionId. None when there is no answer."""
    directory = _registry_dir()
    try:
        names = sorted(n for n in os.listdir(str(directory)) if n.endswith(".json"))[:MAX_REGISTRY_FILES]
    except OSError:
        return None
    for name in names:
        entry = _small_json(directory / name, 16384)
        if entry and entry.get("sessionId") == session_id:
            host = entry.get("hostSessionId")
            return host if isinstance(host, str) and LOCAL_RE.fullmatch(host) else None
    return None


def _find_by_host(root: Path, host: str) -> Optional[Tuple[str, Path]]:
    for scope, directory in _scope_dirs(root):
        path = directory / (host + ".json")
        if path.is_file():
            return scope, path
    return None


def _find_by_head(root: Path, cli_id: str) -> Optional[Tuple[str, Path]]:
    """The session file whose cliSessionId is the hook's id, found by looking at the first bytes of each
    file (nothing is parsed: the files are big)."""
    needle = re.compile(b'"cliSessionId"\\s*:\\s*"' + re.escape(cli_id.encode("ascii")) + b'"')
    for scope, directory in _scope_dirs(root):
        try:
            with os.scandir(str(directory)) as entries:
                for e in sorted(entries, key=lambda e: e.name):  # a corrupt pair with one id must resolve the same everywhere
                    if not (e.name.startswith("local_") and e.name.endswith(".json")):
                        continue
                    try:
                        with open(e.path, "rb") as fh:
                            if needle.search(fh.read(HEAD_BYTES)):
                                return scope, Path(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _resolve(scope: str, path: Path) -> Optional[Dict[str, Any]]:
    """Title, parent and ancestors of one desktop session: its file, then the files of the threads that
    started it, up to MAX_ANCESTORS levels. The parent is named by its hook id (the board's id), taken from
    the nearest ancestor whose file has one."""
    current = _read_session(path)
    if current is None:
        return None
    entry: Dict[str, Any] = {"sid": current["sid"], "scope": scope, "title": current["title"], "parent": "",
                             "anc": [], "mt": current["key"][0], "sz": current["key"][1]}
    seen = {current["sid"]}
    for _ in range(MAX_ANCESTORS):
        psid = current["spawned"]
        if not psid or psid in seen:
            break
        seen.add(psid)
        parent = _read_session(path.parent / (psid + ".json"))
        if parent is None:
            break
        entry["anc"].append(psid)
        if not entry["parent"] and ID_RE.fullmatch(parent["cli"]):
            entry["parent"] = parent["cli"]
        current = parent
    return entry


def _group_for(root: Path, entry: Dict[str, Any]) -> Optional[str]:
    """The sidebar group's name: the session's own assignment, else the nearest ancestor's (an unassigned
    child shows under its parent's group in the sidebar), else NO_GROUP when the config says it has none.
    None when it cannot be known (any trouble with the config): the screen then keeps the group it has, so a
    bad read never moves a row. Reads ONLY the groups and assignments of this folder's scope."""
    try:
        with open(str(root / "claude_desktop_config.json"), "rb") as fh:
            raw = fh.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        scope = json.loads(raw)["preferences"]["epitaxyPrefs"]["dframe-group-scopes"][entry["scope"]]
        groups, assignments = scope["groups"], scope["assignments"]
        if not isinstance(groups, list) or not isinstance(assignments, dict):
            return None
        names = {g["id"]: g["name"] for g in groups
                 if isinstance(g, dict) and isinstance(g.get("id"), str) and isinstance(g.get("name"), str)}
        for sid in [entry["sid"]] + list(entry["anc"]):
            group_id = assignments.get("code:" + sid)
            if group_id is None:
                continue
            name = names.get(group_id) if isinstance(group_id, str) else None
            return (_one_line(name, GROUP_CHARS) or None) if name is not None else None
        return NO_GROUP
    except Exception:
        return None


# The index cache. Shape: {"v": 1, "sessions": {<hook id>: {sid, scope, title, parent, anc, mt, sz, at}},
# "miss": {<hook id>: <epoch s>}}. It never holds a group name or anything from the desktop config.

def _valid_entry(entry: Any) -> bool:
    return (isinstance(entry, dict)
            and isinstance(entry.get("sid"), str) and bool(LOCAL_RE.fullmatch(entry["sid"]))
            and isinstance(entry.get("scope"), str) and bool(SCOPE_RE.fullmatch(entry["scope"]))
            and isinstance(entry.get("title"), str) and len(entry["title"]) <= TITLE_CHARS
            and isinstance(entry.get("parent"), str) and (entry["parent"] == "" or bool(ID_RE.fullmatch(entry["parent"])))
            and isinstance(entry.get("anc"), list) and len(entry["anc"]) <= MAX_ANCESTORS
            and all(isinstance(a, str) and LOCAL_RE.fullmatch(a) for a in entry["anc"])
            and isinstance(entry.get("mt"), int) and not isinstance(entry["mt"], bool)
            and isinstance(entry.get("sz"), int) and not isinstance(entry["sz"], bool)
            and _is_number(entry.get("at")))


def _load_cache() -> Dict[str, Any]:
    """The cache, or an empty one when the file is missing, not ours, readable by others, too big, not
    json or not shaped right. Entries that are not shaped right are dropped one by one."""
    empty: Dict[str, Any] = {"v": 1, "sessions": {}, "miss": {}}
    try:
        path = _cache_dir() / "threads.json"
        info = os.stat(str(path))
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 \
                or info.st_size > MAX_CACHE_BYTES:
            return empty
        data = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(data, dict) and data.get("v") == 1 and isinstance(data.get("sessions"), dict)
                and isinstance(data.get("miss"), dict)):
            return empty
        return {"v": 1,
                "sessions": {k: v for k, v in data["sessions"].items() if ID_RE.fullmatch(k) and _valid_entry(v)},
                "miss": {k: v for k, v in data["miss"].items() if ID_RE.fullmatch(k) and _is_number(v)}}
    except Exception:
        return empty


def _write_private(name: str, body: bytes) -> None:
    """Write one small file of ours into the cache folder: a private temporary file, then a rename, so a reader
    never sees half of it. The folder is made 0700 and must be ours (our own default folder is tightened when
    somebody else's umask made it too open). No lock: two hooks writing at once may lose one write, which only
    costs a lookup. Any trouble is swallowed."""
    directory = _cache_dir()
    tmp = directory / ("%s.%d.tmp" % (name, os.getpid()))
    try:
        os.makedirs(str(directory), mode=0o700, exist_ok=True)
        info = os.stat(str(directory))
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return
        if info.st_mode & 0o077 and "CLAUDE_BUDDY_CACHE_DIR" not in os.environ:
            os.chmod(str(directory), 0o700)  # our own default folder: tighten it, whoever made it
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(directory / name))
    except Exception:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass


def _save_cache(cache: Dict[str, Any], now: float) -> None:
    """Write the index cache. Any trouble is swallowed."""
    try:
        sessions = {k: v for k, v in cache["sessions"].items() if now - v["at"] < CACHE_KEEP_S}
        if len(sessions) > MAX_CACHE_ENTRIES:
            sessions = dict(sorted(sessions.items(), key=lambda kv: -kv[1]["at"])[:MAX_CACHE_ENTRIES])
        miss = {k: v for k, v in cache["miss"].items() if now - v < MISS_KEEP_S}
        if len(miss) > MAX_CACHE_ENTRIES:
            miss = dict(sorted(miss.items(), key=lambda kv: -kv[1])[:MAX_CACHE_ENTRIES])
        body = json.dumps({"v": 1, "sessions": sessions, "miss": miss}, separators=(",", ":")).encode("utf-8")
    except Exception:
        return
    _write_private("threads.json", body)


def _revalidate(root: Path, entry: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """An entry past its time: still good if its desktop file did not change, re-read if it did, gone if
    the file is gone."""
    path = root / "claude-code-sessions" / entry["scope"] / (entry["sid"] + ".json")
    try:
        info = os.stat(str(path))
    except OSError:
        return None
    if (info.st_mtime_ns, info.st_size) == (entry["mt"], entry["sz"]):
        entry["at"] = now
        return entry
    fresh = _resolve(entry["scope"], path)
    if fresh is not None:
        fresh["at"] = now
    return fresh


def _lookup(root: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """The desktop session of a hook id that is not in the cache: by the live-process list first, then by
    the first bytes of the desktop's files (also when the list says "no host": an older Claude Code may not
    write it)."""
    found = None
    host = _registry_host(session_id)
    if host:
        found = _find_by_host(root, host)
    if found is None:
        found = _find_by_head(root, session_id)
    return _resolve(found[0], found[1]) if found else None


def thread_info(session_id: Any, now: Optional[float] = None) -> Dict[str, str]:
    """{"title", "group", "parent"}: only the ones known, and {} for a terminal session Claude desktop does
    not know, for any failure and with CLAUDE_BUDDY_THREADS=off. Never raises."""
    try:
        if _off("CLAUDE_BUDDY_THREADS") or not isinstance(session_id, str) or not ID_RE.fullmatch(session_id):
            return {}
        now = time.time() if now is None else now
        root = _root()
        cache = _load_cache()
        sessions, miss = cache["sessions"], cache["miss"]
        entry = sessions.get(session_id)
        dirty = False
        if entry is not None:
            age = now - entry["at"]
            if age < 0 or age >= CACHE_TTL_S:
                entry = _revalidate(root, entry, now)
                dirty = True
        if entry is None:
            sessions.pop(session_id, None)
            missed = miss.get(session_id)
            if not dirty and missed is not None and 0 <= now - missed < MISS_TTL_S:
                return {}
            entry = _lookup(root, session_id)
            dirty = True
            if entry is None:
                miss[session_id] = now
                _save_cache(cache, now)
                return {}
            entry["at"] = now
            miss.pop(session_id, None)
        sessions[session_id] = entry
        if dirty:
            _save_cache(cache, now)
        out: Dict[str, str] = {}
        if entry["title"]:
            out["title"] = entry["title"]
        group = _group_for(root, entry)
        if group:
            out["group"] = group
        if entry["parent"] and entry["parent"] != session_id:
            out["parent"] = entry["parent"]
        return out
    except Exception:
        return {}


# -- the wire ----------------------------------------------------------------------------------------------

def peer_may_hear_said(address: Any) -> bool:
    """`said` is a piece of what Claude wrote, so it only goes to the machine itself or into the tailnet
    (100.64.0.0/10 and fd7a:115c:a1e0::/48). 100.64.0.0/10 is also carrier-grade NAT and other VPNs, so this
    proves "not the home LAN", not "Tailscale"."""
    import ipaddress  # only when a Stop is posted: the import costs a few milliseconds on every other event
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])  # IPv6 peers can carry a %scope
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is not "loopback" to Python 3.9 until unwrapped
        tailnet = ipaddress.ip_network("100.64.0.0/10" if ip.version == 4 else "fd7a:115c:a1e0::/48")
        return bool(ip.is_loopback or ip in tailnet)
    except ValueError:
        return False


def fit_body(report: Dict[str, Any]) -> bytes:
    """The request body, small enough for the listener (it refuses over 4096 bytes and the report would be
    lost, idle state included). json.dumps keeps its ASCII escapes: an emoji costs 12 bytes. When it is too
    big the optional parts go in this order: said, a shorter title, parent, a shorter group. id, project,
    state, tool and what are capped when the report is built and never touched."""
    report = dict(report)
    body = json.dumps(report).encode("utf-8")
    for key, keep in (("said", None), ("title", 24), ("parent", None), ("group", 20)):
        if len(body) <= MAX_POST_BYTES:
            break
        if key not in report:
            continue
        if keep is None:
            del report[key]
        else:
            report[key] = report[key][:keep]
        body = json.dumps(report).encode("utf-8")
    return body


def read_token(path_text: str) -> Optional[str]:
    """The token, only from a file that is ours and unreadable to others; otherwise nothing."""
    path = Path(os.path.expanduser(path_text))
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if len(token) >= MIN_TOKEN_CHARS else None


def post(url: str, token: str, report: Dict[str, Any]) -> None:
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=CONNECT_TIMEOUT_S)
    try:
        conn.connect()
        conn.sock.settimeout(REPLY_TIMEOUT_S)
        if "said" in report and not peer_may_hear_said(conn.sock.getpeername()[0]):
            report = {k: v for k, v in report.items() if k != "said"}  # the buddy is not on this machine or the tailnet
        conn.request("POST", "/session", body=fit_body(report),
                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                              "Host": parts.netloc})
        conn.getresponse().read()
    finally:
        conn.close()


# -- a buddy that does not answer: leave it alone for a while, and never wait long for it --------------------

DOWN_FILE = "down_until"
_NET = {"busy": False}  # True while the post is in flight: only then does a missed deadline mean "buddy is down"


def buddy_is_down(now: Optional[float] = None) -> bool:
    """True while the marker of a recent failure is in the future. Never raises. A marker that is not ours,
    is readable by others, is not a plain number or promises more than DOWN_S is ignored, so a stray file
    can never mute the screen for long."""
    try:
        now = time.time() if now is None else now
        path = _cache_dir() / DOWN_FILE
        info = os.stat(str(path))
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 64:
            return False
        until = float(path.read_text(encoding="ascii").strip())
        return now < until <= now + DOWN_S + 1.0
    except Exception:
        return False


def mark_buddy_down(now: Optional[float] = None) -> None:
    try:
        now = time.time() if now is None else now
        _write_private(DOWN_FILE, ("%.3f" % (now + DOWN_S)).encode("ascii"))
    except Exception:
        pass


def clear_buddy_down() -> None:
    try:
        os.unlink(str(_cache_dir() / DOWN_FILE))
    except OSError:
        pass


def _give_up() -> None:
    """The deadline. It runs in a timer thread, so it works even when the main thread is stuck where no timeout
    reaches it (the DNS lookup of the buddy's name, a reply that trickles in one byte at a time)."""
    try:
        if _NET["busy"]:
            mark_buddy_down()
    finally:
        os._exit(0)


def main() -> int:
    if _off("CLAUDE_BUDDY_CONTEXT"):
        return 0
    watchdog = threading.Timer(DEADLINE_S, _give_up)
    watchdog.daemon = True
    watchdog.start()
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN).decode("utf-8", "replace")
        hook = json.loads(raw) if raw.strip() else {}
        if not isinstance(hook, dict):
            return 0
        token = read_token(os.environ.get("CLAUDE_BUDDY_TOKEN_FILE", DEFAULT_TOKEN_FILE))
        if token is None:  # nobody to tell: do none of the work below (the transcript, the desktop's files)
            return 0
        event = hook.get("hook_event_name")
        if STATES.get(event if isinstance(event, str) else "") == "working" and buddy_is_down():
            return 0  # it did not answer a moment ago: a tool call is not worth another wait ("Stop" and "SessionEnd" still try)
        report = build_report(hook)
        if report is None:
            return 0
        if report["state"] != "gone":
            report.update(thread_info(report["id"]))
        _NET["busy"] = True
        try:
            post(os.environ.get("CLAUDE_BUDDY_URL", DEFAULT_URL), token, report)
        except (OSError, http.client.HTTPException):  # refused, unreachable, timed out, cut off: not there
            mark_buddy_down()
        else:
            clear_buddy_down()
        finally:
            _NET["busy"] = False
    except Exception:
        pass  # the screen is a convenience; the session never waits on it
    finally:
        watchdog.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
