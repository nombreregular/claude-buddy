"""tools/census.py: what runs on the Mac, told to the screen, and nothing else.

The census reads the process table of the Mac, so the unit tests never run `ps` for real (one test checks the argument list
of the call with a fake process) and never read the real ~/.claude or the real token: the table is text in the shapes the
Mac's `ps` really prints (Claude desktop's children with spaces in the program path, remote-control children that carry
--print, the Python.app interpreter, wrappers, chains of shell scripts), with neutral names, and the registry, the
transcripts and the config are built in tmp_path. A key that must never leave is planted in the commands: no body, log or
Proc may contain it.

The last section runs the whole thing as processes: a decoy that `ps` shows as `claude ... -p <prompt>`, the daemon's real
listener on loopback and `tools/census.py` as a subprocess in a HOME, a ~/.claude, a config and a token of its own
(tests/census_machine.py has the decoy and the driver). Those do run the real `ps`, but read nothing of the real machine
except the process table."""
import ast
import asyncio
import calendar
import collections
import contextlib
import functools
import io
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census  # noqa: E402
import census_machine  # noqa: E402
import context_hook  # noqa: E402
from test_hermes import hermes  # noqa: E402
from test_ui import Rig  # noqa: E402
from ui_client import http  # noqa: E402

CENSUS = Path(__file__).resolve().parent.parent / "tools" / "census.py"
EXAMPLE = Path(__file__).resolve().parent.parent / "tools" / "census.example.json"
SYSTEM_PYTHON = "/usr/bin/python3"
HOME = "/Users/dev"
FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE1234567890"
PROMPT = "Read docs/rules.md and do the work. key " + FAKE_KEY + " end"
SESSION = "3f2a9c1e-7b44-4d0a-9e51-0c8d6a2b1f77"
TOKEN = "T" * 32
NOW = 1_760_800_000.0
DESK = "/Users/dev/Library/Application Support/Claude/claude-code/2.1.299/4fa9/claude.app/Contents/MacOS/claude"
DESK_FLAGS = " --output-format stream-json --verbose --input-format stream-json --model opus --permission-mode default"
PYTHON_APP = ("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/"
              "Python.app/Contents/MacOS/Python")
LONG_TOOLS = " ".join("Tool%d" % i for i in range(45))
FAR_WORDS = " ".join("word%d" % i for i in range(85))

# (pid, ppid, etime, command, is a headless run): the shapes of a real Mac, under neutral names
SHAPES = [
    (1, 0, "05-01:35:20", "/sbin/launchd", False),
    (119, 1, "05:00:00", "-zsh", False),
    # Claude desktop: the app, its disclaimer wrapper and the claude it runs (spaces in the path, stream-json)
    (100, 1, "13:26:32", "/Applications/Claude.app/Contents/MacOS/Claude", False),
    (101, 100, "13:26:17", "/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup -- " + DESK + DESK_FLAGS, False),
    (102, 101, "13:26:17", DESK + DESK_FLAGS, False),
    (103, 102, "04:10", "claude -p summarize the folder", False),  # a tool call of a desktop thread: below Claude.app
    # remote control: the tmux wrapper, the launcher and the child that carries --print and --sdk-url
    (110, 1, "05-01:34:50", "/opt/homebrew/bin/tmux new -d -s rc claude remote-control --name mac", False),
    (111, 110, "05-01:34:50", "/Users/dev/.local/bin/claude remote-control --name mac --permission-mode default", False),
    (112, 111, "05-01:34:44", "/Users/dev/.local/share/claude/versions/2.1.281 --print --sdk-url wss://example.invalid/s "
     "--session-id abc --input-format stream-json --output-format stream-json --replay-user-messages", False),
    # terminal sessions: bare, and one with a prompt that says -p
    (120, 119, "01:05:00", "claude", False),
    (121, 119, "00:40", "claude explain what -p does", False),
    # headless runs in every shape they take
    (130, 1, "02:00", "claude --model opus --allowedTools " + LONG_TOOLS + " -p " + PROMPT + " --permission-mode acceptEdits", True),
    (140, 1, "02:00", "claude --strict-mcp-config --mcp-config {\"mcpServers\":{}} -p " + PROMPT + " --permission-mode acceptEdits", True),
    (150, 1, "02:00", "claude -p " + PROMPT + " --output-format json", True),
    (151, 1, "02:00", "claude --input-format stream-json --output-format stream-json --verbose -p " + PROMPT, True),
    # the chain of a launcher script: subshell, runner, claude and tee
    (160, 1, "54:31", "bash /Users/dev/jobs/spawn.sh alpha " + PROMPT + " opus", False),
    (161, 160, "54:31", "bash ./.runner.sh", False),
    (162, 161, "54:25", "claude --model opus -p " + PROMPT + " --permission-mode acceptEdits --max-turns 300 "
     "--allowedTools Read,Bash(ls:*)", True),
    (163, 161, "54:25", "tee /Users/dev/work-alpha/.run.log", False),
    (169, 1, "03:12", "bash ./.runner.sh", False),
    (170, 169, "03:12", "opencode run -m vendor/model " + PROMPT, False),
    # coordinators and look-alikes
    (180, 1, "37:30", "bash ./nightly.sh", False),
    (181, 180, "00:03", "sleep 60", False),
    (182, 180, "37:29", "bash ./nightly.sh", False),  # a subshell of the same script
    (183, 1, "00:02", "pgrep -f nightly.sh", False),
    (184, 1, "12:00", "bash -c ./nightly.sh " + FAKE_KEY, False),
    (185, 1, "12:00", "tail -f nightly.log", False),
    (190, 1, "00:40", PYTHON_APP + " backup.py --token=" + FAKE_KEY, False),  # macOS shows python3 as Python.app/.../Python
    # prompts that talk about flags
    (200, 1, "02:00", "claude --model opus -p Explain the -p flag and the --print flag please", True),
    (201, 1, "02:00", "claude -p Explain what --sdk-url does", False),  # inside the first 80 words: the known limit
    (202, 1, "02:00", "claude -p " + FAR_WORDS + " --sdk-url wss://example.invalid/s", True),  # beyond them: a run
    # zombies
    (210, 1, "07:00", "(claude)", False),
    (211, 1, "07:00", "claude -p stale <defunct>", False),
    # wrappers are never runs, their child is
    (220, 1, "01:30", "bash -c cd /Users/dev/work-beta && /Users/dev/.local/bin/claude -p task", False),
    (221, 220, "01:30", "/Users/dev/.local/bin/claude -p task", True),
    (222, 1, "01:30", "caffeinate -i /Users/dev/.local/bin/claude -p task", False),
    (223, 222, "01:30", "/Users/dev/.local/bin/claude -p task", True),
    (224, 1, "01:30", "ssh host claude -p task", False),
    (225, 1, "01:30", "python3 /opt/tools/claude -p task", False),
    # younger than min_age_s, and a run nested in a run
    (230, 1, "00:05", "claude -p short helper", True),
    (240, 1, "10:00", "claude -p outer task", True),
    (241, 240, "09:50", "bash", False),
    (242, 241, "09:40", "claude -p inner task", False),
]
CWD = {130: HOME + "/work-long", 140: HOME + "/work-provider", 150: HOME + "/work-json", 151: HOME + "/work-json",
       162: HOME + "/work-alpha", 170: HOME + "/work-alpha", 200: HOME + "/work-words", 202: "/",
       221: HOME + "/work-beta", 223: HOME + "/Documents/gamma", 230: HOME + "/work-young", 240: HOME + "/work-outer",
       190: HOME}
FILLER = [(20000 + i, 1, "01:00", "/bin/filler%d" % i, False) for i in range(30)]
SLOW = ("nightly", "backup.py", "opencode")


def line(pid, ppid, etime, command):
    return "%5d %5d %11s %s" % (pid, ppid, etime, command)


def table(shapes=SHAPES, filler=True):
    return "\n".join(line(*s[:4]) for s in list(shapes) + (FILLER if filler else [])) + "\n"


def cwd(pid):
    return CWD.get(pid)


def no_session(pid, elapsed, now):
    return None


def no_idle(session, now):
    return None


def write_config(tmp_path, raw, mode=0o600, name="census.json"):
    path = tmp_path / name
    path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    path.chmod(mode)
    return path


def make_config(tmp_path, raw=None, env=None):
    base = {"strip_prefix": ["work-"], "jobs": [{"match": "nightly"}, {"match": "backup.py", "name": "copia"},
                                                 {"match": "opencode", "name": "{cwd}"}], "ignore": [{"match": "tee"}]}
    base.update(raw or {})
    return census.load_config(str(write_config(tmp_path, base)), env or {})


def scan(tmp_path, text=None, cfg=None, cap=99, **kw):
    """The snapshot of a pretend table. `cap` replaces max_rows (the config itself never allows more than 8)."""
    procs = census.parse_ps(text if text is not None else table())
    kw.setdefault("cwd", cwd)
    kw.setdefault("session", no_session)
    kw.setdefault("idle", no_idle)
    kw.setdefault("home", HOME)
    cfg = cfg or make_config(tmp_path)
    return census.scan(procs, cfg._replace(max_rows=cap) if cap else cfg, NOW, **kw)


def ids(snap):
    return [r["id"] for r in snap["rows"]]


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path, monkeypatch):
    """Nothing here may read the real ~/.claude, the real token or the real config."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    for name in ("CLAUDE_BUDDY_URL", "CLAUDE_BUDDY_TOKEN_FILE", "CLAUDE_BUDDY_CENSUS", "CLAUDE_BUDDY_CENSUS_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    real_token = os.path.realpath(os.path.expanduser(census.DEFAULT_TOKEN_FILE))
    read_token = census.read_token

    def guarded(path_text):
        assert os.path.realpath(os.path.expanduser(path_text)) != real_token, "a test reached for the real token"
        return read_token(path_text)

    monkeypatch.setattr(census, "read_token", guarded)


# --- what is a run -------------------------------------------------------------------------------------

def test_what_is_a_run_and_what_is_not():
    procs = census.parse_ps(table())
    by_pid = {p.pid: p for p in procs}
    assert len(procs) == len(SHAPES) + len(FILLER)
    wrong = [s[0] for s in SHAPES if census.is_headless_run(by_pid[s[0]], by_pid) != s[4]]
    assert wrong == []


def test_the_word_after_the_program_must_be_a_flag():
    # `claude remote-control` and `claude mcp ...` are subcommands, `claude explain what -p does` is a prompt: none is a run
    for command in ("claude remote-control --print", "claude mcp serve -p", "claude \"fix it\" -p", "claude explain what -p does",
                    "claude"):
        assert not census.parse_ps(line(5, 1, "01:00", command))[0].headless, command
    assert census.parse_ps(line(5, 1, "01:00", "claude --model opus -p go"))[0].headless


def test_a_run_is_found_when_a_long_tool_list_comes_before_p():
    command = [s for s in SHAPES if s[0] == 130][0][3]
    words = command.split()[1:]
    assert 45 <= words.index("-p") <= 60
    assert census.parse_ps(line(5, 1, "01:00", command))[0].headless


def test_p_must_be_among_the_first_80_words():
    near = "claude --flag " + " ".join("w%d" % i for i in range(78)) + " -p"  # -p is word 80
    far = "claude --flag " + " ".join("w%d" % i for i in range(79)) + " -p"  # -p is word 81
    assert census.parse_ps(line(5, 1, "01:00", near))[0].headless
    assert not census.parse_ps(line(5, 1, "01:00", far))[0].headless
    assert census.parse_ps(line(5, 1, "01:00", "claude --print go"))[0].headless


def test_output_and_input_format_do_not_make_a_run_managed():
    for flags in ("--output-format json", "--output-format stream-json --verbose", "--input-format stream-json"):
        assert census.parse_ps(line(5, 1, "01:00", "claude %s -p go" % flags))[0].headless, flags


def test_sdk_url_hides_a_run_only_inside_the_first_80_words():
    # the contract's rule (d); the cost is a run whose prompt says --sdk-url within its first 80 words
    assert not census.parse_ps(line(5, 1, "01:00", "claude -p Explain --sdk-url please"))[0].headless
    assert not census.parse_ps(line(5, 1, "01:00", "claude --print --sdk-url=wss://x"))[0].headless
    assert census.parse_ps(line(5, 1, "01:00", "claude -p " + FAR_WORDS + " --sdk-url x"))[0].headless


def test_nothing_below_claude_app_is_a_run():
    shapes = [(100, 1, "13:00", "/Applications/Claude.app/Contents/MacOS/Claude", False),
              (101, 100, "13:00", "/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup -- /x/claude -p go", False),
              (102, 101, "12:00", "claude -p go", False),
              (103, 1, "12:00", "/opt/other/claude -p go", True)]
    table_ = {p.pid: p for p in census.parse_ps(table(shapes, filler=False))}
    assert {pid: census.is_headless_run(p, table_) for pid, p in table_.items()} == {100: False, 101: False, 102: False, 103: True}
    assert table_[100].managed and table_[101].managed and not table_[103].managed
    # the program of a path with spaces counts up to where the flags begin, and a flag value does not make a program managed
    assert census.parse_ps(line(5, 1, "01:00", DESK + DESK_FLAGS))[0].managed
    assert not census.parse_ps(line(5, 1, "01:00", "claude -p open /Applications/Claude.app/x"))[0].managed


def test_a_run_inside_a_run_collapses_into_the_outer_one(tmp_path):
    snap = scan(tmp_path)
    assert "run-outer" in ids(snap)
    assert not [r for r in snap["rows"] if r["elapsed"] == 9 * 60 + 40]  # the inner one, 09:40 old, has no row
    lone = table([(240, 1, "10:00", "claude -p outer task", True), (242, 241, "09:40", "claude -p inner task", True)])
    assert ids(scan(tmp_path, lone, cwd=lambda pid: HOME + "/work-%d" % pid)) == ["run-240", "run-242"]  # no parent in the table: both


def test_zombies_and_wrappers_are_nothing():
    for command in ("(claude)", "claude -p stale <defunct>", "bash -c claude -p x", "caffeinate -i /abs/claude -p x",
                    "ssh host claude -p x", "python3 /opt/tools/claude -p x", "Claude -p x"):
        assert not census.parse_ps(line(5, 1, "01:00", command))[0].headless, command


def test_a_loop_in_the_parent_chain_ends():
    table_ = {p.pid: p for p in census.parse_ps("  5  6 01:00 a\n  6  7 01:00 b\n  7  5 01:00 c\n  8  8 01:00 d\n")}
    assert [a.pid for a in census.ancestors(table_, 5)] == [6, 7]
    assert census.ancestors(table_, 8) == [] and census.ancestors(table_, 99) == []


def test_etime_forms():
    parsed = census.parse_ps("  1 0 05-01:34:50 a\n  2 0 13:26:32 b\n  3 0 54:01 c\n  4 0 00:07 d\n  5 0 99-23:59:59 e\n")
    assert [p.elapsed for p in parsed] == [5 * 86400 + 3600 + 34 * 60 + 50, 13 * 3600 + 26 * 60 + 32, 54 * 60 + 1, 7,
                                           99 * 86400 + 86399]


def test_lines_that_are_not_rows_are_skipped():
    assert census.parse_ps("garbage\n\n  12 x 00:10 cmd\n  PID PPID ELAPSED COMMAND\n") == []
    assert [p.pid for p in census.parse_ps("  7 1 00:30\n  8 1 00:30 /bin/x\n")] == [8]  # a row with no command at all is not one


def test_parse_ps_keeps_no_command_line():
    for p in census.parse_ps(table()):
        assert set(p._fields) == {"pid", "ppid", "elapsed", "prog", "script", "headless", "managed"}
        text = repr(p)
        assert FAKE_KEY not in text and "Read docs" not in text and "acceptEdits" not in text and "Application Support" not in text
        assert "/" not in p.prog and "/" not in p.script


@pytest.mark.parametrize("prog, family", [("bash", "bash"), ("Python", "python"), ("python3", "python"), ("python3.13", "python"),
                                          ("node", "node"), ("perl5.34", "perl"), ("ruby3.2", "ruby"), ("sh", "sh"),
                                          ("zsh", "zsh"), ("dash", "dash"), ("claude", ""), ("-zsh", ""), ("nodejs", ""),
                                          ("pythonista", "")])
def test_interpreter_families(prog, family):
    assert census.interpreter_of(prog) == family


@pytest.mark.parametrize("family, words, script", [
    ("bash", ["./nightly.sh", "--now"], "nightly.sh"),
    ("bash", ["-x", "-e", "/Users/dev/bin/nightly.sh"], "nightly.sh"),  # -e is errexit for a shell, not code
    ("bash", ["-c", "./nightly.sh"], ""),
    ("bash", ["-lc", "./nightly.sh"], ""),
    ("zsh", ["--command", "x"], ""),
    ("sh", ["run"], ""),
    ("python", ["backup.py", "--token=" + FAKE_KEY], "backup.py"),
    ("python", ["-u", "backup.py"], "backup.py"),
    ("python", ["-W", "ignore", "backup.py"], "backup.py"),
    ("python", ["-X", "dev", "backup.py"], "backup.py"),
    ("python", ["-m", "http.server"], ""),
    ("python", ["-c", "import os"], ""),
    ("node", ["--require", "./preload.js", "app.js"], "app.js"),
    ("node", ["-r", "./preload.js", "app.js"], "app.js"),
    ("node", ["--import", "./x.mjs", "app.js"], "app.js"),
    ("node", ["--loader", "./l.mjs", "app.js"], "app.js"),
    ("node", ["--experimental-loader", "./l.mjs", "app.js"], "app.js"),
    ("node", ["--require=./preload.js", "app.js"], "app.js"),
    ("node", ["-e", "setInterval(1)"], ""),
    ("node", ["-p", "1"], ""),
    ("node", ["--eval", "1"], ""),
    ("node", ["--print", "1"], ""),
    ("node", ["server/index"], ""),  # no known extension
    ("node", ["app", "other.js"], ""),  # only the first word that is not a flag counts: the rest are arguments
    ("ruby", ["-e", "puts 1"], ""),
    ("ruby", ["report.rb"], "report.rb"),
    ("perl", ["sync.pl"], "sync.pl"),
    ("node", ["tool.ts"], "tool.ts"),
    ("bash", [], ""),
])
def test_which_word_of_an_interpreter_is_its_script(family, words, script):
    assert census.script_of(family, words) == script


def test_the_macos_python_binary_is_an_interpreter_and_its_script_is_the_label():
    p = census.parse_ps(line(9, 1, "01:00", PYTHON_APP + " backup.py --token=" + FAKE_KEY))[0]
    assert (p.prog, p.script) == ("Python", "backup.py")
    q = census.parse_ps(line(9, 1, "01:00", "/Users/dev/.local/share/uv/python/cpython-3.12/bin/python3.12 -u sync.py"))[0]
    assert (q.prog, q.script) == ("python3.12", "sync.py")


# --- names, ids, rows ------------------------------------------------------------------------------------

def test_the_rows_of_the_real_shapes(tmp_path):
    snap = scan(tmp_path)
    rows = {r["id"]: r for r in snap["rows"]}
    assert set(rows) == {"run-long", "run-provider", "run-json", "run-json-p151", "run-alpha", "run-words", "run-claude-202",
                         "run-beta", "run-gamma", "run-outer", "job-nightly", "job-copia", "job-alpha"}
    assert rows["run-alpha"]["elapsed"] == 54 * 60 + 25 and rows["run-alpha"]["project"] == "work-alpha"
    assert rows["run-gamma"]["project"] == "gamma" and rows["run-json-p151"]["name"] == "json"
    assert rows["job-nightly"]["elapsed"] == 37 * 60 + 30  # the root, not the subshell
    assert rows["job-alpha"]["name"] == "alpha" and rows["job-copia"]["name"] == "copia"  # {cwd} minus the prefix
    assert "project" not in rows["run-claude-202"] and "project" not in rows["job-copia"]
    assert all(r["state"] == "working" and r["group"] == "Corridas" for r in snap["rows"])
    assert (snap["v"], snap["source"], snap["ttl"], snap["more"], snap["seq"]) == (1, "mac", 60, 0, int(NOW * 1000))
    assert not [i for i in rows if "tee" in i or "runner" in i or "spawn" in i or "sleep" in i or "inner" in i]


def test_runs_come_before_jobs_and_the_oldest_first_and_the_rest_is_counted(tmp_path):
    snap = scan(tmp_path, cap=None)  # max_rows 8
    assert ids(snap)[:2] == ["run-alpha", "run-outer"]
    assert [i.startswith("run-") for i in ids(snap)] == [True] * 8
    assert snap["more"] == 5 and len(snap["rows"]) == 8  # 10 runs + 3 jobs
    elapsed = [r["elapsed"] for r in snap["rows"]]
    assert elapsed == sorted(elapsed, reverse=True)


def test_young_processes_are_not_listed(tmp_path):
    assert "run-young" not in ids(scan(tmp_path))
    assert "run-young" in ids(scan(tmp_path, cfg=make_config(tmp_path)._replace(min_age=0)))


def test_names_when_the_folder_says_nothing(tmp_path):
    lines = [(100 + i, 1, "02:00", "claude -p go", True) for i in range(5)]
    where = {100: None, 101: "/", 102: HOME, 103: HOME + "/", 104: HOME + "/ñññ"}
    snap = scan(tmp_path, table(lines), cwd=where.get, cfg=make_config(tmp_path, {"jobs": []}))
    assert sorted(ids(snap)) == ["run-claude-%d" % (100 + i) for i in range(5)]
    assert not [r for r in snap["rows"] if "project" in r]


def test_names_are_ascii_short_and_safe(tmp_path):
    folders = {100: HOME + "/Proyecto ñandú", 101: HOME + "/" + "x" * 70 + "-tail", 102: HOME + "/<img src=x onerror=alert(1)>",
               103: HOME + "/---a b---", 104: HOME + "/work-", 105: HOME + "/work-" + "y" * 50}
    lines = [(pid, 1, "02:00", "claude -p go", True) for pid in folders]
    snap = scan(tmp_path, table(lines), cwd=folders.get, cfg=make_config(tmp_path, {"jobs": []}))
    names = {r["id"]: r["name"] for r in snap["rows"]}
    assert names["run-Proyecto-and"] == "Proyecto-and"
    assert names["run-" + "x" * 40] == "x" * 40
    assert names["run-img-src-x-onerror-alert-1"] == "img-src-x-onerror-alert-1"
    assert names["run-a-b"] == "a-b"
    assert names["run-work"] == "work" and names["run-" + "y" * 40] == "y" * 40  # a folder that IS the prefix keeps it
    for r in snap["rows"]:
        assert census.SAFE.sub("", r["name"]) == r["name"] and len(r["name"]) <= 40 and len(r["id"]) <= 64
        assert census.SAFE.sub("", r.get("project", "")) == r.get("project", "") and len(r.get("project", "")) <= 40
    assert len(names) == len(folders)


def test_two_runs_with_one_name_the_lowest_pid_keeps_the_id(tmp_path):
    lines = [(300, 1, "02:00", "claude -p b", True), (200, 1, "03:00", "claude -p a", True), (250, 1, "01:00", "claude -p c", True)]
    snap = scan(tmp_path, table(lines), cwd=lambda pid: HOME + "/work-x", cfg=make_config(tmp_path, {"jobs": []}))
    assert {r["id"]: r["elapsed"] for r in snap["rows"]} == {"run-x": 180, "run-x-p250": 60, "run-x-p300": 120}
    assert {r["name"] for r in snap["rows"]} == {"x"}


def test_ids_are_unique_even_for_names_that_look_like_a_collision(tmp_path):
    folders = {200: HOME + "/x", 201: HOME + "/x", 202: HOME + "/x-p201"}
    lines = [(pid, 1, "02:00", "claude -p go", True) for pid in folders]
    snap = scan(tmp_path, table(lines), cwd=folders.get, cfg=make_config(tmp_path, {"jobs": []}))
    assert len(set(ids(snap))) == 3


def test_a_job_99_days_old_is_clamped_not_dropped(tmp_path):
    text = table([(500, 1, "99-23:59:59", "bash ./nightly.sh", False), (501, 1, "99-00:00:00", "claude -p go", True)])
    snap = scan(tmp_path, text, cwd=cwd)
    assert {r["id"]: r["elapsed"] for r in snap["rows"]} == {"job-nightly": 2678400, "run-claude-501": 2678400}


def test_the_session_and_the_silence_of_a_run_ride_along(tmp_path):
    seen = []

    def session(pid, elapsed, now):
        seen.append((pid, elapsed, now))
        return SESSION if pid == 162 else None

    snap = scan(tmp_path, session=session, idle=lambda s, now: 538 if s == SESSION else None)
    row = [r for r in snap["rows"] if r["id"] == "run-alpha"][0]
    assert (row["session"], row["idle"]) == (SESSION, 538)
    assert (162, 3265, NOW) in seen
    assert not [r for r in snap["rows"] if r["id"] != "run-alpha" and ("session" in r or "idle" in r)]
    huge = scan(tmp_path, session=lambda *a: SESSION, idle=lambda s, now: 10 ** 9)
    assert {r["idle"] for r in huge["rows"] if r["id"].startswith("run-")} == {2678400}


def test_a_lookup_that_blows_up_costs_that_run_its_session_and_nothing_else(tmp_path):
    def boom(*args):
        raise RecursionError("a file nested deeper than the parser allows")

    plain = scan(tmp_path)
    assert ids(scan(tmp_path, session=boom)) == ids(plain)  # the pass still has all its rows
    assert not [r for r in scan(tmp_path, session=boom)["rows"] if "session" in r or "idle" in r]
    only_idle = scan(tmp_path, session=lambda *a: SESSION, idle=boom)  # the transcript lookup fails: the session is still known
    runs = [r for r in only_idle["rows"] if r["id"].startswith("run-")]
    assert runs and all(r["session"] == SESSION and "idle" not in r for r in runs)
    assert ids(only_idle) == ids(plain)


def test_a_label_is_cut_before_any_pattern_looks_at_it(tmp_path):
    """A script name can be as long as the kernel allows an argument to be, and any process of the user can pick it: a
    pattern with two `.*` takes quadratic time on a label that long."""
    label = "sync" * 30000 + ".sh"
    (proc,) = [p for p in census.parse_ps(line(300, 1, "05:00", "/bin/bash " + label)) if p.pid == 300]
    assert len(proc.script) <= 80 and len(proc.prog) <= 80
    cfg = make_config(tmp_path, {"jobs": [{"match": "sync.*foo.*\\.sh", "regex": True, "min_age_s": 0}], "ignore": []})
    assert cfg.jobs and cfg.jobs[0].regex is not None
    start = time.monotonic()
    snap = scan(tmp_path, text=table() + line(300, 1, "05:00", "/bin/bash " + label) + "\n", cfg=cfg)
    assert time.monotonic() - start < 1.0
    assert not [r for r in snap["rows"] if r["id"].startswith("job-")]


# --- jobs ------------------------------------------------------------------------------------------------

def test_jobs_are_matched_by_the_label_only(tmp_path):
    cfg = make_config(tmp_path, {"jobs": [{"match": "nightly"}, {"match": r"^backup\.py$", "regex": True},
                                           {"match": "pgrep", "min_age_s": 0}]})
    jobs = [i for i in ids(scan(tmp_path, cfg=cfg)) if i.startswith("job-")]
    assert sorted(jobs) == ["job-backup", "job-nightly", "job-pgrep"]  # pgrep's argument names nightly.sh and does not count
    for pattern in (FAKE_KEY, "sk-ant", "token", "Read docs", "-f"):  # arguments are not kept, so nothing can match them
        cfg = make_config(tmp_path, {"jobs": [{"match": pattern}]})
        assert not [i for i in ids(scan(tmp_path, cfg=cfg)) if i.startswith("job-")], pattern


def test_a_macos_python_script_is_a_job(tmp_path):
    cfg = make_config(tmp_path, {"jobs": [{"match": "backup.py", "name": "copia"}]})
    assert [r for r in scan(tmp_path, cfg=cfg)["rows"] if r["id"].startswith("job-")][0]["id"] == "job-copia"
    # the label of an interpreter that runs a script is the script, so a pattern on the program name does not match it
    assert not [i for i in ids(scan(tmp_path, cfg=make_config(tmp_path, {"jobs": [{"match": "Python"}]}))) if i.startswith("job-")]
    bare = table([(10, 1, "05:00", PYTHON_APP + " -m http.server", False)])  # no script: the label is the program, case sensitive
    assert ids(scan(tmp_path, bare, cfg=make_config(tmp_path, {"jobs": [{"match": "Python"}]}))) == ["job-Python"]
    assert ids(scan(tmp_path, bare, cfg=make_config(tmp_path, {"jobs": [{"match": "python"}]}))) == []


def test_subshells_of_one_job_collapse_and_a_second_instance_stays_visible(tmp_path):
    shapes = [(10, 1, "05:00", "bash ./nightly.sh", False), (11, 10, "04:59", "bash ./nightly.sh", False),
              (12, 11, "04:58", "bash ./nightly.sh", False), (20, 1, "03:00", "bash ./nightly.sh", False),
              (30, 1, "02:00", "node nightly.js", False)]
    snap = scan(tmp_path, table(shapes), cfg=make_config(tmp_path, {"jobs": [{"match": "nightly"}]}))
    assert {r["id"]: r["elapsed"] for r in snap["rows"]} == {"job-nightly": 300, "job-nightly-p20": 180, "job-nightly-p30": 120}


def test_a_loose_pattern_is_capped_at_ten_and_says_so_once(tmp_path):
    shapes = [(100 + i, 1, "05:%02d" % i, "bash ./x%d.sh" % i, False) for i in range(15)]
    notes = []
    cfg = make_config(tmp_path, {"jobs": [{"match": "x"}]})
    snap = scan(tmp_path, table(shapes), cfg=cfg, cap=None, notes=notes)
    assert len(snap["rows"]) == 8 and snap["more"] == 2  # ten kept: eight shown, two counted
    assert len(notes) == 1 and "#1" in notes[0] and "15" in notes[0] and "bash" not in notes[0] and "x0" not in notes[0]
    logs = []
    buddy = Buddy(200)
    c = census_for(tmp_path, buddy, text=table(shapes), logs=logs, cfg=cfg)
    c.cycle()
    c.cycle()
    assert len([m for m in logs if "coincide" in m]) == 1


def test_the_cwd_placeholder_and_the_other_placeholders(tmp_path):
    cfg = make_config(tmp_path, {"jobs": [{"match": "opencode", "name": "{cwd}"}, {"match": "nightly", "name": "{prog}-{script}"},
                                           {"match": "backup", "name": "fixed"}]})
    jobs = sorted(i for i in ids(scan(tmp_path, cfg=cfg)) if i.startswith("job-"))
    assert jobs == ["job-alpha", "job-bash-nightly", "job-fixed"]
    shapes = [(10, 1, "05:00", "opencode run x", False)]
    cfg = make_config(tmp_path, {"jobs": [{"match": "opencode", "name": "{cwd}"}]})
    snap = scan(tmp_path, table(shapes), cwd=lambda pid: None, cfg=cfg)
    assert ids(snap) == ["job-opencode"]  # an unknown folder falls back to the program


def test_ignore_comes_before_jobs_and_min_age_is_per_job(tmp_path):
    cfg = make_config(tmp_path, {"jobs": [{"match": "nightly"}], "ignore": [{"match": "nightly"}]})
    assert [i for i in ids(scan(tmp_path, cfg=cfg)) if i.startswith("job-")] == []
    cfg = make_config(tmp_path, {"jobs": [{"match": "sleep", "min_age_s": 0}, {"match": "nightly", "min_age_s": 3600}]})
    assert [i for i in ids(scan(tmp_path, cfg=cfg)) if i.startswith("job-")] == ["job-sleep"]  # the sleep is 3 s old: its own limit is 0
    cfg = make_config(tmp_path, {"jobs": [{"match": "nightly", "min_age_s": 99999}]})
    assert [i for i in ids(scan(tmp_path, cfg=cfg)) if i.startswith("job-")] == ["job-nightly"]  # out of range: the global 20 s


def test_a_headless_claude_is_a_run_and_never_a_job(tmp_path):
    shapes = [(10, 1, "05:00", "claude -p go", True), (11, 1, "04:00", "claude --model opus", False), (12, 1, "03:00", "claude", False)]
    cfg = make_config(tmp_path, {"jobs": [{"match": "claude"}]})
    found = {r["id"]: r["elapsed"] for r in scan(tmp_path, table(shapes), cfg=cfg, cwd=lambda pid: HOME + "/work-x")["rows"]}
    assert found == {"run-x": 300, "job-claude": 240, "job-claude-p12": 180}  # only the sessions that are not runs can be jobs
    off = make_config(tmp_path, {"claude_runs": False, "jobs": [{"match": "claude"}]})
    assert not [i for i in ids(scan(tmp_path, table(shapes), cfg=off)) if i.startswith("run-")]
    assert ids(scan(tmp_path, table(shapes), cfg=off)) == ["job-claude", "job-claude-p12"]  # a headless claude is still no job


# --- the registry and the transcripts -----------------------------------------------------------------------

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def lstart(epoch):
    t = time.gmtime(epoch)
    return "%s %s %2d %02d:%02d:%02d %d" % (DAYS[t.tm_wday], MONTHS[t.tm_mon - 1], t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, t.tm_year)


def registry(tmp_path, pid=4321, **fields):
    """~/.claude with a sessions/<pid>.json, rewritten each time (the folder is shared by the pids of one test)."""
    folder = tmp_path / "claude" / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "sessionId": SESSION, "cwd": "/somewhere", "status": "busy"}
    data.update(fields)
    (folder / ("%d.json" % pid)).write_text(json.dumps(data), encoding="utf-8")
    return str(tmp_path / "claude")


def test_lstart_is_parsed_as_utc():
    assert census.parse_lstart("Sun Oct 18 14:54:26 2026") == calendar.timegm((2026, 10, 18, 14, 54, 26))
    assert census.parse_lstart("Thu Oct  1 08:00:00 2026") == calendar.timegm((2026, 10, 1, 8, 0, 0))
    for bad in ("", "yesterday", "Sun Foo 18 14:54:26 2026", "Sun Oct 99 14:54:26 2026", "Sun Oct 18 14:54 2026"):
        assert census.parse_lstart(bad) is None, bad
    assert census.parse_lstart(lstart(NOW)) == NOW and census.parse_lstart(lstart(NOW + 86400 * 20)) == NOW + 86400 * 20


def test_the_registry_links_a_pid_to_its_session_by_its_start_time(tmp_path):
    began = NOW - 3600
    for off, ok in ((0, True), (2, True), (3, True), (-3, True), (4, False), (-5, False), (60, False)):
        claude = registry(tmp_path, procStart=lstart(began + off), startedAt=int((began + 1) * 1000))
        assert (census.registry_session(claude, 4321, 3600, NOW) == SESSION) == ok, off


def test_without_procstart_the_registry_falls_back_to_startedat_with_a_wider_window(tmp_path):
    began = NOW - 600
    for off, ok in ((0, True), (19, True), (-19, True), (25, False), (-77, False)):
        claude = registry(tmp_path, startedAt=int((began + off) * 1000))
        assert (census.registry_session(claude, 4321, 600, NOW) == SESSION) == ok, off
    assert census.registry_session(registry(tmp_path, procStart="not a date", startedAt=int(began * 1000)), 4321, 600, NOW) == SESSION
    assert census.registry_session(registry(tmp_path), 4321, 600, NOW) is None  # nothing to compare with
    assert census.registry_session(registry(tmp_path, startedAt="soon"), 4321, 600, NOW) is None
    assert census.registry_session(registry(tmp_path, startedAt=True), 4321, 600, NOW) is None


def test_the_registry_refuses_what_is_not_this_process(tmp_path):
    fine = {"procStart": lstart(NOW - 600)}
    assert census.registry_session(registry(tmp_path, **fine), 4321, 600, NOW) == SESSION
    folder = tmp_path / "claude" / "sessions"
    assert census.registry_session(str(tmp_path / "claude"), 4999, 600, NOW) is None  # no file for this pid
    (folder / "4322.json").write_text(json.dumps({"pid": 1, "sessionId": SESSION, **fine}), encoding="utf-8")
    assert census.registry_session(str(tmp_path / "claude"), 4322, 600, NOW) is None  # a file of another pid
    for bad in ("a/b", "x" * 65, "", 5, None, "has space"):
        assert census.registry_session(registry(tmp_path, sessionId=bad, **fine), 4321, 600, NOW) is None, bad
    (folder / "4323.json").write_text("{not json", encoding="utf-8")
    (folder / "4324.json").write_text("[1, 2]", encoding="utf-8")
    (folder / "4325.json").write_text(json.dumps({"pid": 4325, "sessionId": SESSION, "x": "y" * 20000, **fine}), encoding="utf-8")
    (folder / "4326.json").mkdir()
    for pid in (4323, 4324, 4325, 4326):
        assert census.registry_session(str(tmp_path / "claude"), pid, 600, NOW) is None


def test_a_registry_file_that_the_parser_cannot_take_is_no_session_and_no_crash(tmp_path, monkeypatch):
    fine = {"procStart": lstart(NOW - 600)}
    claude = registry(tmp_path, **fine)
    assert census.registry_session(registry(tmp_path, startedAt=10 ** 400), 4321, 600, NOW) is None  # no float holds it
    assert census.registry_session(registry(tmp_path, startedAt=-10 ** 400), 4321, 600, NOW) is None
    (Path(claude) / "sessions" / "4330.json").write_text("[" * 7000 + "]" * 7000, encoding="utf-8")  # under the size limit, too deep
    assert census.registry_session(claude, 4330, 600, NOW) is None
    assert census.registry_session(registry(tmp_path, **fine), 4321, 600, NOW) == SESSION

    def too_deep(*args, **kw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(census.json, "loads", too_deep)  # whatever the interpreter does with the deep file above
    assert census.registry_session(claude, 4321, 600, NOW) is None


def test_the_key_files_next_to_the_registry_are_never_read(tmp_path, monkeypatch):
    claude = registry(tmp_path, procStart=lstart(NOW - 600))
    (Path(claude) / "sessions" / "4321.key").write_text(FAKE_KEY, encoding="utf-8")
    opened = []
    real_open = open

    def spy(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    assert census.registry_session(claude, 4321, 600, NOW) == SESSION
    assert opened and all(p.endswith("4321.json") for p in opened)


def transcript(tmp_path, mtime, session=SESSION, folder="-Users-dev-work-alpha"):
    path = tmp_path / "claude" / "projects" / folder / (session + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"secret": "%s"}\n' % FAKE_KEY, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def subagent(path, mtime, name="agent-1.jsonl"):
    sub = path.parent / path.stem / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / name).write_text("{}\n", encoding="utf-8")
    os.utime(sub / name, (mtime, mtime))


def test_silence_is_the_newest_change_of_the_transcript_and_its_subagents(tmp_path):
    main = transcript(tmp_path, NOW - 538)
    probe = census.Transcripts(str(tmp_path / "claude"))
    assert probe.idle(SESSION, NOW) == 538
    subagent(main, NOW - 30)
    subagent(main, NOW - 900, "agent-2.jsonl")
    (main.parent / main.stem / "subagents" / "notes.txt").write_text("x", encoding="utf-8")
    os.utime(main.parent / main.stem / "subagents" / "notes.txt", (NOW - 1, NOW - 1))  # not a transcript
    assert probe.idle(SESSION, NOW) == 30
    os.utime(main, (NOW + 50, NOW + 50))
    assert probe.idle(SESSION, NOW) == 0  # a clock a little behind never gives a negative age


def test_transcripts_are_measured_and_never_opened(tmp_path, monkeypatch):
    transcript(tmp_path, NOW - 100)
    real_open = open

    def guarded(path, *a, **kw):
        assert ".jsonl" not in str(path), "the transcript was opened"
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", guarded)
    assert census.Transcripts(str(tmp_path / "claude")).idle(SESSION, NOW) == 100


def test_a_transcript_is_looked_up_once_and_forgotten_when_it_goes(tmp_path, monkeypatch):
    main = transcript(tmp_path, NOW - 10)
    probe = census.Transcripts(str(tmp_path / "claude"))
    listed = []
    real_listdir = os.listdir

    def spy(path):
        listed.append(str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", spy)
    assert probe.idle(SESSION, NOW) == 10 and probe.idle(SESSION, NOW) == 10
    assert len(listed) == 1
    main.unlink()
    assert probe.idle(SESSION, NOW) is None and SESSION not in probe.paths
    assert census.Transcripts(str(tmp_path / "nowhere")).idle(SESSION, NOW) is None


# --- config -----------------------------------------------------------------------------------------------------

def test_no_config_is_the_safe_default(tmp_path):
    cfg = census.load_config(str(tmp_path / "missing.json"), {})
    assert (cfg.claude_runs, cfg.jobs, cfg.ignore, cfg.group, cfg.max_rows, cfg.min_age, cfg.interval, cfg.ttl, cfg.source) == (
        True, (), (), "Corridas", 8, 20, 15.0, 60, "mac")
    assert (cfg.url, cfg.token_file, cfg.insecure_url_ok, cfg.warnings) == (census.DEFAULT_URL, census.DEFAULT_TOKEN_FILE, False, ())


def test_a_config_that_cannot_be_trusted_is_ignored_with_a_warning(tmp_path):
    good = {"jobs": [{"match": "x"}]}
    cases = {
        "loose": (write_config(tmp_path, good, mode=0o666, name="a.json"), "otros"),
        "group-writable": (write_config(tmp_path, good, mode=0o620, name="b.json"), "otros"),
        "not json": (write_config(tmp_path, "{not json", name="c.json"), "JSON"),
        "list": (write_config(tmp_path, "[1]", name="d.json"), "objeto"),
        "big": (write_config(tmp_path, '{"jobs": [], "x": "%s"}' % ("y" * 70000), name="e.json"), "64 KB"),
        "binary": (write_config(tmp_path, "\x00\x01", name="f.json"), "JSON"),
    }
    for name, (path, word) in cases.items():
        cfg = census.load_config(str(path), {})
        assert cfg.jobs == () and cfg.claude_runs and len(cfg.warnings) == 1 and word in cfg.warnings[0], name
    (tmp_path / "dir.json").mkdir()
    assert census.load_config(str(tmp_path / "dir.json"), {}).warnings
    assert len(census.load_config(str(write_config(tmp_path, good, mode=0o644, name="g.json")), {}).jobs) == 1  # others may read it


def test_a_config_the_parser_or_the_floats_cannot_take_is_ignored_with_a_warning_not_a_crash(tmp_path, monkeypatch):
    """The agent is restarted by launchd every 30 s when it dies on start, so a bad file must never be fatal."""
    big = write_config(tmp_path, '{"interval_s": %s, "max_rows": %s, "min_age_s": -%s}' % ("9" * 400, "9" * 400, "9" * 400), name="a.json")
    cfg = census.load_config(str(big), {})
    assert (cfg.interval, cfg.max_rows, cfg.min_age, cfg.claude_runs) == (15.0, 8, 20, True)
    assert len(cfg.warnings) == 3 and all("numero" in w for w in cfg.warnings)
    deep = write_config(tmp_path, "[" * 30000 + "]" * 30000, name="b.json")  # 60 KB: under the size limit, too deep for the parser
    cfg = census.load_config(str(deep), {})
    assert cfg.claude_runs and len(cfg.warnings) == 1
    fine = write_config(tmp_path, {"jobs": [{"match": "x"}]}, name="c.json")

    def too_deep(*args, **kw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(census.json, "loads", too_deep)  # whatever the interpreter does with the file above
    cfg = census.load_config(str(fine), {})
    assert cfg.jobs == () and cfg.claude_runs and len(cfg.warnings) == 1 and "JSON" in cfg.warnings[0]
    assert census.run({"CLAUDE_BUDDY_CENSUS_CONFIG": str(fine)}, once=True, dry=True, read_ps=lambda *a, **k: table(), cwd=cwd,
                      session=no_session, idle=no_idle, log=lambda m: None, out=io.StringIO()) == 0


def test_numbers_are_clamped_and_junk_falls_back(tmp_path):
    def load(**raw):
        return census.load_config(str(write_config(tmp_path, raw)), {})

    assert (load(interval_s=1).interval, load(interval_s=1).ttl) == (5.0, 20)
    assert (load(interval_s=60).interval, load(interval_s=60).ttl) == (30.0, 120)
    assert (load(interval_s=7.5).interval, load(interval_s=7.5).ttl) == (7.5, 30)
    assert (load(interval_s=30).ttl, load(interval_s=5).ttl) == (120, 20)
    for junk in ("fast", None, True, [5], {"a": 1}, float("nan"), float("inf")):
        cfg = load(interval_s=junk, max_rows=junk, min_age_s=junk)
        assert (cfg.interval, cfg.max_rows, cfg.min_age) == (15.0, 8, 20), junk
    assert (load(max_rows=99).max_rows, load(max_rows=0).max_rows, load(max_rows=3).max_rows) == (8, 1, 3)
    assert (load(min_age_s=-5).min_age, load(min_age_s=99999).min_age, load(min_age_s=0).min_age) == (0, 3600, 0)
    assert load(interval_s=1).warnings and not load(interval_s=15).warnings
    assert load(claude_runs=False).claude_runs is False and load(claude_runs="no").claude_runs is True


def test_text_settings_are_checked(tmp_path):
    def load(**raw):
        return census.load_config(str(write_config(tmp_path, raw)), {})

    assert load(group="x" * 24).group == "x" * 24 and load(group="x" * 25).group == "Corridas"
    assert load(group="Maquinas").group == "Maquinas" and load(group="Máquinas").group == "Corridas" and load(group="").group == "Corridas"
    assert load(group="a\nb").group == "Corridas" and load(group=5).group == "Corridas"
    assert load(group=" x").group == "Corridas" and load(group="x ").group == "Corridas" and load(group="   ").group == "Corridas"
    assert load(group="Las noches").group == "Las noches"
    assert load(source="casa-2").source == "casa-2"
    for bad in ("Mac", "a b", "", "x" * 25, 5, "ñ"):
        assert load(source=bad).source == "mac", bad
    assert load(strip_prefix=["relay-", 5, "", "x" * 21, None]).strip_prefix == ("relay-",)
    assert load(strip_prefix="relay-").strip_prefix == () and load(strip_prefix="relay-").warnings
    assert load(insecure_url_ok=True).insecure_url_ok and not load(insecure_url_ok="yes").insecure_url_ok


def test_the_environment_wins_over_the_config_for_the_url_and_the_token(tmp_path):
    path = str(write_config(tmp_path, {"url": "http://from-config:1", "token_file": "/from/config"}))
    assert census.load_config(path, {}).url == "http://from-config:1"
    cfg = census.load_config(path, {"CLAUDE_BUDDY_URL": "http://from-env:2", "CLAUDE_BUDDY_TOKEN_FILE": "/from/env"})
    assert (cfg.url, cfg.token_file) == ("http://from-env:2", "/from/env")
    cfg = census.load_config(str(write_config(tmp_path, {"url": 5, "token_file": ""})), {"CLAUDE_BUDDY_URL": ""})
    assert (cfg.url, cfg.token_file) == (census.DEFAULT_URL, census.DEFAULT_TOKEN_FILE)


def test_bad_entries_are_skipped_and_reported_by_index_never_by_text(tmp_path):
    secret = "SECRET-TEXT-NOT-FOR-LOGS"
    raw = {"jobs": [{"match": "ok"}, {"match": "("+ secret, "regex": True}, {"match": ""}, 5, {"match": 7, "name": secret},
                    {"match": "x" * 121}, {"match": "fine", "name": 5, "min_age_s": "soon", "group": "g" * 30, "regex": "yes"}],
           "ignore": [{"match": "a"}, "b", 9, None, {"nomatch": secret}], "unknown": secret, "_about": [secret]}
    cfg = census.load_config(str(write_config(tmp_path, raw)), {})
    assert [j.match for j in cfg.jobs] == ["ok", "fine"] and [j.match for j in cfg.ignore] == ["a", "b"]
    assert (cfg.jobs[1].name, cfg.jobs[1].min_age, cfg.jobs[1].group, cfg.jobs[1].regex) == ("{script}", 20, "Corridas", None)
    text = "\n".join(cfg.warnings)
    assert secret not in text and "jobs[1]" in text and "jobs[5]" in text and "ignore[2]" in text
    assert len(cfg.warnings) == 12  # five bad jobs, four bad fields of the last one, three bad ignores
    assert all(w.startswith(("jobs[", "ignore[")) for w in cfg.warnings)


def test_at_most_twenty_jobs_and_twenty_ignores(tmp_path):
    cfg = census.load_config(str(write_config(tmp_path, {"jobs": [{"match": "j%d" % i} for i in range(30)],
                                                         "ignore": ["i%d" % i for i in range(30)]})), {})
    assert len(cfg.jobs) == 20 and len(cfg.ignore) == 20 and len(cfg.warnings) == 2


def test_the_example_config_loads_clean_and_says_what_it_is(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    cfg = census.load_config(str(write_config(tmp_path, EXAMPLE.read_text(encoding="utf-8"))), {})
    assert cfg.warnings == () and cfg.claude_runs and cfg.strip_prefix == ("work-",)
    assert [j.match for j in cfg.jobs] == ["nightly", "^sync-.*\\.sh$", "backup.py", "opencode"] and cfg.jobs[1].regex is not None
    assert [j.match for j in cfg.ignore] == ["caffeinate", "tee"]
    assert (cfg.interval, cfg.max_rows, cfg.min_age, cfg.group, cfg.source, cfg.insecure_url_ok) == (15.0, 8, 20, "Corridas", "mac", False)
    assert raw["version"] == 1 and isinstance(raw["_about"], list)
    text = EXAMPLE.read_text(encoding="utf-8")
    for personal in ("relay", "quietud", "noche", "tanda", "someuser", "ana"):
        assert personal not in text.lower(), personal


# --- the wire ------------------------------------------------------------------------------------------------------

def test_only_names_and_numbers_leave(tmp_path):
    sid = "3f2a9c1e-7b44-4d0a-9e51-0c8d6a2b1f77"
    snap = scan(tmp_path, session=lambda pid, e, n: sid if pid == 162 else None, idle=lambda s, n: 538)
    wire = census.encode(snap).decode("ascii")
    for secret in (FAKE_KEY, "sk-ant", "Read docs", "acceptEdits", "max-turns", "stream-json", "Application Support", "/Users", "moonshot",
                   "vendor/model", "--token", "opus", "Bearer", "allowedTools", "Tool1", "mcpServers"):
        assert secret not in wire, secret
    assert {k for r in snap["rows"] for k in r} <= {"id", "name", "group", "state", "elapsed", "idle", "session", "project", "what"}
    assert set(snap) == {"v", "source", "seq", "ttl", "more", "rows"}
    assert len(wire.encode()) < 1800 and ": " not in wire.split('"rows"')[0]  # compact
    assert json.loads(wire) == snap


def test_eight_rows_of_the_biggest_fields_fit_the_listeners_cap(tmp_path):
    folders = {1000 + i: HOME + "/" + ("p%d" % i) + "z" * 60 for i in range(8)}
    lines = [(pid, 1, "99-23:59:59", "claude -p go", True) for pid in folders]
    cfg = make_config(tmp_path, {"jobs": [], "group": "G" * 24})
    snap = scan(tmp_path, table(lines), cwd=folders.get, cfg=cfg, session=lambda *a: "S" * 64, idle=lambda s, n: 10 ** 9)
    data = census.encode(snap)
    assert len(snap["rows"]) == 8 and snap["more"] == 0
    assert 2500 < len(data) < 4096 and json.loads(data)["rows"] == snap["rows"]
    assert all(len(r["name"]) == 40 and len(r["session"]) == 64 and len(r["group"]) == 24 and r["idle"] == r["elapsed"] == 2678400
               for r in snap["rows"])


def test_non_ascii_folders_never_reach_the_wire(tmp_path):
    folders = {1000 + i: HOME + "/" + ("\U0001F600" * 40 if i % 2 else "ñandú-" * 7) for i in range(8)}
    lines = [(pid, 1, "02:00", "claude -p go", True) for pid in folders]
    snap = scan(tmp_path, table(lines), cwd=folders.get, cfg=make_config(tmp_path, {"jobs": []}))
    data = census.encode(snap)
    assert b"\\u" not in data and data.decode("ascii") and len(data) < 4096
    assert len(snap["rows"]) == 8
    fallbacks = sorted(r["name"] for r in snap["rows"] if r["id"].startswith("run-claude-"))
    assert fallbacks == ["claude-1001", "claude-1003", "claude-1005", "claude-1007"]
    assert {r.get("project") for r in snap["rows"]} == {None, "and---and---and---and---and---and---and"}  # each ñ and ú is one -


def test_a_body_that_would_not_fit_loses_its_last_rows_and_counts_them():
    rows = [{"id": "run-%d" % i, "name": "n" * 40, "group": "g", "state": "working", "elapsed": 1, "session": "s" * 64,
             "project": "p" * 40, "extra": "z" * 300} for i in range(8)]
    data = census.encode({"v": 1, "source": "mac", "seq": 1, "ttl": 60, "more": 2, "rows": rows})
    body = json.loads(data)
    assert len(data) <= census.MAX_POST_BYTES and len(body["rows"]) < 8
    assert body["more"] == 2 + (8 - len(body["rows"]))
    assert [r["id"] for r in body["rows"]] == ["run-%d" % i for i in range(len(body["rows"]))]
    assert census.encode({"v": 1, "source": "mac", "ttl": 5, "more": 0, "rows": []}) == b'{"v":1,"source":"mac","ttl":5,"more":0,"rows":[]}'
    tiny = census.encode({"v": 1, "source": "mac", "seq": 1, "ttl": 60, "more": 998, "rows": [{"id": "run-x", "pad": "z" * 5000}]})
    assert json.loads(tiny)["more"] == 999 and json.loads(tiny)["rows"] == []


def test_twelve_jobs_send_eight_and_count_four(tmp_path):
    shapes = [(100 + i, 1, "05:%02d" % i, "bash ./%s%d.sh" % ("nightly" if i % 2 else "weekly", i), False) for i in range(12)]
    snap = scan(tmp_path, table(shapes), cfg=make_config(tmp_path, {"jobs": [{"match": "nightly"}, {"match": "weekly"}]}), cap=None)
    assert len(snap["rows"]) == 8 and snap["more"] == 4 and len(census.encode(snap)) < 4096


def test_the_seq_is_the_start_of_the_pass_in_milliseconds(tmp_path):
    assert scan(tmp_path)["seq"] == 1_760_800_000_000
    snap = census.scan(census.parse_ps(table()), make_config(tmp_path), 1_760_800_000.1239, cwd, no_session, no_idle, HOME)
    assert snap["seq"] == 1_760_800_000_123


def test_the_group_and_the_source_come_from_the_config(tmp_path):
    cfg = make_config(tmp_path, {"group": "Maquinas", "source": "casa", "jobs": [{"match": "nightly", "group": "Cron"}]})
    snap = scan(tmp_path, cfg=cfg._replace(max_rows=99))
    assert snap["source"] == "casa"
    assert {r["group"] for r in snap["rows"] if r["id"].startswith("run-")} == {"Maquinas"}
    assert {r["group"] for r in snap["rows"] if r["id"].startswith("job-")} == {"Cron"}


# --- the token and where it may go ---------------------------------------------------------------------------------

def test_the_token_rule_is_the_hooks(tmp_path):
    good = tmp_path / "t"
    good.write_text("T" * 32 + "\n", encoding="utf-8")
    for mode in (0o600, 0o400, 0o640, 0o644, 0o660, 0o606, 0o666, 0o700):
        good.chmod(mode)
        assert census.read_token(str(good)) == context_hook.read_token(str(good)), oct(mode)
    good.chmod(0o600)
    assert census.read_token(str(good)) == "T" * 32
    for text in ("short", "", " " * 40, "x" * 15 + "\n", "x" * 16):
        good.write_text(text, encoding="utf-8")
        assert census.read_token(str(good)) == context_hook.read_token(str(good)), repr(text)
    assert census.read_token(str(tmp_path / "none")) is None and census.read_token(str(tmp_path)) is None
    link = tmp_path / "link"
    good.write_text("T" * 32, encoding="utf-8")
    link.symlink_to(good)
    assert census.read_token(str(link)) == context_hook.read_token(str(link))
    (tmp_path / "bin").write_bytes(b"\xff\xfe" * 20)
    (tmp_path / "bin").chmod(0o600)
    assert census.read_token(str(tmp_path / "bin")) is None  # (the hook raises on bytes that are not UTF-8: the census just has no token)


@pytest.mark.parametrize("url", ["http://buddy:8766", "http://BUDDY:8766", "http://buddy", "http://127.0.0.1:8766", "http://127.9.9.9:1",
                                 "http://localhost:8766", "http://[::1]:8766", "http://100.101.102.103:8766", "http://100.64.0.1",
                                 "http://100.127.255.255:1", "http://my-box.tail1234.ts.net:8766", "http://pi:8766/", "http://pi-4:8766"])
def test_urls_the_token_may_go_to(url):
    assert census.url_problem(url, False) is None


@pytest.mark.parametrize("url", ["http://buddy.local:8766", "http://192.168.1.50:8766", "http://100.128.0.1:8766",
                                 "http://100.63.255.255:8766", "http://example.com", "http://10.0.0.5:8766", "http://[fe80::1]:8766",
                                 "http://[fd7a:115c:a1e0::1]:8766", "http://evil.ts.net.example.com", "http://1.1.1.1",
                                 # a word with no dot that is a number: the resolver turns each of these into an IPv4 address
                                 "http://3232235777:8766", "http://0xc0a80101:8766", "http://017700000001:8766", "http://134744072",
                                 "http://1:8766", "http://0x08080808", "http://-buddy:8766", "http://9buddy:8766"])
def test_urls_the_token_must_not_go_to(url):
    assert "claro" in census.url_problem(url, False)
    assert census.url_problem(url, True) is None  # insecure_url_ok lifts the rule about the host


@pytest.mark.parametrize("url", ["https://buddy:8766", "ftp://buddy", "buddy:8766", "http://", "", "http://user:pw@buddy:8766",
                                 "http://buddy:99999", "http://buddy:0", "//buddy", "http://[::1"])
def test_urls_that_are_not_plain_http_are_never_ok(url):
    assert census.url_problem(url, False) and census.url_problem(url, True)


def test_a_refused_url_sends_nothing_and_says_so_once(tmp_path):
    logs = []
    buddy = Buddy(200)
    env = {"CLAUDE_BUDDY_URL": "http://example.com:8766", "CLAUDE_BUDDY_CENSUS_CONFIG": str(tmp_path / "none.json")}
    assert census.run(env, read_ps=lambda *a, **k: table(), post=buddy, token=lambda p: TOKEN, log=logs.append, cwd=cwd) == 0
    assert buddy.calls == [] and len(logs) == 1 and "no mando nada" in logs[0] and TOKEN not in logs[0]
    path = write_config(tmp_path, {"insecure_url_ok": True, "interval_s": 5})
    env["CLAUDE_BUDDY_CENSUS_CONFIG"] = str(path)
    census.run(env, once=True, read_ps=lambda *a, **k: table(), post=buddy, token=lambda p: TOKEN, log=logs.append, cwd=cwd,
               session=no_session, idle=no_idle)
    assert len(buddy.calls) == 1


# --- one pass, with a pretend ps and a pretend buddy ---------------------------------------------------------------------

class Buddy:
    """The buddy's side: answers with scripted statuses (an exception instance is raised); the last one repeats."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, token, data, **kw):
        self.calls.append((url, token, data, kw))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def census_for(tmp_path, buddy, text=None, logs=None, cfg=None, token=TOKEN, **kw):
    kw.setdefault("read_ps", lambda *a, **k: text if text is not None else table())
    return census.Census(cfg or make_config(tmp_path), claude_dir=str(tmp_path / "claude"), post=buddy, cwd=cwd, session=no_session,
                         idle=no_idle, token=lambda p: token, log=(logs if logs is not None else []).append, clock=lambda: NOW, **kw)


def logged(logs, word):
    return [m for m in logs if word in m]


def test_one_pass_posts_one_snapshot_to_the_census_route(tmp_path):
    buddy = Buddy(200)
    c = census_for(tmp_path, buddy)
    assert c.cycle() == 15.0
    (url, token, data, kw), = buddy.calls
    assert (url, token) == (census.DEFAULT_URL, TOKEN) and kw.get("stop") is c.stop
    body = json.loads(data)
    assert body["v"] == 1 and body["source"] == "mac" and body["ttl"] == 60 and body["seq"] == int(NOW * 1000) and len(body["rows"]) == 8


def test_the_registry_is_asked_for_the_moment_ps_answered_and_the_seq_stays_at_the_start_of_the_pass(tmp_path):
    """ps measures how long every process has run when it answers. On a busy Mac that is seconds after the pass began, and
    the 3 s window that tells one process's registry file from another's would never match if it counted from the start."""
    t = [NOW]
    asked = []

    def slow_ps(*a, **k):
        t[0] += 5
        return table()

    def session(pid, elapsed, now):
        asked.append(now)
        return None

    buddy = Buddy(200)
    c = census.Census(make_config(tmp_path), claude_dir=str(tmp_path / "claude"), post=buddy, cwd=cwd, session=session, idle=no_idle,
                      token=lambda p: TOKEN, log=lambda m: None, clock=lambda: t[0], read_ps=slow_ps)
    c.cycle()
    assert asked and set(asked) == {NOW + 5}
    assert json.loads(buddy.calls[0][2])["seq"] == int(NOW * 1000)  # snapshots are ordered by when their pass began
    t[0] = NOW
    asked.clear()
    c.read_ps = lambda *a, **k: (t.__setitem__(0, NOW - 3), table())[1]  # a clock set back while ps ran: never earlier than the start
    c.cycle()
    assert set(asked) == {NOW}


def test_network_errors_back_off_15_30_60_and_say_so_once(tmp_path):
    logs = []
    buddy = Buddy(ConnectionRefusedError("secret detail"), socket.gaierror("secret detail"), TimeoutError("post"), OSError(), 200, 200)
    c = census_for(tmp_path, buddy, logs=logs)
    assert [c.cycle() for _ in range(4)] == [15.0, 30.0, 60.0, 60.0]
    assert logged(logs, "no responde") == ["buddy no responde (ConnectionRefusedError)"]
    assert c.cycle() == 15.0 and logged(logs, "volvio") == ["buddy volvio"]
    c.cycle()
    assert len(logged(logs, "volvio")) == 1 and not logged(logs, "secret")


def test_a_name_that_leads_to_a_foreign_address_is_said_in_words_once_and_backs_off_like_no_answer(tmp_path):
    logs = []
    c = census_for(tmp_path, Buddy(census.PeerRefused("peer"), census.PeerRefused("peer"), 200), logs=logs)
    assert [c.cycle() for _ in range(3)] == [15.0, 30.0, 15.0]  # (the third is the 200)
    (said,) = [m for m in logs if "direccion" in m]
    assert "no le mando el token" in said and "insecure_url_ok" in said and not logged(logs, "PeerRefused") and TOKEN not in said


def test_a_slow_interval_never_backs_off_to_less_than_itself(tmp_path):
    cfg = make_config(tmp_path, {"interval_s": 30})
    c = census_for(tmp_path, Buddy(OSError()), cfg=cfg)
    assert [c.cycle() for _ in range(3)] == [30.0, 30.0, 60.0]


def test_a_5xx_is_as_if_the_buddy_did_not_answer(tmp_path):
    logs = []
    c = census_for(tmp_path, Buddy(500, 503, 418, 200), logs=logs)
    assert [c.cycle() for _ in range(4)] == [15.0, 30.0, 60.0, 15.0]
    assert logged(logs, "no responde") == ["buddy no responde (http 500)"] and logged(logs, "volvio")


def test_a_wrong_token_waits_ten_minutes_and_says_so_once(tmp_path):
    logs = []
    c = census_for(tmp_path, Buddy(403, 403, 403, 200), logs=logs)
    assert [c.cycle() for _ in range(3)] == [600.0, 600.0, 600.0]
    assert len(logged(logs, "403")) == 1
    assert c.cycle() == 15.0 and logged(logs, "volvio")


def test_an_old_daemon_without_census_waits_five_minutes(tmp_path):
    logs = []
    c = census_for(tmp_path, Buddy(404), logs=logs)
    assert [c.cycle() for _ in range(2)] == [300.0, 300.0]
    assert len(logged(logs, "/census")) == 1 and logged(logs, "daemon viejo")


def test_too_many_waits_a_third_of_the_ttl(tmp_path):
    c = census_for(tmp_path, Buddy(429, 200))
    assert c.cycle() == 20.0 and c.cfg.ttl / 3.0 == 20.0
    slow = census_for(tmp_path, Buddy(429), cfg=make_config(tmp_path, {"interval_s": 30}))
    assert slow.cycle() == 40.0 <= slow.cfg.ttl


def test_a_body_the_listener_refuses_keeps_the_cadence_and_says_so_once(tmp_path):
    logs = []
    c = census_for(tmp_path, Buddy(400, 413, 413, 413, 200), logs=logs)
    assert [c.cycle() for _ in range(4)] == [15.0] * 4
    assert len(logged(logs, "400")) == 1 and len(logged(logs, "413")) == 1
    c.cycle()
    assert logged(logs, "volvio")


def test_a_pass_that_cannot_see_posts_nothing(tmp_path):
    logs = []
    buddy = Buddy(200)
    short = "\n".join(line(*s[:4]) for s in SHAPES[:5] + FILLER[:10]) + "\n"
    assert len(census.parse_ps(short)) < census.MIN_PROCS_SANE
    for text in (None, "", short, "Segmentation fault\n"):
        c = census.Census(make_config(tmp_path), claude_dir=str(tmp_path), post=buddy, token=lambda p: TOKEN, log=logs.append,
                          read_ps=lambda *a, **k: text, cwd=cwd, session=no_session, idle=no_idle, clock=lambda: NOW)
        assert c.cycle() == 15.0
    assert buddy.calls == []
    assert len(logged(logs, "procesos")) == 4  # once for each Census, and the loop keeps going
    c = census_for(tmp_path, buddy, logs=logs)
    c.read_ps = lambda *a, **k: None
    c.cycle()
    c.cycle()
    assert len(logged(logs, "no pude leer")) == 5
    c.read_ps = lambda *a, **k: table()
    c.cycle()
    assert logged(logs, "volvio") and len(buddy.calls) == 1


def test_no_usable_token_posts_nothing_and_says_so_once_an_hour(tmp_path):
    logs = []
    clock = [1000.0]
    buddy = Buddy(200)
    c = census_for(tmp_path, buddy, logs=logs, token=None, monotonic=lambda: clock[0])
    for step in (0, 60, 3000, 3599):
        clock[0] = 1000.0 + step
        assert c.cycle() == 15.0
    assert len(logged(logs, "sin token")) == 1 and buddy.calls == []
    clock[0] = 1000.0 + 3600
    c.cycle()
    assert len(logged(logs, "sin token")) == 2


def test_the_log_only_changes_when_the_set_of_rows_changes(tmp_path):
    logs = []
    c = census_for(tmp_path, Buddy(200), logs=logs)
    c.cycle()
    c.cycle()
    assert len(logged(logs, "corridas")) == 1 and "run-alpha" in logs[0] and "(+5)" in logs[0]
    c.read_ps = lambda *a, **k: table([s for s in SHAPES if s[0] != 162])
    c.cycle()
    assert len(logged(logs, "corridas")) == 2 and "run-alpha" not in logs[-1]
    c.cycle()
    assert len(logged(logs, "corridas")) == 2


def test_nothing_private_reaches_the_log_or_the_wire(tmp_path):
    logs = []
    buddy = Buddy(ConnectionRefusedError(FAKE_KEY), 200)
    c = census_for(tmp_path, buddy, logs=logs, token=TOKEN)
    c.cycle()
    c.cycle()
    c.farewell()
    everything = "\n".join(logs) + b"".join(call[2] for call in buddy.calls).decode("ascii")
    for secret in (FAKE_KEY, TOKEN, "Read docs", "acceptEdits", "stream-json", "Application Support", "vendor/model", "Bearer"):
        assert secret not in everything, secret


def test_a_dry_pass_prints_the_snapshot_and_touches_nothing_else(tmp_path):
    out = io.StringIO()

    def forbidden(*a, **k):
        raise AssertionError("a dry pass must not read the token or post")

    c = census.Census(make_config(tmp_path), claude_dir=str(tmp_path), post=forbidden, token=forbidden, read_ps=lambda *a, **k: table(),
                      cwd=cwd, session=no_session, idle=no_idle, log=lambda m: None, clock=lambda: NOW, out=out)
    assert c.cycle(dry=True) == 15.0
    printed = json.loads(out.getvalue())
    assert printed["rows"][0]["id"] == "run-alpha" and FAKE_KEY not in out.getvalue()


def test_a_bug_in_a_pass_does_not_end_the_loop(tmp_path):
    logs = []
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError(FAKE_KEY)

    sleeps = []
    env = {"CLAUDE_BUDDY_URL": "http://127.0.0.1:1", "CLAUDE_BUDDY_CENSUS_CONFIG": str(tmp_path / "none.json")}
    rc = census.run(env, read_ps=boom, log=logs.append, token=lambda p: TOKEN, post=Buddy(200), cwd=cwd,
                    sleep=lambda d: sleeps.append(d) or len(sleeps) >= 3)
    assert rc == 0 and len(calls) == 3 and len(sleeps) == 3 and all(0 < d <= 15 for d in sleeps)
    assert len(logged(logs, "inesperado")) == 1 and FAKE_KEY not in "\n".join(logs)
    with pytest.raises(RuntimeError):
        census.run(env, once=True, read_ps=boom, log=logs.append, token=lambda p: TOKEN, post=Buddy(200), cwd=cwd)


# --- the loop, the clock and SIGTERM -----------------------------------------------------------------------------------------

def run_env(tmp_path):
    return {"CLAUDE_BUDDY_URL": "http://127.0.0.1:1", "CLAUDE_BUDDY_CENSUS_CONFIG": str(tmp_path / "none.json"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}


def test_the_next_pass_is_due_from_the_start_of_this_one(tmp_path):
    ticks = iter([100.0, 103.0, 200.0, 204.5])
    delays = []
    buddy = Buddy(200)
    rc = census.run(run_env(tmp_path), read_ps=lambda *a, **k: table(), post=buddy, token=lambda p: TOKEN, cwd=cwd, session=no_session,
                    idle=no_idle, log=lambda m: None, clock=lambda: NOW, monotonic=lambda: next(ticks),
                    sleep=lambda d: delays.append(d) or len(delays) >= 2)
    assert rc == 0 and delays == [12.0, 10.5]
    assert len(buddy.calls) == 3  # two passes and the farewell


def test_a_pass_that_ran_long_does_not_wait_at_all(tmp_path):
    ticks = iter([100.0, 140.0])
    delays = []
    census.run(run_env(tmp_path), once=False, read_ps=lambda *a, **k: table(), post=Buddy(200), token=lambda p: TOKEN, cwd=cwd,
               session=no_session, idle=no_idle, log=lambda m: None, clock=lambda: NOW, monotonic=lambda: next(ticks),
               sleep=lambda d: delays.append(d) or True)
    assert delays == [0.0]


def test_sigterm_leaves_an_empty_snapshot_within_the_deadline_and_exits_0(tmp_path):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals are handled in the main thread")
    sent = []

    def buddy(url, token, data, **kw):
        sent.append((url, token, data, kw))
        if len(sent) == 1:
            os.kill(os.getpid(), signal.SIGTERM)  # arrives while the first snapshot is on its way
        return 200

    before = signal.getsignal(signal.SIGTERM)
    start = time.monotonic()
    rc = census.run(run_env(tmp_path), read_ps=lambda *a, **k: table(), post=buddy, token=lambda p: TOKEN, cwd=cwd, session=no_session,
                    idle=no_idle, log=lambda m: None)
    assert rc == 0 and time.monotonic() - start < 8  # it did not sit out the 15 s wait
    assert len(sent) == 2 and sent[1][2] == b'{"v":1,"source":"mac","ttl":5,"more":0,"rows":[]}'
    assert sent[1][3].get("join_s") == census.FAREWELL_JOIN_S == 1.5 and sent[1][1] == TOKEN
    assert signal.getsignal(signal.SIGTERM) == before  # the handlers are put back


def test_no_farewell_after_a_refused_token_or_without_one(tmp_path):
    for status in (403, 404):
        buddy = Buddy(status)
        c = census_for(tmp_path, buddy)
        c.cycle()
        c.farewell()
        assert len(buddy.calls) == 1, status
    buddy = Buddy(200)
    c = census_for(tmp_path, buddy, token=None)
    c.farewell()
    assert buddy.calls == []
    buddy = Buddy(OSError())
    c = census_for(tmp_path, buddy)
    c.farewell()  # a farewell that cannot be delivered is silent
    assert len(buddy.calls) == 1


def test_a_dry_run_does_not_say_goodbye_and_the_off_switch_does_nothing(tmp_path):
    buddy = Buddy(200)
    assert census.run(run_env(tmp_path), once=True, dry=True, read_ps=lambda *a, **k: table(), post=buddy, token=lambda p: TOKEN,
                      cwd=cwd, session=no_session, idle=no_idle, log=lambda m: None, out=io.StringIO()) == 0
    assert buddy.calls == []

    def forbidden(*a, **k):
        raise AssertionError("off means off")

    for value in ("off", "OFF", "0", "no", " off "):
        env = dict(run_env(tmp_path), CLAUDE_BUDDY_CENSUS=value)
        assert census.run(env, read_ps=forbidden, post=forbidden, token=forbidden, log=forbidden) == 0, value


def test_stop_is_an_event_with_no_lock_to_deadlock_on():
    stop = census.Stop()
    assert not stop.is_set()
    start = time.monotonic()
    assert stop.wait(0.3) is False and 0.25 < time.monotonic() - start < 4
    threading.Timer(0.2, stop.set).start()
    start = time.monotonic()
    assert stop.wait(30) is True and time.monotonic() - start < 8  # woken by set(), not by the 30 s
    assert stop.is_set() and stop.wait(0) is True and stop.wait(5) is True
    assert not hasattr(stop, "_cond")  # nothing for a signal handler to wait on
    # the real thing: set() from the handler while the main thread is inside wait()
    if threading.current_thread() is threading.main_thread():
        again = census.Stop()
        before = signal.signal(signal.SIGUSR1, lambda *_: again.set())
        try:
            threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGUSR1)).start()
            start = time.monotonic()
            assert again.wait(30) is True and time.monotonic() - start < 8
        finally:
            signal.signal(signal.SIGUSR1, before)


def test_post_gives_up_on_a_post_that_hangs_and_on_stop(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(census, "post", lambda url, token, data: release.wait(5) or 200)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        census.post_bounded("http://x", TOKEN, b"{}", join_s=0.3)
    assert 0.25 < time.monotonic() - start < 4
    stop = census.Stop()
    threading.Timer(0.2, stop.set).start()
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        census.post_bounded("http://x", TOKEN, b"{}", stop=stop, join_s=30)
    assert time.monotonic() - start < 8  # stopped by SIGTERM, not by the 30 s
    release.set()
    monkeypatch.setattr(census, "post", lambda url, token, data: 200)
    assert census.post_bounded("http://x", TOKEN, b"{}") == 200
    monkeypatch.setattr(census, "post", lambda url, token, data: (_ for _ in ()).throw(ConnectionResetError()))
    with pytest.raises(ConnectionResetError):
        census.post_bounded("http://x", TOKEN, b"{}")


def test_post_sends_what_the_listener_expects():
    seen = {}
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            head, _, rest = data.partition(b"\r\n\r\n")
            length = int([h for h in head.decode().split("\r\n") if h.lower().startswith("content-length")][0].split(":")[1])
            while len(rest) < length:
                rest += conn.recv(4096)
            seen.update(head=head.decode(), body=rest)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert census.post("http://127.0.0.1:%d" % port, TOKEN, b'{"v":1}') == 200
    finally:
        thread.join(5)
        server.close()
    lines = seen["head"].split("\r\n")
    headers = {h.split(":", 1)[0].lower(): h.split(":", 1)[1].strip() for h in lines[1:]}
    assert lines[0] == "POST /census HTTP/1.1" and seen["body"] == b'{"v":1}'
    assert headers["authorization"] == "Bearer " + TOKEN and headers["content-type"] == "application/json"
    assert headers["host"] == "127.0.0.1:%d" % port


def test_post_to_nobody_raises_instead_of_hanging():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(OSError):
        census.post("http://127.0.0.1:%d" % port, TOKEN, b"{}")


def reach(monkeypatch, peer):
    """http.client connections that go nowhere and say they reached `peer`; the list collects the requests they were asked to send."""
    sent = []

    class Conn:
        def __init__(self, host, port, timeout=None):
            pass

        def connect(self):
            self.sock = types.SimpleNamespace(settimeout=lambda t: None, getpeername=lambda: (peer, 8766))

        def request(self, method, path, body=None, headers=None):
            sent.append(headers)

        def getresponse(self):
            return types.SimpleNamespace(status=200, read=lambda n: b"")

        def close(self):
            pass

    monkeypatch.setattr(census.http.client, "HTTPConnection", Conn)
    return sent


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "100.101.102.103", "100.64.0.1", "100.127.255.255",
                                  "fd7a:115c:a1e0::5", "::ffff:100.101.102.103"])
def test_the_token_goes_to_an_address_of_this_mac_or_the_tailnet(monkeypatch, peer):
    sent = reach(monkeypatch, peer)
    assert census.post("http://buddy:8766", TOKEN, b"{}") == 200 and len(sent) == 1


@pytest.mark.parametrize("peer", ["192.168.1.50", "8.8.8.8", "100.128.0.1", "100.63.255.255", "10.0.0.5", "fe80::1", "fe80::1%en0",
                                  "fd00::1", "::ffff:192.168.1.1", "not an address", ""])
def test_a_name_that_leads_anywhere_else_gets_no_token(monkeypatch, peer):
    """The URL is judged by its text; the address the name really led to is what the token meets."""
    sent = reach(monkeypatch, peer)
    with pytest.raises(census.PeerRefused):
        census.post("http://buddy:8766", TOKEN, b"{}")
    assert sent == []  # not even the request was written
    assert census.post("http://example.com:8766", TOKEN, b"{}") == 200 and len(sent) == 1  # insecure_url_ok chose this URL on purpose


# --- reading the process table ------------------------------------------------------------------------------------------------

class FakeChild:
    def __init__(self, out=b"", code=0, hang=False):
        self.out, self.returncode, self.hang, self.killed = out, code, hang, False

    def communicate(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ps", timeout or 0)
        return self.out, None

    def kill(self):
        self.killed = True


def test_ps_is_one_fixed_call_without_a_shell_or_the_environment_flag(monkeypatch):
    seen = {}

    def popen(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return FakeChild(table().encode())

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert census.read_ps() == table()
    assert seen["argv"] == ["/bin/ps", "-U", str(os.getuid()), "-ww", "-o", "pid=,ppid=,etime=,command="]
    assert not seen["kw"].get("shell") and seen["kw"]["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    assert not any(a in ("e", "E", "-e", "-E", "eww", "auxeww") for a in seen["argv"])


def test_ps_that_fails_or_hangs_gives_nothing(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: FakeChild(table().encode(), code=1))
    assert census.read_ps() is None
    hung = FakeChild(hang=True)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: hung)
    start = time.monotonic()
    assert census.read_ps(timeout=0.4) is None and hung.killed
    assert time.monotonic() - start < 8
    hung2 = FakeChild(hang=True)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: hung2)
    stop = census.Stop()
    stop.set()
    assert census.read_ps(stop=stop, timeout=30) is None and hung2.killed  # SIGTERM does not wait for a slow ps

    def missing(argv, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "Popen", missing)
    assert census.read_ps() is None


def test_the_real_ps_call_is_valid_and_lists_this_process():
    if not os.path.exists("/bin/ps"):
        pytest.skip("no /bin/ps here")
    text = census.read_ps()
    assert text is not None
    me = [p for p in census.parse_ps(text) if p.pid == os.getpid()]
    assert me and me[0].elapsed >= 0 and len(census.parse_ps(text)) >= census.MIN_PROCS_SANE


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS")
def test_the_working_directory_comes_from_libproc():
    assert census.cwd_of(os.getpid()) == os.path.realpath(os.getcwd())
    assert census.cwd_of(2 ** 22 + 12345) is None


def test_the_working_directory_of_a_pid_that_is_not_there_is_none():
    assert census.cwd_of(2 ** 22 + 12345) is None


def test_the_start_of_a_run_reports_the_config_warnings_by_phrase_only(tmp_path):
    logs = []
    path = write_config(tmp_path, {"jobs": [{"match": "(" + FAKE_KEY, "regex": True}], "interval_s": 1, "version": 2})
    env = dict(run_env(tmp_path), CLAUDE_BUDDY_CENSUS_CONFIG=str(path))
    census.run(env, once=True, read_ps=lambda *a, **k: table(), post=Buddy(200), token=lambda p: TOKEN, cwd=cwd, session=no_session,
               idle=no_idle, log=logs.append, clock=lambda: NOW)
    assert [m for m in logs if m.startswith("config: ")] == ["config: version desconocida, leo solo lo que entiendo",
                                                              "config: jobs[0]: la regex no compila",
                                                              "config: interval_s: fuera de 5 a 30, uso 5"]
    assert logged(logs, "arranca") == ["census arranca: cada 5 s, 0 trabajos, corridas de Claude si"]
    assert FAKE_KEY not in "\n".join(logs)


def test_the_ttl_follows_the_interval_in_the_snapshot(tmp_path):
    for interval, ttl in ((5, 20), (15, 60), (30, 120), (1, 20), (999, 120)):
        cfg = make_config(tmp_path, {"interval_s": interval})
        assert scan(tmp_path, cfg=cfg)["ttl"] == ttl == cfg.ttl, interval
        assert 4 * cfg.interval <= cfg.ttl <= 120  # one lost snapshot does not blink the group


def test_a_pass_cut_short_by_sigterm_is_not_a_failure(tmp_path):
    logs = []
    c = census_for(tmp_path, Buddy(TimeoutError("post")), logs=logs)
    c.stop.set()  # what the signal handler does while a post is waiting
    assert c.cycle() == 15.0 and not logged(logs, "no responde") and c.st.failures == 0


# --- the command line and Python 3.9 ----------------------------------------------------------------------------------------------

def test_usage_errors(capsys):
    for argv in (["census.py"], ["census.py", "go"], ["census.py", "run", "--print"], ["census.py", "once", "--now"],
                 ["census.py", "once", "--print", "x"]):
        assert census.main(argv) == 2, argv
    assert "usage" in capsys.readouterr().err


def test_main_runs_once_and_prints_without_posting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(census, "read_ps", lambda *a, **k: table())
    monkeypatch.setattr(census, "cwd_of", cwd)
    monkeypatch.setattr(census, "post_bounded", forbidden_post)
    monkeypatch.setattr(census, "HOME", HOME)
    monkeypatch.setenv("CLAUDE_BUDDY_CENSUS_CONFIG", str(write_config(tmp_path, {"strip_prefix": ["work-"]})))
    assert census.main(["census.py", "once", "--print"]) == 0
    out, err = capsys.readouterr()
    printed = json.loads(out)
    assert printed["rows"][0]["id"] == "run-alpha" and FAKE_KEY not in out + err and "Application Support" not in out + err
    monkeypatch.setenv("CLAUDE_BUDDY_CENSUS", "off")
    assert census.main(["census.py", "run"]) == 0
    assert capsys.readouterr() == ("", "")


def forbidden_post(*a, **k):
    raise AssertionError("--print must not post")


def test_the_census_is_standard_library_only_and_python39_syntax():
    source = CENSUS.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))  # no match statements, no parenthesized with-items
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"calendar", "ctypes", "http", "ipaddress", "json", "math", "os", "re", "signal", "stat", "subprocess", "sys",
                        "threading", "time", "typing", "urllib"}


def test_it_imports_and_classifies_under_system_python39():
    if not os.path.exists(SYSTEM_PYTHON):
        pytest.skip("no system python")
    version = subprocess.run([SYSTEM_PYTHON, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
                             capture_output=True, text=True).stdout.strip()
    if version != "True":
        pytest.skip("/usr/bin/python3 is not 3.9 here")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import census; "
            "p = census.parse_ps('  7  1 01:30 claude -p hi\\n  8  1 01:30 bash ./a.sh\\n'); print(p[0].headless, p[1].script)")
    done = subprocess.run([SYSTEM_PYTHON, "-I", "-c", code, str(CENSUS.parent)], capture_output=True, text=True, timeout=60)
    assert (done.returncode, done.stdout.strip()) == (0, "True a.sh"), done.stderr


# --- the whole thing as processes: a decoy run, the daemon's real listener and census.py ---------------------------------------

needs_processes = pytest.mark.skipif(shutil.which("bash") is None or not os.path.exists("/bin/ps"), reason="needs bash and /bin/ps")
Done = collections.namedtuple("Done", "code out err")
EMPTY_SNAPSHOT = b'{"v":1,"source":"mac","ttl":5,"more":0,"rows":[]}'


class Decoy:
    """A process that `ps` shows as `claude -c ... -p <prompt>`, started in a folder called relay-decoy-<hex>."""

    def __init__(self, pid, folder):
        self.pid, self.folder, self.alive = pid, folder, True

    @property
    def name(self):
        # the folder comes from libproc, which is macOS only: elsewhere the census names a run after its pid
        return self.folder.name[len("relay-"):] if sys.platform == "darwin" else "claude-%d" % self.pid

    @property
    def run_id(self):
        return "run-" + self.name

    def stop(self):
        if self.alive:
            self.alive = False
            census_machine.stop_decoy(self.pid)


@pytest.fixture
def decoy(tmp_path):
    """Starts decoys (headless runs with the fake key in their prompt) and always kills them at the end."""
    made = []

    def start(prompt=PROMPT):
        folder = tmp_path / ("relay-decoy-" + uuid.uuid4().hex[:8])
        made.append(Decoy(census_machine.start_decoy(folder, prompt), folder))
        return made[-1]

    yield start
    for each in made:
        each.stop()


@contextlib.asynccontextmanager
async def listening(rig, board=True):
    """The daemon's real HermesServer on loopback, writing down every request it is sent. Without a board it is an old
    daemon: /census (like /session) is a 404."""
    async with hermes(rig, token=TOKEN, tasks=rig.core.tasks if board else None) as server:
        seen = []
        route = server._route

        async def spy(request, reader, writer):
            seen.append((request.method, request.path, request.body))
            await route(request, reader, writer)

        server._route = spy
        yield server, seen


def posts(seen):
    """The bodies of the snapshots the listener was sent, as it received them."""
    return [body for method, path, body in seen if (method, path) == ("POST", "/census")]


def on_screen(rig):
    return rig.view()["tasks"]


async def until(check, what, timeout=20.0):
    end = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < end, "waited %g s for %s" % (timeout, what)
        await asyncio.sleep(0.05)


async def finish(proc, timeout=30):
    """Waits for a process without blocking the listener, which lives in this same event loop."""
    loop = asyncio.get_running_loop()
    try:
        out, err = await loop.run_in_executor(None, functools.partial(proc.communicate, timeout=timeout))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("the census did not end in %g s" % timeout)
    return Done(proc.returncode, out, err)


async def hook_says(server, session, **fields):
    """What tools/context_hook.py does: a session says what it is doing."""
    status, _, body = await http(server, "POST", "/session", headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
                                 body=json.dumps(dict(fields, id=session)).encode())
    assert status == 200, body


def nothing_private(*texts):
    """The fake key, the words of the prompt, the command the decoy runs and the token are in no body, stdout or stderr."""
    for text in texts:
        text = text.decode("ascii") if isinstance(text, bytes) else text
        for secret in (FAKE_KEY, "sk-ant", "Read docs", "rules.md", "sleep %d" % census_machine.DECOY_LIFE_S, TOKEN, "Bearer"):
            assert secret not in text, secret


class Mac:
    """The pretend Mac of a test: the census runs in a HOME and a ~/.claude of its own, reads a config and a token file
    from tmp_path and talks to the listener it is given."""

    def __init__(self, tmp_path, server, token=TOKEN, **config):
        (tmp_path / "home").mkdir(exist_ok=True)
        self.token = tmp_path / "hermes.token"
        self.token.write_text(token, encoding="utf-8")
        self.token.chmod(0o600)
        path = write_config(tmp_path, dict({"min_age_s": 0, "strip_prefix": ["relay-"]}, **config))
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
                    "CLAUDE_BUDDY_URL": server.url, "CLAUDE_BUDDY_TOKEN_FILE": str(self.token), "CLAUDE_BUDDY_CENSUS_CONFIG": str(path)}

    def start(self, mode, ps="mine", keep=0, **env):
        """tools/census.py <mode> as a process. ps "real" is the plain command; the others go through
        census_machine.py, which only changes what `ps` shows it (see there). `keep` is the decoy's pid."""
        if ps == "real":
            cmd = [sys.executable, "-I", "-u", str(CENSUS), mode]
        else:
            cmd = [sys.executable, "-I", "-u", str(census_machine.SELF), ps, str(keep), mode]
        return subprocess.Popen(cmd, env=dict(self.env, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    async def once(self, ps="mine", keep=0, **env):
        return await finish(self.start("once", ps, keep, **env))


@needs_processes
def test_a_decoy_run_is_on_the_screen_in_corridas_until_it_dies(tmp_path, decoy, caplog):
    caplog.set_level(logging.DEBUG)  # the daemon's own debug line for a census says how many rows, never which
    rig = Rig(tmp_path / "state")
    run = decoy()

    async def scenario():
        async with listening(rig) as (server, seen):
            mac = Mac(tmp_path, server)
            first = await mac.once(keep=run.pid)
            assert first.code == 0, first.err
            (row,) = on_screen(rig)  # the other headless runs of this machine are taken out of what `ps` shows
            assert row["id"] == run.run_id and row["group"] == "Corridas" and row["source"] == "census"
            assert (row["state"], row["title"], row["project"]) == ("working", run.name, run.name)
            assert isinstance(row["elapsed"], int) and 0 <= row["elapsed"] < 60 and "idle" not in row  # nothing says what session it is
            (body,) = posts(seen)
            snap = json.loads(body)
            assert (snap["v"], snap["source"], snap["ttl"], snap["more"]) == (1, "mac", 60, 0)
            (sent,) = snap["rows"]
            assert sent["id"] == run.run_id and sent["name"] == run.name and (sent["group"], sent["state"]) == ("Corridas", "working")
            assert set(sent) <= {"id", "name", "group", "state", "elapsed", "idle", "session", "project", "what"}
            run.stop()  # killed: no hook, no SessionEnd, nothing says goodbye for it
            second = await mac.once(keep=run.pid)
            assert second.code == 0, second.err
            assert on_screen(rig) == []
            assert json.loads(posts(seen)[1])["rows"] == []
            nothing_private(first.out, first.err, second.out, second.err, *posts(seen))
            assert "census: 1 rows" in caplog.text and FAKE_KEY not in caplog.text and run.name not in caplog.text

    asyncio.run(scenario())


@needs_processes
def test_the_plain_command_finds_the_decoy_in_this_machines_own_process_table(tmp_path, decoy):
    # no driver, no filter: tools/census.py once, exactly as the LaunchAgent runs it. A Mac with eight older runs of its own
    # cuts the youngest (the decoy) off the eight rows, and then there is nothing to see
    if len(census.parse_ps(census.read_ps() or "")) < census.MIN_PROCS_SANE:
        pytest.skip("this user has fewer than %d processes: the census takes such a list for a failed ps" % census.MIN_PROCS_SANE)
    rig = Rig(tmp_path / "state")
    run = decoy()

    async def scenario():
        async with listening(rig) as (server, seen):
            done = await Mac(tmp_path, server).once(ps="real")
            assert done.code == 0, done.err[-300:]
            (body,) = posts(seen)
            board = {t.id: t for t in rig.core.tasks.tasks()}
            return json.loads(body), board, done

    snap, board, done = asyncio.run(scenario())
    if run.run_id not in [r["id"] for r in snap["rows"]] and snap["more"] > 0:
        pytest.skip("this machine runs eight or more older headless runs of its own: the decoy is past the cut")
    (sent,) = [r for r in snap["rows"] if r["id"] == run.run_id]
    assert (sent["group"], sent["state"]) == ("Corridas", "working")
    assert (board[run.run_id].source, board[run.run_id].group) == ("census", "Corridas")
    nothing_private(done.out, json.dumps(snap), done.err)


@needs_processes
def test_a_session_that_is_a_run_is_one_row_and_both_go_when_the_run_is_killed(tmp_path, decoy):
    rig = Rig(tmp_path / "state")
    run = decoy()
    registry(tmp_path, pid=run.pid, startedAt=int(time.time() * 1000))  # Claude Code's own list of live processes: pid -> session
    transcript(tmp_path, time.time() - 300)  # the session's transcript, silent for five minutes (stat only, never opened)

    async def scenario():
        async with listening(rig) as (server, seen):
            mac = Mac(tmp_path, server)
            # another folder than the run's: only its session id can tell that this session is that run
            await hook_says(server, SESSION, project="another-folder", tool="Bash", what="pytest -q")
            assert [t["id"] for t in on_screen(rig)] == [SESSION]  # before the census it is a session like any other
            first = await mac.once(keep=run.pid)
            assert first.code == 0, first.err
            (row,) = on_screen(rig)  # ONE row: the run, saying what its session is doing
            assert (row["id"], row["source"], row["group"]) == (run.run_id, "census", "Corridas")
            assert (row["tool"], row["what"]) == ("Bash", "pytest -q")
            assert 300 <= row["idle"] <= 330 and row["age"] == row["idle"]
            (sent,) = json.loads(posts(seen)[0])["rows"]
            assert sent["session"] == SESSION and 300 <= sent["idle"] <= 330
            run.stop()
            second = await mac.once(keep=run.pid)
            assert second.code == 0, second.err
            assert on_screen(rig) == [] and rig.core.tasks.tasks() == []  # the session is not left behind, alive for ten minutes
            nothing_private(first.out, first.err, second.out, second.err, *posts(seen))

    asyncio.run(scenario())


@needs_processes
def test_sigterm_leaves_the_empty_snapshot_with_a_ttl_of_five_and_the_run_is_gone_at_once(tmp_path, decoy):
    rig = Rig(tmp_path / "state")
    run = decoy()

    async def scenario():
        async with listening(rig) as (server, seen):
            proc = Mac(tmp_path, server).start("run", keep=run.pid)
            try:
                await until(lambda: [t["id"] for t in on_screen(rig)] == [run.run_id], "the decoy on the screen")
                asked = time.monotonic()
                proc.send_signal(signal.SIGTERM)
                done = await finish(proc, 20)
            finally:
                if proc.poll() is None:
                    proc.kill()
            assert done.code == 0, done.err
            assert time.monotonic() - asked < 8  # it did not sit out its 15 s wait
            sent = posts(seen)
            assert len(sent) >= 2 and sent[-1] == EMPTY_SNAPSHOT
            assert on_screen(rig) == []  # the first snapshot is good for 60 s: only the empty one can have emptied the screen
            assert "census arranca" in done.err and run.run_id in done.err
            nothing_private(done.out, done.err, *sent)

    asyncio.run(scenario())


@needs_processes
def test_a_pass_that_cannot_read_ps_or_gets_a_short_list_posts_nothing_and_leaves_the_screen_alone(tmp_path, decoy):
    rig = Rig(tmp_path / "state")
    run = decoy()

    async def scenario():
        async with listening(rig) as (server, seen):
            mac = Mac(tmp_path, server)
            first = await mac.once(keep=run.pid)
            assert first.code == 0 and [t["id"] for t in on_screen(rig)] == [run.run_id], first.err
            assert len(posts(seen)) == 1
            for ps in ("fail", "short"):  # ps exits 1; ps lists ten processes
                done = await mac.once(ps=ps, keep=run.pid)
                assert done.code == 0, ps
                assert "no pude leer la lista de procesos: no mando nada" in done.err, ps
                nothing_private(done.out, done.err)
            assert len(posts(seen)) == 1  # nothing was sent, so nothing was emptied
            assert [t["id"] for t in on_screen(rig)] == [run.run_id]

    asyncio.run(scenario())


@needs_processes
def test_a_daemon_that_answers_404_for_census_is_noted_once_and_left_alone_for_minutes(tmp_path, decoy):
    rig = Rig(tmp_path / "state")
    run = decoy()

    async def scenario():
        async with listening(rig, board=False) as (server, seen):
            proc = Mac(tmp_path, server, interval_s=5).start("run", keep=run.pid)  # five seconds is the fastest cadence it allows
            try:
                await until(lambda: len(posts(seen)) == 1, "the first snapshot")
                await asyncio.sleep(6.5)  # a normal cadence would have asked again by now
                assert len(posts(seen)) == 1
                proc.send_signal(signal.SIGTERM)
                done = await finish(proc, 20)
            finally:
                if proc.poll() is None:
                    proc.kill()
            assert done.code == 0, done.err
            assert done.err.count("este buddy no conoce /census (daemon viejo): reintento en 5 min") == 1
            assert len(posts(seen)) == 1  # and no goodbye to a daemon that has nothing of ours
            nothing_private(done.out, done.err, *posts(seen))

    asyncio.run(scenario())


@needs_processes
def test_a_token_the_listener_does_not_know_is_noted_and_changes_nothing(tmp_path, decoy):
    rig = Rig(tmp_path / "state")
    run = decoy()

    async def scenario():
        async with listening(rig) as (server, seen):
            done = await Mac(tmp_path, server, token="another-token-0123456789").once(keep=run.pid)
            assert done.code == 0, done.err
            assert "buddy rechazo el token (403): reintento en 10 min" in done.err
            assert len(posts(seen)) == 1 and on_screen(rig) == []
            nothing_private(done.out, done.err)
            assert "another-token" not in done.err + done.out

    asyncio.run(scenario())


@needs_processes
def test_the_census_sends_nothing_when_it_is_off_has_no_usable_token_or_no_safe_place_to_send_it(tmp_path):
    rig = Rig(tmp_path / "state")

    async def scenario():
        async with listening(rig) as (server, seen):
            mac = Mac(tmp_path, server)
            off = await mac.once(CLAUDE_BUDDY_CENSUS="off")
            assert (off.code, off.out, off.err) == (0, "", "")
            mac.token.chmod(0o644)  # readable by others: not a token it will use
            loose = await mac.once()
            assert loose.code == 0 and "sin token usable" in loose.err
            mac.token.chmod(0o600)
            elsewhere = await mac.once(CLAUDE_BUDDY_URL="http://example.invalid:8766")
            assert elsewhere.code == 0 and "no mando nada" in elsewhere.err
            assert seen == []  # not one request, to the listener or anywhere it could be told to go
            nothing_private(off.out, off.err, loose.out, loose.err, elsewhere.out, elsewhere.err)

    asyncio.run(scenario())
