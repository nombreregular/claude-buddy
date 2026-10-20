"""tools/notify.py: the command Hermes runs to leave a notice on the screen.

The unit tests call `notify.main` with the HTTP call replaced by a scripted buddy (what was asked, what it answers, which
exception it raises), a token file of their own in tmp_path and the environment passed in, so nothing here reads the real
~/.config/claude-buddy or the real environment. A planted token and a planted text ride in every call: no stdout, no
stderr and no exception phrase may contain either. The transport tests answer from a tiny loopback server that keeps the
bytes it was sent (the headers, the body, how many connections) and can hang, trickle bytes or redirect; the last tests run
the script as a process (also under the Python 3.9 of macOS) against that server.

The last section runs the script as a process against the daemon's real HermesServer on loopback, wired the way buddy.py
wires it (the core's NoticeBoard and the hub's wake), on a hub with the fake clock of test_night's rig."""
import ast
import asyncio
import collections
import contextlib
import functools
import http.client
import io
import json
import logging
import os
import random
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import buddy_core  # noqa: E402
import buddy_hermes  # noqa: E402
import census  # noqa: E402
import context_hook  # noqa: E402
import notify  # noqa: E402
from buddy_ui import AWAKE_S, MAX_BODY_BYTES  # noqa: E402
from test_hermes import hermes  # noqa: E402
from test_night import Night, at  # noqa: E402
from ui_client import http as raw_http  # noqa: E402  (the module `http` is http.client's, used above)

NOTIFY = Path(__file__).resolve().parent.parent / "tools" / "notify.py"
SYSTEM_PYTHON = "/usr/bin/python3"
TOKEN = "ZQXTOKEN" + "7" * 24
TEXT = "ZQXTEXT"  # rides in titles, bodies and sources of the failing calls: it must never come back out
REPLY = {"ok": True, "id": "n-1", "active": 1}
Done = collections.namedtuple("Done", "code out err calls")


# --- a pretend buddy and a way to run the tool against it ---------------------------------------------------------------

Call = collections.namedtuple("Call", "url token method path body strict")


class Buddy:
    """Stands in for notify.request. Answers with scripted outcomes, a (status, reply) pair or an exception instance that is
    raised; the last one repeats. A reply that is not bytes is sent as JSON."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [(200, REPLY)]
        self.calls = []

    def __call__(self, url, token, method, path, body, strict):
        self.calls.append(Call(url, token, method, path, body, strict))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        status, reply = outcome
        return status, reply if isinstance(reply, bytes) else json.dumps(reply).encode("utf-8")


class Pipe:
    """A stdin that records what it was asked for and always has more than that."""

    def __init__(self, data=b"", fail=False):
        self.data, self.fail, self.asked = data, fail, []

    def read(self, size=-1):
        self.asked.append(size)
        if self.fail:
            raise AssertionError("stdin was read")
        return self.data if size < 0 else self.data[:size]


def write_token(path, text=TOKEN, mode=0o600):
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


class Rig:
    def __init__(self, tmp_path, monkeypatch, capsys):
        self.tmp_path, self.monkeypatch, self.capsys = tmp_path, monkeypatch, capsys
        self.token_file = write_token(tmp_path / "hermes.token")

    def env(self, **extra):
        return dict({"CLAUDE_BUDDY_TOKEN_FILE": str(self.token_file)}, **extra)

    def run(self, *argv, stdin=b"", buddy=None, env=None):
        buddy = buddy if buddy is not None else Buddy()
        self.monkeypatch.setattr(notify, "request", buddy)
        stream = io.BytesIO(stdin) if isinstance(stdin, bytes) else stdin
        code = notify.main(list(argv), self.env() if env is None else env, stream)
        out, err = self.capsys.readouterr()
        return Done(code, out, err, buddy.calls)


@pytest.fixture
def rig(tmp_path, monkeypatch, capsys):
    return Rig(tmp_path, monkeypatch, capsys)


def clean(done):
    """Whatever came out is ASCII and holds neither the token nor the text."""
    shown = done.out + done.err
    assert TOKEN not in shown and "ZQX" not in shown, shown
    assert shown.isascii(), shown


def body_of(done):
    (call,) = done.calls
    assert (call.method, call.path) == ("POST", "/notice")
    return call.body


# --- sending ------------------------------------------------------------------------------------------------------------

def test_a_title_alone_posts_one_notice_and_prints_the_answer(rig):
    done = rig.run("--title", "La tanda termino")
    assert (done.code, done.err) == (0, "")
    (call,) = done.calls
    assert (call.url, call.token, call.method, call.path) == (notify.DEFAULT_URL, TOKEN, "POST", "/notice")
    assert call.body == {"title": "La tanda termino"} and call.strict is True
    assert done.out == '{"ok":true,"id":"n-1","active":1}\n'


def test_every_option_reaches_the_body(rig):
    done = rig.run("--title", "Ana escribio", "--body", "hola", "--kind", "warn", "--ttl", "600", "--wake", "--id", "ana:1",
                   "--source", "correo")
    body = body_of(done)
    assert body == {"title": "Ana escribio", "body": "hola", "kind": "warn", "ttl": 600, "wake": True, "id": "ana:1",
                    "source": "correo"}
    assert type(body["ttl"]) is int


def test_an_option_can_be_given_as_name_equals_value(rig):
    words = ["--title", "a=b", "--body", "x=y", "--kind", "ok", "--ttl", "45.5", "--id", "z", "--source", "s"]
    spaced = body_of(rig.run(*words))
    joined = body_of(rig.run(*["%s=%s" % (words[i], words[i + 1]) for i in range(0, len(words), 2)]))
    assert spaced == joined == {"title": "a=b", "body": "x=y", "kind": "ok", "ttl": 45.5, "id": "z", "source": "s"}


@pytest.mark.parametrize("title", ["-x", "- hecho", "--", "-", "--nope", "-h"])
def test_a_value_may_start_with_a_dash(rig, title):
    assert body_of(rig.run("--title", title)) == {"title": title}


@pytest.mark.parametrize("argv", [["--title", "--wake"], ["--title", "T", "--body", "--kind", "ok"], ["--title"], ["--title", "T", "--id"],
                                  ["--title", "--help"]])
def test_a_forgotten_value_is_not_swallowed_from_the_next_option(rig, argv):
    done = rig.run(*argv)
    assert done.code == 2 and done.calls == [] and done.out == ""
    assert "le falta el valor" in done.err


def test_optional_fields_are_left_out_when_they_are_empty(rig):
    done = rig.run("--title", "T", "--body", " \n \t", "--source", "  ")
    assert body_of(done) == {"title": "T"}
    done = rig.run("--title", "T", "--body", "-", stdin=b"")
    assert body_of(done) == {"title": "T"}


@pytest.mark.parametrize("text, expected", [
    ("  hola \t mundo\n", "hola mundo"),
    ("a\x00b\x07c\x1bd\x7fe\x85f", "a b c d e f"),
    ("uno\r\ndos\ntres", "uno dos tres"),
    ("x" * 100, "x" * 60),
    ("y " * 50, ("y " * 30).strip()),  # cut at 60, and the space the cut leaves at the end goes
    ("\U0001F600" * 70, "\U0001F600" * 60),  # characters, not bytes
    ("Reunion en 10 min \u00f1\u00e1", "Reunion en 10 min \u00f1\u00e1"),
    ("a\udcffb", "a?b"),  # a byte of argv that is not UTF-8
])
def test_a_title_is_cleaned_and_cut_the_way_the_buddy_does(rig, text, expected):
    assert body_of(rig.run("--title", text))["title"] == expected


@pytest.mark.parametrize("text", ["", " ", "\t\n", "\x00\x07", "   \u00a0 "])
def test_a_title_with_nothing_in_it_is_a_usage_error(rig, text):
    done = rig.run("--title", text)
    assert (done.code, done.calls, done.out) == (2, [], "")
    assert "--title esta vacio" in done.err


@pytest.mark.parametrize("text, expected", [
    ("una linea", "una linea"),
    ("a\r\nb\rc", "a\nbc"),  # CR removed, not turned into a break
    ("  a   b  \n\n  c\t\td ", "a b\n\nc d"),
    ("\n\nhola\n\n", "hola"),
    ("1\n2\n3\n4\n5\n6", "1\n2\n3\n4"),
    ("p\x00q\x1b[0m", "p q [0m"),
    ("z" * 500, "z" * 240),
    ("ab\n" * 100, "ab\nab\nab\nab"),
    ("\u00e9" * 300, "\u00e9" * 240),
])
def test_a_body_keeps_its_lines_up_to_four_and_240_characters(rig, text, expected):
    assert body_of(rig.run("--title", "T", "--body", text))["body"] == expected


def test_a_source_is_cleaned_and_cut_to_24(rig):
    assert body_of(rig.run("--title", "T", "--source", " ca\tsa\n"))["source"] == "ca sa"
    assert body_of(rig.run("--title", "T", "--source", "s" * 40))["source"] == "s" * 24


def test_text_that_json_could_not_carry_is_made_safe(rig):
    body = body_of(rig.run("--title", "a\udcffb", "--body", "c\ud800d", "--source", "\udfffs"))
    assert body == {"title": "a?b", "body": "c?d", "source": "?s"}
    notify.encode(body).decode("ascii")  # (a lone surrogate would raise here)


# --- the body from stdin -------------------------------------------------------------------------------------------------

def test_a_dash_body_comes_from_stdin_and_is_cut(rig):
    assert body_of(rig.run("--title", "T", "--body", "-", stdin=b"detalle\nlargo\n"))["body"] == "detalle\nlargo"
    pipe = Pipe(b"x" * 100_000)
    assert body_of(rig.run("--title", "T", "--body", "-", stdin=pipe))["body"] == "x" * 240
    assert pipe.asked == [notify.MAX_STDIN]  # a stream that never ends is read once, and only so far


def test_stdin_is_decoded_leniently(rig):
    body = body_of(rig.run("--title", "T", "--body", "-", stdin="caf\u00e9 \U0001F600".encode("utf-8") + b"\xff\xfe!"))["body"]
    assert body.startswith("caf\u00e9 \U0001F600") and body.endswith("!")


def test_stdin_is_not_touched_unless_the_body_is_a_dash(rig):
    for argv in (["--title", "T"], ["--title", "T", "--body", "texto"], ["--title", "T", "--body", "- no"],
                 ["--status", "abc"], ["--withdraw", "abc"]):
        pipe = Pipe(fail=True)
        assert rig.run(*argv, stdin=pipe).code == 0 and pipe.asked == []
    assert body_of(rig.run("--title", "T", "--body", "- no", stdin=Pipe(fail=True)))["body"] == "- no"


def test_without_a_stdin_the_dash_body_is_empty(rig, monkeypatch):
    buddy = Buddy()
    monkeypatch.setattr(notify, "request", buddy)
    for stream in (None, io.StringIO("")):  # a stdin that was closed; one that is not binary and has nothing
        monkeypatch.setattr(sys, "stdin", stream)
        assert notify.main(["--title", "T", "--body", "-"], rig.env(), None) == 0
    assert [call.body for call in buddy.calls] == [{"title": "T"}] * 2


def test_the_process_stdin_is_the_default(rig, monkeypatch):
    buddy = Buddy()
    monkeypatch.setattr(notify, "request", buddy)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO("desde stdin\n".encode("utf-8"))))
    assert notify.main(["--title", "T", "--body", "-"], rig.env()) == 0
    assert buddy.calls[0].body == {"title": "T", "body": "desde stdin"}


def test_a_stdin_that_fails_is_an_empty_body(rig):
    class Broken:
        def read(self, size=-1):
            raise OSError("ZQXTOKEN")

    done = rig.run("--title", "T", "--body", "-", stdin=Broken())
    assert (done.code, done.calls[0].body) == (0, {"title": "T"})


class Stuck:
    """A stdin that is open and says nothing (or says `first` and then nothing): a read returns only when `release` is set."""

    def __init__(self, first=b""):
        self.first, self.release, self.asked = first, threading.Event(), []

    def read(self, size=-1):
        self.asked.append(size)
        self.release.wait(10)
        return self.first


def test_a_stdin_that_stays_open_is_given_up_on_at_the_total_with_a_fixed_phrase(rig, monkeypatch):
    monkeypatch.setattr(notify, "TOTAL_TIMEOUT_S", 0.4)
    for first in (b"", b"hola"):  # nothing ever written; some text but no end of file
        stuck = Stuck(first)
        started = time.monotonic()
        done = rig.run("--title", TEXT, "--body", "-", stdin=stuck)
        elapsed = time.monotonic() - started
        stuck.release.set()
        assert elapsed < 2.5, elapsed
        assert (done.code, done.out, done.calls) == (1, "", [])  # nothing was sent: there was no text to send
        assert done.err.startswith("notify: la entrada estandar no se cerro a tiempo") and done.err.count("\n") == 1
        assert stuck.asked == [notify.MAX_STDIN]
        clean(done)


def test_stdin_and_the_network_share_one_total(rig, monkeypatch):
    """The 3 seconds are for the whole call: what the wait for stdin took is not given to the network again."""
    class Slow:
        def read(self, size=-1):
            time.sleep(0.3)
            return b"detalle"

    budgets = []
    monkeypatch.setattr(notify, "send", lambda *a: budgets.append(a[6]) or (200, b"{}"))
    started = time.monotonic()
    assert rig.run("--title", "T", "--body", "-", stdin=Slow()).code == 0
    assert 0 < budgets[0] <= notify.TOTAL_TIMEOUT_S - 0.25
    assert budgets[0] + (time.monotonic() - started) <= notify.TOTAL_TIMEOUT_S + 0.1
    budgets.clear()
    assert rig.run("--title", "T", "--body", "texto", stdin=Pipe(fail=True)).code == 0  # no stdin to wait for: nearly all of it is left
    assert notify.TOTAL_TIMEOUT_S - 0.25 < budgets[0] <= notify.TOTAL_TIMEOUT_S


def test_a_stdin_read_that_is_slow_but_ends_in_time_still_delivers(rig, monkeypatch):
    monkeypatch.setattr(notify, "TOTAL_TIMEOUT_S", 2.0)

    class Late:
        def read(self, size=-1):
            time.sleep(0.2)
            return b"llego tarde pero llego"

    assert body_of(rig.run("--title", "T", "--body", "-", stdin=Late()))["body"] == "llego tarde pero llego"


# --- the body is cut the way the buddy cuts it ------------------------------------------------------------------------------

def daemon_body(text):
    return buddy_core._notice_body(text, buddy_core.MAX_NOTICE_BODY_CHARS, buddy_core.MAX_NOTICE_BODY_LINES)


@pytest.mark.parametrize("given, kept", [
    ("uno\n\n\n\ndos\n", "uno\n\ndos"),  # a run of blank lines is one line, and it does not eat what comes after it
    ("a\n\n\nb\nc\nd\n", "a\n\nb\nc"),
    ("l\u00ednea uno\nl\u00ednea dos\n\n\n\nl\u00ednea tres", "l\u00ednea uno\nl\u00ednea dos\n\nl\u00ednea tres"),
    ("Resumen:\n- uno\n\n\nDetalle", "Resumen:\n- uno\n\nDetalle"),
    ("a\n\n\n\n\n\nb", "a\n\nb"),
    ("\n\n a \n \n\t\n b \n\n", "a\n\nb"),  # (a line of blanks is a blank line)
    ("a\r\n\r\n\r\nb\r\nc\r\nd\r\ne", "a\n\nb\nc"),
    ("1\n2\n3\n4\n5", "1\n2\n3\n4"),
])
def test_a_run_of_blank_lines_costs_a_body_one_line_and_never_the_text_after_it(given, kept):
    assert notify.body_text(given) == kept == daemon_body(given)


def test_the_tool_never_cuts_what_the_buddy_would_keep():
    """The buddy cannot give back text the tool left out, so for any text the tool's body and the buddy's agree (the buddy's cleaning
    of what the tool sent is the buddy's cleaning of the text itself). A seeded fuzz over blanks, breaks and long lines."""
    rng = random.Random(21)
    pieces = ["a", "bb", "x" * 30, "y" * 100, "z" * 250, "\n", "\n", "\n\n", "\n \n", " ", "  ", "\t", "\r\n", "\x07", "ñ", "✓"]
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        assert daemon_body(notify.body_text(text)) == daemon_body(text), repr(text)


# --- kind, ttl, id --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["info", "ok", "warn"])
def test_the_three_kinds_pass(rig, kind):
    assert body_of(rig.run("--title", "T", "--kind", kind))["kind"] == kind


@pytest.mark.parametrize("kind", ["", "INFO", "Warn", "error", "ok ", "warning", "ZQXKIND"])
def test_any_other_kind_is_a_usage_error_that_does_not_echo_it(rig, kind):
    done = rig.run("--title", "T", "--kind", kind)
    assert (done.code, done.calls, done.out) == (2, [], "")
    assert "--kind tiene que ser info, ok o warn" in done.err
    clean(done)


@pytest.mark.parametrize("text, expected", [
    ("30", 30), ("300", 300), ("3600", 3600), ("30.0", 30), ("1e3", 1000), ("+7", 7), (" 45 ", 45),
    ("12.5", 12.5), (".5", 0.5), ("2.", 2),
    ("5", 5), ("0", 0), ("-3", -3), ("99999", 99999),  # the buddy clamps to 10..3600: the tool only passes the number on
])
def test_a_ttl_is_a_number_passed_on_as_it_is(rig, text, expected):
    ttl = body_of(rig.run("--title", "T", "--ttl", text))["ttl"]
    assert ttl == expected and type(ttl) is type(expected)


@pytest.mark.parametrize("text", ["", "abc", "nan", "NaN", "inf", "-inf", "infinity", "1e999", "-1e999", "0x10", "1_0", "5s", "1,5",
                                  "--", "ZQXTTL", "1 2", "\u0663", "True"])
def test_a_ttl_that_is_not_a_finite_number_is_a_usage_error(rig, text):
    done = rig.run("--title", "T", "--ttl", text)
    assert (done.code, done.calls, done.out) == (2, [], "")
    assert "--ttl tiene que ser un numero" in done.err
    clean(done)


@pytest.mark.parametrize("ident", ["a", "n-1", "tanda:21.b_c", "A" * 64, "..", "0", "x.y-z_w:1"])
def test_ids_the_buddy_accepts(rig, ident):
    assert body_of(rig.run("--title", "T", "--id", ident))["id"] == ident
    assert rig.run("--status", ident).calls[0].path == "/notice/" + ident
    assert rig.run("--withdraw", ident).calls[0].path == "/notice/" + ident


@pytest.mark.parametrize("ident", ["", "A" * 65, "a b", "a/b", "../x", "\u00e9", "a\n", "\na", "a?x=1", "a#b", "a%2e", "a\x00", "ZQXID/"])
def test_ids_the_buddy_would_refuse_are_usage_errors_in_every_mode(rig, ident):
    for argv in (["--title", "T", "--id", ident], ["--status", ident], ["--withdraw", ident]):
        done = rig.run(*argv)
        assert (done.code, done.calls, done.out) == (2, [], "")
        assert "de 1 a 64 letras, numeros o _ . : -" in done.err
        clean(done)


# --- status and withdraw -------------------------------------------------------------------------------------------------

def test_status_asks_for_one_notice_and_prints_the_state(rig):
    done = rig.run("--status", "tanda-21", buddy=Buddy((200, {"id": "tanda-21", "state": "shown"})))
    (call,) = done.calls
    assert (done.code, call.method, call.path, call.body, call.token) == (0, "GET", "/notice/tanda-21", None, TOKEN)
    assert done.out == '{"id":"tanda-21","state":"shown"}\n' and done.err == ""


def test_withdraw_deletes_it_and_an_unknown_id_is_fine(rig):
    done = rig.run("--withdraw", "tanda-21", buddy=Buddy((200, {"ok": True, "state": "unknown"})))
    (call,) = done.calls
    assert (done.code, call.method, call.path, call.body) == (0, "DELETE", "/notice/tanda-21", None)
    assert json.loads(done.out) == {"ok": True, "state": "unknown"}


def test_the_insecure_flag_goes_with_every_mode(rig):
    for argv in (["--title", "T"], ["--status", "a"], ["--withdraw", "a"]):
        assert rig.run(*argv, "--insecure-url-ok").code == 0


# --- usage ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    [], ["--insecure-url-ok"], ["--wake"],
    ["--title", "T", "--title", "U"], ["--title", "T", "--wake", "--wake"], ["--status", "a", "--status", "b"],
    ["--status"], ["--withdraw"],
    ["--status", "a", "--withdraw", "b"], ["--status", "a", "--title", "T"], ["--withdraw", "a", "--wake"],
    ["--status", "a", "--body", "x"], ["--withdraw", "a", "--ttl", "30"], ["--status", "a", "--kind", "ok"],
    ["--title", "T", "extra"], ["--title", "T", "two", "words"], ["extra"], ["-x"], ["-"], ["--title", "T", "--nope"],
    ["--title", "T", "--nope=1"], ["--title", "T", "--wake=1"], ["--title", "T", "--insecure-url-ok=no"], ["--help=1"],
    ["--TITLE", "T"], ["--tit", "T"], ["--title", "T", "--url", "http://x"], ["--title", "T", "-p"],
])
def test_a_wrong_command_line_exits_2_sends_nothing_and_says_how_to_use_it(rig, argv):
    done = rig.run(*argv)
    assert (done.code, done.calls, done.out) == (2, [], "")
    assert done.err.startswith("notify: ") and done.err.endswith(notify.USAGE)


def test_usage_errors_never_echo_what_was_typed(rig):
    for argv in (["--title", "T", "--ZQXOPT"], ["--title", "T", "ZQXWORD"], ["--ZQXOPT=ZQXTEXT"], ["--title", "T", "--wake=ZQXTEXT"],
                 ["--title", "ZQXTEXT", "ZQXWORD"], ["--status", "ZQXTEXT/", "--withdraw", "x"], ["ZQXTEXT"]):
        done = rig.run(*argv)
        assert done.code == 2 and done.calls == []
        clean(done)


def test_help_prints_the_usage_on_stdout_and_exits_0(rig):
    for flag in ("--help", "-h"):
        done = rig.run(flag, buddy=Buddy(OSError("never called")), env={})
        assert (done.code, done.out, done.err, done.calls) == (0, notify.USAGE, "", [])
    assert "--body -" in notify.USAGE and "--wake" in notify.USAGE and "--withdraw" in notify.USAGE
    assert notify.USAGE.isascii()


# --- the URL the token may go to ----------------------------------------------------------------------------------------

OK_URLS = ["http://buddy:8766", "http://BUDDY:8766", "http://buddy", "http://127.0.0.1:8766", "http://127.9.9.9:1",
           "http://localhost:8766", "http://[::1]:8766", "http://100.101.102.103:8766", "http://100.64.0.1",
           "http://100.127.255.255:1", "http://my-box.tail1234.ts.net:8766", "http://pi:8766/", "http://pi-4:8766"]
REFUSED_URLS = ["http://buddy.local:8766", "http://192.168.1.50:8766", "http://100.128.0.1:8766", "http://100.63.255.255:8766",
                "http://example.com", "http://10.0.0.5:8766", "http://[fe80::1]:8766", "http://[fd7a:115c:a1e0::1]:8766",
                "http://evil.ts.net.example.com", "http://1.1.1.1",
                # a word with no dot that is a number: the resolver turns each of these into an IPv4 address
                "http://3232235777:8766", "http://0xc0a80101:8766", "http://017700000001:8766", "http://134744072", "http://1:8766",
                "http://0x08080808", "http://-buddy:8766", "http://9buddy:8766"]
NOT_HTTP_URLS = ["https://buddy:8766", "ftp://buddy", "buddy:8766", "http://", "", "http://user:pw@buddy:8766", "http://buddy:99999",
                 "http://buddy:0", "//buddy", "http://[::1"]


@pytest.mark.parametrize("url", OK_URLS)
def test_urls_the_token_may_go_to(url):
    assert notify.url_problem(url, False) is None


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_urls_the_token_must_not_go_to(url):
    assert "no mando nada" in notify.url_problem(url, False)
    assert notify.url_problem(url, True) is None  # --insecure-url-ok lifts the rule about the host


@pytest.mark.parametrize("url", NOT_HTTP_URLS)
def test_urls_that_are_not_plain_http_are_never_ok(url):
    assert notify.url_problem(url, False) and notify.url_problem(url, True)


def test_the_url_rule_is_the_censuss():
    for url in OK_URLS + REFUSED_URLS + NOT_HTTP_URLS + ["http://Buddy.TS.NET", "http://[::ffff:127.0.0.1]:1", "http://0.0.0.0"]:
        for insecure in (False, True):
            assert (notify.url_problem(url, insecure) is None) == (census.url_problem(url, insecure) is None), (url, insecure)


@pytest.mark.parametrize("url", REFUSED_URLS + [u for u in NOT_HTTP_URLS if u])  # (an empty variable means the default)
def test_a_refused_url_sends_nothing_and_does_not_even_open_the_token_file(rig, monkeypatch, url):
    monkeypatch.setattr(notify, "read_token", lambda path: pytest.fail("the token file was opened"))
    done = rig.run("--title", "T", env=rig.env(CLAUDE_BUDDY_URL=url))
    assert (done.code, done.calls, done.out) == (1, [], "")
    assert done.err.startswith("notify: la URL del buddy") and "no mando nada" in done.err
    clean(done)


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_the_flag_lets_a_refused_host_through_without_the_address_check(rig, url):
    done = rig.run("--title", "T", "--insecure-url-ok", env=rig.env(CLAUDE_BUDDY_URL=url))
    (call,) = done.calls
    assert done.code == 0 and call.url == url and call.strict is False


@pytest.mark.parametrize("url", [u for u in NOT_HTTP_URLS if u])
def test_the_flag_never_makes_a_url_that_is_not_http_ok(rig, url):
    done = rig.run("--title", "T", "--insecure-url-ok", env=rig.env(CLAUDE_BUDDY_URL=url))
    assert (done.code, done.calls) == (1, [])


def test_the_url_comes_from_the_environment_that_is_passed_in(rig, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDDY_URL", "http://example.com")  # (the process's own is not used when an env is given)
    assert rig.run("--title", "T").calls[0].url == notify.DEFAULT_URL == "http://buddy:8766"
    assert rig.run("--title", "T", env=rig.env(CLAUDE_BUDDY_URL="")).calls[0].url == notify.DEFAULT_URL
    assert rig.run("--title", "T", env=rig.env(CLAUDE_BUDDY_URL="  http://100.101.102.103:8766 \n")).calls[0].url == "http://100.101.102.103:8766"
    monkeypatch.setattr(notify, "request", Buddy())
    assert notify.main(["--title", "T"], None) == 1  # the process's own environment: example.com is refused
    assert "no mando nada" in rig.capsys.readouterr().err


# --- the token -----------------------------------------------------------------------------------------------------------

def test_the_token_rule_is_the_hooks(tmp_path):
    good = tmp_path / "t"
    good.write_text("T" * 32 + "\n", encoding="utf-8")
    for mode in (0o600, 0o400, 0o640, 0o644, 0o660, 0o606, 0o666, 0o700):
        good.chmod(mode)
        assert notify.read_token(str(good)) == context_hook.read_token(str(good)), oct(mode)
    good.chmod(0o600)
    assert notify.read_token(str(good)) == "T" * 32
    for text in ("short", "", " " * 40, "x" * 15 + "\n", "x" * 16, "  " + "y" * 20 + "  \n\n", "a b" * 10):
        good.write_text(text, encoding="utf-8")
        assert notify.read_token(str(good)) == context_hook.read_token(str(good)), repr(text)
    assert notify.read_token(str(tmp_path / "none")) is None and notify.read_token(str(tmp_path)) is None
    link = tmp_path / "link"
    good.write_text("T" * 32, encoding="utf-8")
    link.symlink_to(good)
    assert notify.read_token(str(link)) == context_hook.read_token(str(link))
    (tmp_path / "bin").write_bytes(b"\xff\xfe" * 20)
    (tmp_path / "bin").chmod(0o600)
    assert notify.read_token(str(tmp_path / "bin")) is None  # (the hook raises on bytes that are not UTF-8: the tool just has no token)


def test_the_token_rule_is_the_censuss_and_the_minimum_is_the_same():
    assert notify.MIN_TOKEN_CHARS == context_hook.MIN_TOKEN_CHARS == census.MIN_TOKEN_CHARS == 16
    assert notify.DEFAULT_TOKEN_FILE == context_hook.DEFAULT_TOKEN_FILE == census.DEFAULT_TOKEN_FILE
    assert notify.DEFAULT_URL == context_hook.DEFAULT_URL == census.DEFAULT_URL


def test_a_token_in_the_home_folder_is_the_default(rig, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "claude-buddy").mkdir(parents=True)
    write_token(home / ".config" / "claude-buddy" / "hermes.token", "H" * 32)
    monkeypatch.setenv("HOME", str(home))
    done = rig.run("--title", "T", env={})
    assert (done.code, done.calls[0].token) == (0, "H" * 32)
    done = rig.run("--title", "T", env={"CLAUDE_BUDDY_TOKEN_FILE": ""})
    assert (done.code, done.calls[0].token) == (0, "H" * 32)


@pytest.mark.parametrize("make", ["missing", "readable", "group", "short", "empty", "folder", "binary"])
def test_a_token_that_cannot_be_used_sends_nothing_and_says_so(rig, tmp_path, make):
    path = tmp_path / "other.token"
    if make == "readable":
        write_token(path, mode=0o644)
    elif make == "group":
        write_token(path, mode=0o660)
    elif make == "short":
        write_token(path, "x" * 15)
    elif make == "empty":
        write_token(path, "")
    elif make == "folder":
        path.mkdir()
    elif make == "binary":
        path.write_bytes(b"\xff\xfe" * 20)
        path.chmod(0o600)
    done = rig.run("--title", "T", "--body", TEXT, env={"CLAUDE_BUDDY_TOKEN_FILE": str(path)})
    assert (done.code, done.calls, done.out) == (1, [], "")
    assert done.err == ("notify: no hay un token que se pueda usar: el archivo tiene que ser un archivo tuyo, 0600 y de 16 o mas "
                        "caracteres\n")
    clean(done)


# --- what the buddy answers ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201, 202, 299])
def test_any_2xx_with_a_json_object_is_delivered(rig, status):
    done = rig.run("--title", "T", buddy=Buddy((status, {"ok": True, "id": "n-7", "active": 2})))
    assert (done.code, done.err, json.loads(done.out)) == (0, "", {"ok": True, "id": "n-7", "active": 2})


def test_the_answer_is_printed_as_one_ascii_line_of_json(rig):
    done = rig.run("--title", "T", buddy=Buddy((200, b'{ "ok" : true,\n "id" : "caf\\u00e9", "n": [1, 2], "z": "\xc3\xa9" }')))
    assert done.out == '{"ok":true,"id":"caf\\u00e9","n":[1,2],"z":"\\u00e9"}\n'
    assert done.err == ""


REFUSAL_PHRASES = {
    400: "notify: buddy no acepto el aviso (400): el titulo, el tipo, el ttl o el id no valen\n",
    403: "notify: buddy rechazo el token o el Host (403)\n",
    404: "notify: este buddy no conoce /notice (daemon viejo o sin tablero de avisos)\n",
    413: "notify: buddy dice que el aviso es muy grande (413)\n",
    415: "notify: buddy no acepto el tipo de contenido (415)\n",
    429: "notify: buddy dice que son demasiados avisos (429): esperar un minuto\n",
    401: "notify: buddy contesto 401\n", 405: "notify: buddy contesto 405\n", 500: "notify: buddy contesto 500\n",
    502: "notify: buddy contesto 502\n", 503: "notify: buddy contesto 503\n", 301: "notify: buddy contesto 301\n",
    302: "notify: buddy contesto 302\n", 304: "notify: buddy contesto 304\n",
}


@pytest.mark.parametrize("status", sorted(REFUSAL_PHRASES))
def test_a_4xx_or_5xx_exits_1_with_a_fixed_phrase_and_prints_nothing_on_stdout(rig, status):
    # the answer even carries the token and the text, as a hostile or confused server might: none of it is passed on
    reply = {"ok": False, "error": "%s %s" % (TOKEN, TEXT)}
    for argv in (["--title", TEXT, "--body", TEXT], ["--status", "abc"], ["--withdraw", "abc"]):
        done = rig.run(*argv, buddy=Buddy((status, reply)))
        assert (done.code, done.out, done.err) == (1, "", REFUSAL_PHRASES[status])
        clean(done)


@pytest.mark.parametrize("raw", [b"ok", b"", b"[1]", b'"x"', b"7", b"null", b"{", b"\xff\xfe", b'{"a": NaN}', b'{"a": Infinity}',
                                 b"[" * 5000, b'{"a":"' + b"x" * 9000 + b'"}', b"<html>200</html>"])
def test_a_2xx_that_is_not_a_json_object_is_not_a_delivery(rig, raw):
    done = rig.run("--title", TEXT, buddy=Buddy((200, raw)))
    assert (done.code, done.out) == (1, "")
    assert done.err == "notify: la respuesta del buddy no se entiende (200): no se sabe si llego\n"
    clean(done)


class Weird(Exception):
    pass


@pytest.mark.parametrize("exc, name", [
    (ConnectionRefusedError("%s %s" % (TOKEN, TEXT)), "ConnectionRefusedError"),
    (ConnectionResetError(TOKEN), "ConnectionResetError"),
    (BrokenPipeError(TEXT), "BrokenPipeError"),
    (socket.gaierror(8, TEXT), "gaierror"),
    (TimeoutError(TOKEN), "TimeoutError"),
    (OSError(65, "No route to host " + TEXT), "OSError"),
    (http.client.BadStatusLine(TEXT), "BadStatusLine"),
    (http.client.RemoteDisconnected(TOKEN), "RemoteDisconnected"),
    (http.client.IncompleteRead(b"x"), "IncompleteRead"),
    (http.client.InvalidURL(TEXT), "InvalidURL"),
    (ValueError("Invalid header value 'Bearer %s'" % TOKEN), "ValueError"),
])
def test_a_network_failure_exits_1_with_the_type_name_and_nothing_else(rig, exc, name):
    done = rig.run("--title", TEXT, "--body", TEXT, buddy=Buddy(exc))
    assert (done.code, done.out, done.err) == (1, "", "notify: no se pudo entregar (%s)\n" % name)
    clean(done)


def test_an_address_the_token_must_not_go_to_has_its_own_phrase(rig):
    done = rig.run("--title", TEXT, buddy=Buddy(notify.PeerRefused("peer")))
    assert (done.code, done.out) == (1, "")
    assert done.err.startswith("notify: el nombre del buddy lleva a una direccion que no es de esta maquina ni de la tailnet")
    assert "--insecure-url-ok" in done.err
    clean(done)


def test_a_bug_is_one_phrase_with_its_type_and_exit_1(rig, monkeypatch):
    monkeypatch.setattr(notify, "read_token", lambda path: (_ for _ in ()).throw(Weird("%s %s" % (TOKEN, TEXT))))
    done = rig.run("--title", TEXT)
    assert (done.code, done.out, done.err) == (1, "", "notify: fallo inesperado (Weird)\n")
    clean(done)


def test_ctrl_c_exits_1_politely(rig, monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    done = rig.run("--title", TEXT)
    assert (done.code, done.out, done.err) == (1, "", "notify: interrumpido\n")


def test_nothing_is_retried(rig):
    done = rig.run("--title", "T", buddy=Buddy((403, {}), (200, REPLY)))
    assert done.code == 1 and len(done.calls) == 1  # a wrong token feeds the lockout the hooks share


def test_a_call_that_takes_longer_than_the_total_is_abandoned(rig, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(notify, "TOTAL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(notify, "request", lambda *a: release.wait(10) and (200, b"{}"))
    started = time.monotonic()
    code = notify.main(["--title", TEXT], rig.env(), io.BytesIO())
    release.set()
    out, err = rig.capsys.readouterr()
    assert time.monotonic() - started < 2.5
    assert (code, out, err) == (1, "", "notify: no se pudo entregar (TimeoutError)\n")


# --- the real transport, against a tiny server on loopback ---------------------------------------------------------------

Seen = collections.namedtuple("Seen", "method path headers body")


class Server:
    """Answers each connection with the next scripted reply (the last repeats) and keeps what it was sent.
    A reply is (status, body[, extra headers]) or "hang" (reads the request, never answers), "drip" (answers with a header that
    never ends, a byte every 50 ms) or "close" (reads the request and hangs up)."""

    def __init__(self, *replies):
        self.replies = list(replies) or [(200, REPLY)]
        self.seen = []
        self.connections = 0
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
                reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            threading.Thread(target=self.serve, args=(conn, reply), daemon=True).start()

    def serve(self, conn, reply):
        try:
            conn.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return  # the client left without asking anything (the address check)
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
            while len(rest) < int(headers.get("content-length", "0")):
                rest += conn.recv(65536)
            with self.lock:
                self.seen.append(Seen(method, path, headers, rest))
            self.answer(conn, reply)
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def answer(self, conn, reply):
        if reply == "hang":
            self.done.wait(10)
        elif reply == "drip":
            conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            for _ in range(200):
                if self.done.wait(0.05):
                    return
                conn.sendall(b"x")
        elif reply != "close":
            status, body, *extra = reply
            body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            lines = ["HTTP/1.1 %d X" % status, "Content-Type: application/json", "Content-Length: %d" % len(body),
                     "Connection: close"] + ["%s: %s" % h for h in (extra[0].items() if extra else ())]
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)

    def wait(self, connections=1, requests=0, seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end and (self.connections < connections or len(self.seen) < requests):
            time.sleep(0.01)

    def close(self):
        self.done.set()
        self.sock.close()


@pytest.fixture
def server():
    made = []

    def start(*replies):
        made.append(Server(*replies))
        return made[-1]

    yield start
    for each in made:
        each.close()


def real(rig, url, *argv, stdin=b"", env=None):
    """notify.main with the real transport against `url`."""
    code = notify.main(list(argv), env or rig.env(CLAUDE_BUDDY_URL=url), io.BytesIO(stdin))
    out, err = rig.capsys.readouterr()
    return Done(code, out, err, [])


def test_a_notice_on_the_wire(rig, server):
    buddy = server()
    done = real(rig, buddy.url, "--title", "Reuni\u00f3n en 10 min", "--body", "sala\n\u00f1", "--kind", "warn", "--ttl", "90", "--wake",
                "--id", "reu:1", "--source", "agenda")
    assert (done.code, done.err) == (0, "") and json.loads(done.out) == REPLY
    (seen,) = buddy.seen
    assert (seen.method, seen.path) == ("POST", "/notice")
    h = seen.headers
    assert h["authorization"] == "Bearer " + TOKEN
    assert h["host"] == "127.0.0.1:%d" % buddy.port  # what the listener compares with the address it was reached at
    assert h["content-type"] == "application/json" and h["connection"] == "close" and h["accept"] == "application/json"
    assert int(h["content-length"]) == len(seen.body) <= notify.MAX_POST_BYTES
    seen.body.decode("ascii")
    assert json.loads(seen.body) == {"title": "Reuni\u00f3n en 10 min", "body": "sala\n\u00f1", "kind": "warn", "ttl": 90, "wake": True,
                                     "id": "reu:1", "source": "agenda"}


def test_status_and_withdraw_on_the_wire(rig, server):
    buddy = server((200, {"id": "a:b", "state": "shown"}), (200, {"ok": True, "state": "withdrawn"}))
    assert json.loads(real(rig, buddy.url, "--status", "a:b").out) == {"id": "a:b", "state": "shown"}
    assert json.loads(real(rig, buddy.url, "--withdraw", "a:b").out) == {"ok": True, "state": "withdrawn"}
    get, delete = buddy.seen
    assert (get.method, get.path, get.body) == ("GET", "/notice/a:b", b"")
    assert (delete.method, delete.path, delete.body) == ("DELETE", "/notice/a:b", b"")
    for seen in (get, delete):
        assert seen.headers["authorization"] == "Bearer " + TOKEN and "content-type" not in seen.headers
        assert seen.headers["host"] == "127.0.0.1:%d" % buddy.port


def test_a_notice_of_nothing_but_emoji_still_fits_the_listener(rig, server):
    buddy = server()
    emoji = "\U0001F600"
    done = real(rig, buddy.url, "--title", emoji * 100, "--body", emoji * 300, "--source", emoji * 40, "--id", "i" * 64)
    assert done.code == 0
    (seen,) = buddy.seen
    assert len(seen.body) <= notify.MAX_POST_BYTES
    assert json.loads(seen.body.decode("utf-8")) == {"title": emoji * 60, "body": emoji * 240, "source": emoji * 24, "id": "i" * 64}


def test_the_host_header_is_the_authority_of_the_url_as_typed(rig, server):
    buddy = server()
    url = "http://LOCALHOST:%d" % buddy.port  # (http.client would have sent it in lower case)
    assert real(rig, url, "--title", "T").code == 0
    assert buddy.seen[0].headers["host"] == "LOCALHOST:%d" % buddy.port


def test_a_redirect_is_never_followed(rig, server):
    buddy = server((302, b"", {"Location": "http://127.0.0.1:1/elsewhere"}))
    done = real(rig, buddy.url, "--title", TEXT)
    assert (done.code, done.out, done.err) == (1, "", "notify: buddy contesto 302\n")
    assert len(buddy.seen) == 1


def test_the_reply_travels_through(rig, server):
    for status, reply, code in ((200, REPLY, 0), (403, {"ok": False, "error": "bad token"}, 1), (404, {"ok": False}, 1),
                                (429, b"too many", 1), (200, b"ok", 1)):
        done = real(rig, server((status, reply)).url, "--title", "T")
        assert done.code == code, (status, done)
        clean(done)


@pytest.mark.parametrize("odd", ["ZQXTOKEN\u0141" + "9" * 20, "ZQXTOKEN" + "9" * 10 + "\n" + "9" * 10])
def test_a_token_a_header_cannot_carry_fails_with_a_type_name_only(rig, server, odd):
    buddy = server()
    token_file = write_token(rig.tmp_path / "odd.token", odd)  # (a letter outside latin-1; a line break in the middle)
    done = real(rig, buddy.url, "--title", TEXT, env=rig.env(CLAUDE_BUDDY_URL=buddy.url, CLAUDE_BUDDY_TOKEN_FILE=str(token_file)))
    assert done.code == 1 and done.out == "" and done.err in ("notify: no se pudo entregar (UnicodeEncodeError)\n",
                                                             "notify: no se pudo entregar (ValueError)\n")
    clean(done)
    assert buddy.seen == []


def test_a_buddy_that_never_answers_is_given_up_on_at_the_total(rig, server, monkeypatch):
    monkeypatch.setattr(notify, "TOTAL_TIMEOUT_S", 0.4)
    for script in ("hang", "close"):
        started = time.monotonic()
        done = real(rig, server(script).url, "--title", TEXT)
        assert time.monotonic() - started < 2.5
        assert done.code == 1 and done.out == "" and done.err.startswith("notify: no se pudo entregar (")
        clean(done)


def test_a_buddy_that_trickles_bytes_cannot_hold_the_call_past_the_total(rig, server, monkeypatch):
    """Every byte resets the socket's own timeout; only the bound on the whole call stops this one."""
    monkeypatch.setattr(notify, "TOTAL_TIMEOUT_S", 0.5)
    started = time.monotonic()
    done = real(rig, server("drip").url, "--title", TEXT)
    assert time.monotonic() - started < 2.5
    assert (done.code, done.out, done.err) == (1, "", "notify: no se pudo entregar (TimeoutError)\n")


def test_a_name_lookup_that_never_ends_cannot_hold_the_call(rig, monkeypatch):
    release = threading.Event()

    class Stuck(http.client.HTTPConnection):
        def connect(self):
            release.wait(10)  # getaddrinfo ignores socket timeouts
            raise OSError("never")

    monkeypatch.setattr(notify.http.client, "HTTPConnection", Stuck)
    monkeypatch.setattr(notify, "TOTAL_TIMEOUT_S", 0.3)
    started = time.monotonic()
    done = real(rig, "http://buddy:8766", "--title", TEXT)
    release.set()
    assert time.monotonic() - started < 2.5
    assert (done.code, done.out, done.err) == (1, "", "notify: no se pudo entregar (TimeoutError)\n")


def test_nothing_listening_is_a_connection_error(rig):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    done = real(rig, "http://127.0.0.1:%d" % port, "--title", TEXT)
    assert (done.code, done.out, done.err) == (1, "", "notify: no se pudo entregar (ConnectionRefusedError)\n")


def test_the_token_is_not_written_to_an_address_that_may_not_hear_it(rig, server, monkeypatch):
    buddy = server()
    monkeypatch.setattr(notify, "peer_may_hear_token", lambda address: False)
    done = real(rig, buddy.url, "--title", TEXT)
    buddy.wait(connections=1)
    time.sleep(0.2)
    assert buddy.connections == 1 and buddy.seen == []  # it connected, looked at who answered, and left without a byte
    assert done.code == 1 and done.out == "" and "no le mando el token" in done.err
    clean(done)


def test_the_flag_skips_that_check_for_a_name_the_rule_refused(rig, server, monkeypatch):
    buddy = server()
    try:
        disguised = "2130706433"  # 127.0.0.1 as one number: the text rule refuses it, the resolver understands it
        if socket.getaddrinfo(disguised, buddy.port, socket.AF_INET)[0][4][0] != "127.0.0.1":
            pytest.skip("this resolver does not read a number as an address")
    except socket.gaierror:
        pytest.skip("this resolver does not read a number as an address")
    url = "http://%s:%d" % (disguised, buddy.port)
    refused = real(rig, url, "--title", TEXT)
    assert (refused.code, buddy.seen) == (1, [])
    monkeypatch.setattr(notify, "peer_may_hear_token", lambda address: pytest.fail("the address check ran"))
    done = real(rig, url, "--title", "T", "--insecure-url-ok")
    assert done.code == 0 and len(buddy.seen) == 1
    assert buddy.seen[0].headers["host"] == "%s:%d" % (disguised, buddy.port)


def test_the_flag_lifts_the_address_check_for_a_name_that_passed_the_text_rule_too(rig, server, monkeypatch):
    """The refusal tells the person to pass --insecure-url-ok, so passing it has to be what lets the notice through (the default
    http://buddy:8766 passes the text rule on its own, then the name leads to a LAN address)."""
    buddy = server()
    monkeypatch.setattr(notify, "peer_may_hear_token", lambda address: False)  # (this machine's loopback stands in for the LAN address)
    assert notify.url_problem(buddy.url, False) is None  # the text rule has nothing against it, flag or no flag
    refused_run = real(rig, buddy.url, "--title", TEXT)
    buddy.wait(connections=1)
    time.sleep(0.2)
    assert refused_run.code == 1 and "--insecure-url-ok" in refused_run.err and buddy.seen == []
    done = real(rig, buddy.url, "--title", "T", "--insecure-url-ok")
    assert (done.code, done.err) == (0, "") and json.loads(done.out) == REPLY
    assert len(buddy.seen) == 1 and buddy.seen[0].headers["authorization"] == "Bearer " + TOKEN
    for argv in (["--status", "abc"], ["--withdraw", "abc"]):  # every mode
        assert real(rig, buddy.url, *argv, "--insecure-url-ok").code == 0
    assert real(rig, buddy.url, "--title", TEXT).code == 1 and len(buddy.seen) == 3  # and without it, refused again


@pytest.mark.parametrize("address, may", [
    ("127.0.0.1", True), ("127.9.9.9", True), ("::1", True), ("::ffff:127.0.0.1", True), ("100.64.0.1", True),
    ("100.127.255.255", True), ("100.101.102.103", True), ("fd7a:115c:a1e0::1", True), ("fd7a:115c:a1e0:ab12::9", True),
    ("::ffff:100.100.1.1", True), ("100.128.0.1", False), ("100.63.255.255", False), ("192.168.1.50", False),
    ("10.0.0.1", False), ("8.8.8.8", False), ("0.0.0.0", False), ("::", False), ("fe80::1", False), ("fe80::1%en0", False),
    ("fd7a:115c:a1e1::1", False), ("::ffff:192.168.1.1", False), ("garbage", False), ("", False), (None, False),
])
def test_the_address_rule_is_the_hooks_and_the_censuss(address, may):
    assert notify.peer_may_hear_token(address) is may
    assert context_hook.peer_may_hear_said(address) is may
    assert census.peer_may_hear_token(address) is may


# --- the script itself -----------------------------------------------------------------------------------------------------

def test_it_imports_nothing_outside_the_standard_library_and_nothing_from_the_repo():
    source = NOTIFY.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))  # no match statements, no parenthesized with-items
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"http", "ipaddress", "json", "math", "os", "re", "stat", "sys", "threading", "time", "typing", "urllib"}
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations += [node.returns] + [a.annotation for a in node.args.args + node.args.kwonlyargs]
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    for annotation in filter(None, annotations):
        assert not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(annotation)), ast.dump(annotation)


def process(args, tmp_path, *, stdin=b"", python=None, **env):
    """tools/notify.py as a process, in an environment of its own."""
    child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "LC_ALL": "C"}
    child_env.update(env)
    done = subprocess.run([python or sys.executable, "-I", str(NOTIFY)] + list(args), input=stdin, capture_output=True, env=child_env,
                          timeout=60)
    return Done(done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8"), [])


def test_as_a_process_it_prints_the_answer_and_exits_0(tmp_path, server):
    buddy = server()
    token = write_token(tmp_path / "hermes.token")
    done = process(["--title", "Hecho", "--body", "-", "--kind", "ok", "--wake"], tmp_path, stdin=b"37 de 40\nlisto\n",
                   CLAUDE_BUDDY_URL=buddy.url, CLAUDE_BUDDY_TOKEN_FILE=str(token))
    assert (done.code, done.err, done.out) == (0, "", '{"ok":true,"id":"n-1","active":1}\n')
    (seen,) = buddy.seen
    assert json.loads(seen.body) == {"title": "Hecho", "body": "37 de 40\nlisto", "kind": "ok", "wake": True}
    assert seen.headers["authorization"] == "Bearer " + TOKEN


def test_as_a_process_the_exit_codes_and_the_streams_are_the_contracts(tmp_path, server):
    token = write_token(tmp_path / "hermes.token")
    env = {"CLAUDE_BUDDY_TOKEN_FILE": str(token)}
    usage = process(["--title", "T", "--kind", "ZQXKIND"], tmp_path, **env)
    assert (usage.code, usage.out) == (2, "") and usage.err.startswith("notify: --kind tiene que ser") and "ZQX" not in usage.err
    helped = process(["--help"], tmp_path, **env)
    assert (helped.code, helped.out, helped.err) == (0, notify.USAGE, "")
    refused = process(["--title", "T"], tmp_path, CLAUDE_BUDDY_URL="http://example.com", **env)
    assert (refused.code, refused.out) == (1, "") and "no mando nada" in refused.err
    no_token = process(["--title", "T"], tmp_path, CLAUDE_BUDDY_URL=server().url, CLAUDE_BUDDY_TOKEN_FILE=str(tmp_path / "none"))
    assert (no_token.code, no_token.out) == (1, "") and "no hay un token" in no_token.err
    forbidden = process(["--title", TEXT], tmp_path, CLAUDE_BUDDY_URL=server((403, {"ok": False})).url, **env)
    assert (forbidden.code, forbidden.out, forbidden.err) == (1, "", "notify: buddy rechazo el token o el Host (403)\n")
    missing = process(["--status", "x"], tmp_path, CLAUDE_BUDDY_URL=server((404, {"ok": False})).url, **env)
    assert (missing.code, missing.out) == (1, "") and "no conoce /notice" in missing.err
    for done in (usage, refused, no_token, forbidden, missing):
        assert TOKEN not in done.out + done.err


def test_as_a_process_a_buddy_that_does_not_answer_is_left_after_3_seconds(tmp_path, server):
    token = write_token(tmp_path / "hermes.token")
    started = time.monotonic()
    done = process(["--title", TEXT], tmp_path, CLAUDE_BUDDY_URL=server("drip").url, CLAUDE_BUDDY_TOKEN_FILE=str(token))
    assert 2.5 < time.monotonic() - started < 8
    assert (done.code, done.out, done.err) == (1, "", "notify: no se pudo entregar (TimeoutError)\n")


def test_as_a_process_a_stdin_that_stays_open_does_not_hold_the_call_past_3_seconds(tmp_path, server):
    """`--body -` from a pipe nobody writes to or closes (Hermes' runner that forgot to pipe the text): the same total as everything else."""
    buddy = server()
    token = write_token(tmp_path / "hermes.token")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "LC_ALL": "C", "CLAUDE_BUDDY_URL": buddy.url,
           "CLAUDE_BUDDY_TOKEN_FILE": str(token)}
    started = time.monotonic()
    procs = []
    for first in (b"", b"hola\n"):  # (both at once: nothing written; a few bytes and no end of file)
        proc = subprocess.Popen([sys.executable, "-I", str(NOTIFY), "--title", TEXT, "--body", "-"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if first:
            proc.stdin.write(first)
            proc.stdin.flush()
        procs.append(proc)
    try:
        results = []
        for proc in procs:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail("the tool is still waiting for its stdin")
            results.append((proc.returncode, proc.stdout.read().decode("utf-8"), proc.stderr.read().decode("utf-8")))
    finally:
        for proc in procs:
            proc.kill()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                pipe.close()
    assert 2.5 < time.monotonic() - started < 15
    for code, out, err in results:
        assert (code, out) == (1, "") and err.startswith("notify: la entrada estandar no se cerro a tiempo") and err.count("\n") == 1, err
        assert TOKEN not in err and "ZQX" not in err
    assert buddy.connections == 0  # it never got as far as the network


def test_it_runs_under_the_python_39_that_ships_with_macos(tmp_path, server):
    if not os.path.exists(SYSTEM_PYTHON):
        pytest.skip("no system python")
    version = subprocess.run([SYSTEM_PYTHON, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
                             capture_output=True, text=True).stdout.strip()
    if version != "True":
        pytest.skip("/usr/bin/python3 is not 3.9 here")
    buddy = server()
    token = write_token(tmp_path / "hermes.token")
    done = process(["--title", "Hola \u00f1", "--ttl", "30"], tmp_path, python=SYSTEM_PYTHON, CLAUDE_BUDDY_URL=buddy.url,
                   CLAUDE_BUDDY_TOKEN_FILE=str(token))
    assert (done.code, done.err, json.loads(done.out)) == (0, "", REPLY)
    assert json.loads(buddy.seen[0].body) == {"title": "Hola \u00f1", "ttl": 30}
    assert process(["--bogus"], tmp_path, python=SYSTEM_PYTHON).code == 2


# --- the tool as a process, against the daemon's real listener ---------------------------------------------------------------
#
# Hermes' machine and the Pi meet here. tools/notify.py runs as a process, the way Hermes runs it, against the daemon's real
# HermesServer on loopback, wired as buddy.py wires it (the core's sessions and NoticeBoard, the hub's wake). The core and the hub
# are test_night's rig on a fake clock (23:30 for the night, noon for the day), so a ttl of an hour and the 30 s of a wake cost
# nothing. The listener lives in this test's event loop: every run of the tool goes through an executor.

WRONG = "ZQXWRONG" + "3" * 24  # a token the listener does not know
MARK = "zqxid"  # rides in the id of the leak test: the daemon's answer may echo an id, but no log and no stderr may
ZERO_WIDTH = "\u200b\u202e"  # nothing the tool cleans away and nothing the daemon keeps: the tool sends it, the daemon says 400
SESSION_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}


@contextlib.asynccontextmanager
async def listening(night, **kw):
    """The daemon's real HermesServer on loopback, writing down every request it is sent. `notices=None, wake=None` makes it a
    daemon from before the notices (its /notice is a 404)."""
    kw.setdefault("tasks", night.core.tasks)
    kw.setdefault("notices", night.core.notices)
    kw.setdefault("wake", night.hub.wake)
    async with hermes(night, token=TOKEN, **kw) as server:
        seen = []
        route = server._route

        async def spy(request, reader, writer):
            seen.append(request)
            await route(request, reader, writer)

        server._route = spy
        yield server, seen


class Homelab:
    """Where Hermes lives: tools/notify.py as a process of its own (its own HOME and environment), a token file of its own and
    the address of a listener. Keeps what every run printed."""

    def __init__(self, tmp_path, server, token=TOKEN):
        self.tmp_path, self.server = tmp_path, server
        self.token_file = self.token(token, "hermes.token")
        self.runs = []

    def token(self, text, name):
        return write_token(self.tmp_path / name, text)

    async def notify(self, *argv, stdin=b"", url=None, **env):
        env = dict({"CLAUDE_BUDDY_URL": url or self.server.url, "CLAUDE_BUDDY_TOKEN_FILE": str(self.token_file)}, **env)
        loop = asyncio.get_running_loop()  # the listener runs in this loop: waiting for the tool here would starve it
        done = await loop.run_in_executor(None, functools.partial(process, list(argv), self.tmp_path, stdin=stdin, **env))
        self.runs.append(done)
        return done


def delivered(done):
    """A call the buddy took: exit 0, nothing on stderr, one line of JSON on stdout. Returns the JSON."""
    assert (done.code, done.err) == (0, ""), done
    assert done.out.endswith("\n") and done.out.count("\n") == 1 and done.out.isascii(), done
    return json.loads(done.out)


def refused(done, phrase):
    """A call that did not get through: exit 1, nothing on stdout, the one fixed phrase on stderr."""
    assert (done.code, done.out, done.err) == (1, "", "notify: " + phrase + "\n"), done


def on_screen(night):
    return night.view()["notices"]


def ids(night):
    return [n["id"] for n in on_screen(night)]


def posts(seen):
    return [r for r in seen if (r.method, r.path) == ("POST", "/notice")]


def test_a_notice_sent_by_the_tool_is_on_the_screen_with_every_word_and_setting(tmp_path):
    night = Night(tmp_path, second=at(12))
    title, body = "Reuni\u00f3n en 10 min", "sala 2\n\u00f1and\u00fa \u2713"

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            done = await lab.notify("--title", title, "--body", body, "--kind", "warn",
                                    "--ttl", "90", "--id", "reu:1", "--source", "agenda")
            assert (done.code, done.out, done.err) == (0, '{"ok":true,"id":"reu:1","active":1}\n', "")
            assert on_screen(night) == [{"id": "reu:1", "title": title, "body": body, "kind": "warn",
                                         "source": "agenda", "age": 0, "rev": 1}]
            assert [n.ttl for n in night.core.notices.active()] == [90.0]
            (request,) = seen  # what the listener was sent, as it parsed it
            assert (request.method, request.path) == ("POST", "/notice")
            h = request.headers
            assert h["authorization"] == "Bearer " + TOKEN and h["content-type"] == "application/json"
            assert h["host"] == "127.0.0.1:%d" % server.port and "origin" not in h
            assert len(request.body) <= notify.MAX_POST_BYTES
            request.body.decode("ascii")  # (what is not ASCII travels escaped)
            assert json.loads(request.body) == {"title": title, "body": body, "kind": "warn", "ttl": 90,
                                                "id": "reu:1", "source": "agenda"}

    asyncio.run(scenario())


def test_a_title_alone_gets_the_daemons_defaults_and_the_daemon_numbers_the_notices(tmp_path):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            assert delivered(await lab.notify("--title", "La tanda termino")) == {"ok": True, "id": "n-1", "active": 1}
            assert delivered(await lab.notify("--title", "Otra")) == {"ok": True, "id": "n-2", "active": 2}
            assert on_screen(night) == [
                {"id": "n-2", "title": "Otra", "kind": "info", "source": "hermes", "age": 0, "rev": 1},
                {"id": "n-1", "title": "La tanda termino", "kind": "info", "source": "hermes", "age": 0, "rev": 1},
            ]
            assert [n.ttl for n in night.core.notices.active()] == [300.0, 300.0]
            assert [json.loads(r.body) for r in posts(seen)] == [{"title": "La tanda termino"}, {"title": "Otra"}]  # nothing else was sent

    asyncio.run(scenario())


def test_the_same_id_replaces_the_notice_and_its_rev_goes_up_only_when_the_words_change(tmp_path):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)

            async def send(*argv, ttl="60"):
                return delivered(await lab.notify("--id", "tanda", "--ttl", ttl, *argv))

            async def state(ident="tanda"):
                return delivered(await lab.notify("--status", ident))["state"]

            assert await send("--title", "La tanda corre", "--body", "12 de 40") == {"ok": True, "id": "tanda", "active": 1}
            assert on_screen(night) == [{"id": "tanda", "title": "La tanda corre", "body": "12 de 40", "kind": "info", "source": "hermes",
                                         "age": 0, "rev": 1}]
            night.advance(45)
            assert on_screen(night)[0]["age"] == 45

            # the same words again (only spaces differ, which both ends clean away), a new source: a re-send, not a second notice
            assert (await send("--title", "La  tanda corre ", "--body", "12 de 40", "--source", "otro"))["active"] == 1
            (note,) = on_screen(night)
            assert (note["age"], note["rev"], note["source"]) == (0, 1, "otro")  # the clock restarted, nothing changed
            night.advance(30)  # 75 s after the first send, past its ttl of 60; the re-send is the one that counts
            assert await state() == "shown"

            await send("--title", "La tanda sigue", "--body", "12 de 40")
            assert on_screen(night)[0]["rev"] == 2  # the title
            await send("--title", "La tanda sigue", "--body", "13 de 40")
            assert on_screen(night)[0]["rev"] == 3  # the body
            await send("--title", "La tanda sigue", "--body", "13 de 40", "--kind", "ok")
            assert on_screen(night)[0]["rev"] == 4  # the kind
            await send("--title", "La tanda sigue", "--body", "13 de 40", "--kind", "ok", "--wake", "--source", "mas", ttl="120")
            (note,) = on_screen(night)
            assert (note["rev"], note["kind"], note["source"]) == (4, "ok", "mas")  # ttl, wake and source are not words

            # a re-send goes first, whatever came after the notice
            assert delivered(await lab.notify("--title", "Otra cosa", "--id", "otra")) == {"ok": True, "id": "otra", "active": 2}
            assert ids(night) == ["otra", "tanda"]
            await send("--title", "La tanda sigue", "--body", "13 de 40", "--kind", "ok")  # (ttl 60 again)
            assert ids(night) == ["tanda", "otra"] and on_screen(night)[0]["rev"] == 4

            night.advance(59)  # the ttl counts from the last send
            assert await state() == "shown"
            night.advance(2)
            assert await state() == "expired" and ids(night) == ["otra"]

    asyncio.run(scenario())


def test_status_tells_how_a_notice_stands_through_every_way_it_can_end(tmp_path):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)

            async def state(ident):
                answer = delivered(await lab.notify("--status", ident))
                assert answer["id"] == ident and set(answer) == {"id", "state"}
                return answer["state"]

            async def send(ident, *argv):
                delivered(await lab.notify("--title", "aviso " + ident, "--id", ident, *argv))

            assert await state("nunca") == "unknown"

            await send("listo")
            assert await state("listo") == "shown"
            assert night.hub.dismiss("listo") == "dismissed"  # the tap on "Listo" (the page's POST /dismiss lands here)
            assert await state("listo") == "dismissed" and ids(night) == []

            await send("vence", "--ttl", "5")  # (the daemon holds a ttl to 10 s at least)
            night.advance(9.5)
            assert await state("vence") == "shown"
            night.advance(1)
            assert await state("vence") == "expired"

            await send("retirado")
            assert delivered(await lab.notify("--withdraw", "retirado")) == {"ok": True, "state": "withdrawn"}
            assert await state("retirado") == "withdrawn" and ids(night) == []

            for ident in ("sobra", "a", "b", "c"):  # three at a time: the fourth pushes the oldest off the screen
                await send(ident)
            assert await state("sobra") == "evicted" and ids(night) == ["c", "b", "a"]

            night.advance(3601)  # the daemon remembers how a notice ended for an hour; the three on the screen end by themselves now
            assert [await state(i) for i in ("listo", "vence", "retirado", "sobra")] == ["unknown"] * 4
            assert [await state(i) for i in ("a", "b", "c")] == ["expired"] * 3 and ids(night) == []

    asyncio.run(scenario())


def test_withdraw_takes_the_notice_off_the_screen_and_is_harmless_when_there_is_nothing_to_take(tmp_path):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            await lab.notify("--title", "uno", "--id", "uno")
            await lab.notify("--title", "dos", "--id", "dos")
            assert ids(night) == ["dos", "uno"]
            assert delivered(await lab.notify("--withdraw", "uno")) == {"ok": True, "state": "withdrawn"}
            assert ids(night) == ["dos"]  # only that one
            assert delivered(await lab.notify("--withdraw", "uno")) == {"ok": True, "state": "withdrawn"}  # again: the same answer
            assert delivered(await lab.notify("--withdraw", "jamas")) == {"ok": True, "state": "unknown"}  # an id nobody sent: exit 0 too
            night.hub.dismiss("dos")  # a notice that already ended keeps the way it ended
            assert delivered(await lab.notify("--withdraw", "dos")) == {"ok": True, "state": "dismissed"}
            assert [(r.method, r.path) for r in seen] == [("POST", "/notice")] * 2 + [("DELETE", "/notice/" + i)
                                                                                         for i in ("uno", "uno", "jamas", "dos")]

    asyncio.run(scenario())


def test_a_notice_sent_with_wake_lights_the_dim_night_screen_for_thirty_seconds(tmp_path):
    night = Night(tmp_path, second=at(23, 30))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            assert night.dim() is True and night.view()["night"] is True  # (dark before, or the test proves nothing)
            delivered(await lab.notify("--title", "La tanda termino", "--id", "tanda", "--wake"))
            assert night.dim() is False and ids(night) == ["tanda"]
            assert json.loads(posts(seen)[0].body)["wake"] is True  # asked for on the wire, not guessed
            night.advance(AWAKE_S - 0.5)
            assert night.dim() is False
            night.advance(1)
            assert night.dim() is True and ids(night) == ["tanda"]  # asleep again; the notice waits for the next time it wakes

    asyncio.run(scenario())


def test_a_notice_sent_without_wake_leaves_the_dim_night_screen_dark_until_somebody_wakes_it(tmp_path):
    night = Night(tmp_path, second=at(23, 30))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            delivered(await lab.notify("--title", "Ana escribio", "--id", "juan"))
            night.advance(5)
            assert night.dim() is True and ids(night) == ["juan"]
            delivered(await lab.notify("--title", "Ana escribio otra vez", "--id", "juan", "--kind", "warn"))  # a re-send does not wake either
            night.advance(5)
            assert night.dim() is True and on_screen(night)[0]["rev"] == 2
            assert all("wake" not in json.loads(r.body) for r in posts(seen))  # no --wake, no key
            night.hub.wake()  # a tap
            assert night.dim() is False and ids(night) == ["juan"]  # and there it is
            night.advance(AWAKE_S + 1)
            assert night.dim() is True

    asyncio.run(scenario())


def test_by_day_wake_changes_nothing_you_can_see_and_the_notice_is_there(tmp_path):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            before = {k: v for k, v in night.view().items() if k != "notices"}
            delivered(await lab.notify("--title", "Reunion en 10 min", "--id", "reu", "--wake"))
            assert {k: v for k, v in night.view().items() if k != "notices"} == before
            assert night.view()["night"] is False and night.dim() is False and ids(night) == ["reu"]
            assert json.loads(posts(seen)[0].body)["wake"] is True  # it did ask

    asyncio.run(scenario())


def test_a_daemon_that_does_not_know_notices_is_exit_1_for_every_command(tmp_path):
    night = Night(tmp_path, second=at(23, 30))
    phrase = "este buddy no conoce /notice (daemon viejo o sin tablero de avisos)"

    async def scenario():
        async with listening(night, notices=None, wake=None) as (server, seen):  # what a Pi that was not redeployed runs
            lab = Homelab(tmp_path, server)
            assert notify.REFUSALS[404] == phrase
            for argv in (["--title", TEXT], ["--title", TEXT, "--wake"], ["--status", "tanda"], ["--withdraw", "tanda"]):
                refused(await lab.notify(*argv), phrase)
            assert [(r.method, r.path) for r in seen] == [("POST", "/notice")] * 2 + [("GET", "/notice/tanda"), ("DELETE", "/notice/tanda")]
            assert on_screen(night) == [] and night.dim() is True
            status, _, _ = await raw_http(server, "POST", "/session", headers=SESSION_HEADERS, body=b'{"id": "s1"}')
            assert status == 200  # and it is the same daemon that still takes what it always took

    asyncio.run(scenario())


def test_a_wrong_token_is_a_403_that_changes_nothing_and_enough_of_them_lock_the_hooks_out_as_well(tmp_path):
    night = Night(tmp_path, second=at(23, 30))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server, token=WRONG)
            right = lab.token(TOKEN, "right.token")
            for _ in range(buddy_hermes.AUTH_FAIL_MAX):
                refused(await lab.notify("--title", TEXT, "--body", TEXT, "--wake"), notify.REFUSALS[403])
            assert [(r.method, r.path) for r in seen] == [("POST", "/notice")] * buddy_hermes.AUTH_FAIL_MAX
            assert on_screen(night) == [] and night.dim() is True  # nothing was shown and nothing woke
            # the counter is the listener's, shared by every route: the next call is refused even with the right token...
            refused(await lab.notify("--title", "uno", CLAUDE_BUDDY_TOKEN_FILE=str(right)), notify.REFUSALS[429])
            assert on_screen(night) == []
            # ...and so are the hooks of the Mac, which is why the tool never retries a 403
            status, _, _ = await raw_http(server, "POST", "/session", headers=SESSION_HEADERS, body=b'{"id": "s1"}')
            assert status == 429

    asyncio.run(scenario())


def test_a_name_the_listener_was_never_told_is_a_403_and_works_once_it_is(tmp_path):
    night = Night(tmp_path, second=at(12))
    try:
        if "127.0.0.1" not in {a[4][0] for a in socket.getaddrinfo("localhost", None, socket.AF_INET)}:
            pytest.skip("localhost is not 127.0.0.1 here")
    except socket.gaierror:
        pytest.skip("no localhost here")

    async def scenario():
        async with listening(night) as (server, seen):  # it answers to 127.0.0.1 and to the hosts it was started with: none
            lab = Homelab(tmp_path, server)
            refused(await lab.notify("--title", "uno", url="http://localhost:%d" % server.port), notify.REFUSALS[403])
            assert on_screen(night) == []
            assert delivered(await lab.notify("--title", "uno")) == {"ok": True, "id": "n-1", "active": 1}  # the address it listens on
        async with listening(night, hosts=["localhost"]) as (server, seen):  # buddy.py gives it the machine's own name
            lab = Homelab(tmp_path, server)
            assert delivered(await lab.notify("--title", "dos", url="http://localhost:%d" % server.port))["active"] == 2
            assert seen[0].headers["host"] == "localhost:%d" % server.port  # the Host is the URL as it was typed
            assert ids(night) == ["n-2", "n-1"]

    asyncio.run(scenario())


def test_the_notices_and_the_hooks_have_budgets_of_their_own_in_both_directions(tmp_path):
    night = Night(tmp_path, second=at(12))
    notices, hooks = buddy_hermes.NOTICE_RATE_MAX, buddy_hermes.NOTICE_RATE_MAX + 10  # (the hooks' budget is ten times the notices')
    assert hooks < buddy_hermes.RATE_MAX

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            for i in range(hooks):  # a busy hour of hooks: it does not use up what Hermes may say
                status, _, _ = await raw_http(server, "POST", "/session", headers=SESSION_HEADERS, body=json.dumps({"id": "s%d" % i}).encode())
                assert status == 200
            assert delivered(await lab.notify("--title", "uno")) == {"ok": True, "id": "n-1", "active": 1}
            for i in range(notices - 1):  # the rest of the listener's budget for this client, spent without 30 processes
                status, _, _ = await raw_http(server, "POST", "/notice", headers=SESSION_HEADERS, body=json.dumps({"title": "n%d" % i}).encode())
                assert status == 200
            for argv in (["--title", "uno de mas"], ["--status", "n-1"], ["--withdraw", "n-1"]):  # all three routes spend the same budget
                refused(await lab.notify(*argv), notify.REFUSALS[429])
            assert len(ids(night)) == 3 and night.core.notices.state("n-1") == "evicted"  # nothing the tool said got through
            status, _, _ = await raw_http(server, "POST", "/session", headers=SESSION_HEADERS, body=b'{"id": "s1"}')
            assert status == 200  # and the hooks still have theirs

    asyncio.run(scenario())


@pytest.mark.parametrize("stdin, body", [
    (b"37 de 40 pasaron\nlisto\n", "37 de 40 pasaron\nlisto"),
    (b"uno\r\ndos\r\ntres\r\ncuatro\r\ncinco\r\nseis\r\n", "uno\ndos\ntres\ncuatro"),  # four lines, and CRLF is one break
    (b"\n\n  con  aire \t adentro \n\nfin\n\n", "con aire adentro\n\nfin"),  # no blank line at either end
    ((b"x" * 100 + b"\n") * 5, "x" * 100 + "\n" + "x" * 100 + "\n" + "x" * 38),  # 240 in all, breaks included
    (b"x" * 20000, "x" * 240),  # more than the tool reads (16 KiB): it takes its share and goes
    (b"\xff\xfe termino", None),  # (a stdin that is not UTF-8 still sends something; the text is checked below)
    (b"   \n\n \t ", ""),
    (b"", ""),
])
def test_a_body_from_stdin_reaches_the_screen_cut_to_four_lines_and_240_characters(tmp_path, stdin, body):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            done = await lab.notify("--title", "Aviso", "--body", "-", "--id", "e", stdin=stdin)
            assert delivered(done) == {"ok": True, "id": "e", "active": 1}
            (note,) = on_screen(night)
            if body is None:
                assert note["body"].endswith(" termino") and notify.CONTROL.search(note["body"]) is None
            elif body:
                assert note["body"] == body
            else:
                assert "body" not in note  # nothing left: no empty body on the wire and none on the screen
                assert "body" not in json.loads(posts(seen)[0].body)
            assert len(posts(seen)[0].body) <= notify.MAX_POST_BYTES

    asyncio.run(scenario())


@pytest.mark.parametrize("stdin, kept", [
    (b"uno\n\n\n\ndos\n", "uno\n\ndos"),
    (b"a\n\n\nb\nc\nd\n", "a\n\nb\nc"),
])
def test_a_run_of_blank_lines_in_a_body_costs_it_one_line_and_not_the_text_after_it(tmp_path, stdin, kept):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            delivered(await lab.notify("--title", "Aviso", "--body", "-", stdin=stdin))
            assert on_screen(night)[0]["body"] == kept
            raw = json.dumps({"title": "Aviso", "body": stdin.decode()}).encode()  # and it is what the same text posted as it is comes to
            status, _, _ = await raw_http(server, "POST", "/notice", headers=SESSION_HEADERS, body=raw)
            assert status == 200 and on_screen(night)[0]["body"] == kept

    asyncio.run(scenario())


def test_a_body_given_on_the_command_line_does_not_touch_stdin(tmp_path):
    night = Night(tmp_path, second=at(12))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            delivered(await lab.notify("--title", "Aviso", "--body", "del argumento", "--id", "e", stdin=b"de la entrada"))
            assert on_screen(night)[0]["body"] == "del argumento"

    asyncio.run(scenario())


def test_the_tool_passes_a_ttl_on_as_it_is_and_the_daemon_clamps_it(tmp_path):
    night = Night(tmp_path, second=at(12))
    cases = [(None, 300.0), ("5", 10.0), ("0", 10.0), ("-3", 10.0), ("10", 10.0), ("45.5", 45.5), ("3600", 3600.0), ("99999", 3600.0)]

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            for given, kept in cases:
                delivered(await lab.notify("--title", "t", "--id", "a", *([] if given is None else ["--ttl", given])))
                assert [n.ttl for n in night.core.notices.active()] == [kept], given
                assert json.loads(posts(seen)[-1].body).get("ttl") == (None if given is None else float(given)), given  # sent unclamped

    asyncio.run(scenario())


def test_text_of_nothing_but_emoji_still_gets_through_the_real_listener(tmp_path):
    night = Night(tmp_path, second=at(12))
    emoji = "\U0001F600"

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            done = await lab.notify("--title", emoji * 100, "--body", emoji * 300, "--source", emoji * 40, "--id", "i" * 64, "--kind", "warn")
            assert delivered(done) == {"ok": True, "id": "i" * 64, "active": 1}
            (request,) = seen
            assert len(request.body) <= notify.MAX_POST_BYTES < MAX_BODY_BYTES  # the worst case the tool can build fits
            (note,) = on_screen(night)
            assert (note["title"], note["body"], note["source"]) == (emoji * 60, emoji * 240, emoji * 24)

    asyncio.run(scenario())


def test_emoji_made_of_several_parts_reach_the_screen_whole(tmp_path):
    night = Night(tmp_path, second=at(12))
    sequences = ["\U0001F469\u200d\U0001F4BB", "\U0001F3F3\ufe0f\u200d\U0001F308", "\u2764\ufe0f\u200d\U0001F525"]  # a woman technologist, the rainbow flag, a heart on fire

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            for number, emoji in enumerate(sequences):
                delivered(await lab.notify("--title", "Listo %s" % emoji, "--body", "-", "--id", "e%d" % number, stdin=("arriba %s\nabajo" % emoji).encode()))
            assert [(n["title"], n["body"]) for n in reversed(on_screen(night))] == [
                ("Listo %s" % emoji, "arriba %s\nabajo" % emoji) for emoji in sequences]

    asyncio.run(scenario())


def test_a_title_the_tool_lets_through_and_the_daemon_empties_is_a_400_that_changes_nothing(tmp_path):
    night = Night(tmp_path, second=at(23, 30))

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            delivered(await lab.notify("--title", "uno", "--id", "uno"))
            refused(await lab.notify("--title", ZERO_WIDTH, "--body", TEXT, "--id", "uno", "--wake"), notify.REFUSALS[400])
            refused(await lab.notify("--title", ZERO_WIDTH, "--wake"), notify.REFUSALS[400])
            assert [(n["id"], n["title"], n["rev"]) for n in on_screen(night)] == [("uno", "uno", 1)] and night.dim() is True
            assert len(posts(seen)) == 3  # all three reached the listener

    asyncio.run(scenario())


def test_neither_the_token_nor_the_text_is_in_the_daemons_logs_or_in_what_the_tool_prints(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)  # every logger of the daemon, the debug lines too
    night = Night(tmp_path, second=at(23, 30))
    ident = "id-" + MARK + ":1"

    async def scenario():
        async with listening(night) as (server, seen):
            lab = Homelab(tmp_path, server)
            wrong = lab.token(WRONG, "wrong.token")
            delivered(await lab.notify("--title", TEXT + " titulo", "--body", "-", "--kind", "warn", "--wake", "--id", ident,
                                       "--source", TEXT + "src", stdin=(TEXT + " uno\ndos\n").encode()))
            (note,) = on_screen(night)
            assert (note["title"], note["body"], note["source"]) == (TEXT + " titulo", TEXT + " uno\ndos", TEXT + "src")  # it did get it all
            delivered(await lab.notify("--title", TEXT + " otro", "--body", TEXT, "--id", ident))
            assert delivered(await lab.notify("--status", ident)) == {"id": ident, "state": "shown"}
            refused(await lab.notify("--title", ZERO_WIDTH, "--body", TEXT, "--source", TEXT, "--id", ident), notify.REFUSALS[400])
            refused(await lab.notify("--title", TEXT, "--body", TEXT, "--id", ident, "--wake", CLAUDE_BUDDY_TOKEN_FILE=str(wrong)),
                    notify.REFUSALS[403])
            assert [n["title"] for n in on_screen(night)] == [TEXT + " otro"]
            assert delivered(await lab.notify("--withdraw", ident)) == {"ok": True, "state": "withdrawn"}
            assert delivered(await lab.notify("--status", ident)) == {"id": ident, "state": "withdrawn"}
            return lab.runs

    runs = asyncio.run(scenario())
    assert len(runs) == 7
    for line in ("aviso: warn, 1 activos", "aviso: info, 1 activos", "aviso: retirado, 0 activos", "bad token"):
        assert line in caplog.text, line  # (the daemon did log: this is not an empty log)
    for secret in (TOKEN, WRONG, TEXT, MARK):
        assert secret not in caplog.text, secret
    for done in runs:
        shown = done.out + done.err
        assert shown.isascii() and MARK not in done.err, done
        for secret in (TOKEN, WRONG, TEXT):
            assert secret not in shown, (secret, done)
