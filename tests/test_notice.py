"""Notices from Hermes (POST /notice): the board, the listener's routes, the screen's POST /dismiss, the view's `notices`
and how a notice wakes the dim night screen. The page's half (the "Avisos" group, the detail, "Listo") is in
test_page_notice.py and the command-line tool's in test_notify.py.

The board and the routes run on the fake clock of the core, so a ttl of an hour costs nothing. The wiring in buddy.py
runs the real run_daemon in this process (a stand-in for the radio, a temporary directory laid out like the backlight)
with a real Hermes listener on loopback. A marker planted in the text of the notices must never reach a log."""
import asyncio
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy  # noqa: E402
import buddy_hermes  # noqa: E402
import buddy_ui  # noqa: E402
from buddy_core import (MAX_NOTICES, NOTICE_HISTORY, NOTICE_HISTORY_S, NOTICE_STATES, NOTICE_TTL_DEFAULT_S,  # noqa: E402
                        NOTICE_TTL_MAX_S, NOTICE_TTL_MIN_S, BuddyCore, NoticeBoard, encode_line)
from buddy_ui import AWAKE_S, DISMISS_RATE_MAX, DISMISS_RATE_WINDOW_S, WAKE_RATE_MAX, UIServer  # noqa: E402
from test_hermes import TOKEN, auth, error_of, hermes, report, screen  # noqa: E402
from test_night import FakeTransport, Night, at, daemon_args, level, make_panel, wait_until  # noqa: E402
from test_ui import PROMPT, Rig, heartbeat, run, serving  # noqa: E402
from ui_client import Stream, http  # noqa: E402

MARKER = "MARCA-SECRETA-4f9a"


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


@pytest.fixture
def night(tmp_path):
    return Night(tmp_path, second=at(23, 30))  # night, and nobody has touched the screen


def board_at(start=1000.0):
    """A board on a clock the test moves (`t[0] += 5`)."""
    t = [start]
    return NoticeBoard(clock=lambda: t[0]), t


# --- the board ---------------------------------------------------------------------------------


def test_the_limits_are_the_ones_of_the_contract():
    assert (NOTICE_TTL_MIN_S, NOTICE_TTL_MAX_S, NOTICE_TTL_DEFAULT_S) == (10.0, 3600.0, 300.0)
    assert (MAX_NOTICES, NOTICE_HISTORY, NOTICE_HISTORY_S, buddy_hermes.NOTICE_RATE_MAX) == (3, 50, 3600.0, 30)
    assert (DISMISS_RATE_MAX, DISMISS_RATE_WINDOW_S) == (WAKE_RATE_MAX, 10.0)  # like /wake's
    assert NOTICE_STATES == ("shown", "dismissed", "expired", "withdrawn", "evicted", "unknown")


def test_a_notice_keeps_what_it_was_given_and_the_defaults_fill_the_rest():
    board, t = board_at()
    n = board.add(title="La tanda terminó")
    assert (n.id, n.title, n.body, n.kind, n.source, n.ttl, n.rev, n.at) == (
        "n-1", "La tanda terminó", "", "info", "hermes", 300.0, 1, 1000.0)
    n = board.add(title="Ana escribió", body="Dice que mañana", kind="warn", ttl=120, source="discord", notice_id="ana:1")
    assert (n.id, n.title, n.body, n.kind, n.source, n.ttl) == ("ana:1", "Ana escribió", "Dice que mañana", "warn",
                                                              "discord", 120.0)
    assert board.state("n-1") == board.state("ana:1") == "shown"


def test_the_board_lists_the_newest_first_and_ages_in_whole_seconds():
    board, t = board_at()
    board.add(title="a")
    t[0] += 5
    board.add(title="b")
    t[0] += 5.9
    board.add(title="c")
    t[0] += 2.9
    got = board.active()
    assert [n.title for n in got] == ["c", "b", "a"]
    assert [board.age(n) for n in got] == [2, 8, 13]  # whole seconds, never rounded up


@pytest.mark.parametrize("given, kept", [
    ("  hola \n\t mundo \x07 x  ", "hola mundo x"),
    ("a\x00b", "a b"),
    ("<img src=x onerror=alert(1)>", "<img src=x onerror=alert(1)>"),  # text is text: the page writes it with textContent
    ("ñandú ✓ 🎉", "ñandú ✓ 🎉"),
    ("‮evil", "evil"),  # a right-to-left override is a control character
    ("x​y", "x y"),  # and so is a zero-width space
    ("a 　b", "a b"),
    ("a" * 100, "a" * 60),
    ("a" * 59 + " b", "a" * 59),  # cut, and the cut does not leave a space at the end
    ("a" * 60, "a" * 60),
    ("\ud800 lone surrogate", "lone surrogate"),
])
def test_a_title_is_one_clean_line_of_at_most_60_characters(given, kept):
    board, _ = board_at()
    assert board.add(title=given).title == kept


TECHNOLOGIST = "\U0001F469\u200d\U0001F4BB"  # a woman and a laptop joined by a zero width joiner
RAINBOW_FLAG = "\U0001F3F3\ufe0f\u200d\U0001F308"
HEART_ON_FIRE = "\u2764\ufe0f\u200d\U0001F525"


@pytest.mark.parametrize("given, kept", [
    (TECHNOLOGIST + " listo", TECHNOLOGIST + " listo"),  # an emoji made of several parts stays one emoji
    ("a " + RAINBOW_FLAG + " b", "a " + RAINBOW_FLAG + " b"),
    (HEART_ON_FIRE, HEART_ON_FIRE),
    ("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645", "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"),  # (a Persian word, which is written with a non-joiner)
    ("a\u200d", "a"),  # a joiner with nothing after it is nothing
    ("\u200da", "a"),  # nor one with nothing before it
    ("a \u200d b", "a b"),  # nor one next to a space
    ("a\u200d\u200db", "a b"),  # nor two together
    ("a\u200d\n", "a"),
    ("x\u200b\u200dy", "x y"),  # the joiner needs a visible character on both sides, and a zero width space is not one
    ("\U0001F469\u200d\u202e\U0001F4BB", "\U0001F469 \U0001F4BB"),  # nor a bidi override in between: the override is gone, so is the glue
    ("\u202ea\u200db", "a\u200db"),  # (the override goes, the joiner between letters stays)
])
def test_a_joiner_between_two_visible_characters_stays_and_everything_else_unprintable_is_a_space(given, kept):
    board, _ = board_at()
    assert board.add(title=given, body=given).title == kept
    assert board.add(title="t", body=given).body == kept


def test_a_cut_never_leaves_a_joiner_hanging_at_the_end():
    board, _ = board_at()
    note = board.add(title="a" * 59 + "\u200d" + "b", body="x" * 239 + "\u200d" + "y")
    assert (note.title, note.body) == ("a" * 59, "x" * 239)  # (the 60th and the 240th characters are the joiners)
    note = board.add(title="x" + TECHNOLOGIST * 30)  # the cut at 60 falls right after the joiner of the 20th woman
    assert note.title == "x" + TECHNOLOGIST * 19 + "\U0001F469"


@pytest.mark.parametrize("empty", ["", "   ", "\n\t\x07", "​‮", "\x00", None, 7, ["a"], {"a": 1}, True])
def test_a_title_with_nothing_left_is_refused(empty):
    board, _ = board_at()
    with pytest.raises(ValueError):
        board.add(title=empty)
    assert board.active() == []


@pytest.mark.parametrize("given, kept", [
    ("una\r\ndos\rtres\ncuatro\ncinco\nseis", "una\ndos\ntres\ncuatro"),  # four lines, and CR and CRLF are one break
    ("\n\n a \n\n\n\n b \n\n", "a\n\nb"),  # no blank line at the ends and never two in a row
    ("a   b\t\tc\x07d", "a b c d"),
    ("x" * 500, "x" * 240),
    ("x" * 100 + "\n" + "x" * 100 + "\n" + "x" * 100, "x" * 100 + "\n" + "x" * 100 + "\n" + "x" * 38),  # 240 in all, breaks included
    ("x" * 239 + " y", "x" * 239),
    ("", ""),
    (" \n \r\n ", ""),
    ("‮<b>hi</b>\n​", "<b>hi</b>"),
    (None, ""),  # the board is lenient about what is not text; the listener is the one that says 400
    (7, ""),
])
def test_a_body_keeps_its_line_breaks_up_to_four_lines_and_240_characters(given, kept):
    board, _ = board_at()
    assert board.add(title="t", body=given).body == kept


def test_the_source_is_a_clean_short_name_that_defaults_to_hermes():
    board, _ = board_at()
    for given, kept in (("  hermes   prod  ", "hermes prod"), ("", "hermes"), ("\x07 \n", "hermes"), (None, "hermes"),
                        ("s" * 50, "s" * 24), ("Ana ✓", "Ana ✓")):
        assert board.add(title="t", source=given).source == kept, given


def test_a_kind_or_an_id_that_is_not_allowed_is_refused_and_nothing_is_added():
    board, _ = board_at()
    for kind in ("error", "INFO", "", None, 1, ["info"], " ok"):
        with pytest.raises(ValueError):
            board.add(title="t", kind=kind)
    for bad in ("", "has space", "a/b", "x" * 65, "ñandú", "a\n", 7, ["a"]):
        with pytest.raises(ValueError):
            board.add(title="t", notice_id=bad)
    for good in ("a", "x" * 64, "tanda-19", "a.b_c:d-e", "constructor", "__proto__", "0"):
        assert board.add(title="t", notice_id=good).id == good
    assert board.state("nope") == "unknown"


@pytest.mark.parametrize("given, kept", [
    (1, 10.0), (0, 10.0), (-5, 10.0), (9.99, 10.0), (10, 10.0), (300.5, 300.5), (3600, 3600.0), (3601, 3600.0),
    (10 ** 30, 3600.0), (-10 ** 30, 10.0), (1e300, 3600.0),
    (float("nan"), 300.0), (float("inf"), 300.0), (float("-inf"), 300.0), (True, 300.0), ("60", 300.0), (None, 300.0),
])
def test_the_ttl_is_clamped_and_what_is_not_a_finite_number_gets_the_default(given, kept):
    board, _ = board_at()
    assert board.add(title="t", ttl=given).ttl == kept


def test_a_notice_leaves_by_itself_when_its_ttl_is_over_and_the_board_remembers_that():
    board, t = board_at()
    board.add(title="a", ttl=30, notice_id="a")
    board.add(title="b", notice_id="b")  # 300 s
    t[0] = 1029.5
    assert [n.id for n in board.active()] == ["b", "a"]
    t[0] = 1030.0
    assert [n.id for n in board.active()] == ["b"] and board.state("a") == "expired"
    t[0] = 1299.5
    assert board.state("b") == "shown"
    t[0] = 1300.0
    assert board.active() == [] and board.state("b") == "expired"


def test_the_same_id_replaces_the_notice_and_restarts_its_ttl_and_its_age():
    board, t = board_at()
    board.add(title="A", body="b", kind="info", ttl=100, notice_id="x")
    t[0] += 40
    again = board.add(title="A", body="b", kind="info", ttl=500, source="cli", notice_id="x")
    assert (again.rev, again.at, again.ttl, again.source) == (1, t[0], 500.0, "cli")
    assert board.age(again) == 0 and len(board.active()) == 1
    t[0] += 450
    assert board.state("x") == "shown"  # the first ttl (100 s) would be long over: the clock restarted with the re-send
    t[0] += 51
    assert board.state("x") == "expired"


def test_rev_goes_up_only_when_the_title_the_body_or_the_kind_changed():
    board, t = board_at()
    revs = []

    def send(**kw):
        t[0] += 1
        revs.append(board.add(notice_id="x", **{"title": "A", "body": "b", "kind": "info", **kw}).rev)

    send()                         # 1
    send()                         # same again
    send(title="A  ")              # the same once cleaned
    send(body="b\r\n")             # also
    send(ttl=600, source="cli")    # not part of what rev follows
    send(title="B")                # 2
    send(body="c")                 # 3
    send(body="")                  # 4: the body went away
    send(kind="warn")              # 5
    send(kind="warn")              # same
    assert revs == [1, 1, 1, 1, 1, 2, 3, 4, 5, 5]
    board.add(title="Z", notice_id="other")
    assert board.active()[0].rev == 1  # each notice counts its own


def test_a_re_sent_notice_goes_to_the_front_and_the_others_keep_their_order():
    board, t = board_at()
    for name in "abc":
        t[0] += 1
        board.add(title=name, notice_id=name)
    t[0] += 1
    board.add(title="a", notice_id="a")
    assert [n.id for n in board.active()] == ["a", "c", "b"]


def test_a_fourth_notice_evicts_the_oldest_and_a_re_send_never_evicts():
    board, t = board_at()
    for name in "abc":
        t[0] += 1
        board.add(title=name, notice_id=name)
    t[0] += 1
    board.add(title="b2", notice_id="b")  # full, and it takes its own place: nobody leaves
    assert [n.id for n in board.active()] == ["b", "c", "a"] and board.state("a") == "shown"
    t[0] += 1
    board.add(title="d", notice_id="d")
    assert [n.id for n in board.active()] == ["d", "b", "c"]  # a was the oldest (b was re-sent after it)
    assert board.state("a") == "evicted" and board.state("d") == "shown"


def test_a_notice_that_already_expired_does_not_cost_a_live_one_its_place():
    board, t = board_at()
    board.add(title="a", ttl=10, notice_id="a")
    t[0] += 5
    board.add(title="b", notice_id="b")
    board.add(title="c", notice_id="c")
    t[0] += 6  # a is over, though nobody has looked yet
    board.add(title="d", notice_id="d")
    assert [n.id for n in board.active()] == ["d", "c", "b"]
    assert board.state("a") == "expired"  # not "evicted": it ran out, nobody pushed it


def test_dismissing_and_withdrawing_say_how_it_stands_and_a_finished_notice_keeps_how_it_ended():
    board, t = board_at()
    for name in "abcd":
        board.add(title=name, notice_id=name, ttl=60 if name == "d" else 300)
    # a was evicted by d
    assert board.dismiss("b") == "dismissed" and board.state("b") == "dismissed"
    assert board.withdraw("c") == "withdrawn" and board.state("c") == "withdrawn"
    assert board.dismiss("b") == "dismissed" and board.withdraw("b") == "dismissed"  # harmless again, and it stays what it was
    assert board.dismiss("c") == "withdrawn" and board.dismiss("a") == "evicted"
    assert board.dismiss("never") == board.withdraw("never") == "unknown"
    t[0] += 61
    assert board.dismiss("d") == "expired" and board.withdraw("d") == "expired"
    assert board.active() == []


def test_finished_notices_are_remembered_for_an_hour_and_at_most_fifty():
    board, t = board_at()
    for n in range(NOTICE_HISTORY + 10):
        board.add(title="t", notice_id=f"n{n}")
        board.dismiss(f"n{n}")
    assert [board.state(f"n{n}") for n in (0, 9)] == ["unknown", "unknown"]  # the ones remembered the longest went first
    assert [board.state(f"n{n}") for n in (10, NOTICE_HISTORY + 9)] == ["dismissed", "dismissed"]
    t[0] += NOTICE_HISTORY_S
    assert board.state("n59") == "dismissed"
    t[0] += 0.5
    assert board.state("n59") == "unknown" and board.state("n10") == "unknown"


def test_the_hour_of_an_expired_notice_counts_from_when_it_ran_out_not_from_when_somebody_looked():
    board, t = board_at()
    board.add(title="a", ttl=10, notice_id="a")
    t[0] += 3000
    assert board.state("a") == "expired"  # 2990 s since it ran out
    t[0] += 700
    assert board.state("a") == "unknown"  # 3690
    lazy, t = board_at()
    lazy.add(title="b", ttl=10, notice_id="b")
    t[0] += 3700  # nobody looked in all that time: when it is looked at, it ran out more than an hour ago
    assert lazy.state("b") == "unknown"


def test_a_notice_sent_again_after_it_ended_is_shown_again_with_a_rev_above_the_old_one():
    board, t = board_at()
    board.add(title="A", notice_id="x")
    board.dismiss("x")
    assert board.add(title="A", notice_id="x").rev == 2 and board.state("x") == "shown"  # a screen can tell it is back
    assert board.add(title="B", notice_id="x").rev == 3
    board.withdraw("x")
    assert board.add(title="B", notice_id="x").rev == 4
    t[0] += NOTICE_HISTORY_S + 301
    assert board.state("x") == "unknown" and board.add(title="B", notice_id="x").rev == 1  # once it is forgotten it is new


def test_an_id_the_daemon_makes_is_never_one_that_is_taken_or_remembered():
    board, _ = board_at()
    board.add(title="mine", notice_id="n-2")
    assert [board.add(title="t").id for _ in range(2)] == ["n-1", "n-3"]
    board.add(title="mine", notice_id="n-5")
    board.dismiss("n-5")
    assert [board.add(title="t").id for _ in range(2)] == ["n-4", "n-6"]  # n-5 is remembered: its state would lie


def test_listeners_hear_every_change_a_person_or_a_sender_makes_and_nothing_else():
    board, t = board_at()
    heard = []
    stop = board.subscribe(lambda: heard.append(len(board._active)))
    board.add(title="a", notice_id="a", ttl=10)
    board.add(title="a", notice_id="a", ttl=10)  # a re-send
    board.dismiss("a")
    board.dismiss("a")  # nothing changed
    board.withdraw("never")
    board.dismiss("never")
    assert heard == [1, 1, 0]
    for name in "bcde":
        board.add(title=name, notice_id=name, ttl=10)
    assert heard[3:] == [1, 2, 3, 3]  # the one that evicts is one change, not two
    t[0] += 11
    board.active()
    board.state("b")
    assert heard[-1] == 3 and len(heard) == 7  # expiry is noticed by whoever looks; it tells no one
    stop()
    board.add(title="f")
    assert len(heard) == 7


def test_a_listener_that_fails_does_not_stop_the_others_or_the_change(caplog):
    board, _ = board_at()
    heard = []
    board.subscribe(lambda: 1 / 0)
    board.subscribe(lambda: heard.append("ok"))
    assert board.add(title="a", notice_id="a").id == "a"
    assert heard == ["ok"] and board.state("a") == "shown"
    assert "notice listener failed" in caplog.text


# --- the view ----------------------------------------------------------------------------------


def test_a_hub_whose_core_has_no_board_still_has_a_list_and_listo_still_has_an_answer(rig):
    rig.core.notices = None
    assert rig.view()["notices"] == [] and json.loads(rig.hub.current())["notices"] == []
    assert rig.hub.dismiss("x") == "unknown"


def test_the_view_lists_the_newest_first_with_a_body_only_when_there_is_one(rig):
    board = rig.core.notices
    board.add(title="uno", notice_id="a")
    rig.t += 3
    board.add(title="dos", body="línea 1\nlínea 2", kind="warn", source="discord", notice_id="b")
    rig.t += 4.9
    assert rig.view()["notices"] == [
        {"id": "b", "title": "dos", "body": "línea 1\nlínea 2", "kind": "warn", "source": "discord", "age": 4, "rev": 1},
        {"id": "a", "title": "uno", "kind": "info", "source": "hermes", "age": 7, "rev": 1},
    ]


def test_at_most_three_reach_the_screen_and_they_are_the_newest(rig):
    for n in range(6):
        rig.t += 1
        rig.core.notices.add(title=f"aviso {n}")
    assert [n["title"] for n in rig.view()["notices"]] == ["aviso 5", "aviso 4", "aviso 3"]


def test_the_view_is_ascii_on_the_wire_whatever_the_text_was(rig):
    rig.core.notices.add(title="ñandú ✓ 🎉", body="línea\nsegunda", source="Ana ✓", notice_id="x")
    text = rig.hub.current()
    assert text.isascii()
    (n,) = json.loads(text)["notices"]
    assert (n["title"], n["body"], n["source"]) == ("ñandú ✓ 🎉", "línea\nsegunda", "Ana ✓")


def test_the_screen_hears_about_a_notice_without_anybody_asking_for_the_state(rig):
    """The board tells the hub, the hub coalesces: no refresh() is called here, only the event loop runs."""
    texts = []

    async def scenario():
        rig.hub.subscribe(texts.append)
        rig.core.notices.add(title="uno", notice_id="a")
        await asyncio.sleep(0.05)
        assert [n["id"] for n in json.loads(texts[-1])["notices"]] == ["a"]
        rig.core.notices.add(title="dos", notice_id="b")
        rig.core.notices.add(title="tres", notice_id="c")
        await asyncio.sleep(0.05)
        assert [n["id"] for n in json.loads(texts[-1])["notices"]] == ["c", "b", "a"]
        before = len(texts)
        rig.core.notices.dismiss("b")
        await asyncio.sleep(0.05)
        assert len(texts) == before + 1 and [n["id"] for n in json.loads(texts[-1])["notices"]] == ["c", "a"]
        rig.core.notices.withdraw("a")
        await asyncio.sleep(0.05)
        assert [n["id"] for n in json.loads(texts[-1])["notices"]] == ["c"]
        rig.hub.unbind()
        rig.core.notices.add(title="cuatro", notice_id="d")
        await asyncio.sleep(0.05)
        assert [n["id"] for n in json.loads(texts[-1])["notices"]] == ["c"]  # an unbound hub is not told

    run(scenario)


def test_an_expiry_reaches_the_screen_on_the_next_refresh_of_the_second_tick(rig):
    texts = []
    rig.hub.subscribe(texts.append)
    rig.core.notices.add(title="uno", ttl=30, notice_id="a")
    rig.hub.refresh()
    assert [n["id"] for n in json.loads(texts[-1])["notices"]] == ["a"]
    rig.t += 29
    rig.hub.refresh()  # the daemon's ticker does this every second
    assert [n["age"] for n in json.loads(texts[-1])["notices"]] == [29]
    rig.t += 1
    assert rig.hub.refresh() is True
    assert json.loads(texts[-1])["notices"] == []


# --- POST /notice ------------------------------------------------------------------------------


def notify(server, body, token=TOKEN, **kw):
    headers = {"Content-Type": "application/json", **auth(token), **kw.pop("headers", {})}
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return http(server, "POST", "/notice", headers=headers, body=data, **kw)


def status_of(server, notice_id, token=TOKEN):
    return http(server, "GET", f"/notice/{notice_id}", headers=auth(token))


def take_back(server, notice_id, token=TOKEN):
    return http(server, "DELETE", f"/notice/{notice_id}", headers=auth(token))


def listener(rig, **kw):
    """The Hermes listener with the core's board, and the hub's wake: what buddy.py gives it."""
    return hermes(rig, notices=rig.core.notices, wake=rig.hub.wake, **kw)


def titles(rig):
    return [n["title"] for n in rig.view()["notices"]]


def test_a_notice_is_taken_and_the_screen_lists_it(rig):
    async def scenario():
        async with listener(rig) as server:
            status, headers, body = await notify(server, {"title": "La tanda terminó"})
            assert status == 200 and headers["content-type"] == "application/json"
            assert json.loads(body) == {"ok": True, "id": "n-1", "active": 1}
            assert rig.view()["notices"] == [
                {"id": "n-1", "title": "La tanda terminó", "kind": "info", "source": "hermes", "age": 0, "rev": 1}]
            status, _, body = await notify(server, {"title": "Ana escribió", "body": "Dice que\nmañana", "kind": "warn",
                                                    "ttl": 120, "id": "ana:1", "source": "discord", "wake": False})
            assert status == 200 and json.loads(body) == {"ok": True, "id": "ana:1", "active": 2}
            rig.t += 7.9
            assert rig.view()["notices"][0] == {"id": "ana:1", "title": "Ana escribió", "body": "Dice que\nmañana",
                                                "kind": "warn", "source": "discord", "age": 7, "rev": 1}

    run(scenario)


def test_every_kind_is_taken_and_info_is_the_default(rig):
    async def scenario():
        async with listener(rig) as server:
            for kind in ("info", "ok", "warn"):
                assert (await notify(server, {"title": kind, "kind": kind, "id": kind}))[0] == 200
            assert {n["id"]: n["kind"] for n in rig.view()["notices"]} == {"info": "info", "ok": "ok", "warn": "warn"}
            assert (await notify(server, {"title": "sin tipo", "id": "plain"}))[0] == 200
            assert rig.view()["notices"][0]["kind"] == "info"

    run(scenario)


def test_the_same_id_replaces_the_notice_over_http_and_a_fourth_evicts_the_oldest(rig):
    async def scenario():
        async with listener(rig) as server:
            for name in "abc":
                rig.t += 1
                assert (await notify(server, {"title": name, "id": name}))[0] == 200
            status, _, body = await notify(server, {"title": "b", "id": "b"})
            assert status == 200 and json.loads(body)["active"] == 3  # a re-send, not a fourth
            assert [(n["id"], n["rev"]) for n in rig.view()["notices"]] == [("b", 1), ("c", 1), ("a", 1)]
            status, _, body = await notify(server, {"title": "b nuevo", "id": "b"})
            assert json.loads(body)["active"] == 3 and rig.view()["notices"][0]["rev"] == 2
            status, _, body = await notify(server, {"title": "d", "id": "d"})
            assert json.loads(body) == {"ok": True, "id": "d", "active": 3}
            assert [n["id"] for n in rig.view()["notices"]] == ["d", "b", "c"]
            assert json.loads((await status_of(server, "a"))[2]) == {"id": "a", "state": "evicted"}

    run(scenario)


BAD_NOTICES = [
    b"", b"not json", b"\xff\xfe", b"[]", b"null", b"42", b'"hola"', b"{}",
    {"body": "x"},  # no title
    {"title": None}, {"title": 7}, {"title": ["a"]}, {"title": True}, {"title": {"a": 1}}, {"title": 1.5},
    {"title": ""}, {"title": "   "}, {"title": "\n\t\x07"}, {"title": "​‮"},  # nothing left once cleaned
    {"title": "a", "body": 7}, {"title": "a", "body": None}, {"title": "a", "body": ["x"]}, {"title": "a", "body": {}},
    {"title": "a", "kind": "error"}, {"title": "a", "kind": "INFO"}, {"title": "a", "kind": ""},
    {"title": "a", "kind": None}, {"title": "a", "kind": 1}, {"title": "a", "kind": ["info"]}, {"title": "a", "kind": " ok"},
    {"title": "a", "ttl": "300"}, {"title": "a", "ttl": True}, {"title": "a", "ttl": False}, {"title": "a", "ttl": None},
    {"title": "a", "ttl": [30]}, {"title": "a", "ttl": {}},
    b'{"title":"a","ttl":NaN}', b'{"title":"a","ttl":Infinity}', b'{"title":"a","ttl":-Infinity}',  # it would never expire
    b'{"title":"a","ttl":1e999}',
    {"title": "a", "wake": 1}, {"title": "a", "wake": 0}, {"title": "a", "wake": "true"}, {"title": "a", "wake": None},
    {"title": "a", "wake": []},
    {"title": "a", "id": ""}, {"title": "a", "id": 7}, {"title": "a", "id": None}, {"title": "a", "id": ["a"]},
    {"title": "a", "id": "has space"}, {"title": "a", "id": "a/b"}, {"title": "a", "id": "x" * 65},
    {"title": "a", "id": "ñandú"}, {"title": "a", "id": "../x"}, {"title": "a", "id": "a\n"}, {"title": "a", "id": "a?b"},
    {"title": "a", "source": 7}, {"title": "a", "source": None}, {"title": "a", "source": ["x"]},
]


@pytest.mark.parametrize("body", BAD_NOTICES, ids=lambda b: repr(b)[:60])
def test_a_notice_of_the_wrong_type_or_shape_is_a_400_and_changes_nothing(rig, body):
    seen = []

    async def scenario():
        async with hermes(rig, notices=rig.core.notices, wake=seen.append) as server:
            assert (await notify(server, {"title": "keep", "id": "keep"}))[0] == 200
            status, _, answer = await notify(server, body)
            assert status == 400, body
            assert json.loads(answer)["ok"] is False and error_of(answer)
            assert titles(rig) == ["keep"]  # nothing half-applied
            assert seen == []  # and a refused notice wakes nobody, whatever its wake said

    run(scenario)


def test_a_wake_on_a_refused_notice_does_not_wake_the_screen(night):
    async def scenario():
        async with listener(night) as server:
            assert (await notify(server, {"title": "", "wake": True}))[0] == 400
            assert (await notify(server, {"title": "a", "kind": "boom", "wake": True}))[0] == 400
            assert night.dim() is True

    run(scenario)


def test_what_is_too_long_is_cut_and_what_is_out_of_range_is_clamped_never_refused(rig):
    async def scenario():
        async with listener(rig) as server:
            body = {"title": "T" * 100, "body": "\n".join(f"línea {n}" for n in range(10)) + "x" * 500, "source": "s" * 50,
                    "id": "a:b.c_d-" + "e" * 56}
            status, _, answer = await notify(server, body)
            assert status == 200 and json.loads(answer)["id"] == body["id"] and len(body["id"]) == 64
            (n,) = rig.view()["notices"]
            assert (n["title"], n["source"]) == ("T" * 60, "s" * 24)
            assert n["body"] == "\n".join(f"línea {k}" for k in range(4))  # the first four lines
            assert (await notify(server, {"title": "b", "body": "x" * 500, "id": "b"}))[0] == 200
            assert rig.view()["notices"][0]["body"] == "x" * 240
            assert (await notify(server, {"title": "c", "source": " \n ", "id": "c"}))[0] == 200
            assert rig.view()["notices"][0]["source"] == "hermes"

    run(scenario)


@pytest.mark.parametrize("given, lasts", [
    (1, 10), (0, 10), (-5, 10), (0.5, 10), (10, 10), (45.5, 45.5), (99999, 3600), (3600, 3600), (10 ** 30, 3600),
    (None, 300),  # (left out)
])
def test_the_ttl_is_clamped_to_ten_seconds_and_an_hour_with_five_minutes_by_default(rig, given, lasts):
    async def scenario():
        async with listener(rig) as server:
            body = {"title": "a", "id": "a"}
            if given is not None:
                body["ttl"] = given
            assert (await notify(server, body))[0] == 200
            rig.t += lasts - 0.01
            assert titles(rig) == ["a"], "went early"
            rig.t += 0.02
            assert titles(rig) == [], "stayed too long"

    run(scenario)


def test_unknown_keys_are_ignored_and_nothing_in_the_body_opens_a_connection(rig):
    async def scenario():
        async with listener(rig) as server:
            body = {"title": "a", "callback": "http://10.0.0.5:9/hook", "tool": "rm -rf /", "approve": True, "future": [1],
                    "key": "claude:req_1", "decision": "once"}
            assert (await notify(server, body))[0] == 200
            (n,) = rig.view()["notices"]
            assert set(n) == {"id", "title", "kind", "source", "age", "rev"}  # none of those reached the view

    run(scenario)


def test_hostile_text_is_kept_as_text_and_ids_that_are_names_of_object_properties_are_harmless(rig):
    hostile = '<img src=x onerror="alert(1)"></script><script>alert(2)</script>'

    async def scenario():
        async with listener(rig) as server:
            assert (await notify(server, {"title": hostile, "body": hostile, "source": hostile, "id": "constructor"}))[0] == 200
            assert (await notify(server, {"title": "b", "id": "__proto__"}))[0] == 200
            assert (await notify(server, {"title": "ñandú ✓ 🎉", "id": "toString"}))[0] == 200
            v = rig.view()["notices"]
            assert [n["id"] for n in v] == ["toString", "__proto__", "constructor"]
            assert v[2]["title"] == hostile[:60] and v[2]["body"] == hostile[:240] and v[2]["source"] == hostile[:24]
            assert rig.hub.current().isascii()
            assert json.loads(rig.hub.current())["notices"][0]["title"] == "ñandú ✓ 🎉"
            assert json.loads((await status_of(server, "constructor"))[2]) == {"id": "constructor", "state": "shown"}

    run(scenario)


def test_the_largest_honest_notice_fits_the_body_cap_even_in_the_worst_encoding(rig):
    async def scenario():
        async with listener(rig) as server:
            worst = json.dumps({"title": "🎉" * 60, "body": "🎉" * 240, "source": "🎉" * 24, "id": "x" * 64, "ttl": 3600.5,
                                "kind": "warn", "wake": True})  # ensure_ascii: 12 bytes for each of them
            assert len(worst.encode()) < 4096
            assert (await notify(server, worst.encode()))[0] == 200
            (n,) = rig.view()["notices"]
            assert len(n["title"]) == 60 and len(n["body"]) == 240 and len(n["source"]) == 24
            huge = json.dumps({"title": "a", "junk": "x" * 5000}).encode()
            assert len(huge) > 4096
            assert (await notify(server, huge))[0] == 413
            assert len(rig.view()["notices"]) == 1

    run(scenario)


# --- who may talk to it ------------------------------------------------------------------------


def test_the_notice_routes_need_the_token(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "AUTH_FAIL_MAX", 100)  # the lockout has its own test below
    monkeypatch.setattr(buddy_hermes, "NOTICE_RATE_MAX", 1000)  # and so has the budget

    async def scenario():
        async with listener(rig) as server:
            assert (await notify(server, {"title": "a", "id": "a"}))[0] == 200
            for token in ("", "wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), "ñandú", None):
                for call in (lambda: notify(server, {"title": "b"}, token=token), lambda: status_of(server, "a", token),
                             lambda: take_back(server, "a", token)):
                    status, _, answer = await call()
                    assert status == 403 and error_of(answer) == "bad token", token
            for headers in ({"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}, {"X-Buddy-Token": TOKEN},
                            {"Authorization": f"Bearer  {TOKEN} extra"}):
                assert (await notify(server, {"title": "b"}, token=None, headers=headers))[0] == 403, headers
            assert titles(rig) == ["a"] and rig.core.notices.state("a") == "shown"  # nothing was added, nothing was taken back

    run(scenario)


def test_a_notice_needs_a_json_body_the_right_method_and_a_listener_that_is_not_a_browser(rig):
    async def scenario():
        async with listener(rig) as server:
            for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x", None):
                headers = {**auth(), **({"Content-Type": ctype} if ctype else {})}
                status, _, _ = await http(server, "POST", "/notice", headers=headers, body=json.dumps({"title": "a"}).encode())
                assert status == 415, ctype
            assert (await notify(server, {"title": "a"}, headers={"Content-Type": "application/json; charset=utf-8"}))[0] == 200
            for method in ("GET", "PUT", "DELETE", "PATCH"):
                assert (await http(server, method, "/notice", headers={**auth(), "Content-Type": "application/json"},
                                   body=b"{}"))[0] == 405, method
            for method in ("POST", "PUT", "PATCH"):
                assert (await http(server, method, "/notice/n-1", headers={**auth(), "Content-Type": "application/json"},
                                   body=b"{}"))[0] == 405, method
            assert (await notify(server, {"title": "b"}, headers={"Origin": "http://evil.example"}))[0] == 403
            assert (await notify(server, {"title": "b"}, host="evil.example:80"))[0] == 403
            assert (await status_of(server, "n-1", ))[0] == 200
            assert (await http(server, "GET", "/notice/n-1", headers={**auth(), "Origin": "null"}))[0] == 403
            assert (await http(server, "DELETE", "/notice/n-1", headers=auth(), host="10.0.0.5:80"))[0] == 403
            assert titles(rig) == ["a"]

    run(scenario)


@pytest.mark.parametrize("path", ["/notice/", "/notice/a/b", "/notice/..%2Fx", "/notice/a%20b", "/notice/" + "x" * 65,
                                  "/notice/ñandú", "/notice//", "/notices", "/notice/a?b"])
def test_an_id_that_could_not_be_one_is_a_404_not_a_state(rig, path):
    async def scenario():
        async with listener(rig) as server:
            expected = 200 if path == "/notice/a?b" else 404  # a query string is not part of the id
            assert (await http(server, "GET", path, headers=auth()))[0] == expected
            assert (await http(server, "DELETE", path, headers=auth()))[0] == expected

    run(scenario)


def test_without_a_board_there_is_no_notice_route_but_the_token_is_still_checked_first(rig):
    async def scenario():
        async with hermes(rig) as server:
            assert (await notify(server, {"title": "a"}))[0] == 404
            assert (await status_of(server, "a"))[0] == 404
            assert (await take_back(server, "a"))[0] == 404
            assert (await notify(server, {"title": "a"}, token="wrong-token-0123456789"))[0] == 403

    run(scenario)


def test_the_screens_port_has_no_notice_route_and_hermes_port_has_no_dismiss(rig):
    async def scenario():
        rig.core.notices.add(title="a", notice_id="a")
        async with listener(rig) as server, screen(rig) as page:
            headers = {"Content-Type": "application/json", "X-Buddy-Token": page.token, **auth()}
            assert (await http(page, "POST", "/notice", headers=headers, body=json.dumps({"title": "x"}).encode()))[0] == 404
            assert (await http(page, "GET", "/notice/a", headers=headers))[0] == 404
            assert (await http(page, "DELETE", "/notice/a", headers=headers))[0] == 404
            assert (await http(server, "POST", "/dismiss", headers=headers, body=json.dumps({"id": "a"}).encode()))[0] == 404
            assert titles(rig) == ["a"]

    run(scenario)


def test_bad_tokens_on_the_notice_routes_feed_the_shared_lockout(rig):
    async def scenario():
        async with listener(rig) as server:
            for _ in range(buddy_hermes.AUTH_FAIL_MAX):
                assert (await notify(server, {"title": "a"}, token="wrong"))[0] == 403
            status, _, answer = await notify(server, {"title": "a"})  # the right token, too late
            assert status == 429 and "bad tokens" in error_of(answer)
            status, _, answer = await report(server, {"id": "s", "project": "p"})  # the hooks share the lockout
            assert status == 429 and "bad tokens" in error_of(answer)
            assert rig.view()["notices"] == []
        async with listener(rig) as server:  # and the other way: bad tokens elsewhere lock the notices
            for _ in range(buddy_hermes.AUTH_FAIL_MAX):
                assert (await report(server, {"id": "s"}, token="wrong"))[0] == 403
            assert (await notify(server, {"title": "a"}))[0] == 429
            assert (await status_of(server, "a"))[0] == 429

    run(scenario)


def test_the_notices_have_their_own_request_budget_and_cannot_starve_the_hooks(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "RATE_MAX", 20)  # the hooks' 300 a minute would only make this slow

    async def scenario():
        async with listener(rig, tasks=rig.core.tasks) as server:
            for _ in range(buddy_hermes.NOTICE_RATE_MAX):
                assert (await notify(server, {"title": "a", "id": "a"}))[0] == 200
            status, _, answer = await notify(server, {"title": "a", "id": "a"})
            assert status == 429 and error_of(answer) == "too many requests"
            assert (await status_of(server, "a"))[0] == 429 and (await take_back(server, "a"))[0] == 429  # one budget for all three
            for n in range(buddy_hermes.RATE_MAX):  # all of the hooks' budget is still theirs
                assert (await report(server, {"id": "s", "project": "p", "tool": f"T{n}"}))[0] == 200, n
            assert (await report(server, {"id": "s", "project": "p"}))[0] == 429
            assert (await notify(server, {"title": "a", "id": "a"}))[0] == 429
        async with listener(rig, tasks=rig.core.tasks) as server:  # and the other way round
            for n in range(buddy_hermes.RATE_MAX):
                assert (await report(server, {"id": "s", "project": "p", "tool": f"T{n}"}))[0] == 200, n
            assert (await report(server, {"id": "s", "project": "p"}))[0] == 429
            for _ in range(buddy_hermes.NOTICE_RATE_MAX):
                assert (await notify(server, {"title": "b", "id": "b"}))[0] == 200
            assert (await notify(server, {"title": "b", "id": "b"}))[0] == 429

    run(scenario)


def test_the_notices_and_the_census_do_not_share_a_budget_either(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "CENSUS_RATE_MAX", 5)

    async def scenario():
        async with listener(rig, tasks=rig.core.tasks) as server:
            census = {"v": 1, "source": "mac", "ttl": 60, "more": 0, "rows": []}
            headers = {"Content-Type": "application/json", **auth()}
            for _ in range(5):
                assert (await http(server, "POST", "/census", headers=headers, body=json.dumps(census).encode()))[0] == 200
            assert (await http(server, "POST", "/census", headers=headers, body=json.dumps(census).encode()))[0] == 429
            assert (await notify(server, {"title": "a"}))[0] == 200

    run(scenario)


def test_a_notice_refused_for_its_shape_still_costs_budget_so_a_flood_of_bad_ones_is_throttled(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "NOTICE_RATE_MAX", 4)

    async def scenario():
        async with listener(rig, tasks=rig.core.tasks) as server:
            for _ in range(4):
                assert (await notify(server, b"not json"))[0] == 400
            assert (await notify(server, {"title": "a"}))[0] == 429
            assert (await report(server, {"id": "s", "project": "p"}))[0] == 200  # the hooks do not notice

    run(scenario)


# --- GET and DELETE /notice/<id> ---------------------------------------------------------------


def test_the_state_of_a_notice_follows_its_life(rig):
    async def scenario():
        async with listener(rig) as server:
            async def state(notice_id):
                status, headers, answer = await status_of(server, notice_id)
                assert status == 200 and headers["content-type"] == "application/json"
                assert json.loads(answer)["id"] == notice_id
                return json.loads(answer)["state"]

            assert await state("never") == "unknown"
            await notify(server, {"title": "a", "id": "a"})
            assert await state("a") == "shown"
            rig.hub.dismiss("a")  # the person taps Listo
            assert await state("a") == "dismissed"
            await notify(server, {"title": "b", "id": "b", "ttl": 10})
            rig.t += 10
            assert await state("b") == "expired"
            for name in "cdef":
                await notify(server, {"title": name, "id": name})
            assert await state("c") == "evicted" and await state("f") == "shown"
            await notify(server, {"title": "a again", "id": "a"})
            assert await state("a") == "shown"  # an id that ended can be used again
            rig.t += NOTICE_HISTORY_S + 1000
            assert await state("b") == "unknown"  # forgotten after an hour

    run(scenario)


def test_hermes_can_take_a_notice_back_and_asking_again_is_harmless(rig):
    async def scenario():
        async with listener(rig) as server:
            await notify(server, {"title": "a", "id": "a"})
            await notify(server, {"title": "b", "id": "b"})
            status, headers, answer = await take_back(server, "a")
            assert status == 200 and json.loads(answer) == {"ok": True, "state": "withdrawn"}
            assert headers["content-type"] == "application/json"
            assert titles(rig) == ["b"]
            assert json.loads((await status_of(server, "a"))[2])["state"] == "withdrawn"
            assert json.loads((await take_back(server, "a"))[2]) == {"ok": True, "state": "withdrawn"}
            status, _, answer = await take_back(server, "never")
            assert status == 200 and json.loads(answer) == {"ok": True, "state": "unknown"}  # not a 404: it is idempotent
            rig.hub.dismiss("b")
            assert json.loads((await take_back(server, "b"))[2]) == {"ok": True, "state": "dismissed"}  # how it ended stays
            assert rig.view()["notices"] == []

    run(scenario)


def test_a_notice_can_be_sent_again_after_it_was_taken_back(rig):
    async def scenario():
        async with listener(rig) as server:
            await notify(server, {"title": "a", "id": "a"})
            await take_back(server, "a")
            status, _, answer = await notify(server, {"title": "a", "id": "a"})
            assert status == 200 and json.loads(answer)["active"] == 1
            assert [(n["id"], n["rev"]) for n in rig.view()["notices"]] == [("a", 2)]
            assert json.loads((await status_of(server, "a"))[2])["state"] == "shown"

    run(scenario)


# --- the screen hears about it -----------------------------------------------------------------


def test_the_open_page_gets_the_notice_and_loses_it_without_any_other_event(rig):
    async def scenario():
        async with listener(rig) as server, serving(rig) as page:
            stream = await Stream.open(page)
            try:
                assert (await stream.state())["notices"] == []
                await notify(server, {"title": "La tanda terminó", "id": "tanda"})
                got = await stream.state(lambda v: v["notices"])
                assert [n["title"] for n in got["notices"]] == ["La tanda terminó"]
                status, _, answer = await http(page, "POST", "/dismiss", headers={
                    "Content-Type": "application/json", "X-Buddy-Token": page.token}, body=json.dumps({"id": "tanda"}).encode())
                assert status == 200 and json.loads(answer) == {"ok": True, "state": "dismissed"}
                assert (await stream.state(lambda v: not v["notices"]))["notices"] == []
                await notify(server, {"title": "otro", "id": "otro"})
                await stream.state(lambda v: v["notices"])
                await take_back(server, "otro")
                assert (await stream.state(lambda v: not v["notices"]))["notices"] == []
            finally:
                stream.close()

    run(scenario)


def test_a_notice_never_approves_or_denies_anything(rig):
    rig.rx(heartbeat(waiting=1, prompt=PROMPT))
    assert [a["key"] for a in rig.view()["approvals"]] == ["claude:req_1"]

    async def scenario():
        async with listener(rig) as server:
            body = {"title": "aprobar claude:req_1", "body": "decision: once", "key": "claude:req_1", "decision": "once",
                    "id": "claude:req_1", "wake": True}
            assert (await notify(server, body))[0] == 200
            assert (await take_back(server, "claude:req_1"))[0] == 200
            assert (await notify(server, {"title": "otra", "id": "hermes:h_1"}))[0] == 200
            assert [a["key"] for a in rig.view()["approvals"]] == ["claude:req_1"]  # still waiting for a person
            assert [m for m in rig.sent if m.get("cmd") == "permission"] == []
            assert (rig.core.state.approvals, rig.core.state.denials) == (0, 0)

    run(scenario)


def test_a_notice_does_not_change_what_the_buddy_feels(rig):
    before = rig.view()["mood"]

    async def scenario():
        async with listener(rig) as server:
            await notify(server, {"title": "a", "kind": "warn", "wake": True})
            assert rig.view()["mood"] == before

    run(scenario)


# --- POST /dismiss -----------------------------------------------------------------------------


def dismiss_request(server, notice_id, **kw):
    headers = {"Content-Type": "application/json", "X-Buddy-Token": server.token, **kw.pop("headers", {})}
    body = kw.pop("body", json.dumps({"id": notice_id}).encode())
    return http(server, "POST", "/dismiss", headers=headers, body=body, **kw)


def test_listo_takes_the_notice_off_the_screen_and_says_how_it_stands(rig):
    board = rig.core.notices
    board.add(title="uno", notice_id="a")
    board.add(title="dos", notice_id="b", ttl=10)

    async def scenario():
        async with serving(rig) as server:
            status, headers, body = await dismiss_request(server, "a")
            assert status == 200 and json.loads(body) == {"ok": True, "state": "dismissed"}
            assert headers["content-type"] == "application/json" and headers["cache-control"] == "no-store"
            assert titles(rig) == ["dos"] and board.state("a") == "dismissed"  # and Hermes can ask
            assert json.loads((await dismiss_request(server, "a"))[2]) == {"ok": True, "state": "dismissed"}  # again: harmless
            assert json.loads((await dismiss_request(server, "never"))[2]) == {"ok": True, "state": "unknown"}
            for odd in ("", "has space", "x" * 500, "ñandú", "../x", "constructor"):  # any text is just an id nobody has
                status, _, body = await dismiss_request(server, odd)
                assert status == 200 and json.loads(body) == {"ok": True, "state": "unknown"}, odd
            rig.t += 11
            assert json.loads((await dismiss_request(server, "b"))[2]) == {"ok": True, "state": "expired"}

    run(scenario)


def test_listo_on_one_notice_leaves_the_others(rig):
    for name in "abc":
        rig.core.notices.add(title=name, notice_id=name)

    async def scenario():
        async with serving(rig) as server:
            assert (await dismiss_request(server, "b"))[0] == 200
            assert [n["id"] for n in rig.view()["notices"]] == ["c", "a"]

    run(scenario)


def test_without_the_right_token_listo_does_nothing(rig):
    rig.core.notices.add(title="uno", notice_id="a")

    async def scenario():
        async with serving(rig) as server:
            for token in ("", "wrong", server.token[:-1], server.token + "x", "ñandú"):
                headers = {"Content-Type": "application/json", "X-Buddy-Token": token}
                assert (await http(server, "POST", "/dismiss", headers=headers, body=b'{"id": "a"}'))[0] == 403, token
            assert (await http(server, "POST", "/dismiss", headers={"Content-Type": "application/json"}, body=b'{"id": "a"}'))[0] == 403
            status, _, body = await http(server, "POST", "/dismiss", headers={"X-Buddy-Token": "no"})
            assert status == 403 and json.loads(body) == {"ok": False, "error": "bad token"}  # the same shape as /decide
            assert (await http(server, "POST", "/dismiss", headers={"Authorization": f"Bearer {TOKEN}"}, body=b'{"id": "a"}'))[0] == 403
            assert titles(rig) == ["uno"]

    run(scenario)


def test_listo_wants_a_json_body_with_the_id_of_a_notice(rig):
    rig.core.notices.add(title="uno", notice_id="a")

    async def scenario():
        async with serving(rig) as server:
            for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
                assert (await dismiss_request(server, "a", headers={"Content-Type": ctype}))[0] == 415, ctype
            assert (await http(server, "POST", "/dismiss", headers={"X-Buddy-Token": server.token}, body=b'{"id": "a"}'))[0] == 415
            assert (await http(server, "POST", "/dismiss", headers={"X-Buddy-Token": server.token}))[0] == 415  # no type at all
            for body in (b"", b"not json", b"\xff", b"[]", b"null", b"42", b'"a"', b"{}", b'{"id": 7}', b'{"id": null}',
                         b'{"id": ["a"]}', b'{"id": {"a": 1}}', b'{"notice": "a"}', b'{"key": "a"}'):
                assert (await dismiss_request(server, "a", body=body))[0] == 400, body
            assert (await dismiss_request(server, "a", headers={"Content-Type": "application/json; charset=utf-8"}))[0] == 200
            assert titles(rig) == []

    run(scenario)


def test_only_post_dismisses(rig):
    rig.core.notices.add(title="uno", notice_id="a")

    async def scenario():
        async with serving(rig) as server:
            for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
                status, _, _ = await http(server, method, "/dismiss", headers={"X-Buddy-Token": server.token})
                assert status == 405, method
            assert titles(rig) == ["uno"]

    run(scenario)


def test_the_host_and_the_origin_of_listo_are_checked_like_for_decide(rig):
    rig.core.notices.add(title="uno", notice_id="a")

    async def scenario():
        async with serving(rig) as server:
            port = server.port
            for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", "", f"10.0.0.5:{port}"):
                status, _, _ = await dismiss_request(server, "a", host=host or None, headers={"Host": host})
                assert status == 403, host
            for origin in ("http://evil.example", "https://127.0.0.1:%d" % port, "http://127.0.0.1:%d" % (port + 1), "null"):
                assert (await dismiss_request(server, "a", headers={"Origin": origin}))[0] == 403, origin
            assert titles(rig) == ["uno"]
            host = f"localhost:{port}"  # the page itself, which sends its own origin
            assert (await dismiss_request(server, "a", headers={"Host": host, "Origin": f"http://{host}"}))[0] == 200

    run(scenario)


def test_an_oversized_or_chunked_body_is_refused_before_anything_is_dismissed(rig):
    rig.core.notices.add(title="uno", notice_id="a")

    async def scenario():
        async with serving(rig) as server:
            host = f"Host: 127.0.0.1:{server.port}\r\nX-Buddy-Token: {server.token}\r\nContent-Type: application/json\r\n".encode()
            assert (await http(server, raw=b"POST /dismiss HTTP/1.1\r\n" + host + b"Content-Length: 99999999\r\n\r\n"))[0] == 413
            assert (await http(server, raw=b"POST /dismiss HTTP/1.1\r\n" + host + b"Transfer-Encoding: chunked\r\n\r\n"))[0] == 400
            assert titles(rig) == ["uno"]

    run(scenario)


def test_listo_only_listens_on_loopback_like_the_rest_of_the_screen(rig):
    for host in ("0.0.0.0", "192.168.1.5", "buddy.local", ""):
        with pytest.raises(ValueError):
            UIServer(rig.hub, host=host)

    async def scenario():
        async with serving(rig) as server:
            assert server._server.sockets[0].getsockname()[0] == "127.0.0.1"

    run(scenario)


def test_a_flood_of_listo_is_throttled_after_the_token_and_leaves_wake_decide_and_the_stream_alone(rig):
    async def scenario():
        now = [500.0]
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig, clock=lambda: now[0]) as server:
            stream = await Stream.open(server)
            try:
                await stream.state()
                for _ in range(30):  # bad tokens spend nothing of the budget
                    assert (await http(server, "POST", "/dismiss", headers={"X-Buddy-Token": "no"}))[0] == 403
                statuses = [(await dismiss_request(server, "x"))[0] for _ in range(DISMISS_RATE_MAX + 5)]
                assert statuses == [200] * DISMISS_RATE_MAX + [429] * 5
                status, _, body = await dismiss_request(server, "x")
                assert status == 429 and json.loads(body) == {"ok": False, "error": "too many requests"}
                # the flood did not starve the rest: /wake has its own budget, the state is served, an answer goes through
                assert (await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token}))[0] == 200
                assert (await http(server, "GET", "/state"))[0] == 200
                other = await Stream.open(server)
                assert other.status == 200
                other.close()
                body = json.dumps({"key": "claude:req_1", "decision": "once"}).encode()
                assert (await http(server, "POST", "/decide", body=body, headers={
                    "Content-Type": "application/json", "X-Buddy-Token": server.token}))[0] == 200
                now[0] += DISMISS_RATE_WINDOW_S + 0.5  # and it is back when the window has passed
                assert (await dismiss_request(server, "x"))[0] == 200
                # the other way round: a flood of wakes leaves "Listo" alone
                for _ in range(WAKE_RATE_MAX + 2):
                    await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token})
                assert (await dismiss_request(server, "x"))[0] == 200
            finally:
                stream.close()

    run(scenario)


def test_listo_dismisses_and_does_nothing_else(night):
    night.core.notices.add(title="uno", notice_id="a")
    night.core.feed(encode_line(heartbeat(waiting=1, prompt=PROMPT)))

    async def scenario():
        async with serving(night) as server:
            body = json.dumps({"id": "a", "key": "claude:req_1", "decision": "once", "dim": False}).encode()
            assert (await dismiss_request(server, "a", body=body))[0] == 200
            assert [a["key"] for a in night.view()["approvals"]] == ["claude:req_1"]  # still waiting for a person
            assert not [m for m in night.sent if m.get("cmd") == "permission"]  # the Mac heard nothing
            assert (night.core.state.approvals, night.core.state.denials) == (0, 0)

    run(scenario)


def test_listo_does_not_wake_the_dim_screen(night):
    night.core.notices.add(title="uno", notice_id="a")

    async def scenario():
        async with serving(night) as server:
            assert night.dim() is True
            assert (await dismiss_request(server, "a"))[0] == 200
            assert night.dim() is True

    run(scenario)


# --- waking the dim night screen ---------------------------------------------------------------


def test_a_notice_that_asks_to_wake_lights_the_dim_screen_for_thirty_seconds(night):
    async def scenario():
        async with listener(night) as server:
            assert night.dim() is True
            assert (await notify(server, {"title": "La tanda terminó", "wake": True, "id": "tanda"}))[0] == 200
            assert night.dim() is False and [n["id"] for n in night.view()["notices"]] == ["tanda"]
            night.advance(AWAKE_S - 0.1)
            assert night.dim() is False
            night.advance(0.2)
            assert night.dim() is True  # the screen goes back to sleep...
            assert [n["id"] for n in night.view()["notices"]] == ["tanda"]  # ...and the notice is still there for the next wake

    run(scenario)


def test_a_notice_that_does_not_ask_never_wakes_it_and_waits_for_the_next_wake(night):
    async def scenario():
        async with listener(night) as server:
            assert (await notify(server, {"title": "uno", "id": "a"}))[0] == 200  # (wake left out)
            assert (await notify(server, {"title": "dos", "id": "b", "wake": False, "kind": "warn"}))[0] == 200
            night.advance(5)
            assert night.dim() is True and [n["id"] for n in night.view()["notices"]] == ["b", "a"]
            night.hub.wake()  # a tap
            assert night.dim() is False and [n["id"] for n in night.view()["notices"]] == ["b", "a"]
            night.advance(AWAKE_S + 1)
            assert night.dim() is True

    run(scenario)


def test_by_day_a_wake_changes_nothing_you_can_see(tmp_path):
    day = Night(tmp_path, second=at(12))

    async def scenario():
        async with listener(day) as server:
            before = {k: v for k, v in day.view().items() if k != "notices"}
            assert (await notify(server, {"title": "uno", "wake": True}))[0] == 200
            assert {k: v for k, v in day.view().items() if k != "notices"} == before
            assert day.view()["night"] is False and day.dim() is False
            assert [n["title"] for n in day.view()["notices"]] == ["uno"]

    run(scenario)


def test_a_notice_wakes_through_the_one_wake_the_taps_and_the_permissions_use(night):
    seen = []
    asked = []

    def wake(reason):  # what the hub is told, and what the screen would already show when it wakes
        seen.append((reason, [n.title for n in night.core.notices.active()][:1]))

    async def scenario():
        async with hermes(night, notices=night.core.notices, wake=wake) as server:
            await notify(server, {"title": "a", "wake": True})
            await notify(server, {"title": "b", "wake": False})
            await notify(server, {"title": "c"})
            await notify(server, {"title": "d", "wake": 1})  # refused
            await notify(server, {"title": "e", "wake": True, "id": "e"})
            await notify(server, {"title": "e", "wake": True, "id": "e"})  # a re-send that asks again asks again
            assert seen == [("aviso", ["a"]), ("aviso", ["e"]), ("aviso", ["e"])]  # the board has it before the wake
        night.hub.wake = lambda reason="toque": asked.append(reason) or False
        async with listener(night) as server:
            await notify(server, {"title": "f", "wake": True})
            assert asked == ["aviso"]

    run(scenario)


def test_the_night_line_names_the_reason_and_never_the_text(tmp_path):
    said = []
    night = Night(tmp_path, second=at(23, 30), notice=said.append)

    async def scenario():
        async with listener(night) as server:
            night.hub.refresh()
            assert (await notify(server, {"title": MARKER, "body": MARKER, "source": MARKER, "id": "m" + MARKER, "wake": True}))[0] == 200
            night.hub.refresh()
            night.advance(AWAKE_S + 1)
            night.hub.refresh()

    run(scenario)
    assert said == ["noche: pantalla tenue", "noche: despierta (aviso)", "noche: pantalla tenue"]


def test_waking_again_with_a_re_send_restarts_the_thirty_seconds(night):
    """On purpose, even when the notice is the same: a sender may escalate one that waited dim by sending it again with wake."""
    async def scenario():
        async with listener(night) as server:
            await notify(server, {"title": "a", "id": "a", "wake": True})
            night.advance(20)
            await notify(server, {"title": "a", "id": "a", "wake": True})
            night.advance(29)  # 49 s after the first, 29 after the second
            assert night.dim() is False
            night.advance(1.1)
            assert night.dim() is True

    run(scenario)


def test_a_pending_permission_keeps_the_screen_and_the_notice_waits_beside_it(night):
    got = night.ask("h1")

    async def scenario():
        async with listener(night) as server:
            assert night.dim() is False
            assert (await notify(server, {"title": "La tanda terminó", "id": "tanda", "wake": True, "ttl": 3600}))[0] == 200
            v = night.view()
            assert [a["key"] for a in v["approvals"]] == ["hermes:h1"] and [n["id"] for n in v["notices"]] == ["tanda"]
            assert v["dim"] is False and got == []  # the notice answered nothing
            night.advance(600)
            assert night.dim() is False  # the permission, not the notice, keeps it
            night.core.broker.decide("hermes:h1", "once")
            night.advance(AWAKE_S + 1)
            assert night.dim() is True and [n["id"] for n in night.view()["notices"]] == ["tanda"]
            assert got == ["once"]

    run(scenario)


def test_a_listener_without_a_hub_stores_a_notice_that_asks_to_wake_and_a_hub_that_fails_does_not_make_it_a_500(rig, caplog):
    def broken(reason):
        raise RuntimeError("no screen")

    async def scenario():
        async with hermes(rig, notices=rig.core.notices) as server:  # no way to wake anything
            assert (await notify(server, {"title": "a", "id": "a", "wake": True}))[0] == 200
        async with hermes(rig, notices=rig.core.notices, wake=broken) as server:
            status, _, answer = await notify(server, {"title": "b", "id": "b", "wake": True})
            assert status == 200 and json.loads(answer)["active"] == 2
        assert titles(rig) == ["b", "a"]

    run(scenario)
    assert "could not wake the screen for a notice" in caplog.text


# --- nothing of the text in a log, nothing kept on disk ----------------------------------------


def test_the_text_of_a_notice_never_reaches_a_log_only_its_kind_and_a_count(rig, caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with listener(rig) as server, serving(rig) as page:
            body = {"title": MARKER, "body": MARKER, "source": MARKER, "id": "id-" + MARKER, "kind": "warn", "wake": True}
            assert (await notify(server, body))[0] == 200
            assert (await notify(server, body))[0] == 200  # a re-send
            for refused in ({**body, "kind": MARKER}, {**body, "title": [MARKER]}, {**body, "ttl": MARKER},
                            {**body, "id": MARKER + " no"}, {**body, "wake": MARKER}, {**body, "body": [MARKER]}):
                status, _, answer = await notify(server, refused)
                assert status == 400 and MARKER not in answer.decode()  # nor in what the sender is told
            assert (await notify(server, {**body, "title": "​"}))[0] == 400
            assert (await notify(server, b"{" + MARKER.encode()))[0] == 400
            assert (await notify(server, body, token="wrong-" + MARKER))[0] == 403
            assert (await status_of(server, "id-" + MARKER))[0] == 200
            assert (await dismiss_request(page, "id-" + MARKER))[0] == 200
            assert (await dismiss_request(page, "x" + MARKER))[0] == 200
            await notify(server, body)
            assert (await take_back(server, "id-" + MARKER))[0] == 200
            assert (await take_back(server, "id-" + MARKER + "x"))[0] == 200

    run(scenario)
    assert "aviso: warn, 1 activos" in caplog.text  # the capture works, and says what it should...
    assert "hermes listener on" in caplog.text
    assert MARKER not in caplog.text and MARKER.lower() not in caplog.text  # ...and nothing else about the notice
    assert TOKEN not in caplog.text


def test_a_restart_clears_the_notices_and_nothing_of_them_is_written_to_disk(tmp_path):
    state = tmp_path / "state"
    sent = []
    first = BuddyCore(state, sent.append)
    first.notices.add(title=MARKER, body=MARKER, source=MARKER, notice_id="id-" + MARKER)
    first.notices.dismiss("id-" + MARKER)
    first.notices.add(title=MARKER, body=MARKER, notice_id="live")
    first.store.flush_if_due()
    first.close()
    for path in state.rglob("*"):
        if path.is_file():
            assert MARKER.encode() not in path.read_bytes(), path
    second = BuddyCore(state, sent.append)  # the daemon started again on the same folder
    assert second.notices.active() == [] and second.notices.state("live") == "unknown"
    second.close()


# --- the daemon: buddy.py puts the board, the listener and the hub's wake together ---------------


async def boot(args, capsys):
    """Start the real run_daemon in this process; return (task, hermes port, screen port, screen token)."""
    task = asyncio.ensure_future(buddy.run_daemon(args))
    out = ""
    for _ in range(500):
        out += capsys.readouterr().out
        if "Hermes en http://127.0.0.1:" in out and "Pantalla en http://127.0.0.1:" in out:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"the daemon never came up:\n{out}")
    hport = int(re.search(r"Hermes en http://127\.0\.0\.1:(\d+)", out).group(1))
    port = int(re.search(r"Pantalla en http://127\.0\.0\.1:(\d+)", out).group(1))
    page = (await http(port))[2].decode("utf-8")
    return task, hport, port, re.search(r'<meta name="buddy-token" content="([^"]+)">', page).group(1)


def test_the_daemon_takes_a_notice_from_hermes_wakes_the_panel_only_when_asked_and_forgets_it_on_restart(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(buddy_ui, "AWAKE_S", 0.8)
    panel = make_panel(tmp_path / "bl")
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: FakeTransport(panel))
    token_file = tmp_path / "hermes.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    args = daemon_args(tmp_path, panel, "--hermes-bind", "127.0.0.1", "--hermes-port", "0", "--hermes-token-file", str(token_file))

    async def state(port):
        return json.loads((await http(port, path="/state"))[2])

    async def scenario():
        task, hport, port, page_token = await boot(args, capsys)
        try:
            await wait_until(lambda: level(panel) == 12)  # always night (08:00-08:00)
            headers = {"Content-Type": "application/json", **auth()}
            status, _, body = await http(hport, "POST", "/notice", headers=headers,
                                         body=json.dumps({"title": MARKER, "body": MARKER, "id": "uno"}).encode())
            assert status == 200 and json.loads(body) == {"ok": True, "id": "uno", "active": 1}
            await asyncio.sleep(0.2)
            assert level(panel) == 12  # no wake: the panel stays dim...
            v = await state(port)
            assert v["dim"] is True and [n["id"] for n in v["notices"]] == ["uno"]  # ...and the page already has it
            status, _, body = await http(hport, "POST", "/notice", headers=headers,
                                         body=json.dumps({"title": "dos", "id": "dos", "wake": True}).encode())
            assert status == 200
            await wait_until(lambda: level(panel) == 255)  # wake: true lights the panel
            v = await state(port)
            assert v["dim"] is False and [n["id"] for n in v["notices"]] == ["dos", "uno"]
            await wait_until(lambda: level(panel) == 12)  # and it goes back down by itself
            assert [n["id"] for n in (await state(port))["notices"]] == ["dos", "uno"]  # the notices waited
            status, _, body = await http(port, "POST", "/dismiss", headers={"Content-Type": "application/json", "X-Buddy-Token": page_token},
                                         body=json.dumps({"id": "uno"}).encode())
            assert status == 200 and json.loads(body) == {"ok": True, "state": "dismissed"}
            assert json.loads((await http(hport, "GET", "/notice/uno", headers=auth()))[2]) == {"id": "uno", "state": "dismissed"}
            assert [n["id"] for n in (await state(port))["notices"]] == ["dos"]
            assert (await http(hport, "DELETE", "/notice/dos", headers=auth()))[0] == 200
            assert (await state(port))["notices"] == []
            assert (await http(hport, "POST", "/notice", headers=headers, body=json.dumps({"title": "tres"}).encode()))[0] == 200
        finally:
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 10) == 0

    run(scenario)
    out = capsys.readouterr().out
    assert "noche: despierta (aviso)" in out and "noche: despierta (toque)" not in out
    assert MARKER not in out  # what the service prints says why the screen woke, never what the notice said
    assert level(panel) == 255

    async def again():  # the daemon started again: the notice that was left is gone
        task, hport, port, _ = await boot(args, capsys)
        try:
            assert (await http(hport, "GET", "/notice/n-1", headers=auth()))[2] == b'{"id":"n-1","state":"unknown"}'
            assert json.loads((await http(port, path="/state"))[2])["notices"] == []
        finally:
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 10) == 0

    run(again)


def test_a_daemon_with_hermes_but_no_screen_still_takes_notices(tmp_path, monkeypatch, capsys):
    panel = make_panel(tmp_path / "bl")
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: FakeTransport(panel))
    token_file = tmp_path / "hermes.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    args = daemon_args(tmp_path, panel, "--no-ui", "--hermes-bind", "127.0.0.1", "--hermes-port", "0",
                       "--hermes-token-file", str(token_file))

    async def scenario():
        task = asyncio.ensure_future(buddy.run_daemon(args))
        out = ""
        try:
            for _ in range(500):
                out += capsys.readouterr().out
                if "Hermes en http://127.0.0.1:" in out:
                    break
                await asyncio.sleep(0.01)
            hport = int(re.search(r"Hermes en http://127\.0\.0\.1:(\d+)", out).group(1))
            headers = {"Content-Type": "application/json", **auth()}
            status, _, body = await http(hport, "POST", "/notice", headers=headers,
                                         body=json.dumps({"title": "uno", "wake": True}).encode())
            assert status == 200 and json.loads(body)["active"] == 1  # nobody to show it to, and no harm done
            assert level(panel) == 255  # night mode is off without a screen: the panel is never touched
        finally:
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 10) == 0

    run(scenario)
