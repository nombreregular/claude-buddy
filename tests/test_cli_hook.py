"""tools/cli_hook.py: Claude Code in a terminal asks on the screen, and anything but a tap ends in the terminal's prompt.

The hook runs as Claude Code runs it (a process, the event on stdin) against the real Hermes listener."""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli_hook  # noqa: E402
from test_hermes import TOKEN, hermes  # noqa: E402
from test_ui import Rig, run  # noqa: E402

HOOK = Path(__file__).resolve().parent.parent / "tools" / "cli_hook.py"


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "claude-buddy").mkdir(parents=True)
    (home / "Projects" / "app").mkdir(parents=True)
    token = home / ".config" / "claude-buddy" / "hermes.token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    return home


def event(home, tool="Bash", tool_input=None, **kw):
    return {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": str(home / "Projects" / "app"),
            "permission_mode": "default", "tool_name": tool,
            "tool_input": {"command": "git push origin main", "description": "Push"} if tool_input is None else tool_input,
            **kw}


async def start_hook(home, url, payload, *args, timeout="5", env_extra=None):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "CLAUDE_BUDDY_URL": url,
           "CLAUDE_BUDDY_TIMEOUT": timeout, "CLAUDE_BUDDY_HOOK_LOG": str(home / "hook.log"), **(env_extra or {})}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(HOOK), *args, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    proc.stdin.write(data)
    proc.stdin.close()
    return proc


async def finish(proc, limit=20):
    out, err = await asyncio.wait_for(proc.communicate(), limit)
    return proc.returncode, out.decode(), err.decode()


async def on_screen(rig, limit=5.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if rig.view()["approvals"]:
            return rig.view()["approvals"][0]
        await asyncio.sleep(0.05)
    raise AssertionError("the request never reached the screen")


def url_of(server):
    return f"http://127.0.0.1:{server.port}"


# --- a tap answers it ------------------------------------------------------------------


@pytest.mark.parametrize("tap, behavior", [("once", "allow"), ("deny", "deny")])
def test_a_tap_on_the_screen_is_the_answer_claude_code_gets(rig, home, tap, behavior):
    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home))
            shown = await on_screen(rig)
            assert shown["source"] == "cli" and shown["tool"] == "Bash" and shown["key"].startswith("cli:cli-")
            assert shown["hint"] == "git push origin main\nen ~/Projects/app"
            assert rig.hub.decide(shown["key"], tap)
            return await finish(proc)

    code, out, _ = run(scenario)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PermissionRequest" and decision["decision"]["behavior"] == behavior
    if behavior == "deny":
        assert decision["decision"]["message"]
    else:
        assert set(decision["decision"]) == {"behavior"}  # nothing else: no new rules, no rewritten input
    assert TOKEN not in (home / "hook.log").read_text()


RULE = {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm run *"}], "behavior": "allow",
        "destination": "localSettings"}  # what Claude Code 2.1.302 proposed for `npm run lint`


def test_siempre_saves_the_rule_claude_code_proposed_and_nothing_else(rig, home):
    folder = {"type": "addDirectories", "directories": ["/tmp"], "destination": "session"}
    payload = event(home, tool_input={"command": "npm run lint"}, permission_suggestions=[folder, RULE])

    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), payload)
            shown = await on_screen(rig)
            assert shown["always"] == "Bash(npm run *) en este proyecto"
            assert rig.hub.decide(shown["key"], "always")
            return await finish(proc)

    code, out, _ = run(scenario)
    decision = json.loads(out)["hookSpecificOutput"]["decision"]
    assert code == 0 and decision == {"behavior": "allow", "updatedPermissions": [RULE]}  # handed back untouched
    assert "always (tapped on the screen: Bash(npm run *) en este proyecto)" in (home / "hook.log").read_text()


@pytest.mark.parametrize("tap", ["once", "deny"])
def test_with_siempre_offered_the_other_buttons_save_nothing(rig, home, tap):
    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home, permission_suggestions=[RULE]))
            assert rig.hub.decide((await on_screen(rig))["key"], tap)
            return await finish(proc)

    decision = json.loads(run(scenario)[1])["hookSpecificOutput"]["decision"]
    assert "updatedPermissions" not in decision and decision["behavior"] == ("allow" if tap == "once" else "deny")


def test_without_a_plain_rule_there_is_no_siempre(rig, home):
    mode = {"type": "setMode", "mode": "acceptEdits", "destination": "session"}  # what Write gets

    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home, "Write", {"file_path": "/tmp/a"},
                                                                permission_suggestions=[mode]))
            shown = await on_screen(rig)
            with pytest.raises(ValueError):
                rig.hub.decide(shown["key"], "always")
            assert rig.hub.decide(shown["key"], "once")
            return shown["always"], await finish(proc)

    always, (code, out, _) = run(scenario)
    assert always == "" and json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_only_an_allow_rule_claude_code_proposed_is_offered():
    pick = cli_hook.pick_always
    assert pick([RULE]) == (RULE, "Bash(npm run *) en este proyecto")
    two = {**RULE, "rules": [{"toolName": "Read"}, {"toolName": "Bash", "ruleContent": "ls *"}], "destination": "session"}
    assert pick([two]) == (two, "Read, Bash(ls *) en esta sesión")
    assert pick([{**RULE, "destination": "userSettings"}])[1] == "Bash(npm run *) en todos tus proyectos"
    for nothing in (None, "x", [], [None], [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}],
                    [{"type": "addDirectories", "directories": ["/"], "destination": "session"}],
                    [{**RULE, "behavior": "deny"}], [{**RULE, "behavior": "ask"}], [{**RULE, "type": "replaceRules"}],
                    [{**RULE, "destination": "policySettings"}], [{**RULE, "destination": ["localSettings"]}],
                    [{**RULE, "rules": []}], [{**RULE, "rules": "Bash"}], [{**RULE, "rules": [{"toolName": ""}]}],
                    [{**RULE, "rules": [{"toolName": "Bash", "ruleContent": 7}]}],
                    [{**RULE, "rules": [{"toolName": "Bash", "ruleContent": "a\nb"}]}],
                    [{**RULE, "rules": [{"toolName": "Bash", "ruleContent": "x" * 200}]}]):
        assert pick(nothing) == (None, ""), nothing


def test_a_flag_wins_over_the_environment(rig, home):
    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, "http://127.0.0.1:9", event(home), "--url", url_of(server))
            rig.hub.decide((await on_screen(rig))["key"], "once")
            return await finish(proc)

    assert json.loads(run(scenario)[1])["hookSpecificOutput"]["decision"]["behavior"] == "allow"


# --- anything else: the terminal asks --------------------------------------------------


def test_with_no_tap_in_time_the_terminal_asks_and_the_screen_lets_go(rig, home):
    async def scenario():
        async with hermes(rig) as server:
            started = time.monotonic()
            proc = await start_hook(home, url_of(server), event(home), timeout="2")
            key = (await on_screen(rig))["key"]
            code, out, err = await finish(proc)
            return code, out, err, time.monotonic() - started, server.record(key.split(":", 1)[1]).state, rig.view()

    code, out, err, took, state, view = run(scenario)
    assert code == 0 and out == "" and "no tap in 2 s" in err
    assert took < 6
    assert state == "withdrawn" and view["approvals"] == []


def test_with_the_buddy_off_the_terminal_asks_at_once(home):
    async def scenario():
        started = time.monotonic()
        proc = await start_hook(home, "http://127.0.0.1:9", event(home), timeout="60")  # nothing listens on 9
        return (*(await finish(proc)), time.monotonic() - started)

    code, out, err, took = run(scenario)
    assert code == 0 and out == "" and "terminal" in err
    assert took < 5


def test_a_buddy_that_never_answers_cannot_hold_the_terminal(home):
    async def scenario():
        clients = []

        async def silent(reader, writer):  # accepts, reads, says nothing
            clients.append(writer)
            await reader.read(1 << 16)

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            started = time.monotonic()
            proc = await start_hook(home, f"http://127.0.0.1:{port}", event(home), timeout="60")
            return (*(await finish(proc)), time.monotonic() - started)
        finally:
            for w in clients:
                w.close()
            server.close()

    code, out, _, took = run(scenario)
    assert code == 0 and out == "" and took < 8


@pytest.mark.parametrize("problem", ["missing", "readable", "short", "wrong"])
def test_without_a_good_token_the_terminal_asks(rig, home, problem):
    token = home / ".config" / "claude-buddy" / "hermes.token"
    if problem == "missing":
        token.unlink()
    elif problem == "readable":
        token.chmod(0o644)
    elif problem == "short":
        token.write_text("short")
    else:
        token.write_text("x" * 32)  # well formed, not the listener's

    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home))
            return (*(await finish(proc)), rig.view()["approvals"])

    code, out, err, approvals = run(scenario)
    assert code == 0 and out == "" and approvals == []
    assert TOKEN not in err and "x" * 32 not in err


@pytest.mark.parametrize("payload", [
    b"", b"not json", b"[]", b'{"hook_event_name": "PreToolUse", "tool_name": "Bash"}',
    {"hook_event_name": "PermissionRequest"},
    {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion", "tool_input": {"questions": []}},
    {"hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode", "tool_input": {"plan": "x"}},
])
def test_what_is_not_a_permission_for_the_screen_stays_in_the_terminal(rig, home, payload):
    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), payload)
            return (*(await finish(proc)), rig.view()["approvals"])

    code, out, _, approvals = run(scenario)
    assert code == 0 and out == "" and approvals == []


# sdk-cli is a `claude -p` run: since eaddfd2 the hook leaves it to Claude Code, like the desktop
@pytest.mark.parametrize("entrypoint, asks", [("claude-desktop", False), ("cli", True), ("sdk-cli", False)])
def test_claude_desktop_sessions_are_left_to_bluetooth(rig, home, entrypoint, asks):
    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home), env_extra={"CLAUDE_CODE_ENTRYPOINT": entrypoint})
            if asks:
                rig.hub.decide((await on_screen(rig))["key"], "once")
                return await finish(proc)
            return (*(await finish(proc))[:2], rig.view()["approvals"])

    result = run(scenario)
    if asks:
        assert json.loads(result[1])["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    else:
        assert result == (0, "", [])


@pytest.mark.parametrize("answer", [
    {"state": "decided", "decision": "always"},
    {"state": "decided", "decision": None},
    {"state": "approved", "decision": "once"},
    {"state": "withdrawn", "decision": None},
    {"state": "expired", "decision": None},
    {"state": "decided", "decision": "once", "id": "someone-else"},
    "not an object",
])
def test_an_answer_that_does_not_look_right_is_not_an_answer(home, answer):
    async def fake(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        line = head.split(b"\r\n", 1)[0].decode()
        length = next((int(h.split(b":", 1)[1]) for h in head.split(b"\r\n") if h.lower().startswith(b"content-length:")), 0)
        body = await reader.readexactly(length) if length else b""
        if line.startswith("POST"):
            status, reply = 202, {"ok": True, "id": json.loads(body)["id"]}
        elif line.startswith("GET"):
            approval_id = line.split()[1].split("?")[0].rsplit("/", 1)[1]
            status, reply = 200, ({"id": approval_id, **answer} if isinstance(answer, dict) else answer)
        else:
            status, reply = 200, {"ok": True}
        data = json.dumps(reply).encode()
        writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + data)
        await writer.drain()
        writer.close()

    async def scenario():
        server = await asyncio.start_server(fake, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            proc = await start_hook(home, f"http://127.0.0.1:{port}", event(home))
            return await finish(proc)
        finally:
            server.close()

    code, out, _ = run(scenario)
    assert code == 0 and out == ""


def test_a_signal_from_claude_code_takes_the_request_off_the_screen(rig, home):
    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home), timeout="30")
            key = (await on_screen(rig))["key"]
            await asyncio.sleep(0.3)  # the hook knows it was queued
            proc.send_signal(signal.SIGTERM)
            code, out, _ = await finish(proc)
            await asyncio.sleep(0.1)
            return code, out, server.record(key.split(":", 1)[1]).state, rig.view()["approvals"]

    code, out, state, approvals = run(scenario)
    assert code == 0 and out == ""
    assert state == "withdrawn" and approvals == []


def transcript_line(kind, item):
    return json.dumps({"type": kind, "message": {"role": kind, "content": [item]}}) + "\n"


@pytest.mark.parametrize("known", [True, False])
def test_an_answer_in_the_terminal_takes_the_request_off_the_screen(rig, home, known):
    """Claude Code shows its own prompt while the hook waits and does not stop the hook when you answer
    there: the tool's result in the transcript is the sign."""
    transcript = home / "session.jsonl"
    ours = {"command": "git push origin main", "description": "Push"}
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "push it"}}) + "\n"]
    if known:
        lines.append(transcript_line("assistant", {"type": "tool_use", "id": "toolu_ours", "name": "Bash", "input": ours}))
    transcript.write_text("".join(lines))

    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home, transcript_path=str(transcript)), timeout="30")
            key = (await on_screen(rig))["key"]
            record = server.record(key.split(":", 1)[1])
            if known:  # another call's result is not ours
                with transcript.open("a") as fh:
                    fh.write(transcript_line("user", {"type": "tool_result", "tool_use_id": "toolu_other", "content": ""}))
                await asyncio.sleep(2.5)
                assert proc.returncode is None and record.state == "pending"
            with transcript.open("a") as fh:
                fh.write(json.dumps({"type": "api-request-blob", "message": {"content": [{"type": "tool_result"}]}}) + "\n")
                fh.write(transcript_line("user", {"type": "tool_result", "tool_use_id": "toolu_ours", "content": ""}))
            started = time.monotonic()
            code, out, err = await finish(proc)
            return code, out, err, time.monotonic() - started, record.state, rig.view()["approvals"]

    code, out, err, took, state, approvals = run(scenario)
    assert code == 0 and out == "" and "answered in the terminal first" in err
    assert took < 4 and state == "withdrawn" and approvals == []


def test_a_tap_that_comes_after_the_terminal_answered_is_dropped(rig, home):
    transcript = home / "session.jsonl"
    ours = {"command": "git push origin main", "description": "Push"}
    transcript.write_text(transcript_line("assistant", {"type": "tool_use", "id": "toolu_ours", "name": "Bash", "input": ours}))

    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home, transcript_path=str(transcript)), timeout="30")
            key = (await on_screen(rig))["key"]
            with transcript.open("a") as fh:  # the terminal answered and the tool ran...
                fh.write(transcript_line("user", {"type": "tool_result", "tool_use_id": "toolu_ours", "content": ""}))
            rig.hub.decide(key, "once")  # ...and right then somebody taps the old request
            return await finish(proc)

    code, out, err = run(scenario)
    assert code == 0 and out == "" and "answered in the terminal first" in err


def test_a_transcript_that_is_missing_or_odd_changes_nothing(rig, home):
    odd = home / "odd.jsonl"
    odd.write_bytes(b"\xff\xfe not json\n" + b'{"type": "user", "message": {"content": "tool_result"}}\n')

    async def scenario():
        results = []
        async with hermes(rig) as server:
            for path in (str(home / "missing.jsonl"), str(odd), 7):
                proc = await start_hook(home, url_of(server), event(home, transcript_path=path))
                await asyncio.sleep(1.5)
                rig.hub.decide((await on_screen(rig))["key"], "once")
                results.append(await finish(proc))
        return results

    for code, out, _ in run(scenario):
        assert code == 0 and json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "allow"


@pytest.mark.parametrize("value", ["0", "-1", "601", "soon"])
def test_a_bad_timeout_leaves_it_to_the_terminal(rig, home, value):
    async def scenario():
        async with hermes(rig) as server:
            proc = await start_hook(home, url_of(server), event(home), timeout=value)
            return (*(await finish(proc)), rig.view()["approvals"])

    code, out, _, approvals = run(scenario)
    assert code == 0 and out == "" and approvals == []


# --- what the screen is told -------------------------------------------------------------


def test_the_summary_is_what_the_tool_wants_to_do_and_where(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cwd = str(tmp_path / "Projects" / "app")
    assert cli_hook.summarize("Bash", {"command": "ls -la"}, cwd) == "ls -la\nen ~/Projects/app"
    assert cli_hook.summarize("Edit", {"file_path": f"{cwd}/a.py", "old_string": "x"}, cwd) == \
        "~/Projects/app/a.py\nen ~/Projects/app"
    assert cli_hook.summarize("WebFetch", {"url": "https://example.com"}, "/srv") == "https://example.com\nen /srv"
    assert cli_hook.summarize("mcp__x__y", {"a": 1}, None) == '{"a": 1}'
    assert cli_hook.summarize("Bash", "nonsense", None) == ""
    long = cli_hook.summarize("Bash", {"command": "x" * 2000}, cwd)
    assert len(long) == cli_hook.MAX_HINT_CHARS and long.endswith("…\nen ~/Projects/app")


def test_batch_runs_never_reach_the_screen():
    assert cli_hook.print_mode(["claude", "--model", "opus", "-p", "haz el trabajo"])
    assert cli_hook.print_mode(["claude", "--print", "x"])
    assert not cli_hook.print_mode(["claude", "--model", "opus"])
    assert not cli_hook.print_mode(["claude"])
