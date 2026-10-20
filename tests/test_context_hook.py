"""tools/context_hook.py: each session tells the screen what it is doing, and never gets in the way.

The hook runs in EVERY Claude Code session on the Mac, so these tests never touch the real Claude desktop
folder, the real ~/.cache or the real ~/.claude: an autouse fixture points all three at tmp_path, and the
desktop's files (session files, the config with its groups, the live-process list) are built there."""
import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import context_hook  # noqa: E402
from test_hermes import TOKEN, hermes  # noqa: E402
from test_ui import Rig, run  # noqa: E402

HOOK = Path(__file__).resolve().parent.parent / "tools" / "context_hook.py"
SYSTEM_PYTHON = "/usr/bin/python3"
ISOLATED = ("CLAUDE_BUDDY_DESKTOP_DIR", "CLAUDE_BUDDY_CACHE_DIR", "CLAUDE_CONFIG_DIR")
EMOJI = "\U0001F600"  # one character, 12 bytes once json.dumps escapes it


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path, monkeypatch):
    """Nothing here may read or write the real desktop folder, ~/.cache or ~/.claude."""
    monkeypatch.setenv("CLAUDE_BUDDY_DESKTOP_DIR", str(tmp_path / "desktop"))
    monkeypatch.setenv("CLAUDE_BUDDY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    for name in ("CLAUDE_BUDDY_SAID", "CLAUDE_BUDDY_SAID_EXCLUDE", "CLAUDE_BUDDY_THREADS", "CLAUDE_BUDDY_CONTEXT",
                 "CLAUDE_BUDDY_URL", "CLAUDE_BUDDY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)


def event(name, **kw):
    base = {"session_id": "11111111-2222-3333-4444-555555555555", "cwd": "/Users/example/Projects/relay",
            "hook_event_name": name}
    base.update(kw)
    return base


def run_hook(payload, env, raw=None, python=None):
    """The hook as a real process, the way settings.json runs it."""
    full = {**os.environ, **env}
    for name in ISOLATED:  # a missing one would mean the process reads the real machine
        assert name in full, name
    data = raw if raw is not None else json.dumps(payload).encode()
    done = subprocess.run([python or sys.executable, str(HOOK)], input=data, capture_output=True, env=full, timeout=30)
    return done.returncode, done.stdout, done.stderr


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "hermes.token"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o600)
    return path


# --- the report -------------------------------------------------------------------------


def test_the_report_says_project_tool_and_a_short_summary():
    r = context_hook.build_report(event("PreToolUse", tool_name="Bash", tool_input={"command": "npm test -- --watch=false"}))
    assert r == {"id": "11111111-2222-3333-4444-555555555555", "project": "relay", "state": "working",
                 "tool": "Bash", "what": "npm test -- --watch=false"}
    r = context_hook.build_report(event("PreToolUse", tool_name="Edit", tool_input={"file_path": "/Users/example/Projects/relay/a.ts"}))
    assert r["tool"] == "Edit" and r["what"].endswith("relay/a.ts")
    assert context_hook.build_report(event("UserPromptSubmit"))["what"] == "nuevo pedido"
    assert context_hook.build_report(event("Stop"))["state"] == "idle"
    assert context_hook.build_report(event("SessionEnd"))["state"] == "gone"
    assert context_hook.build_report(event("SubagentStop")) is None  # a subagent is not a session of its own
    assert context_hook.build_report(event("PreToolUse", session_id="")) is None
    assert context_hook.build_report({"hook_event_name": "Notification"}) is None


def test_project_name_is_the_folder():
    assert context_hook.project_name("/Users/example/Projects/claude-buddy") == "claude-buddy"
    assert context_hook.project_name(str(Path.home())) == "~"
    assert context_hook.project_name("") == "" and context_hook.project_name(None) == ""


def test_project_and_tool_are_capped_so_the_body_stays_small():
    long_name = "p" * 100
    r = context_hook.build_report(event("PreToolUse", cwd="/x/" + long_name, tool_name="mcp__" + "t" * 100, tool_input={}))
    assert r["project"] == "p" * 40 and r["tool"] == ("mcp__" + "t" * 100)[:40]
    assert context_hook.project_name("/x/" + long_name) == "p" * 40


def test_build_report_stays_pure_and_unchanged_without_a_desktop_match(monkeypatch):
    # the report itself never looks at Claude desktop: that is main()'s job, so these stay exact
    monkeypatch.setattr(context_hook, "thread_info", lambda *a, **k: pytest.fail("build_report must not read the desktop"))
    r = context_hook.build_report(event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
    assert set(r) == {"id", "project", "state", "tool", "what"}
    assert set(context_hook.build_report(event("UserPromptSubmit"))) == {"id", "project", "state", "tool", "what"}
    assert set(context_hook.build_report(event("SessionEnd"))) == {"id", "project", "state"}
    assert set(context_hook.build_report(event("Stop"))) == {"id", "project", "state"}  # no text to read, no said


# --- the process ------------------------------------------------------------------------


def test_the_hook_posts_to_the_buddy_and_the_board_lists_the_session(tmp_path, token_file):
    rig = Rig(tmp_path / "state")

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            env = {"CLAUDE_BUDDY_URL": server.url, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file)}
            loop = asyncio.get_running_loop()
            code, out, err = await loop.run_in_executor(
                None, run_hook, event("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}), env)
            assert (code, out) == (0, b""), err
            tasks = rig.view()["tasks"]
            assert [(t["project"], t["tool"], t["what"], t["state"]) for t in tasks] == [("relay", "Bash", "pytest -q", "working")]
            code, out, err = await loop.run_in_executor(None, run_hook, event("Stop"), env)
            assert (code, out) == (0, b"")
            assert rig.view()["tasks"][0]["state"] == "idle"

    run(scenario)


def test_without_a_usable_token_or_buddy_the_hook_is_silent(tmp_path):
    loose = tmp_path / "loose.token"
    loose.write_text(TOKEN, encoding="utf-8")
    loose.chmod(0o644)  # readable by others: not a token we use
    for env in ({"CLAUDE_BUDDY_TOKEN_FILE": str(tmp_path / "missing")},
                {"CLAUDE_BUDDY_TOKEN_FILE": str(loose)},
                {"CLAUDE_BUDDY_CONTEXT": "off"}):
        code, out, _ = run_hook(event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}), env)
        assert (code, out) == (0, b""), env
    good = tmp_path / "good.token"
    good.write_text(TOKEN, encoding="utf-8")
    good.chmod(0o600)
    # a buddy that is off: connection refused, still silent and quick
    code, out, _ = run_hook(event("Stop"), {"CLAUDE_BUDDY_TOKEN_FILE": str(good), "CLAUDE_BUDDY_URL": "http://127.0.0.1:9"})
    assert (code, out) == (0, b"")
    code, out, _ = run_hook({"not": "json shaped"}, {"CLAUDE_BUDDY_TOKEN_FILE": str(good)})
    assert (code, out) == (0, b"")


class FakeStdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


def run_main(monkeypatch, payload, token_file=None, raw=None):
    """main() in this process with post() replaced: returns (exit code, the reports it would have posted)."""
    posts = []
    data = raw if raw is not None else json.dumps(payload).encode()
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))
    if token_file is not None:
        monkeypatch.setenv("CLAUDE_BUDDY_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(context_hook, "post", lambda url, token, report: posts.append(report))
    return context_hook.main(), posts


def test_the_token_is_checked_before_any_report_is_built(monkeypatch, tmp_path, token_file):
    def boom(*a, **k):
        raise AssertionError("work was done for a buddy nobody can talk to")

    monkeypatch.setattr(context_hook, "build_report", boom)
    monkeypatch.setattr(context_hook, "thread_info", boom)
    monkeypatch.setattr(context_hook, "last_said_from_transcript", boom)
    code, posts = run_main(monkeypatch, event("Stop", transcript_path="/nope.jsonl"), tmp_path / "missing.token")
    assert (code, posts) == (0, [])
    loose = tmp_path / "loose.token"
    loose.write_text(TOKEN, encoding="utf-8")
    loose.chmod(0o644)
    assert run_main(monkeypatch, event("Stop"), loose) == (0, [])


def test_invalid_utf8_or_a_closed_stdin_do_not_break_the_session(monkeypatch, token_file):
    code, posts = run_main(monkeypatch, None, token_file,
                           raw=b'{"session_id": "abc-\xff\xfe", "hook_event_name": "PreToolUse", "tool_name": "Bash"}')
    assert code == 0 and len(posts) == 1 and posts[0]["id"].startswith("abc-")  # the bad bytes became U+FFFD
    monkeypatch.setattr(sys, "stdin", None)
    assert context_hook.main() == 0


def test_a_payload_over_one_mebibyte_is_dropped_quietly(monkeypatch, token_file):
    big = event("PreToolUse", tool_name="Write", tool_input={"content": "x" * (context_hook.MAX_STDIN + 10)})
    assert run_main(monkeypatch, big, token_file) == (0, [])  # cut mid-way: not json, no report, no crash
    small = event("PreToolUse", tool_name="Write", tool_input={"content": "x" * (context_hook.MAX_STDIN // 2)})
    code, posts = run_main(monkeypatch, small, token_file)
    assert code == 0 and len(posts) == 1


# --- what the session said --------------------------------------------------------------


def said_of(text, **kw):
    return context_hook.build_report(event("Stop", last_assistant_message=text, **kw))["said"]


def test_stop_report_carries_the_last_assistant_message_cleaned_and_cut():
    r = context_hook.build_report(event("Stop", last_assistant_message="Terminé el login.\n\n**Falta** el test de sesión."))
    assert r == {"id": "11111111-2222-3333-4444-555555555555", "project": "relay", "state": "idle",
                 "said": "Terminé el login.\n\nFalta el test de sesión."}
    assert len(said_of("palabra " * 500)) <= context_hook.MAX_SAID_CHARS
    # only Stop carries it
    for name in ("PreToolUse", "UserPromptSubmit", "SessionEnd"):
        assert "said" not in context_hook.build_report(event(name, last_assistant_message="hola", tool_name="Bash"))
    assert context_hook.build_report(event("SubagentStop", last_assistant_message="hola")) is None
    # blank text is not a message: with no transcript to ask there is nothing to say (the transcript tests cover the rest)
    assert "said" not in context_hook.build_report(event("Stop", last_assistant_message="   \n "))


def test_said_is_cut_keeping_the_start_and_the_closing_question_at_word_boundaries():
    words = ["uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez"]
    body = " ".join(words[i % 10] + str(i) for i in range(120))
    text = "Listo el cambio de login. " + body + " ¿Querés que lo suba ahora?"
    said = said_of(text)
    assert len(said) <= context_hook.MAX_SAID_CHARS
    head, _, tail = said.partition(" … ")
    assert text.startswith(head) and head.startswith("Listo el cambio de login.")
    assert tail.endswith("¿Querés que lo suba ahora?") and text.endswith(tail)
    assert text[len(head)] == " " and text[len(text) - len(tail) - 1] == " "  # both cut on a space
    assert said.count(" … ") == 1
    assert said_of("corto y entero") == "corto y entero"


def test_markdown_is_flattened_and_code_fences_are_dropped():
    raw = ("# Título\n\n- primero\n* segundo\n> cita\n\nVer [la guía](https://example.com/g) y `pytest -q`.\n"
           "```python\nprint('no')\n```\nEditá __init__.py y **no** toques __main__.\n\n\n\nFin.")
    assert said_of(raw) == ("Título\n\n• primero\n• segundo\ncita\n\nVer la guía y pytest -q.\n(código)\n"
                            "Editá __init__.py y no toques __main__.\n\nFin.")
    assert said_of("antes ```print(1)``` después") == "antes (código) después"
    assert said_of("abierto ```print(1) sin cerrar") == "abierto (código)"
    assert said_of("a\x07b\u202ec\r\nd") == "a b c\nd"  # control and bidi characters become spaces


def test_secret_looking_text_is_scrubbed_and_paths_and_short_hashes_survive():
    fakes = {
        "anthropic": "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "aws id": "AKIA" + "IOSFODNN7EXAMPLE",
        "github": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "jwt": "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "SflKxwRJSMeKKF2QT4fw",
        "bearer": "Bearer " + "abcd1234efgh5678ijkl9012",
        "url": "https://usuario:" + "claveSecreta99@example.com/ruta",
        "password": "password=" + "hunter2hunter2",
        "secret": "secret: " + "abcdefgh12345678",
        "env token": "TOKEN=" + "abcd1234abcd1234abcd1234",
        "spanish": "contraseña: " + "hunter2hunter2",
        "aws secret": "aws " + "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY",
        "uuid": "123e4567-e89b-12d3-a456-" + "426614174000",
        "hash": "9f86d081884c7d659a2feaa0c55ad015" + "a3bf4f1b2b0b822cd15d6c15",
    }
    for name, fake in fakes.items():
        said = said_of("antes " + fake + " después")
        secret = fake.split(" ")[-1].split("=")[-1].split("@")[0].split(":")[-1]
        assert context_hook.REDACTED in said, name
        assert secret.rstrip("/") not in said, name
        assert said.startswith("antes ") and said.endswith(" después"), name
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nmasquebrado\n-----END RSA PRIVATE KEY-----"
    assert said_of("clave:\n" + pem + "\nfin") == "clave:\n" + context_hook.REDACTED + "\nfin"
    assert "MIIEvQ" not in said_of("clave:\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B")  # never closed
    keep = ("Editá /Users/example/Projects/claude-buddy/tools/context_hook.py (commit 7e5b924), corré "
            "tests/test_a_300_char_said_in_the_worst_encoding_fits_the_body_limit, la carpeta "
            "-Users-example-Projects-Mi-Proyecto-Grande-2026 y MAX_TASK_SAID_CHARS_300 y mirá el token de acceso.")
    assert said_of(keep) == keep  # a path, a short hash, an identifier and the word token are not secrets


def test_the_redaction_marker_is_not_the_cut_marker():
    assert context_hook.REDACTED != "…" and "…" not in context_hook.REDACTED
    said = said_of("x" * 300 + " sk-ant-api03-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2 " + "y" * 300)
    assert said.count(" … ") == 1


def test_a_hostile_reply_cannot_slow_the_hook_down():
    nasty = {
        "a- run": "a-" * 100_000,
        "spaces and newlines": (" " * 50 + "\n") * 4_000,
        "link openers": "[a](" * 50_000,
        "secret words": "token password bearer secret " * 7_000,
        "key prefixes": "sk-" * 70_000,
        "fence marks": "```" * 70_000,
        "equals": "password=" * 25_000,
        "one long word": "a1" * 100_000,
        "pem openers": "-----BEGIN PRIVATE KEY-----" * 7_500,
        "slashes": "a/b." * 50_000,
    }
    for name, text in nasty.items():
        assert len(text) >= 200_000 - 1000 or name in ("equals",), name
        start = time.perf_counter()
        said = context_hook.clean_said(text)
        took = time.perf_counter() - start
        assert took < 0.5, (name, took)
        assert len(said) <= context_hook.MAX_SAID_CHARS, name
    start = time.perf_counter()
    r = context_hook.build_report(event("Stop", last_assistant_message="a-" * 100_000))
    assert time.perf_counter() - start < 0.5 and len(r["said"]) <= 300


def test_only_the_start_and_the_end_of_a_long_reply_are_read():
    text = "Empiezo así. " + "relleno " * 3000 + "Y termino con una pregunta final: ¿seguimos?"
    said = said_of(text)
    assert said.startswith("Empiezo así.") and said.endswith("¿seguimos?")
    assert said_of("x" * 5000 + " " + "token=" + "abcd1234abcd1234abcd1234") .endswith(context_hook.REDACTED)  # the tail is read too


# --- the transcript, when the hook input has no last_assistant_message -----------------


def user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def assistant(mid, blocks, stop="end_turn", model="claude-x", **kw):
    return dict({"type": "assistant", "isSidechain": False,
                 "message": {"id": mid, "role": "assistant", "model": model, "stop_reason": stop, "content": blocks}}, **kw)


def text(s):
    return {"type": "text", "text": s}


def tool_result(size):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "y" * size}]}}


def attachment(size):
    return {"type": "attachment", "attachment": {"x": "y" * size}}


def transcript(tmp_path, *records, tail=b"", name="t.jsonl"):
    path = tmp_path / name
    with open(path, "wb") as fh:
        for rec in records:
            fh.write((json.dumps(rec) + "\n").encode())
        fh.write(tail)
    return path


def said_from(path):
    return context_hook.last_said_from_transcript(str(path))


def test_without_last_assistant_message_the_hook_reads_the_end_of_the_transcript(tmp_path):
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("respuesta final")]))
    assert said_from(path) == "respuesta final"
    r = context_hook.build_report(event("Stop", transcript_path=str(path)))
    assert r["said"] == "respuesta final"
    # one message written as two lines (a text block each): joined with a newline
    path = transcript(tmp_path, user("hola"), assistant("m2", [text("uno")]), assistant("m2", [text("dos")]))
    assert said_from(path) == "uno\ndos"
    # a half-written last line is skipped
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("completo")]), tail=b'{"type":"assistant","mess')
    assert said_from(path) == "completo"
    # the lines Claude Code appends after the reply do not matter
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("la respuesta")]),
                      {"type": "last-prompt", "x": 1}, {"type": "custom-title", "customTitle": "t"},
                      {"type": "system", "subtype": "stop_hook_summary"})
    assert said_from(path) == "la respuesta"
    # a typed prompt after the reply starts a new turn that has not answered yet
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("vieja")]), user("otra cosa"))
    assert said_from(path) is None


def test_the_transcript_window_grows_past_lines_of_over_a_megabyte(tmp_path):
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("respuesta final")]), attachment(1_200_000))
    assert said_from(path) == "respuesta final"  # a 1.2 MB line after the text
    path = transcript(tmp_path, user("hola"), assistant("m0", [{"type": "tool_use", "id": "t"}], "tool_use"),
                      tool_result(1_200_000), assistant("m1", [text("tras el archivo grande")]))
    assert said_from(path) == "tras el archivo grande"  # a 1.2 MB tool result before it
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("final")]), attachment(5_000_000))
    assert said_from(path) is None  # beyond the 4 MiB window: unknown, not empty


def test_the_transcript_fallback_ignores_sidechains_synthetic_errors_and_a_message_that_stopped_for_a_tool(tmp_path):
    real = assistant("m1", [text("final real")])
    path = transcript(tmp_path, user("hola"), real, assistant("m9", [text("subagente")], isSidechain=True))
    assert said_from(path) == "final real"
    path = transcript(tmp_path, user("hola"), real, assistant("m8", [text("No response requested.")], model="<synthetic>"))
    assert said_from(path) == "final real"
    path = transcript(tmp_path, user("hola"), real, assistant("m7", [text("API Error: 500")], isApiErrorMessage=True))
    assert said_from(path) == "final real"
    # the lag: the hook runs before the final line is written, so the last line is an earlier message of the turn
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("voy a mirar"), {"type": "tool_use", "id": "t"}], "tool_use"))
    assert said_from(path) is None
    # a turn-boundary message in the middle is not text of the final message
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("antes"), {"type": "tool_use", "id": "t"}], "tool_use"),
                      tool_result(10), assistant("m2", [text("después")]))
    assert said_from(path) == "después"


def test_a_final_message_with_no_text_sends_an_empty_said_and_a_failure_sends_none(tmp_path):
    thinking = transcript(tmp_path, user("hola"), assistant("m1", [text("voy a mirar"), {"type": "tool_use", "id": "t"}], "tool_use"),
                          tool_result(10), assistant("m2", [{"type": "thinking", "thinking": "x"}]))
    assert said_from(thinking) == ""
    r = context_hook.build_report(event("Stop", transcript_path=str(thinking)))
    assert r["said"] == ""  # verified: the final message has no text, the board clears the old one
    blank = transcript(tmp_path, user("hola"), assistant("m1", [text("   ")]), name="blank.jsonl")
    assert said_from(blank) == ""
    # a failure to find out sends NO said key at all (it must not clear a good previous one)
    for bad in (None, 5, "", str(tmp_path / "missing.jsonl"), str(tmp_path), "/etc/hosts"):
        assert context_hook.last_said_from_transcript(bad) is None, bad
        assert "said" not in context_hook.build_report(event("Stop", transcript_path=bad))
    not_jsonl = tmp_path / "t.txt"
    not_jsonl.write_text("{}", encoding="utf-8")
    assert said_from(not_jsonl) is None
    garbage = tmp_path / "g.jsonl"
    garbage.write_bytes(os.urandom(5000) + b"\n" + os.urandom(100))
    assert said_from(garbage) is None
    empty = tmp_path / "e.jsonl"
    empty.write_bytes(b"")
    assert said_from(empty) is None
    assert "said" not in context_hook.build_report(event("Stop"))  # no transcript_path at all


def test_the_transcript_must_be_ours(tmp_path, monkeypatch):
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("mía")]))
    assert said_from(path) == "mía"
    monkeypatch.setattr(os, "getuid", lambda: os.stat(str(path)).st_uid + 1)
    assert said_from(path) is None


def test_last_assistant_message_wins_over_the_transcript(tmp_path):
    path = transcript(tmp_path, user("hola"), assistant("m1", [text("del archivo")]))
    r = context_hook.build_report(event("Stop", transcript_path=str(path), last_assistant_message="del hook"))
    assert r["said"] == "del hook"
    for blank in ("", "  \n", None, 7):  # not a usable message: back to the transcript
        r = context_hook.build_report(event("Stop", transcript_path=str(path), last_assistant_message=blank))
        assert r["said"] == "del archivo", blank


def test_claude_buddy_said_off_sends_no_said_and_never_opens_the_transcript(monkeypatch):
    monkeypatch.setattr(context_hook, "last_said_from_transcript", lambda *a: pytest.fail("opened the transcript"))
    for value in ("off", "0", "no", "OFF", " off "):
        monkeypatch.setenv("CLAUDE_BUDDY_SAID", value)
        r = context_hook.build_report(event("Stop", transcript_path="/x.jsonl", last_assistant_message="hola"))
        assert r == {"id": "11111111-2222-3333-4444-555555555555", "project": "relay", "state": "idle"}


def test_claude_buddy_said_exclude_keeps_named_folders_silent(monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDDY_SAID_EXCLUDE", "scratchpad, relay-bitacoras ,,")
    assert "said" not in context_hook.build_report(event("Stop", cwd="/tmp/x/scratchpad", last_assistant_message="hola"))
    assert "said" not in context_hook.build_report(event("Stop", cwd="/a/relay-bitacoras", last_assistant_message="hola"))
    assert context_hook.build_report(event("Stop", cwd="/a/claude-buddy", last_assistant_message="hola"))["said"] == "hola"
    monkeypatch.delenv("CLAUDE_BUDDY_SAID_EXCLUDE")
    assert context_hook.build_report(event("Stop", cwd="", last_assistant_message="hola"))["said"] == "hola"
    monkeypatch.setenv("CLAUDE_BUDDY_SAID_EXCLUDE", ",")
    assert context_hook.build_report(event("Stop", cwd="", last_assistant_message="hola"))["said"] == "hola"
    # the exclusion does not stop the idle report itself, and the transcript of an excluded folder is never opened
    monkeypatch.setenv("CLAUDE_BUDDY_SAID_EXCLUDE", "scratchpad")
    monkeypatch.setattr(context_hook, "last_said_from_transcript", lambda *a: pytest.fail("opened the transcript"))
    r = context_hook.build_report(event("Stop", cwd="/tmp/scratchpad", transcript_path="/x.jsonl"))
    assert r == {"id": "11111111-2222-3333-4444-555555555555", "project": "scratchpad", "state": "idle"}


def test_said_is_only_sent_to_loopback_or_tailscale_peers():
    yes = ("127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1", "100.64.0.1", "100.101.102.103", "100.127.255.255",
           "fd7a:115c:a1e0::1", "fd7a:115c:a1e0:ab12:4843:cd96:6265:4c48", "::1%lo0")
    no = ("100.63.255.255", "100.128.0.1", "192.168.1.50", "10.0.0.5", "8.8.8.8", "::ffff:192.168.1.1",
          "fd7b:115c:a1e0::1", "2001:db8::1", "fe80::1%en0", "", "not an address", None, 5)
    for address in yes:
        assert context_hook.peer_may_hear_said(address), address
    for address in no:
        assert not context_hook.peer_may_hear_said(address), address


class FakeConn:
    """http.client.HTTPConnection that records what would be sent and says who the peer is."""
    peer = "127.0.0.1"
    bodies = []

    def __init__(self, host, port, timeout=None):
        self.sock = self

    def connect(self):
        pass

    def settimeout(self, value):
        pass

    def getpeername(self):
        return (FakeConn.peer, 8766)

    def request(self, method, path, body=None, headers=None):
        FakeConn.bodies.append(json.loads(body))

    def getresponse(self):
        return io.BytesIO(b"{}")

    def close(self):
        pass


def test_post_drops_said_unless_the_peer_is_loopback_or_tailscale(monkeypatch):
    monkeypatch.setattr(context_hook.http.client, "HTTPConnection", FakeConn)
    report = {"id": "a", "project": "p", "state": "idle", "said": "texto", "title": "T"}
    for peer, keeps in (("127.0.0.1", True), ("100.101.102.103", True), ("fd7a:115c:a1e0::5", True),
                        ("192.168.1.50", False), ("100.63.0.1", False), ("8.8.8.8", False)):
        FakeConn.peer, FakeConn.bodies = peer, []
        context_hook.post("http://buddy:8766", "x" * 20, report)
        assert len(FakeConn.bodies) == 1
        sent = FakeConn.bodies[0]
        assert ("said" in sent) is keeps, peer
        assert sent["title"] == "T" and sent["state"] == "idle"  # only said is held back


def test_the_hook_posts_said_to_the_real_listener_and_the_view_lists_it(tmp_path, token_file):
    rig = Rig(tmp_path / "state")

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            env = {"CLAUDE_BUDDY_URL": server.url, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file)}
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, run_hook, event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}), env)
            code, out, err = await loop.run_in_executor(
                None, run_hook, event("Stop", last_assistant_message="Terminé. ¿Lo reviso?"), env)
            assert (code, out) == (0, b""), err
            task = rig.view()["tasks"][0]
            assert task["state"] == "idle" and task["said"] == "Terminé. ¿Lo reviso?"
            assert (task["tool"], task["what"]) == ("Bash", "ls")  # the idle post keeps what it was doing
            # the next post that says nothing about it keeps it; an empty one clears it
            await loop.run_in_executor(None, run_hook, event("UserPromptSubmit"), env)
            assert rig.view()["tasks"][0]["said"] == "Terminé. ¿Lo reviso?"
            thinking = transcript(tmp_path, user("x"), assistant("m1", [{"type": "thinking", "thinking": "x"}]))
            await loop.run_in_executor(None, run_hook, event("Stop", transcript_path=str(thinking)), env)
            assert "said" not in rig.view()["tasks"][0]  # '' arrived: cleared
            # CLAUDE_BUDDY_SAID=off on the Mac: nothing sent
            await loop.run_in_executor(None, run_hook, event("Stop", last_assistant_message="no"), {**env, "CLAUDE_BUDDY_SAID": "off"})
            assert "said" not in rig.view()["tasks"][0]

    run(scenario)


# --- which conversation it is -----------------------------------------------------------

ORG_A = "5f0c9e1a-0000-4000-8000-00000000aaaa/7b3d2c10-0000-4000-8000-00000000bbbb"
ORG_B = "5f0c9e1a-0000-4000-8000-00000000cccc/7b3d2c10-0000-4000-8000-00000000dddd"


def local(n):
    return "local_00000000-0000-4000-8000-%012d" % n


def cli(n):
    return "cccccccc-0000-4000-8000-%012d" % n


class Desktop:
    """A made-up Claude desktop folder (sessions, config with groups, live-process list) and our cache."""

    def __init__(self, base):
        self.root = base / "desktop"
        self.cache = base / "cache"
        self.registry = base / "claude-config" / "sessions"
        self.config_path = self.root / "claude_desktop_config.json"

    def folder(self, scope=ORG_A):
        return self.root / "claude-code-sessions" / scope

    def path(self, n, scope=ORG_A):
        return self.folder(scope) / (local(n) + ".json")

    def session(self, n, cli_id=None, title="", scope=ORG_A, spawned_from=None, filler=0, pretty=False, title_late=False,
                spawned_raw=None, decoy_inside=None, write=True):
        data = {"sessionId": local(n)}
        if cli_id:
            data["cliSessionId"] = cli_id
        data["cwd"] = "/Users/example"
        if not title_late:
            data["title"] = title
        data["titleSource"] = "auto"
        data["lastActivityAt"] = 1760000000000
        data["isArchived"] = False
        # what really makes these files big: the connectors' tool schemas
        data["remoteMcpServersConfig"] = [{"name": "decoy", "accessToken": "DECOY-SECRET-TOKEN",
                                           "blob": "x" * filler, "note": decoy_inside or ""}]
        if title_late:
            data["title"] = title
        if spawned_from is not None:
            data["spawnedFrom"] = {"name": "padre", "sessionId": local(spawned_from), "taskId": "t1", "title": "Padre"}
        if spawned_raw is not None:
            data["spawnedFrom"] = spawned_raw
        if write:
            self.folder(scope).mkdir(parents=True, exist_ok=True)
            tmp = self.path(n, scope).with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2) if pretty else json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(str(tmp), str(self.path(n, scope)))  # the desktop replaces files by rename
        return data

    def config(self, groups, assignments, scope=ORG_A, other_scopes=None):
        """groups: {group id: name}; assignments: {session number: group id}."""
        scopes = {scope: {"groups": [{"id": gid, "name": name} for gid, name in groups.items()],
                          "assignments": {"code:" + local(n): gid for n, gid in assignments.items()}, "order": []}}
        scopes.update(other_scopes or {})
        data = {"coworkUserFilesPath": "/DECOY/files",
                "preferences": {"localAgentModeTrustedFolders": ["/DECOY-SECRET-FOLDER"],
                                "remoteToolsDeviceName": "DECOY-SECRET-DEVICE",
                                "epitaxyPrefs": {"dframe-group-scopes": scopes}}}
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(str(tmp), str(self.config_path))

    def live(self, pid, session_id, host=None):
        self.registry.mkdir(parents=True, exist_ok=True)
        entry = {"pid": pid, "sessionId": session_id, "cwd": "/Users/example", "status": "busy"}
        if host is not None:
            entry["hostSessionId"] = local(host)
        (self.registry / ("%d.json" % pid)).write_text(json.dumps(entry), encoding="utf-8")

    def cache_text(self):
        return (self.cache / "threads.json").read_text(encoding="utf-8")


@pytest.fixture
def desk(tmp_path):
    return Desktop(tmp_path)


def test_thread_info_maps_the_hook_session_to_the_desktop_title_and_group(desk):
    desk.session(1, cli(1), "Arreglar el login")
    desk.session(2, cli(2), "Cuadrar la pantalla")
    desk.config({"g1": "Acme Studio", "g2": "Buddy"}, {1: "g1", 2: "g2"})
    assert context_hook.thread_info(cli(1)) == {"title": "Arreglar el login", "group": "Acme Studio"}
    assert context_hook.thread_info(cli(2)) == {"title": "Cuadrar la pantalla", "group": "Buddy"}


def test_thread_info_unassigned_child_inherits_the_parents_group_and_names_the_parent_by_hook_id(desk):
    desk.session(1, cli(1), "Padre")
    desk.session(2, cli(2), "Hijo sin grupo propio", spawned_from=1)
    desk.session(3, cli(3), "Hijo con grupo propio", spawned_from=1)
    desk.config({"g1": "Acme Studio", "g2": "Buddy"}, {1: "g1", 3: "g2"})
    assert context_hook.thread_info(cli(2)) == {"title": "Hijo sin grupo propio", "group": "Acme Studio", "parent": cli(1)}
    # ASSUMPTION (never observed in the real sidebar): an own assignment beats the parent's
    assert context_hook.thread_info(cli(3)) == {"title": "Hijo con grupo propio", "group": "Buddy", "parent": cli(1)}
    # the parent itself has no parent
    assert "parent" not in context_hook.thread_info(cli(1))


def test_thread_info_names_the_nearest_ancestor_that_has_a_hook_id(desk):
    desk.session(1, cli(1), "Abuelo")
    desk.session(2, None, "Padre sin cliSessionId (una sesion limpiada)", spawned_from=1)
    desk.session(3, cli(3), "Nieto", spawned_from=2)
    desk.config({"g1": "Acme Studio"}, {1: "g1"})
    assert context_hook.thread_info(cli(3)) == {"title": "Nieto", "group": "Acme Studio", "parent": cli(1)}


def test_thread_info_desktop_session_outside_any_group_is_sesiones(desk):
    desk.session(1, cli(1), "Suelta")
    desk.session(2, cli(2), "Agrupada")
    desk.config({"g1": "Buddy"}, {2: "g1"})
    assert context_hook.thread_info(cli(1)) == {"title": "Suelta", "group": "Sesiones"}


def test_thread_info_without_a_group_it_can_trust_sends_no_group_at_all(desk):
    # absent means "keep what the board has": a bad read must never move a row to another group
    desk.session(1, cli(1), "Con titulo")
    desk.session(2, cli(2), "Hijo", spawned_from=1)
    only_title = {"title": "Con titulo"}
    assert context_hook.thread_info(cli(1)) == only_title  # no config file yet
    desk.root.mkdir(parents=True, exist_ok=True)
    desk.config_path.write_text("{not json", encoding="utf-8")
    assert context_hook.thread_info(cli(1)) == only_title
    desk.config_path.write_text("[]", encoding="utf-8")
    assert context_hook.thread_info(cli(1)) == only_title
    desk.config_path.write_text(json.dumps({"preferences": {"epitaxyPrefs": {}}}), encoding="utf-8")  # a renamed pref
    assert context_hook.thread_info(cli(1)) == only_title
    desk.config({"g1": "Buddy"}, {}, scope=ORG_B)  # the config knows another scope only
    assert context_hook.thread_info(cli(1)) == only_title
    desk.config({"g1": "Buddy"}, {1: "grupo-que-no-existe"})  # assigned to a group nobody can name
    assert context_hook.thread_info(cli(1)) == only_title
    desk.config({"g1": "Buddy"}, {1: "g1"})
    desk.config_path.write_bytes(desk.config_path.read_bytes() + b" " * (context_hook.MAX_CONFIG_BYTES + 1))
    assert context_hook.thread_info(cli(1)) == only_title  # a config over 1 MiB is not parsed
    desk.config({"g1": "Buddy"}, {1: "g1"})
    assert context_hook.thread_info(cli(1)) == {"title": "Con titulo", "group": "Buddy"}


def test_thread_info_terminal_or_unknown_session_gets_nothing(desk):
    desk.session(1, cli(1), "De escritorio")
    desk.config({"g1": "Buddy"}, {1: "g1"})
    desk.live(4242, "99999999-0000-4000-8000-000000000001")  # a live terminal process: no hostSessionId
    assert context_hook.thread_info("99999999-0000-4000-8000-000000000001") == {}
    assert context_hook.thread_info("99999999-0000-4000-8000-000000000002") == {}
    assert context_hook.thread_info(cli(1)) == {"title": "De escritorio", "group": "Buddy"}  # and the known one still works


def test_thread_info_reads_the_group_from_the_scope_named_like_the_files_directories(desk):
    desk.session(1, cli(1), "En la cuenta A", scope=ORG_A)
    desk.session(2, cli(2), "En la cuenta B", scope=ORG_B)
    scope_b = {ORG_B: {"groups": [{"id": "g1", "name": "Otro grupo"}], "assignments": {"code:" + local(2): "g1"}}}
    desk.config({"g1": "Acme Studio"}, {1: "g1"}, scope=ORG_A, other_scopes=scope_b)
    assert context_hook.thread_info(cli(1)) == {"title": "En la cuenta A", "group": "Acme Studio"}
    assert context_hook.thread_info(cli(2)) == {"title": "En la cuenta B", "group": "Otro grupo"}


def test_thread_info_title_is_one_line_at_most_60_chars(desk):
    desk.session(1, cli(1), "una\nlinea\tcon   saltos\r\ny   espacios")
    desk.session(2, cli(2), "t" * 200)
    desk.session(3, cli(3), EMOJI * 100)
    desk.session(4, cli(4), "   ")
    desk.config({}, {})
    assert context_hook.thread_info(cli(1))["title"] == "una linea con saltos y espacios"
    assert context_hook.thread_info(cli(2))["title"] == "t" * 60
    assert context_hook.thread_info(cli(3))["title"] == EMOJI * 60
    assert "title" not in context_hook.thread_info(cli(4))  # nothing to say: the board keeps what it has


def test_thread_info_never_raises_on_corrupt_truncated_missing_or_huge_files(desk, tmp_path):
    desk.session(1, cli(1), "Entera")
    desk.config({"g1": "Buddy"}, {1: "g1"})
    whole = desk.path(1).read_bytes()
    good = {"title": "Entera", "group": "Buddy"}
    assert context_hook.thread_info(cli(1), now=1000.0) == good
    for broken in (b"", b"{", b"\xff\xfe\x00garbage", whole[: len(whole) // 2], b"[]", b"null", b'{"sessionId": 5}',
                   b'{"sessionId":"local_00000000-0000-4000-8000-000000000001","cliSessionId":"' + cli(1).encode() + b'"',
                   b"{" * 100_000):
        desk.path(1).write_bytes(broken)
        result = context_hook.thread_info(cli(1), now=2000.0 + len(broken))  # always past the cache time
        assert isinstance(result, dict) and set(result) <= {"title", "group", "parent"}
        assert result.get("title") in (None, "Entera")  # never a title that is not in the file
    desk.path(1).write_bytes(b"x" * (context_hook.MAX_FILE_BYTES + 1000))  # 8 MiB and a bit
    assert context_hook.thread_info(cli(1), now=9000.0) == {}
    desk.path(1).unlink()
    assert context_hook.thread_info(cli(1), now=9500.0) == {}
    # folders and files that are not what they should be
    for victim in ("claude-code-sessions", "claude_desktop_config.json"):
        target = desk.root / victim
        if target.is_dir():
            import shutil
            shutil.rmtree(str(target))
        else:
            target.unlink()
    assert context_hook.thread_info(cli(1), now=9600.0) == {}
    (desk.root / "claude-code-sessions").write_text("a file, not a folder", encoding="utf-8")
    assert context_hook.thread_info(cli(1), now=9700.0) == {}
    # a config whose pieces have the wrong types
    desk.config({"g1": "Buddy"}, {})
    for weird in ({"groups": "x", "assignments": []}, {"groups": [5, None, {"id": 1}], "assignments": {"code:" + local(1): ["g"]}},
                  None, [], "s"):
        data = json.loads(desk.config_path.read_text(encoding="utf-8"))
        data["preferences"]["epitaxyPrefs"]["dframe-group-scopes"][ORG_A] = weird
        desk.config_path.write_text(json.dumps(data), encoding="utf-8")
        assert isinstance(context_hook.thread_info(cli(1), now=9800.0), dict)


def test_thread_info_survives_a_cache_it_cannot_read_or_write(desk, tmp_path):
    desk.session(1, cli(1), "Entera")
    desk.config({"g1": "Buddy"}, {1: "g1"})
    good = {"title": "Entera", "group": "Buddy"}
    desk.cache.parent.mkdir(parents=True, exist_ok=True)
    desk.cache.write_text("a file where the cache folder should be", encoding="utf-8")  # cannot make the folder
    assert context_hook.thread_info(cli(1)) == good
    desk.cache.unlink()
    desk.cache.mkdir()
    (desk.cache / "threads.json").mkdir()  # a folder where the cache file should be
    assert context_hook.thread_info(cli(1)) == good
    (desk.cache / "threads.json").rmdir()
    desk.cache.chmod(0o500)  # read-only folder: cannot write
    try:
        assert context_hook.thread_info(cli(1)) == good
    finally:
        desk.cache.chmod(0o700)


def write_cache(desk, text, mode=0o600):
    desk.cache.mkdir(parents=True, exist_ok=True)
    path = desk.cache / "threads.json"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def test_thread_info_cache_is_private_and_holds_nothing_from_the_config(desk):
    desk.session(1, cli(1), "Privada")
    desk.session(2, cli(2), "Hija", spawned_from=1)
    desk.config({"g1": "Grupo Muy Privado"}, {1: "g1"})
    assert context_hook.thread_info(cli(1)) and context_hook.thread_info(cli(2))
    assert context_hook.thread_info("99999999-0000-4000-8000-000000000001") == {}
    cache = desk.cache / "threads.json"
    assert (cache.stat().st_mode & 0o777) == 0o600 and (desk.cache.stat().st_mode & 0o777) == 0o700
    text = desk.cache_text()
    data = json.loads(text)
    assert data["v"] == 1 and set(data) == {"v", "sessions", "miss"}
    assert set(data["sessions"][cli(2)]) == {"sid", "scope", "title", "parent", "anc", "mt", "sz", "at"}
    assert data["sessions"][cli(2)]["parent"] == cli(1) and "99999999-0000-4000-8000-000000000001" in data["miss"]
    # nothing of the config (group names, the connector settings) and nothing of the session files' big parts
    for private in ("Grupo Muy Privado", "DECOY", "SECRET", "g1", "accessToken", "blob", "epitaxy", "assignments"):
        assert private not in text, private
    assert not list(desk.cache.glob("*.tmp"))  # no temporary file left behind


def test_thread_info_does_not_trust_a_cache_that_is_not_private(desk):
    desk.session(1, cli(1), "El verdadero")
    desk.config({}, {})
    poisoned = {"v": 1, "sessions": {cli(1): {"sid": local(1), "scope": ORG_A, "title": "ENVENENADO", "parent": "",
                                              "anc": [], "mt": 1, "sz": 1, "at": time.time()}}, "miss": {}}
    write_cache(desk, json.dumps(poisoned), mode=0o644)  # readable by others: somebody else could have written it
    assert context_hook.thread_info(cli(1))["title"] == "El verdadero"
    assert (desk.cache / "threads.json").stat().st_mode & 0o077 == 0  # and ours replaced it, private
    write_cache(desk, json.dumps(poisoned), mode=0o666)
    assert context_hook.thread_info(cli(1))["title"] == "El verdadero"
    write_cache(desk, "x" * (context_hook.MAX_CACHE_BYTES + 1))  # too big
    assert context_hook.thread_info(cli(1))["title"] == "El verdadero"


def test_thread_info_throws_away_cache_entries_that_are_not_shaped_right(desk):
    desk.session(1, cli(1), "El verdadero")
    desk.session(2, cli(2), "Otra")
    desk.config({}, {})
    now = 5000.0
    good = {"sid": local(2), "scope": ORG_A, "title": "Otra", "parent": "", "anc": [], "mt": 1, "sz": 1, "at": now}
    bad_entries = {"x": 5, cli(1): "string", "../evil": good,
                   "e1": dict(good, sid="local_../../../etc"), "e2": dict(good, scope="../../etc"), "e3": dict(good, scope="a/../b"),
                   "e4": dict(good, title=["x"]), "e5": dict(good, parent="no good!"), "e6": dict(good, anc="local_x"),
                   "e7": dict(good, anc=[5]), "e8": dict(good, mt="1"), "e9": dict(good, sz=True), "e10": dict(good, at="now"),
                   "e11": dict(good, at=None), "e12": dict(good, title="t" * 61), "e13": {k: v for k, v in good.items() if k != "at"},
                   "e14": dict(good, anc=[local(1)] * 9)}
    write_cache(desk, json.dumps({"v": 1, "sessions": bad_entries, "miss": {"m1": "yesterday", "m2": [], "../m3": 5.0}}))
    assert context_hook.thread_info(cli(1), now=now) == {"title": "El verdadero", "group": "Sesiones"}  # worked, no AttributeError
    saved = json.loads(desk.cache_text())
    assert set(saved["sessions"]) == {cli(1)} and saved["miss"] == {}
    for wrong in ('{"v": 2, "sessions": {}, "miss": {}}', '{"v": 1, "sessions": [], "miss": {}}', "[]", "null", "{"):
        write_cache(desk, wrong)
        assert context_hook.thread_info(cli(2), now=now)["title"] == "Otra"


def test_the_default_cache_folder_is_tightened_to_private(tmp_path, monkeypatch, desk):
    home = tmp_path / "home"
    (home / ".cache" / "claude-buddy").mkdir(parents=True)
    (home / ".cache" / "claude-buddy").chmod(0o755)  # made by somebody else's umask
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_BUDDY_CACHE_DIR")
    desk.session(1, cli(1), "x")
    desk.config({}, {})
    assert context_hook.thread_info(cli(1))
    folder = home / ".cache" / "claude-buddy"
    assert (folder.stat().st_mode & 0o777) == 0o700 and (folder / "threads.json").stat().st_mode & 0o777 == 0o600
    # an explicit CLAUDE_BUDDY_CACHE_DIR that already exists is left as it is
    other = tmp_path / "shared"
    other.mkdir()
    other.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BUDDY_CACHE_DIR", str(other))
    assert context_hook.thread_info(cli(1))
    assert (other.stat().st_mode & 0o777) == 0o755 and (other / "threads.json").stat().st_mode & 0o777 == 0o600


def test_thread_info_ttl_rename_after_30s_group_move_at_once_deleted_file_becomes_unknown(desk):
    desk.session(1, cli(1), "Nombre viejo")
    desk.config({"g1": "Buddy", "g2": "Homelab"}, {1: "g1"})
    t0 = 100000.0
    assert context_hook.thread_info(cli(1), now=t0) == {"title": "Nombre viejo", "group": "Buddy"}
    desk.session(1, cli(1), "Nombre nuevo y mas largo")
    assert context_hook.thread_info(cli(1), now=t0 + 10)["title"] == "Nombre viejo"  # still the cached one
    assert context_hook.thread_info(cli(1), now=t0 + 29.9)["title"] == "Nombre viejo"
    assert context_hook.thread_info(cli(1), now=t0 + 31)["title"] == "Nombre nuevo y mas largo"
    # a group move shows on the very next event, whatever the cache says
    desk.config({"g1": "Buddy", "g2": "Homelab"}, {1: "g2"})
    assert context_hook.thread_info(cli(1), now=t0 + 32)["group"] == "Homelab"
    desk.config({"g1": "Buddy", "g2": "Homelab"}, {})
    assert context_hook.thread_info(cli(1), now=t0 + 33)["group"] == "Sesiones"
    # an unchanged file past its time is not read again, only stat'ed
    reads = []
    original = context_hook._read_session
    context_hook._read_session = lambda path: (reads.append(path), original(path))[1]
    try:
        assert context_hook.thread_info(cli(1), now=t0 + 70)["title"] == "Nombre nuevo y mas largo"
        assert reads == []
    finally:
        context_hook._read_session = original
    desk.path(1).unlink()
    assert context_hook.thread_info(cli(1), now=t0 + 120) == {}  # the conversation is gone: unknown again
    # a clock that went backwards does not freeze an entry
    desk.session(1, cli(1), "Vuelta")
    assert context_hook.thread_info(cli(1), now=t0 + 140)["title"] == "Vuelta"
    desk.session(1, cli(1), "Vuelta otra vez")
    assert context_hook.thread_info(cli(1), now=t0 - 5000)["title"] == "Vuelta otra vez"


def test_thread_info_miss_is_not_searched_again_inside_10s_and_warm_path_opens_no_desktop_file(desk, monkeypatch):
    desk.session(1, cli(1), "Conocida")
    desk.config({"g1": "Buddy"}, {1: "g1"})
    calls = {"head": 0, "registry": 0, "scan": 0, "session": 0}
    for name, key in (("_find_by_head", "head"), ("_registry_host", "registry"), ("_scope_dirs", "scan"), ("_read_session", "session")):
        def spy(*a, _orig=getattr(context_hook, name), _key=key, **k):
            calls[_key] += 1
            return _orig(*a, **k)
        monkeypatch.setattr(context_hook, name, spy)
    unknown = "99999999-0000-4000-8000-000000000009"
    t0 = 7000.0
    assert context_hook.thread_info(unknown, now=t0) == {}
    assert calls["head"] == 1 and calls["registry"] == 1
    for dt in (0.1, 3, 9.9):
        assert context_hook.thread_info(unknown, now=t0 + dt) == {}
    assert calls["head"] == 1 and calls["registry"] == 1  # inside the 10 s: no search at all
    assert context_hook.thread_info(unknown, now=t0 + 10.5) == {}
    assert calls["head"] == 2  # and then one more, no more than that
    # a known session inside its 30 s opens no desktop session file and lists no folder
    assert context_hook.thread_info(cli(1), now=t0) == {"title": "Conocida", "group": "Buddy"}
    before = dict(calls)
    opened = []
    real_open = open

    def watching_open(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", watching_open)
    for dt in (1, 5, 29):
        assert context_hook.thread_info(cli(1), now=t0 + dt)["title"] == "Conocida"
    assert calls == before
    assert not [p for p in opened if "claude-code-sessions" in p]  # only the cache and the config were opened


def test_thread_info_a_brand_new_session_is_found_when_the_desktop_writes_its_id(desk):
    desk.session(1, None, "Nueva")  # the desktop has not written cliSessionId yet
    desk.config({}, {})
    t0 = 400.0
    assert context_hook.thread_info(cli(1), now=t0) == {}
    assert context_hook.thread_info(cli(1), now=t0 + 5) == {}  # inside the miss time
    desk.session(1, cli(1), "Nueva")
    assert context_hook.thread_info(cli(1), now=t0 + 11)["title"] == "Nueva"


def test_thread_info_spawned_from_cycle_terminates_and_never_names_itself_as_parent(desk):
    desk.session(1, cli(1), "Uno", spawned_from=2)
    desk.session(2, cli(2), "Dos", spawned_from=1)
    desk.session(3, cli(3), "Tres, hijo de si mismo", spawned_from=3)
    desk.config({}, {})
    assert context_hook.thread_info(cli(1)) == {"title": "Uno", "group": "Sesiones", "parent": cli(2)}
    assert context_hook.thread_info(cli(2)) == {"title": "Dos", "group": "Sesiones", "parent": cli(1)}
    assert context_hook.thread_info(cli(3)) == {"title": "Tres, hijo de si mismo", "group": "Sesiones"}
    # a parent whose hook id is the child's own (a corrupt pair): no parent, nobody is their own parent
    desk.session(4, cli(4), "Cuatro", spawned_from=5)
    desk.session(5, cli(4), "Cinco: mismo id que su hijo")
    assert context_hook.thread_info(cli(4)) == {"title": "Cuatro", "group": "Sesiones"}


def test_thread_info_follows_at_most_four_levels_of_ancestors(desk):
    desk.session(1, cli(1), "Raiz")
    for n in range(2, 8):
        desk.session(n, cli(n), "Nivel %d" % n, spawned_from=n - 1)
    desk.config({"g1": "Acme Studio"}, {1: "g1"})
    assert context_hook.thread_info(cli(5))["group"] == "Acme Studio"  # 4 levels up
    assert context_hook.thread_info(cli(6))["group"] == "Sesiones"  # 5 levels: given up
    assert context_hook.thread_info(cli(7))["parent"] == cli(6)


def test_thread_info_registry_fast_path_finds_a_session_whose_file_has_no_cli_id_yet(desk):
    desk.session(1, None, "Recien creada")  # no cliSessionId in the file
    desk.config({"g1": "Buddy"}, {1: "g1"})
    assert context_hook.thread_info(cli(1), now=1.0) == {}
    desk.live(777, cli(1), host=1)  # but Claude Code's own live list knows which desktop session hosts it
    assert context_hook.thread_info(cli(1), now=20.0) == {"title": "Recien creada", "group": "Buddy"}
    # a host that is not a desktop id, or whose file does not exist, falls back to the first bytes
    desk.live(778, cli(2), host=99)
    desk.session(2, cli(2), "Por los bytes")
    assert context_hook.thread_info(cli(2), now=40.0)["title"] == "Por los bytes"
    (desk.registry / "779.json").write_text(json.dumps({"sessionId": cli(3), "hostSessionId": "../../etc/passwd"}), encoding="utf-8")
    desk.session(3, cli(3), "Tres")
    assert context_hook.thread_info(cli(3), now=60.0)["title"] == "Tres"
    # a registry of junk is no problem
    (desk.registry / "780.json").write_text("{not json", encoding="utf-8")
    (desk.registry / "781.json").write_text("[]", encoding="utf-8")
    (desk.registry / "782.key").write_text("not read at all", encoding="utf-8")
    assert context_hook.thread_info("99999999-0000-4000-8000-000000000001", now=80.0) == {}


def test_thread_info_off_switch_and_bad_ids(desk, monkeypatch):
    desk.session(1, cli(1), "Hay")
    desk.config({"g1": "Buddy"}, {1: "g1"})
    for value in ("off", "0", "no", "OFF", " No "):
        monkeypatch.setenv("CLAUDE_BUDDY_THREADS", value)
        assert context_hook.thread_info(cli(1)) == {}
    assert not desk.cache.exists()  # off means off: not even the cache is touched
    monkeypatch.setenv("CLAUDE_BUDDY_THREADS", "on")
    assert context_hook.thread_info(cli(1))["title"] == "Hay"
    monkeypatch.delenv("CLAUDE_BUDDY_THREADS")
    touched = []
    monkeypatch.setattr(context_hook, "_scope_dirs", lambda root: touched.append(root) or [])
    for bad in ("", None, 5, ["a"], "x" * 65, "../../etc/passwd", "a b", "a\nb", "ñ", "a/b", "a\x00b"):
        assert context_hook.thread_info(bad) == {}, bad
    assert touched == []


def test_thread_info_tells_the_head_walk_from_a_full_parse_the_same_thing(desk, monkeypatch):
    big = 400_000
    parsed = []
    real_loads = json.loads

    def loads(data, *a, **k):
        parsed.append(len(data))
        return real_loads(data, *a, **k)

    monkeypatch.setattr(context_hook.json, "loads", loads)
    desk.session(1, cli(1), "Titulo", filler=big)
    desk.session(2, cli(2), "Hijo", filler=big, spawned_from=1)
    desk.config({}, {})
    parsed.clear()
    assert context_hook._read_session(desk.path(2))["title"] == "Hijo"
    assert context_hook._read_session(desk.path(2))["spawned"] == local(1)
    assert max(parsed or [0]) < 10_000  # the quick way: the big parts of the file are never parsed
    # the same answers when the file looks different
    cases = {
        "pretty printed": dict(pretty=True),
        "title after the big part": dict(title_late=True),
        "a lookalike inside a string": dict(decoy_inside='texto "spawnedFrom":{"sessionId":"%s"} y mas' % local(9)),
        "null spawnedFrom": dict(spawned_raw=None),
    }
    for name, kw in cases.items():
        desk.session(3, cli(3), "Caso", filler=big, **kw)
        info = context_hook._read_session(desk.path(3))
        assert (info["title"], info["cli"], info["spawned"]) == ("Caso", cli(3), ""), name
    desk.session(3, cli(3), "Caso", filler=big, spawned_raw={"sessionId": local(1)}, title_late=True)
    assert context_hook._read_session(desk.path(3))["spawned"] == local(1)
    desk.session(3, cli(3), "Caso", filler=big, spawned_raw={"sessionId": "no es un id"})
    assert context_hook._read_session(desk.path(3))["spawned"] == ""
    desk.session(3, cli(3), "Caso", filler=big, spawned_raw=None)
    desk.path(3).write_bytes(b"\xef\xbb\xbf" + desk.path(3).read_bytes())  # a byte order mark
    assert context_hook._read_session(desk.path(3))["title"] == "Caso"
    assert context_hook.thread_info(cli(2)) == {"title": "Hijo", "group": "Sesiones", "parent": cli(1)}


def test_the_hook_posts_title_group_and_parent_and_not_for_session_end(desk, tmp_path, token_file, monkeypatch):
    desk.session(1, cli(1), "Padre")
    desk.session(2, cli(2), "Hijo", spawned_from=1)
    desk.config({"g1": "Acme Studio"}, {1: "g1"})
    code, posts = run_main(monkeypatch, event("PreToolUse", session_id=cli(2), tool_name="Bash", tool_input={"command": "ls"}), token_file)
    assert code == 0 and posts == [{"id": cli(2), "project": "relay", "state": "working", "tool": "Bash", "what": "ls",
                                   "title": "Hijo", "group": "Acme Studio", "parent": cli(1)}]
    code, posts = run_main(monkeypatch, event("UserPromptSubmit", session_id=cli(1)), token_file)
    assert posts[0]["title"] == "Padre" and posts[0]["group"] == "Acme Studio" and "parent" not in posts[0]
    code, posts = run_main(monkeypatch, event("Stop", session_id=cli(1), last_assistant_message="listo"), token_file)
    assert posts[0]["said"] == "listo" and posts[0]["title"] == "Padre"
    code, posts = run_main(monkeypatch, event("SessionEnd", session_id=cli(1)), token_file)
    assert posts == [{"id": cli(1), "project": "relay", "state": "gone"}]  # a session that ends is told nothing else
    # a terminal session: nothing but what it always sent
    code, posts = run_main(monkeypatch, event("UserPromptSubmit"), token_file)
    assert posts == [{"id": "11111111-2222-3333-4444-555555555555", "project": "relay", "state": "working",
                      "tool": "", "what": "nuevo pedido"}]
    monkeypatch.setenv("CLAUDE_BUDDY_THREADS", "off")
    code, posts = run_main(monkeypatch, event("UserPromptSubmit", session_id=cli(1)), token_file)
    assert set(posts[0]) == {"id", "project", "state", "tool", "what"}
    monkeypatch.delenv("CLAUDE_BUDDY_THREADS")
    # when the lookup itself breaks, the report still goes out as it was
    monkeypatch.setattr(context_hook, "_load_cache", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    code, posts = run_main(monkeypatch, event("UserPromptSubmit", session_id=cli(1)), token_file)
    assert code == 0 and posts[0]["state"] == "working" and "title" not in posts[0]


def test_the_listener_shows_the_threads_the_hook_reports(desk, tmp_path, token_file):
    desk.session(1, cli(1), "Padre")
    desk.session(2, cli(2), "Hijo", spawned_from=1)
    desk.config({"g1": "Acme Studio"}, {1: "g1"})
    rig = Rig(tmp_path / "state")

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            env = {"CLAUDE_BUDDY_URL": server.url, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file)}
            loop = asyncio.get_running_loop()
            for n in (1, 2):
                code, out, err = await loop.run_in_executor(
                    None, run_hook, event("PreToolUse", session_id=cli(n), tool_name="Bash", tool_input={"command": "ls"}), env)
                assert (code, out, err) == (0, b"", b"")
            by_id = {t["id"]: t for t in rig.view()["tasks"]}
            assert by_id[cli(1)]["title"] == "Padre" and by_id[cli(1)]["group"] == "Acme Studio"
            assert "parent" not in by_id[cli(1)]
            assert by_id[cli(2)]["title"] == "Hijo" and by_id[cli(2)]["parent"] == cli(1)
            # a terminal session shows no title and no group
            await loop.run_in_executor(None, run_hook, event("UserPromptSubmit"), env)
            term = {t["id"]: t for t in rig.view()["tasks"]}["11111111-2222-3333-4444-555555555555"]
            assert "title" not in term and "group" not in term and term["project"] == "relay"
            await loop.run_in_executor(None, run_hook, event("SessionEnd", session_id=cli(2)), env)
            assert cli(2) not in {t["id"] for t in rig.view()["tasks"]}

    run(scenario)


def test_post_body_stays_under_the_server_cap_with_every_field_astral(tmp_path, token_file):
    from buddy_ui import MAX_BODY_BYTES
    assert context_hook.MAX_POST_BYTES < MAX_BODY_BYTES
    ident = "a" * 64
    worst = {"id": ident, "project": EMOJI * 40, "state": "idle", "said": EMOJI * 300, "title": EMOJI * 60,
             "group": EMOJI * 40, "parent": "b" * 64}
    assert len(json.dumps(worst)) > MAX_BODY_BYTES  # what it would be without the guard: a 413 and a lost report
    body = context_hook.fit_body(worst)
    sent = json.loads(body)
    assert len(body) <= context_hook.MAX_POST_BYTES < MAX_BODY_BYTES
    assert "said" not in sent  # the optional text goes first
    assert (sent["id"], sent["project"], sent["state"]) == (ident, EMOJI * 40, "idle")
    assert sent["title"] == EMOJI * 60 and sent["parent"] == "b" * 64 and sent["group"] == EMOJI * 40  # nothing else needed to go
    # a working report with the longest what and tool, and every thread field astral, fits without losing anything
    working = {"id": ident, "project": EMOJI * 40, "state": "working", "tool": EMOJI * 40, "what": EMOJI * 120,
               "title": EMOJI * 60, "group": EMOJI * 40, "parent": "b" * 64}
    assert context_hook.fit_body(working) == json.dumps(working).encode("utf-8")
    assert len(context_hook.fit_body(working)) < MAX_BODY_BYTES
    # when even that is too much, the optional fields shrink in a fixed order and the essentials stay
    huge = dict(worst, title="t" * 5000, group="g" * 5000, parent="p" * 5000)
    body = context_hook.fit_body(huge)
    sent = json.loads(body)
    assert len(body) <= context_hook.MAX_POST_BYTES and (sent["id"], sent["state"]) == (ident, "idle")
    assert "said" not in sent and "parent" not in sent and len(sent.get("title", "")) <= 24 and len(sent.get("group", "")) <= 20
    # a normal report is not touched
    small = {"id": "abc", "project": "relay", "state": "idle", "said": "listo", "title": "T", "group": "G", "parent": "p"}
    assert context_hook.fit_body(small) == json.dumps(small).encode("utf-8")

    # and the real listener takes the worst case a real hook can build
    desk = Desktop(tmp_path)
    desk.session(1, cli(1), EMOJI * 100)
    desk.session(2, cli(2), EMOJI * 100, spawned_from=1)
    desk.config({"g1": EMOJI * 80}, {1: "g1"})
    rig = Rig(tmp_path / "state")

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            env = {"CLAUDE_BUDDY_URL": server.url, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file)}
            loop = asyncio.get_running_loop()
            payload = event("Stop", session_id=cli(2), cwd="/x/" + EMOJI * 100, last_assistant_message=EMOJI * 2000)
            code, out, err = await loop.run_in_executor(None, run_hook, payload, env)
            assert (code, out) == (0, b""), err
            task = rig.view()["tasks"][0]  # a 413 would have left the board empty
            assert task["state"] == "idle" and task["title"] == EMOJI * 60 and task["group"] == EMOJI * 40
            assert task["parent"] == cli(1) and task["project"] == EMOJI * 40

    run(scenario)


def test_the_hook_runs_under_system_python39(tmp_path, token_file):
    if not os.path.exists(SYSTEM_PYTHON):
        pytest.skip("no system python")
    version = subprocess.run([SYSTEM_PYTHON, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
                             capture_output=True, text=True).stdout.strip()
    if version != "True":
        pytest.skip("/usr/bin/python3 is not 3.9 here")
    compiled = subprocess.run(
        [SYSTEM_PYTHON, "-c", "import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
         str(HOOK), str(tmp_path / "hook.pyc")], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    desk = Desktop(tmp_path)
    desk.session(1, cli(1), "Bajo el Python de macOS", spawned_from=None)
    desk.config({"g1": "Buddy"}, {1: "g1"})
    rig = Rig(tmp_path / "state")

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            env = {"CLAUDE_BUDDY_URL": server.url, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file)}
            loop = asyncio.get_running_loop()
            code, out, err = await loop.run_in_executor(
                None, lambda: run_hook(event("Stop", session_id=cli(1), last_assistant_message="Hecho."), env, python=SYSTEM_PYTHON))
            assert (code, out, err) == (0, b"", b"")
            task = rig.view()["tasks"][0]
            assert (task["title"], task["group"], task["said"]) == ("Bajo el Python de macOS", "Buddy", "Hecho.")

    run(scenario)


def test_the_hook_costs_little_when_nothing_is_cached(desk, tmp_path, token_file):
    # a loose guard, not a benchmark: a cold lookup in a big folder still leaves the hook far under its 5 s
    for n in range(1, 40):
        desk.session(n, cli(n), "Sesion %d" % n, filler=60_000)
    desk.config({"g1": "Buddy"}, {39: "g1"})
    start = time.perf_counter()
    assert context_hook.thread_info(cli(39))["group"] == "Buddy"
    assert time.perf_counter() - start < 1.0
    start = time.perf_counter()
    assert context_hook.thread_info(cli(39))["group"] == "Buddy"  # warm
    assert time.perf_counter() - start < 0.2


def test_the_cache_keeps_at_most_64_recent_sessions(desk):
    desk.session(1, cli(1), "La nueva")
    desk.config({}, {})
    now = 10_000_000.0
    sessions = {}
    for n in range(100, 180):  # 80 entries, the oldest ones first
        sessions["old-%d" % n] = {"sid": local(n), "scope": ORG_A, "title": "t", "parent": "", "anc": [], "mt": 1,
                                  "sz": 1, "at": now - 4000 + n}
    sessions["ancient"] = {"sid": local(9), "scope": ORG_A, "title": "t", "parent": "", "anc": [], "mt": 1, "sz": 1,
                           "at": now - 2 * 86400}
    write_cache(desk, json.dumps({"v": 1, "sessions": sessions,
                                  "miss": dict({"m-%d" % n: now - 100 - n for n in range(80)}, **{"m-old": now - 7200})}))
    assert context_hook.thread_info(cli(1), now=now)["title"] == "La nueva"
    saved = json.loads(desk.cache_text())
    assert len(saved["sessions"]) == 64 and cli(1) in saved["sessions"] and "ancient" not in saved["sessions"]
    assert "old-179" in saved["sessions"] and "old-100" not in saved["sessions"]  # the newest stay
    assert len(saved["miss"]) <= 64 and "m-old" not in saved["miss"]


def test_many_hooks_at_once_leave_a_valid_private_cache(desk):
    for n in range(1, 13):
        desk.session(n, cli(n), "Hilo %d" % n)
    desk.config({"g1": "Buddy"}, {n: "g1" for n in range(1, 13)})
    code = ("import sys; sys.path.insert(0, %r); import context_hook as h\n"
            "for _ in range(15):\n"
            "    r = h.thread_info(sys.argv[1])\n"
            "    assert r.get('group') == 'Buddy', r\n" % str(HOOK.parent))
    procs = [subprocess.Popen([sys.executable, "-c", code, cli(n)], stderr=subprocess.PIPE) for n in range(1, 13)]
    for proc in procs:
        assert proc.wait(timeout=60) == 0, proc.stderr.read()
    data = json.loads(desk.cache_text())  # whatever got lost, what is there is whole
    assert data["v"] == 1 and set(data["sessions"]) <= {cli(n) for n in range(1, 13)}
    assert (desk.cache / "threads.json").stat().st_mode & 0o777 == 0o600
    assert not list(desk.cache.glob("*.tmp"))


# --- a buddy that does not answer: a hard deadline, and no retry on every tool call ---------------------------------


def _down_file(tmp_path):
    return tmp_path / "cache" / context_hook.DOWN_FILE


def _no_timers_left():
    for timer in [t for t in threading.enumerate() if isinstance(t, threading.Timer)]:
        timer.join(2)
    return not any(isinstance(t, threading.Timer) and t.is_alive() for t in threading.enumerate())


def test_a_refused_post_leaves_the_buddy_alone_for_tool_calls_but_not_for_stop_or_session_end(monkeypatch, tmp_path, token_file):
    attempts = []
    answers = {"up": False}

    def post(url, token, report):
        attempts.append(report["state"])
        if not answers["up"]:
            raise ConnectionRefusedError("buddy is off")

    def looks_up(*a, **k):
        raise AssertionError("work was done for a buddy that did not answer a moment ago")

    monkeypatch.setenv("CLAUDE_BUDDY_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(context_hook, "post", post)

    def fire(payload):
        monkeypatch.setattr(sys, "stdin", FakeStdin(json.dumps(payload).encode()))
        return context_hook.main()

    assert fire(event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"})) == 0
    assert attempts == ["working"]  # the first call pays for finding out
    marker = _down_file(tmp_path)
    assert marker.stat().st_mode & 0o777 == 0o600 and (tmp_path / "cache").stat().st_mode & 0o777 == 0o700
    assert 25 < float(marker.read_text()) - time.time() <= context_hook.DOWN_S + 1  # about 30 s from now
    real_build, real_info = context_hook.build_report, context_hook.thread_info
    monkeypatch.setattr(context_hook, "build_report", looks_up)
    monkeypatch.setattr(context_hook, "thread_info", looks_up)
    for name in ("PreToolUse", "UserPromptSubmit"):  # every tool call and every prompt: no work, no wait
        assert fire(event(name, tool_name="Bash", tool_input={"command": "ls"})) == 0
    assert attempts == ["working"]
    monkeypatch.setattr(context_hook, "build_report", real_build)  # (never monkeypatch.undo() here: it would also undo the
    monkeypatch.setattr(context_hook, "thread_info", real_info)  # fixture that points the cache at tmp_path)
    assert fire(event("Stop")) == 0 and fire(event("SessionEnd")) == 0  # a lost "finished" would leave a thread "working"
    assert attempts == ["working", "idle", "gone"]
    assert marker.exists()  # still down: the failures renewed it
    answers["up"] = True
    assert fire(event("Stop")) == 0
    assert attempts[-1] == "idle" and not marker.exists()  # an answer ends the quiet time
    assert fire(event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"})) == 0
    assert attempts[-1] == "working" and not marker.exists()
    assert _no_timers_left()  # main() never leaves its watchdog armed in a process that goes on living


def test_an_http_answer_of_any_kind_is_not_a_buddy_that_is_down(monkeypatch, tmp_path, token_file):
    monkeypatch.setenv("CLAUDE_BUDDY_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(context_hook, "post", lambda url, token, report: None)  # post() returns for any status code
    monkeypatch.setattr(sys, "stdin", FakeStdin(json.dumps(event("PreToolUse", tool_name="Bash")).encode()))
    assert context_hook.main() == 0 and not _down_file(tmp_path).exists()


def test_the_down_marker_is_only_believed_when_it_is_ours_private_and_near(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    marker = cache / context_hook.DOWN_FILE
    assert context_hook.buddy_is_down(1000.0) is False  # no file
    context_hook.mark_buddy_down(1000.0)
    assert marker.read_text() == "1030.000" and marker.stat().st_mode & 0o777 == 0o600
    assert context_hook.buddy_is_down(1000.0) and context_hook.buddy_is_down(1029.0)
    assert not context_hook.buddy_is_down(1030.0) and not context_hook.buddy_is_down(2000.0)  # it expires by itself
    marker.chmod(0o644)  # readable by others: not ours to trust
    assert not context_hook.buddy_is_down(1000.0)
    for text in ("", "soon", "nan", "inf", "-5", "9" * 400, "1e300", "99999999999"):  # garbage or a promise of forever
        marker.write_text(text)
        marker.chmod(0o600)
        assert not context_hook.buddy_is_down(1000.0), text
    marker.unlink()
    marker.mkdir()  # a folder where the file should be: no exception, no mute
    assert not context_hook.buddy_is_down(1000.0)
    context_hook.clear_buddy_down()  # unlinking a folder fails quietly
    context_hook.mark_buddy_down(1000.0)  # and so does writing over it
    assert marker.is_dir()


def _hook_script(tmp_path, body):
    """A tiny program that runs main() the way settings.json does, with something slow where the network call is."""
    script = tmp_path / "slow_hook.py"
    script.write_text("import sys, time\nsys.path.insert(0, %r)\nimport context_hook\n%s\nsys.exit(context_hook.main())\n"
                      % (str(HOOK.parent), body), encoding="utf-8")
    return script


def test_a_post_that_never_comes_back_is_cut_at_the_deadline_and_marks_the_buddy_down(tmp_path, token_file):
    # the main thread sits in a call no socket timeout reaches (think of a DNS lookup): only the watchdog can end it
    script = _hook_script(tmp_path, "context_hook.post = lambda url, token, report: time.sleep(60)")
    env = {**os.environ, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file), "CLAUDE_BUDDY_CACHE_DIR": str(tmp_path / "cache"),
           "CLAUDE_BUDDY_DESKTOP_DIR": str(tmp_path / "desktop"), "CLAUDE_CONFIG_DIR": str(tmp_path / "cc")}
    start = time.perf_counter()
    done = subprocess.run([sys.executable, str(script)], input=json.dumps(event("PreToolUse", tool_name="Bash")).encode(),
                          capture_output=True, env=env, timeout=30)
    took = time.perf_counter() - start
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    assert context_hook.DEADLINE_S - 0.2 <= took < context_hook.DEADLINE_S + 2.5, took  # cut by the watchdog, not by luck
    assert _down_file(tmp_path).exists()  # the next tool calls will not wait for this buddy again


def test_work_that_is_not_the_network_is_cut_too_but_does_not_blame_the_buddy(tmp_path, token_file):
    script = _hook_script(tmp_path, "context_hook.thread_info = lambda sid: time.sleep(60)")
    env = {**os.environ, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file), "CLAUDE_BUDDY_CACHE_DIR": str(tmp_path / "cache"),
           "CLAUDE_BUDDY_DESKTOP_DIR": str(tmp_path / "desktop"), "CLAUDE_CONFIG_DIR": str(tmp_path / "cc")}
    done = subprocess.run([sys.executable, str(script)], input=json.dumps(event("PreToolUse", tool_name="Bash")).encode(),
                          capture_output=True, env=env, timeout=30)
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    assert not _down_file(tmp_path).exists()


def _trickling_buddy():
    """A listener that takes the request and then answers one byte every 0.4 s forever: every single wait is short, so
    only a deadline on the whole run can end it. Returns (port, accepted connections, stop)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    accepted = []
    done = threading.Event()

    def serve_one(conn):
        try:
            conn.settimeout(3)
            conn.recv(65536)
            while not done.is_set():
                conn.sendall(b"H")
                time.sleep(0.4)
        except OSError:
            pass
        finally:
            conn.close()

    def accept_loop():
        server.settimeout(0.2)
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            accepted.append(1)
            threading.Thread(target=serve_one, args=(conn,), daemon=True).start()
        server.close()

    threading.Thread(target=accept_loop, daemon=True).start()
    return server.getsockname()[1], accepted, done.set


def test_a_buddy_that_trickles_bytes_costs_one_slow_call_and_then_none_for_30_seconds(tmp_path, token_file):
    port, accepted, stop = _trickling_buddy()
    try:
        env = {"CLAUDE_BUDDY_URL": "http://127.0.0.1:%d" % port, "CLAUDE_BUDDY_TOKEN_FILE": str(token_file)}
        start = time.perf_counter()
        code, out, err = run_hook(event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}), env)
        first = time.perf_counter() - start
        assert (code, out, err) == (0, b"", b"")
        assert first < context_hook.DEADLINE_S + 2.5, first  # without the watchdog this call would run until Claude Code kills it
        assert _down_file(tmp_path).exists() and len(accepted) == 1
        start = time.perf_counter()
        for _ in range(5):  # a tool-heavy minute: none of these touches the network
            assert run_hook(event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}), env)[:2] == (0, b"")
        assert time.perf_counter() - start < 5 * 1.5, "five quiet calls should be quick"
        assert len(accepted) == 1
    finally:
        stop()


def test_a_script_that_is_gone_cannot_block_claude_code_when_registered_the_documented_way(tmp_path):
    missing = tmp_path / "moved" / "context_hook.py"
    bare = subprocess.run("%s %r" % (sys.executable, str(missing)), shell=True, capture_output=True)
    assert bare.returncode == 2  # what python says about a file that is not there, and 2 is the code that blocks a tool call
    guarded = subprocess.run("%s %r 2>/dev/null; exit 0" % (sys.executable, str(missing)), shell=True, capture_output=True)
    assert (guarded.returncode, guarded.stdout, guarded.stderr) == (0, b"", b"")


def test_the_documented_registration_never_blocks_and_the_docs_do_not_promise_what_the_code_cannot_keep():
    guarded = 'tools/context_hook.py\\" 2>/dev/null; exit 0"'
    root = HOOK.parent.parent
    for path in (HOOK, root / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert text.count(guarded) == 4, path.name  # PreToolUse, UserPromptSubmit, Stop and SessionEnd
        assert 'context_hook.py\\"", "timeout"' not in text, path.name  # no bare command left
    for path in (HOOK, root / "README.md", root / "CLAUDE.md"):
        text = path.read_text(encoding="utf-8")
        assert "3 s in all" not in text and "3 s en total" not in text and "a los 3 s" not in text, path.name
        assert "2.5" in text or "2,5" in text, path.name  # the deadline that is really there


def test_the_source_stays_python39_and_has_no_em_dash():
    import ast
    source = HOOK.read_text(encoding="utf-8")
    assert "\u2014" not in source and "\u2013" not in source
    tree = ast.parse(source, feature_version=(3, 9))  # no match statements, no parenthesized with
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, ast.FunctionDef) and node.returns is not None:
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    for annotation in annotations:  # `int | None` parses on 3.9 and then fails when the def runs
        assert not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(annotation))


@pytest.mark.skipif(os.environ.get("CLAUDE_BUDDY_CANARY") != "1", reason="reads the real Claude desktop folder: opt in with CLAUDE_BUDDY_CANARY=1")
def test_real_desktop_files_still_have_the_keys_we_read():
    """Opt-in canary: asserts key NAMES only, prints nothing private. Run it after a Claude desktop update."""
    root = Path.home() / "Library" / "Application Support" / "Claude"
    if not (root / "claude-code-sessions").is_dir():
        pytest.skip("no Claude desktop here")
    scopes = context_hook._scope_dirs(root)
    assert scopes, "claude-code-sessions/<org>/<org> is gone"
    seen_cli = seen_title = False
    for scope, folder in scopes:
        for path in sorted(folder.glob("local_*.json"))[:20]:
            info = context_hook._read_session(path)
            assert info is not None, "a session file no longer parses the way we read it"
            seen_cli = seen_cli or bool(info["cli"])
            seen_title = seen_title or bool(info["title"])
    assert seen_cli and seen_title
    config = json.loads((root / "claude_desktop_config.json").read_text(encoding="utf-8"))
    group_scopes = config["preferences"]["epitaxyPrefs"]["dframe-group-scopes"]
    assert {s for s, _ in scopes} & set(group_scopes), "the group scopes are no longer named like the folders"
    for scope in group_scopes.values():
        assert isinstance(scope["groups"], list) and isinstance(scope["assignments"], dict)
        assert all(set(("id", "name")) <= set(g) for g in scope["groups"])
        assert all(k.startswith("code:") or ":" in k for k in scope["assignments"])
    registry = Path.home() / ".claude" / "sessions"
    if registry.is_dir():
        entries = [context_hook._small_json(p, 16384) for p in registry.glob("*.json")]
        assert any(e and "sessionId" in e for e in entries), "the live-process list lost sessionId"
