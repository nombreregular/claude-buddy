"""Hermes Agent as a second source of approvals: a small HTTP listener for the LAN or Tailscale.

Hermes (the owner's agent) lives on another machine, so it cannot reach the screen's
loopback server, and it must not: POST /decide approves commands. This module opens a second
listener, on its own port and bound to the address you choose (the Pi's Tailscale IP, say), that
only knows how to queue a request in the same `ApprovalBroker` the screen answers and to report
the answer back. The page, the event stream and POST /decide are never served here, so nothing
on this port can approve anything; the person at the screen (or the keyboard) does.

    POST   /approval          {"id": "...", "tool": "Bash", "hint": "rm -rf x", "ttl": 900, "source": "hermes"}
                              202 {"ok": true, "id": ..., "key": "hermes:<id>", "poll": "/approval/<id>"}
                              `source` is optional: "hermes" (the default) or "cli", a Claude Code session in a
                              terminal (tools/cli_hook.py), so the screen names who is asking. Only "cli" may add
                              "always": "<the permission rule a third button would save>", and then the answer can
                              also be "always".
    GET    /approval/<id>     {"id": ..., "key": ..., "state": "pending" | "decided" | "withdrawn" | "expired",
                               "decision": null | "once" | "deny" | "always"}
                              `?wait=N` (0..30) holds the answer up to N seconds: long polling.
    DELETE /approval/<id>     Hermes gave up (its own timeout): the request leaves the screen.
    POST   /session           {"id": "<session id>", "project": "claude-buddy", "tool": "Bash", "what": "pytest -q",
                               "state": "working" | "idle" | "gone", "said": "...", "title": "...", "group": "...",
                               "parent": "<session id>"}  200 {"ok": true, "id": ..., "state": ...}
                              What a Claude Code session on the Mac is doing right now (tools/context_hook.py
                              posts it on every tool call and when a turn ends), so the screen can list the live
                              threads instead of only counting them. `said` (what the session last said, up to
                              300 characters), `title` (60), `group` (40, the sidebar group of Claude desktop) and
                              `parent` (the id of the thread that started this one) are optional strings with one
                              rule: left out keeps what the board has, "" clears it, any text replaces it. A value
                              that is not a string, or a parent that is not an id, is a 400. Same token; nothing
                              here can approve anything. What arrives in these fields is never written to a log.
    POST   /census            {"v": 1, "source": "mac", "seq": 1760793600123, "ttl": 60, "more": 0, "rows": [
                                 {"id": "run-quietud", "name": "quietud", "group": "Corridas", "state": "working",
                                  "elapsed": 3643, "idle": 538, "session": "<session id>", "project": "relay-x"}]}
                              200 {"ok": true, "rows": 1, "ttl": 60}, or {"ok": true, "stale": true, ...} for a snapshot
                              older than the last one of that source (`seq`).
                              What runs on the Mac without a hook speaking for it (headless `claude -p` runs and job
                              scripts), seen by tools/census.py: a whole snapshot that replaces the previous one of its
                              `source`, believed for `ttl` seconds (5 to 120). At most 8 rows (`more` counts the ones
                              that did not fit); ids start with run- or job-; `elapsed` and `idle` are whole seconds
                              and, like ttl and more, are clamped instead of refused. Wrong types or shapes are a
                              400. Its own request budget (CENSUS_RATE_MAX), so the census and the hooks cannot
                              starve each other; bad tokens count in the shared lockout. Same token; nothing here
                              can approve anything; names and folders are never written to a log.
    POST   /notice            {"title": "La tanda terminó", "body": "...", "kind": "info" | "ok" | "warn", "ttl": 300,
                               "wake": false, "id": "tanda-19", "source": "hermes"}
                              200 {"ok": true, "id": ..., "active": 2}
                              A short message for the screen (the "Avisos" group above the threads): `title` (1 to
                              60 characters once cleaned, the only required key), `body` (up to 4 lines and 240
                              characters), `kind` (the tint), `ttl` (seconds it stays unless somebody taps "Listo":
                              clamped to 10..3600, default 300), `wake` (true wakes the dim night screen, see UIHub.wake;
                              false waits until somebody does), `id` (the same id replaces the notice that has it) and
                              `source` (up to 24 characters, shown as "de <source>"). A wrong type or shape is a 400,
                              a number out of range is clamped, unknown keys are ignored. Three at a time: a fourth
                              evicts the oldest. Its own request budget (NOTICE_RATE_MAX), so a flood of notices
                              cannot starve the hooks; bad tokens count in the shared lockout. Same token; nothing
                              here can approve anything; the text is never written to a log. In memory only: a
                              daemon restart clears them.
    GET    /notice/<id>       {"id": ..., "state": "shown" | "dismissed" | "expired" | "withdrawn" | "evicted" | "unknown"}
                              How a notice stands; a finished one is remembered for an hour (50 at most).
    DELETE /notice/<id>       {"ok": true, "state": ...}: Hermes takes it back ("withdrawn"). Harmless again, or for
                              an id the board does not know ("unknown"); one that already ended keeps its state.
    GET    /healthz           "ok", no token needed, says nothing else.

Long polling, not callbacks: Hermes asks `GET /approval/<id>?wait=30` in a loop until the state is
not "pending". The Pi never opens a connection towards a URL that arrived in a request body (so a
stolen token cannot turn it into a scanner of the LAN), Hermes needs no listener of its own, and it
works the same through NAT, Tailscale or a plain LAN. A body with "callback" is refused with 400 so
nobody waits for a call that never comes. A request that nobody answers expires after `ttl` seconds
(15 minutes by default) and leaves the screen by itself.

Security, because this port faces a network:
* Its own token (`Authorization: Bearer <token>`), at least 16 characters, compared in constant time,
  never logged. It is not the page's token; the two sides cannot impersonate each other.
* `Host` must name the address the client actually connected to (or this machine's own name), the
  usual guard against DNS rebinding. Any `Origin` header is refused outright: Hermes is a script, not
  a browser, and a browser is exactly what must not reach this.
* JSON content type required, 4 KB bodies, `id` of 1..64 URL-safe characters, `tool`/`hint` cut to the
  same lengths as a Claude prompt. Per client: a request budget per minute, a lockout after a few bad
  tokens, a cap on open connections and on pending requests.
Pure standard library, like the rest of the core and the screen.
"""
import asyncio
import hmac
import json
import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, Optional
from urllib.parse import parse_qs

from buddy_core import (CENSUS_TTL_DEFAULT_S, CENSUS_TTL_MAX_S, CENSUS_TTL_MIN_S, MAX_CENSUS_MORE, MAX_CENSUS_ROWS,
                        MAX_CENSUS_SECONDS, NOTICE_ID_PATTERN, NOTICE_KINDS, NOTICE_SOURCE, NOTICE_TTL_DEFAULT_S,
                        TASK_STATES, Approval, ApprovalBroker, NoticeBoard, TaskBoard)
from buddy_ui import HttpError, HttpServer, Request

log = logging.getLogger("buddy.hermes")

SOURCE = "hermes"
SOURCES = (SOURCE, "cli")  # who may queue here: Hermes, and Claude Code in a terminal (tools/cli_hook.py)
DEFAULT_PORT = 8766
TOKEN_ENV = "BUDDY_HERMES_TOKEN"
MIN_TOKEN_CHARS = 16
ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_TOOL_CHARS, MAX_HINT_CHARS = 80, 500  # what buddy_core keeps of a Claude prompt
MAX_ALWAYS_CHARS = 120  # the rule behind "always" is shown whole or not offered
DEFAULT_TTL_S, MIN_TTL_S, MAX_TTL_S = 900.0, 1.0, 3600.0
MAX_WAIT_S = 30.0  # the longest a GET ?wait= may hold the connection
DONE_TTL_S = 600.0  # how long an answered request can still be asked about
MAX_DONE = 256
MAX_PENDING = 16
MAX_CLIENTS = 16  # open connections, long polls included
RATE_WINDOW_S = 60.0
RATE_MAX = 300  # requests per client per window: the context hook posts on every tool call of every session
CENSUS_RATE_MAX = 60  # POST /census per client per window, counted apart from RATE_MAX (it posts about 4 a minute)
NOTICE_RATE_MAX = 30  # every /notice request per client per window, counted apart from the other two
AUTH_FAIL_MAX = 5  # bad tokens per client per window before a lockout (shared by every route)
CENSUS_SOURCE_PATTERN = re.compile(r"[a-z0-9_-]{1,24}")  # the machine that posts a census
CENSUS_DEFAULT_SOURCE = "mac"
CENSUS_ID_PREFIXES = ("run-", "job-")  # so a census row can never take the id of a session
MAX_CENSUS_SEQ = 2**63 - 1
STATES = ("pending", "decided", "withdrawn", "expired")


@dataclass
class Record:
    approval: Approval
    state: str = "pending"
    decision: Optional[str] = None
    finished: float = 0.0  # loop time when it stopped being pending
    event: asyncio.Event = field(default_factory=asyncio.Event)
    timer: Optional[asyncio.TimerHandle] = None

    def as_json(self) -> Dict[str, Any]:
        return {"id": self.approval.id, "key": self.approval.key, "tool": self.approval.tool,
                "state": self.state, "decision": self.decision}


def _whole(value: Any) -> bool:
    """A JSON integer: not a float, and not a boolean (which Python counts as 0 and 1)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _checked_row(raw: Any, seen: set) -> Dict[str, Any]:
    """One row of a census snapshot, checked; 400 for a wrong type or shape. Numbers are clamped to 0..31 days."""
    if not isinstance(raw, dict):
        raise HttpError(400, "every row must be an object")
    row_id = raw.get("id")
    if not isinstance(row_id, str) or not ID_PATTERN.fullmatch(row_id) or not row_id.startswith(CENSUS_ID_PREFIXES):
        raise HttpError(400, "a row id must start with run- or job- and keep to letters, digits, _ . - (64 at most)")
    if row_id in seen:
        raise HttpError(400, "a row id is repeated")
    seen.add(row_id)
    if not isinstance(raw.get("name"), str):
        raise HttpError(400, "name must be a string")
    for key in ("group", "project", "what"):
        if key in raw and not isinstance(raw[key], str):
            raise HttpError(400, f"{key} must be a string")
    if raw.get("state", "working") != "working":
        raise HttpError(400, "state must be working: a run that is over is simply not in the snapshot")
    row: Dict[str, Any] = {"id": row_id, "name": raw["name"]}
    for key in ("elapsed", "idle"):
        if key in raw or key == "elapsed":  # idle is optional, elapsed is the point of the row
            if not _whole(raw.get(key)):
                raise HttpError(400, f"{key} must be a whole number of seconds")
            row[key] = min(max(raw[key], 0), MAX_CENSUS_SECONDS)
    session = raw.get("session")
    if "session" in raw:
        if not isinstance(session, str) or not ID_PATTERN.fullmatch(session):
            raise HttpError(400, "session must be 1 to 64 characters of letters, digits, _ . -")
        row["session"] = session
    for key in ("group", "project", "what"):
        if key in raw:
            row[key] = raw[key]
    return row


class HermesServer(HttpServer):
    """The listener Hermes talks to. Needs only the broker: the hub already follows it, so a request
    queued here shows up on the screen (and the terminal) with no further wiring."""

    name = "hermes"
    log = log

    def __init__(self, broker: ApprovalBroker, token: str, *, host: str, port: int = DEFAULT_PORT,
                 hosts: Iterable[str] = (), tasks: Optional[TaskBoard] = None, notices: Optional[NoticeBoard] = None,
                 wake: Optional[Callable[[str], Any]] = None) -> None:
        if not isinstance(token, str) or len(token) < MIN_TOKEN_CHARS:
            raise ValueError(f"the Hermes token needs at least {MIN_TOKEN_CHARS} characters")
        if not host:
            raise ValueError("the Hermes listener needs an address to bind to")
        super().__init__(host, port)
        self.broker = broker
        self.tasks = tasks  # the core's TaskBoard; without one, POST /session is a 404
        self.notices = notices  # the core's NoticeBoard; without one, /notice is a 404
        self._wake = wake  # UIHub.wake: how a notice that asks for it wakes the dim night screen
        self._token = token.encode("utf-8")
        self._names = {h.strip().lower() for h in hosts if h and h.strip()}  # names also accepted in Host
        self._records: Dict[str, Record] = {}
        self._rate: Dict[str, Deque[float]] = {}
        self._census_rate: Dict[str, Deque[float]] = {}  # the census has its own budget: it cannot 429 the hooks
        self._notice_rate: Dict[str, Deque[float]] = {}  # and so do the notices
        self._fails: Dict[str, Deque[float]] = {}

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    async def start(self) -> None:
        await super().start()
        log.info("hermes listener on %s", self.url)  # the token is never logged

    async def stop(self) -> None:
        for record in self._records.values():
            if record.timer is not None:
                record.timer.cancel()
        await super().stop()

    # -- what the rest of the daemon may ask -------------------------------------------

    def pending(self) -> int:
        return sum(1 for r in self._records.values() if r.state == "pending")

    def record(self, approval_id: str) -> Optional[Record]:
        return self._records.get(approval_id)

    # -- routing -----------------------------------------------------------------------

    async def _route(self, request: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._tasks) > MAX_CLIENTS:
            raise HttpError(503, "too many connections")
        peer = self._peer(writer)
        method, path = request.method, request.path
        self._throttle(peer, "census" if path == "/census" else
                       "notice" if path == "/notice" or path.startswith("/notice/") else "")
        self._check_host(request, writer)
        if path == "/healthz":
            self._need(method, "GET")
            await self._send(writer, 200, b"ok", "text/plain")
            return
        self._check_token(request, peer)
        if path == "/approval":
            self._need(method, "POST")
            await self._submit(request, writer)
        elif path == "/session":
            if self.tasks is None:
                raise HttpError(404)
            self._need(method, "POST")
            await self._session(request, writer)
        elif path == "/census":
            if self.tasks is None:
                raise HttpError(404)
            self._need(method, "POST")
            await self._census(request, writer)
        elif path == "/notice":
            if self.notices is None:
                raise HttpError(404)
            self._need(method, "POST")
            await self._notice(request, writer)
        elif path.startswith("/notice/"):
            notice_id = path[len("/notice/"):]
            if self.notices is None or not NOTICE_ID_PATTERN.fullmatch(notice_id):
                raise HttpError(404)
            if method == "GET":
                await self._send(writer, 200, {"id": notice_id, "state": self.notices.state(notice_id)})
            elif method == "DELETE":
                await self._take_back(notice_id, writer)
            else:
                raise HttpError(405)
        elif path.startswith("/approval/"):
            approval_id = path[len("/approval/"):]
            if not ID_PATTERN.fullmatch(approval_id):
                raise HttpError(404)
            if method == "GET":
                await self._poll(approval_id, request, reader, writer)
            elif method == "DELETE":
                await self._withdraw(approval_id, writer)
            else:
                raise HttpError(405)
        else:
            raise HttpError(404)

    @staticmethod
    def _peer(writer: asyncio.StreamWriter) -> str:
        info = writer.get_extra_info("peername")
        return str(info[0]) if info else "?"

    def _throttle(self, peer: str, route: str = "") -> None:
        """Spend one request of the client's budget: the census and the notices each have their own (`route`), so none
        of the three can starve another. A request that is refused later for its token or its shape still spent it."""
        now = time.monotonic()
        tables = (self._rate, self._census_rate, self._notice_rate)
        if any(len(table) > 1024 for table in tables):  # forget quiet clients before the tables grow
            for table in (*tables, self._fails):
                for key in [k for k, q in table.items() if not q or now - q[-1] > RATE_WINDOW_S]:
                    del table[key]
        if route == "census":
            budget, limit = self._census_rate, CENSUS_RATE_MAX
        elif route == "notice":
            budget, limit = self._notice_rate, NOTICE_RATE_MAX
        else:
            budget, limit = self._rate, RATE_MAX
        hits = budget.setdefault(peer, deque())
        while hits and now - hits[0] > RATE_WINDOW_S:
            hits.popleft()
        if len(hits) >= limit:
            raise HttpError(429, "too many requests")
        hits.append(now)
        fails = self._fails.get(peer)
        if fails:
            while fails and now - fails[0] > RATE_WINDOW_S:
                fails.popleft()
            if len(fails) >= AUTH_FAIL_MAX:
                raise HttpError(429, "too many bad tokens, wait a minute")

    def _check_host(self, request: Request, writer: asyncio.StreamWriter) -> None:
        local = writer.get_extra_info("sockname")
        ip = str(local[0]) if local else ""
        if ip.startswith("::ffff:"):  # a v4 client on a dual-stack socket
            ip = ip[len("::ffff:"):]
        if ":" in ip:
            ip = f"[{ip}]"
        allowed = {f"{ip}:{self.port}"} | {f"{n}:{self.port}" for n in self._names}
        if request.headers.get("host", "").lower() not in allowed:
            raise HttpError(403, "unexpected host")
        if "origin" in request.headers:
            raise HttpError(403, "browsers are not welcome here")

    def _check_token(self, request: Request, peer: str) -> None:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        sent = value.strip().encode("utf-8", "replace") if scheme.lower() == "bearer" else b""
        if not hmac.compare_digest(sent, self._token):
            self._fails.setdefault(peer, deque()).append(time.monotonic())
            log.warning("hermes: bad token from %s", peer)
            raise HttpError(403, "bad token")

    # -- the three operations ----------------------------------------------------------

    async def _submit(self, request: Request, writer: asyncio.StreamWriter) -> None:
        data = self._json(request)
        if "callback" in data:
            raise HttpError(400, "callbacks are not supported: poll GET /approval/<id>?wait=30 instead")
        approval_id = data.get("id")
        if not isinstance(approval_id, str) or not ID_PATTERN.fullmatch(approval_id):
            raise HttpError(400, "id must be 1 to 64 characters of letters, digits, _ . -")
        tool, hint = data.get("tool", ""), data.get("hint", "")
        if not isinstance(tool, str) or not isinstance(hint, str):
            raise HttpError(400, "tool and hint must be strings")
        source = data.get("source", SOURCE)
        if not isinstance(source, str) or source not in SOURCES:
            raise HttpError(400, f"source must be one of: {', '.join(SOURCES)}")
        always = data.get("always", "")
        if always != "" and source != "cli":
            raise HttpError(400, "only a Claude Code session (source cli) can offer always")
        if not isinstance(always, str) or len(always) > MAX_ALWAYS_CHARS or always != always.strip() or (
                always and not always.isprintable()):
            raise HttpError(400, f"always must be the rule as text, up to {MAX_ALWAYS_CHARS} characters")
        ttl = data.get("ttl", DEFAULT_TTL_S)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not MIN_TTL_S <= ttl <= MAX_TTL_S:
            raise HttpError(400, f"ttl must be a number of seconds between {MIN_TTL_S:g} and {MAX_TTL_S:g}")
        existing = self._records.get(approval_id)
        if existing is not None and existing.state == "pending":
            raise HttpError(409, "already pending")
        if self.pending() >= MAX_PENDING:
            raise HttpError(429, "too many pending requests")
        approval = Approval(id=approval_id, tool=tool[:MAX_TOOL_CHARS], hint=hint[:MAX_HINT_CHARS], source=source,
                            always=always)
        record = Record(approval)
        self._records[approval_id] = record
        self.broker.submit(approval, lambda decision, i=approval_id: self._settle(i, "decided", decision))
        record.timer = asyncio.get_running_loop().call_later(float(ttl), self._expire, approval_id)
        log.info("hermes: queued %s from %s (%s), ttl %gs", approval_id, source, approval.tool or "-", ttl)
        await self._send(writer, 202, {"ok": True, "id": approval_id, "key": approval.key,
                                       "poll": f"/approval/{approval_id}", "ttl": ttl})

    async def _poll(self, approval_id: str, request: Request, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter) -> None:
        record = self._records.get(approval_id)
        if record is None:
            raise HttpError(404)
        wait = self._wait_seconds(request.query)
        if record.state == "pending" and wait > 0:
            waiter = asyncio.ensure_future(record.event.wait())
            closed = asyncio.ensure_future(reader.read(1))  # done when Hermes hangs up
            try:
                done, _ = await asyncio.wait({waiter, closed}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                if closed in done and waiter not in done:
                    return
            finally:
                for fut in (waiter, closed):
                    if fut.done() and not fut.cancelled():
                        fut.exception()  # a reset connection is not worth a warning
                    fut.cancel()
        await self._send(writer, 200, record.as_json())

    async def _session(self, request: Request, writer: asyncio.StreamWriter) -> None:
        """A session on the Mac says what it is doing. Text only, cut by the board; never shown as HTML."""
        assert self.tasks is not None
        data = self._json(request)
        task_id = data.get("id")
        if not isinstance(task_id, str) or not ID_PATTERN.fullmatch(task_id):
            raise HttpError(400, "id must be 1 to 64 characters of letters, digits, _ . -")
        state = data.get("state", "working")
        if not isinstance(state, str) or state not in TASK_STATES:
            raise HttpError(400, f"state must be one of: {', '.join(TASK_STATES)}")
        for key in ("project", "tool", "what", "said", "title", "group", "parent"):
            if key in data and not isinstance(data[key], str):
                raise HttpError(400, f"{key} must be a string")
        parent = data.get("parent")
        if parent and not ID_PATTERN.fullmatch(parent):  # the id of another thread, or nothing
            raise HttpError(400, "parent must be 1 to 64 characters of letters, digits, _ . -")
        # said, title, group and parent: a key left out reaches the board as None, which keeps what it has.
        task = self.tasks.update(task_id, project=data.get("project", ""), tool=data.get("tool", ""),
                                 what=data.get("what", ""), state=state, said=data.get("said"),
                                 title=data.get("title"), group=data.get("group"), parent=parent)
        await self._send(writer, 200, {"ok": True, "id": task_id, "state": task.state if task else "gone"})

    async def _census(self, request: Request, writer: asyncio.StreamWriter) -> None:
        """The census says what runs on the Mac without a hook to speak for it. A wrong type or shape is a 400 and
        changes nothing; a number out of range is clamped, never a reason to refuse the whole snapshot."""
        assert self.tasks is not None
        data = self._json(request)
        version = data.get("v")
        if not _whole(version) or version != 1:
            raise HttpError(400, "v must be 1")
        source = data.get("source", CENSUS_DEFAULT_SOURCE)
        if not isinstance(source, str) or not CENSUS_SOURCE_PATTERN.fullmatch(source):
            raise HttpError(400, "source must be 1 to 24 characters of a-z, 0-9, _ -")
        seq = data.get("seq")
        if "seq" in data and (not _whole(seq) or not 0 <= seq <= MAX_CENSUS_SEQ):
            raise HttpError(400, "seq must be a whole number of 0 or more")
        ttl = data.get("ttl", CENSUS_TTL_DEFAULT_S)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise HttpError(400, "ttl must be a number of seconds")
        if isinstance(ttl, float) and not math.isfinite(ttl):  # NaN would never expire
            raise HttpError(400, "ttl must be a finite number of seconds")
        ttl = min(max(ttl, CENSUS_TTL_MIN_S), CENSUS_TTL_MAX_S)
        more = data.get("more", 0)
        if not _whole(more):
            raise HttpError(400, "more must be a whole number")
        more = min(max(more, 0), MAX_CENSUS_MORE)
        rows = data.get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_CENSUS_ROWS:
            raise HttpError(400, f"rows must be a list of at most {MAX_CENSUS_ROWS}")
        seen: set = set()
        rows = [_checked_row(raw, seen) for raw in rows]
        fresh = self.tasks.replace_census(source, rows, ttl, more, seq)
        log.debug("census: %d rows%s", len(rows), "" if fresh else " (late, ignored)")  # counts only, never the names
        answer: Dict[str, Any] = {"ok": True, "rows": len(rows), "ttl": ttl}
        if not fresh:
            answer["stale"] = True
        await self._send(writer, 200, answer)

    async def _notice(self, request: Request, writer: asyncio.StreamWriter) -> None:
        """Hermes leaves a short message on the screen. A wrong type or shape is a 400 and changes nothing; a ttl out of
        range is clamped and a text too long is cut, never a reason to refuse. The board cleans the text; it is never
        logged (the log gets the kind and a count), never part of an error, and the page writes it as text."""
        assert self.notices is not None
        data = self._json(request)
        if not isinstance(data.get("title"), str):
            raise HttpError(400, "title must be a string")
        for key in ("body", "source"):
            if key in data and not isinstance(data[key], str):
                raise HttpError(400, f"{key} must be a string")
        kind = data.get("kind", "info")
        if not isinstance(kind, str) or kind not in NOTICE_KINDS:
            raise HttpError(400, f"kind must be one of: {', '.join(NOTICE_KINDS)}")
        ttl = data.get("ttl", NOTICE_TTL_DEFAULT_S)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise HttpError(400, "ttl must be a number of seconds")
        if isinstance(ttl, float) and not math.isfinite(ttl):  # NaN would never expire
            raise HttpError(400, "ttl must be a finite number of seconds")
        wake = data.get("wake", False)
        if not isinstance(wake, bool):
            raise HttpError(400, "wake must be true or false")
        notice_id = data.get("id")
        if "id" in data and (not isinstance(notice_id, str) or not NOTICE_ID_PATTERN.fullmatch(notice_id)):
            raise HttpError(400, "id must be 1 to 64 characters of letters, digits, _ . : -")
        try:
            notice = self.notices.add(title=data["title"], body=data.get("body", ""), kind=kind, ttl=ttl,
                                      source=data.get("source", NOTICE_SOURCE), notice_id=notice_id)
        except ValueError as exc:  # a fixed phrase: here, a title with nothing left once cleaned
            raise HttpError(400, str(exc)) from None
        active = len(self.notices.active())
        log.info("aviso: %s, %d activos", notice.kind, active)  # the kind and a count, never the text
        if wake and self._wake is not None:
            # every POST with wake:true wakes, a re-send of an unchanged notice included: a sender may escalate a notice that waited
            # dim by sending it again with wake. What bounds a sender that keeps doing it is the token and NOTICE_RATE_MAX.
            try:
                self._wake("aviso")  # after the board has it, so the screen that wakes already shows it
            except Exception:  # a screen that cannot wake must not turn a stored notice into a 500
                log.exception("could not wake the screen for a notice")
        await self._send(writer, 200, {"ok": True, "id": notice.id, "active": active})

    async def _take_back(self, notice_id: str, writer: asyncio.StreamWriter) -> None:
        """Hermes no longer wants a notice shown. Idempotent: an id the board does not know, or one that already
        ended, is a 200 that says how it stands."""
        assert self.notices is not None
        was_shown = self.notices.state(notice_id) == "shown"
        state = self.notices.withdraw(notice_id)
        if was_shown:
            log.info("aviso: retirado, %d activos", len(self.notices.active()))
        await self._send(writer, 200, {"ok": True, "state": state})

    async def _withdraw(self, approval_id: str, writer: asyncio.StreamWriter) -> None:
        record = self._records.get(approval_id)
        if record is None:
            raise HttpError(404)
        if record.state == "pending":
            self.broker.withdraw(record.approval.source, approval_id)
            self._settle(approval_id, "withdrawn")
            log.info("hermes: %s withdrawn by Hermes", approval_id)
        await self._send(writer, 200, {"ok": True, **record.as_json()})

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _json(request: Request) -> Dict[str, Any]:
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HttpError(415)
        try:
            data = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "not json") from None
        if not isinstance(data, dict):
            raise HttpError(400, "the body must be a JSON object")
        return data

    @staticmethod
    def _wait_seconds(query: str) -> float:
        values = parse_qs(query, keep_blank_values=True).get("wait")
        if not values:
            return 0.0
        try:
            wait = float(values[-1])
        except ValueError:
            raise HttpError(400, "wait must be a number of seconds") from None
        if not 0 <= wait <= MAX_WAIT_S:
            raise HttpError(400, f"wait must be between 0 and {MAX_WAIT_S:g} seconds")
        return wait

    def _settle(self, approval_id: str, state: str, decision: Optional[str] = None) -> None:
        """The request stopped being pending: remember why, wake the pollers, prune old records."""
        record = self._records.get(approval_id)
        if record is None or record.state != "pending":
            raise RuntimeError("that request is gone")
        record.state, record.decision, record.finished = state, decision, time.monotonic()
        if record.timer is not None:
            record.timer.cancel()
            record.timer = None
        record.event.set()
        if state == "decided":
            log.info("hermes: %s -> %s", approval_id, decision)
        self._prune()

    def _expire(self, approval_id: str) -> None:
        record = self._records.get(approval_id)
        if record is None or record.state != "pending":
            return
        self.broker.withdraw(record.approval.source, approval_id)
        self._settle(approval_id, "expired")
        log.info("hermes: %s expired with no answer", approval_id)

    def _prune(self) -> None:
        """Forget answered requests after a while (and the oldest first when there are too many)."""
        now = time.monotonic()
        done = sorted((r.finished, i) for i, r in self._records.items() if r.state != "pending")
        extra = max(0, len(done) - MAX_DONE)
        for n, (finished, approval_id) in enumerate(done):
            if n < extra or now - finished > DONE_TTL_S:
                del self._records[approval_id]
