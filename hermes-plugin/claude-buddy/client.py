"""The Hermes side of the buddy's approval listener: queue a request, wait for the tap.

Standard library only, no Hermes imports, so it can be tested on any machine against the buddy's
own `HermesServer`. `__init__.py` is the thin Hermes plugin around it.

    outcome = ask_buddy(Settings(url, token_file, timeout), command, description)
    # "once" (tapped Aprobar), "deny" (tapped Denegar) or None (anything else: buddy off, no token,
    # nobody tapped in time, an answer that does not look right). None means "ask somewhere else".

The token is the listener's own (`~/.config/claude-buddy/hermes.token` on the Pi), copied here into a
file only this user can read. It is never logged.
"""
import http.client
import json
import logging
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("hermes.plugins.claude_buddy")

DEFAULT_URL = "http://buddy.local:8766"
DEFAULT_TOKEN_FILE = "~/.hermes/claude-buddy.token"
DEFAULT_TIMEOUT_S = 120.0  # how long the screen gets before Hermes asks somewhere else
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 5.0, 3600.0
CONNECT_TIMEOUT_S = 4.0
WITHDRAW_TIMEOUT_S = 2.0
MAX_WAIT_S = 30  # the listener's ceiling for ?wait=
TTL_MARGIN_S = 10.0
MIN_TOKEN_CHARS = 16
MAX_TOOL_CHARS, MAX_HINT_CHARS = 80, 500  # what the screen keeps
SOURCE = "hermes"
TOOL = "terminal"  # what Hermes asks approval for: shell commands


class Unavailable(Exception):
    """The screen cannot answer this one. The message is for the log only."""


@dataclass(frozen=True)
class Settings:
    url: str = DEFAULT_URL
    token_file: str = DEFAULT_TOKEN_FILE
    timeout: float = DEFAULT_TIMEOUT_S

    @classmethod
    def from_mapping(cls, get: Any) -> "Settings":
        """From a `get(key, default)` callable (the plugin's `ctx.get_config`), with sane fallbacks."""
        url = get("url", DEFAULT_URL)
        token_file = get("token_file", DEFAULT_TOKEN_FILE)
        timeout = get("timeout", DEFAULT_TIMEOUT_S)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        timeout = min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, timeout))
        return cls(url=str(url or DEFAULT_URL), token_file=str(token_file or DEFAULT_TOKEN_FILE), timeout=timeout)


# -- the token -----------------------------------------------------------------------------


def load_token(path: Path) -> str:
    """The listener's token, from a file only this user can read. Never printed, never logged."""
    try:
        info = os.stat(path)
    except OSError:
        raise Unavailable(f"no token file at {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise Unavailable(f"{path} is not a regular file")
    if info.st_mode & 0o077:
        raise Unavailable(f"{path} can be read by others (chmod 600 it)")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise Unavailable(f"{path} belongs to someone else")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise Unavailable(f"could not read {path}") from None
    if len(token) < MIN_TOKEN_CHARS or any(c.isspace() for c in token):
        raise Unavailable(f"the token in {path} does not look right")
    return token


# -- the listener ----------------------------------------------------------------------------


class Buddy:
    def __init__(self, url: str, token: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/") or parts.query:
            raise Unavailable("the url must look like http://host:port")
        self.host = parts.hostname
        self.port = parts.port or 80
        self._token = token

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             timeout: float = CONNECT_TIMEOUT_S) -> Tuple[int, Any]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read(64 * 1024)
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise Unavailable(f"{method} {path}: {type(exc).__name__}") from None
        finally:
            conn.close()
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, None


def hint_for(command: str, description: str) -> str:
    """What the screen shows: the command, and Hermes' own words about it when it has any."""
    command = (command or "").replace("\r", "").strip()
    description = (description or "").replace("\r", "").strip()
    text = command
    if description and description != command:
        text = f"{command}\n{description}" if command else description
    if len(text) > MAX_HINT_CHARS:
        text = text[:MAX_HINT_CHARS - 1] + "…"
    return text


def ask_buddy(settings: Settings, command: str, description: str = "", *,
              deadline: Optional[float] = None, cancelled: Optional[threading.Event] = None) -> Optional[str]:
    """Put the command on the screen and wait for the tap: "once", "deny", or None when the screen
    could not answer (then the caller asks somewhere else). `deadline` (monotonic) caps the wait below
    `settings.timeout`, `cancelled` ends it early; either way the request leaves the screen."""
    approval_id = "h-" + secrets.token_hex(8)
    try:
        buddy = Buddy(settings.url, load_token(Path(settings.token_file).expanduser()))
    except Unavailable as exc:
        log.warning("claude-buddy: %s; asking elsewhere", exc)
        return None
    end = time.monotonic() + settings.timeout
    if deadline is not None:
        end = min(end, deadline)
    ttl = max(1.0, min(3600.0, end - time.monotonic() + TTL_MARGIN_S))
    request = {"id": approval_id, "tool": TOOL, "hint": hint_for(command, description), "source": SOURCE, "ttl": ttl}
    queued = False
    try:
        status, body = buddy.call("POST", "/approval", request)
        if status != 202 or not isinstance(body, dict) or body.get("ok") is not True or body.get("id") != approval_id:
            raise Unavailable(f"POST /approval answered {status}")
        queued = True
        while True:
            if cancelled is not None and cancelled.is_set():
                raise Unavailable("cancelled")
            remaining = end - time.monotonic()
            if remaining < 0.5:
                raise Unavailable(f"no tap in {settings.timeout:g} s")
            wait = max(1, min(MAX_WAIT_S, int(remaining + 0.5)))
            status, body = buddy.call("GET", f"/approval/{approval_id}?wait={wait}", timeout=wait + CONNECT_TIMEOUT_S)
            if status != 200 or not isinstance(body, dict) or body.get("id") != approval_id:
                raise Unavailable(f"GET /approval answered {status}")
            state, decision = body.get("state"), body.get("decision")
            if state == "pending":
                continue
            if state == "decided" and decision in ("once", "deny"):
                queued = False  # answered: nothing left on the screen
                log.info("claude-buddy: %s -> %s (tapped on the screen)", approval_id, decision)
                return decision
            raise Unavailable(f"the request ended as {state!r}")
    except Unavailable as exc:
        log.warning("claude-buddy: %s; asking elsewhere", exc)
        return None
    finally:
        if queued:
            try:
                buddy.call("DELETE", f"/approval/{approval_id}", timeout=WITHDRAW_TIMEOUT_S)
            except Unavailable:
                pass
