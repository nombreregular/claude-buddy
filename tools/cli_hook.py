#!/usr/bin/env python3
"""Claude Code in a terminal asks for permission on the Claude Buddy screen.

A `PermissionRequest` hook for Claude Code (the CLI). Claude Code runs it only when it is about to
show its own permission prompt, so allow rules, `acceptEdits`, `auto` and the rest keep working as
they do; what reaches the hook is exactly what would have asked you. The hook sends the tool and a
summary of the request to the buddy's Hermes listener (`POST /approval` with `"source": "cli"`, so
the screen shows `cli · Bash`), waits for a tap by long polling (`GET /approval/<id>?wait=30`) and
hands the decision back to Claude Code.

When Claude Code proposes a plain allow rule for the request (its "Yes, and don't ask again for ..."),
the screen also offers "Siempre" and shows that rule; a tap there answers allow and hands the very
same suggestion back as `updatedPermissions`, so Claude Code saves it as it would from its own
prompt. Nothing broader is ever made up; other suggestions (folders, modes) stay in the terminal.

Claude Code shows its own prompt while the hook waits, and whichever answer comes first wins. It
does not stop the hook when you answer in the terminal, so the hook watches the session transcript:
once the tool's result is there, the request leaves the screen and the hook goes quietly.

The one rule: anything that is not a tap on the screen ends in Claude Code's normal prompt. Buddy
off, no token, a token anybody can read, a timeout, a 404, a body that does not look right, a
signal: the hook prints nothing and exits 0, and the terminal asks as it always did. It never
allows or denies on its own.

    {"hooks": {"PermissionRequest": [{"matcher": "*", "hooks": [{"type": "command",
        "command": "python3 \"$HOME/Projects/claude-buddy/tools/cli_hook.py\"", "timeout": 90}]}]}}

Options (flag or environment variable; the flag wins):
    --url            CLAUDE_BUDDY_URL         default http://buddy:8766 (the Pi's name on Tailscale)
    --token-file     CLAUDE_BUDDY_TOKEN_FILE  default ~/.config/claude-buddy/hermes.token (0600, owned by you)
    --timeout        CLAUDE_BUDDY_TIMEOUT     seconds to wait for a tap, default 60 (1 to 600); keep the
                                              hook's "timeout" in settings.json above it
    --log            CLAUDE_BUDDY_HOOK_LOG    optional file: one line per request (never the token)

Standard library only; runs on the Python 3.9 that ships with macOS.
"""
import argparse
import http.client
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

DEFAULT_URL = "http://buddy:8766"
DEFAULT_TOKEN_FILE = "~/.config/claude-buddy/hermes.token"
DEFAULT_TIMEOUT_S = 60.0
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 1.0, 600.0
CONNECT_TIMEOUT_S = 4.0  # to reach the buddy at all; past this the terminal asks
WITHDRAW_TIMEOUT_S = 2.0
MAX_WAIT_S = 30  # what the listener allows for ?wait=
TTL_MARGIN_S = 10.0  # the request leaves the screen by itself a bit after we stop waiting
MIN_TOKEN_CHARS = 16
MAX_TOOL_CHARS, MAX_HINT_CHARS = 80, 500  # what the screen keeps
MAX_ALWAYS_CHARS = 120  # the rule behind "Siempre" is shown whole or not offered
# Where Claude Code would save the rule, as the screen says it.
SCOPES = {"localSettings": "en este proyecto", "projectSettings": "en el proyecto (compartido)",
          "userSettings": "en todos tus proyectos", "session": "en esta sesión"}
MAX_STDIN = 1 << 20
WATCH_EVERY_S = 1.0  # how often the transcript is checked while waiting
TAIL_BYTES = 512 * 1024  # how far back to look for the tool call, and the most read per check
SOURCE = "cli"
# Claude desktop reads the same settings.json; its prompts reach the buddy over Bluetooth already, so the
# hook stays out of its sessions (a terminal opened from one inherits the variable too).
# These are questions, not permissions: a tap could not answer them, the terminal must.
SKIP_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
DENY_MESSAGE = "Denegado en la pantalla de Claude Buddy."


def print_mode(argv) -> bool:
    """`claude -p` / `--print`: a batch run with its own allowed tools. Nobody is at a terminal for it, so the screen
    should not get its prompts either (2026-10-18: overnight runs kept the buddy busy all afternoon)."""
    return "-p" in argv or "--print" in argv


def started_by_batch_claude(max_up: int = 4, pid: Optional[int] = None) -> bool:
    """Walks up the parent processes (the hook runs under a shell under claude) looking for a claude in print mode."""
    pid = os.getppid() if pid is None else pid
    for _ in range(max_up):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False
        if not out:
            return False
        ppid, _, args = out.partition(" ")
        argv = args.split()
        if argv and Path(argv[0]).name == "claude" and print_mode(argv):
            return True
        try:
            pid = int(ppid)
        except ValueError:
            return False
        if pid <= 1:
            return False
    return False


class Fallback(Exception):
    """Anything that should end in the terminal's own prompt. The message is for the log only."""


# -- what the request says ---------------------------------------------------------------


def _home(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def summarize(tool: str, data: Any, cwd: Any) -> str:
    """One line of what the tool wants to do, and the folder it runs in."""
    data = data if isinstance(data, dict) else {}

    def field(*keys: str) -> str:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    if tool == "Bash":
        text = field("command")
    elif tool in ("Edit", "MultiEdit", "Write", "Read", "NotebookEdit"):
        text = _home(field("file_path", "notebook_path"))
    elif tool == "WebFetch":
        text = field("url")
    elif tool == "WebSearch":
        text = field("query")
    elif tool in ("Glob", "Grep"):
        text = " ".join(p for p in (field("pattern"), _home(field("path"))) if p)
    else:
        text = ""
    if not text and data:
        text = json.dumps(data, ensure_ascii=False, separators=(", ", ": "))
    place = f"\nen {_home(cwd)}" if isinstance(cwd, str) and cwd else ""
    text = text.replace("\r", "")
    room = MAX_HINT_CHARS - len(place)
    if len(text) > room:
        text = text[:room - 1] + "…"
    return text + place


def pick_always(suggestions: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """The suggestion "Siempre" would hand back, and how the screen says it ("Bash(npm run *) en este
    proyecto"). Only an allow rule Claude Code itself proposed; it goes back untouched."""
    for suggestion in suggestions if isinstance(suggestions, list) else ():
        if not isinstance(suggestion, dict) or suggestion.get("type") != "addRules" or suggestion.get("behavior") != "allow":
            continue
        destination = suggestion.get("destination")
        scope = SCOPES.get(destination) if isinstance(destination, str) else None
        rules = suggestion.get("rules")
        if scope is None or not isinstance(rules, list) or not rules:
            continue
        names = []
        for rule in rules:
            tool = rule.get("toolName") if isinstance(rule, dict) else None
            content = rule.get("ruleContent") if isinstance(rule, dict) else None
            if not isinstance(tool, str) or not tool or (content is not None and not isinstance(content, str)):
                break
            names.append(f"{tool}({content})" if content else tool)
        else:
            label = f"{', '.join(names)} {scope}"
            if len(label) <= MAX_ALWAYS_CHARS and label.isprintable():
                return suggestion, label
    return None, ""


# -- the token -----------------------------------------------------------------------------


def load_token(path: Path) -> str:
    """The listener's token, from a file only you can read. Never printed, never logged."""
    try:
        info = os.stat(path)
    except OSError:
        raise Fallback(f"no token file at {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise Fallback(f"{path} is not a regular file")
    if info.st_mode & 0o077:
        raise Fallback(f"{path} can be read by others (chmod 600 it)")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise Fallback(f"{path} belongs to someone else")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise Fallback(f"could not read {path}") from None
    if len(token) < MIN_TOKEN_CHARS or any(c.isspace() for c in token):
        raise Fallback(f"the token in {path} does not look right")
    return token


# -- talking to the buddy -----------------------------------------------------------------


class Buddy:
    def __init__(self, url: str, token: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/") or parts.query:
            raise Fallback("the url must look like http://host:port")
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
            # errno 65 (no route to host) with the buddy up is macOS' Local Network privacy: see the README
            raise Fallback(f"{method} {path}: {type(exc).__name__} {exc}"[:200]) from None
        finally:
            conn.close()
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, None


def ask_the_screen(buddy: Buddy, approval_id: str, tool: str, hint: str, timeout: float,
                   submitted: threading.Event, always: str = "") -> str:
    """Queue the request, wait for the tap: "allow", "deny" or (if offered) "always". Anything else raises Fallback."""
    deadline = time.monotonic() + timeout
    request = {"id": approval_id, "tool": tool, "hint": hint, "source": SOURCE,
               "ttl": min(3600.0, timeout + TTL_MARGIN_S)}
    if always:
        request["always"] = always
    status, body = buddy.call("POST", "/approval", request)
    if status != 202 or not isinstance(body, dict) or body.get("ok") is not True or body.get("id") != approval_id:
        raise Fallback(f"POST /approval answered {status}")
    submitted.set()
    while True:
        wait = min(MAX_WAIT_S, int(deadline - time.monotonic()))
        if wait < 1:
            raise Fallback(f"no tap in {timeout:g} s")
        status, body = buddy.call("GET", f"/approval/{approval_id}?wait={wait}", timeout=wait + CONNECT_TIMEOUT_S)
        if status != 200 or not isinstance(body, dict) or body.get("id") != approval_id:
            raise Fallback(f"GET /approval answered {status}")
        state, decision = body.get("state"), body.get("decision")
        if state == "pending":
            continue
        if state == "decided" and decision == "once":
            return "allow"
        if state == "decided" and decision == "deny":
            return "deny"
        if state == "decided" and decision == "always" and always:
            return "always"
        raise Fallback(f"the request ended as {state!r}")


class Transcript:
    """The session's transcript, read from where it ended when the hook started.

    The tool's result showing up there means the terminal answered first (and the tool ran, or was
    refused). The call is told apart by its tool_use id when it can be found (same name and input);
    if not, any new result counts. A false alarm only takes the request off the screen: the terminal
    is asking anyway."""

    def __init__(self, path: Any, tool: str, tool_input: Any) -> None:
        self.path: Optional[Path] = Path(path) if isinstance(path, str) and path else None
        self.offset = 0
        self.rest = b""
        self.tool_use_id: Optional[str] = None
        self._tool, self._input = tool, tool_input
        if self.path is None:
            return
        try:
            with open(self.path, "rb") as fh:
                end = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, end - TAIL_BYTES))
                lines = fh.read(TAIL_BYTES).split(b"\n")
        except OSError:
            self.path = None
            return
        self.offset = end
        for entry in self._entries(lines[1:] if end > TAIL_BYTES else lines):
            self._learn(entry)

    def answered(self) -> bool:
        if self.path is None:
            return False
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(TAIL_BYTES)
        except OSError:
            return False
        self.offset += len(chunk)
        lines = (self.rest + chunk).split(b"\n")
        self.rest = lines.pop()[-TAIL_BYTES:]
        for entry in self._entries(lines):
            self._learn(entry)
            for item in self._items(entry, "user", "tool_result"):
                if self.tool_use_id is None or item.get("tool_use_id") == self.tool_use_id:
                    return True
        return False

    def _learn(self, entry: Dict[str, Any]) -> None:
        for item in self._items(entry, "assistant", "tool_use"):
            if item.get("name") == self._tool and item.get("input") == self._input and isinstance(item.get("id"), str):
                self.tool_use_id = item["id"]

    @staticmethod
    def _entries(lines):
        for line in lines:
            if b'"tool_use' not in line and b'"tool_result"' not in line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                yield entry

    @staticmethod
    def _items(entry: Dict[str, Any], kind: str, item_type: str):
        message = entry.get("message")
        content = message.get("content") if entry.get("type") == kind and isinstance(message, dict) else None
        for item in content if isinstance(content, list) else ():
            if isinstance(item, dict) and item.get("type") == item_type:
                yield item


def decision_json(behavior: str, suggestion: Optional[Dict[str, Any]] = None) -> str:
    decision: Dict[str, Any] = {"behavior": "allow" if behavior == "always" else behavior}
    if behavior == "deny":
        decision["message"] = DENY_MESSAGE
    if behavior == "always" and suggestion is not None:
        decision["updatedPermissions"] = [suggestion]
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}})


# -- the hook ------------------------------------------------------------------------------


def options(argv) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description="PermissionRequest hook: approve Claude Code on the buddy's screen")
    parser.add_argument("--url", default=env("CLAUDE_BUDDY_URL", DEFAULT_URL))
    parser.add_argument("--token-file", default=env("CLAUDE_BUDDY_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    parser.add_argument("--timeout", default=env("CLAUDE_BUDDY_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    parser.add_argument("--log", default=env("CLAUDE_BUDDY_HOOK_LOG", ""))
    return parser.parse_args(argv)


class Log:
    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser() if path else None

    def __call__(self, approval_id: str, tool: str, outcome: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {approval_id} {tool or '-'} {outcome}\n"
        sys.stderr.write(f"claude-buddy: {outcome}\n")
        if self.path is None:
            return
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass


def main(argv=None) -> int:
    args = options(argv)
    log = Log(args.log)
    approval_id = "cli-" + secrets.token_hex(8)
    tool = ""
    try:
        try:
            timeout = float(args.timeout)
        except ValueError:
            raise Fallback("--timeout must be a number of seconds") from None
        if not MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S:
            raise Fallback(f"--timeout must be between {MIN_TIMEOUT_S:g} and {MAX_TIMEOUT_S:g} seconds")
        try:
            event = json.loads(sys.stdin.read(MAX_STDIN))
        except (ValueError, UnicodeDecodeError):
            raise Fallback("stdin is not JSON") from None
        if not isinstance(event, dict) or event.get("hook_event_name") != "PermissionRequest":
            raise Fallback("not a PermissionRequest")
        if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop":  # settings.json is shared with the app
            raise Fallback("Claude desktop: its prompts already reach the buddy over Bluetooth")
        if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk") or started_by_batch_claude():
            raise Fallback("claude -p: a batch run, not someone at a terminal")
        tool = event.get("tool_name") if isinstance(event.get("tool_name"), str) else ""
        if not tool or tool in SKIP_TOOLS:
            raise Fallback(f"{tool or 'no tool'}: left to the terminal")
        hint = summarize(tool, event.get("tool_input"), event.get("cwd"))
        transcript = Transcript(event.get("transcript_path"), tool, event.get("tool_input"))
        suggestion, always = pick_always(event.get("permission_suggestions"))
        buddy = Buddy(args.url, load_token(Path(args.token_file).expanduser()))
    except Fallback as exc:
        log(approval_id, tool, f"terminal ({exc})")
        return 0

    # The work runs in a thread so that nothing (a slow name lookup, a hung socket) can keep us past
    # the deadline, and a signal from Claude Code still gets the request off the screen.
    submitted = threading.Event()
    result: Dict[str, str] = {}

    def work() -> None:
        try:
            result["behavior"] = ask_the_screen(buddy, approval_id, tool[:MAX_TOOL_CHARS], hint, timeout, submitted,
                                                always)
        except Fallback as exc:
            result["why"] = str(exc)
        except Exception as exc:  # whatever it is, the terminal asks
            result["why"] = f"unexpected {type(exc).__name__}"

    def withdraw() -> None:
        if not submitted.is_set():
            return
        done = threading.Event()

        def call() -> None:
            try:
                buddy.call("DELETE", f"/approval/{approval_id}", timeout=WITHDRAW_TIMEOUT_S)
            except Exception:
                pass
            done.set()

        threading.Thread(target=call, daemon=True).start()
        done.wait(WITHDRAW_TIMEOUT_S + 1)

    def on_signal(signum, _frame) -> None:
        withdraw()
        log(approval_id, tool, f"terminal (signal {signum})")
        os._exit(0)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    end = time.monotonic() + timeout + CONNECT_TIMEOUT_S + 2
    while worker.is_alive() and time.monotonic() < end:
        worker.join(WATCH_EVERY_S)
        if worker.is_alive() and transcript.answered():
            withdraw()
            log(approval_id, tool, "terminal (answered in the terminal first)")
            return 0
    behavior = result.get("behavior")
    if behavior in ("allow", "deny", "always") and transcript.answered():  # a tap after the terminal's answer
        log(approval_id, tool, f"terminal (answered in the terminal first; the late {behavior} is dropped)")
        return 0
    if behavior in ("allow", "deny", "always"):
        sys.stdout.write(decision_json(behavior, suggestion) + "\n")
        sys.stdout.flush()
        log(approval_id, tool, f"{behavior} (tapped on the screen{': ' + always if behavior == 'always' else ''})")
        return 0
    withdraw()
    log(approval_id, tool, f"terminal ({result.get('why') or 'no answer in time'})")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # argparse, a broken pipe, anything: the terminal asks
        code = 0
    sys.stdout.flush()
    os._exit(code)
