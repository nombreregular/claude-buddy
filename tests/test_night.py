"""Night mode in the daemon: the window, how long the screen stays awake, the panel backlight, POST /wake,
the flags and the wiring in run_daemon. The page's half is in test_page_night.py.

Everything here runs without a panel, a Bluetooth stack or a browser: the backlight is a temporary directory laid out
like /sys/class/backlight/<name>/{brightness,max_brightness}, and the clocks are fake unless the test is about a timer."""
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy  # noqa: E402
import buddy_ui  # noqa: E402
from buddy_backlight import DEFAULT_NIGHT_LEVEL, FLOOR, REPAIR_TRIES, RETRY_S, Backlight  # noqa: E402
from buddy_core import Approval, BuddyCore, encode_line  # noqa: E402
from buddy_ui import AWAKE_S, WAKE_RATE_MAX, UIHub, UIServer, format_night, in_night, parse_night  # noqa: E402
from test_ui import PROMPT, Radio, decide_request, heartbeat, run, serving  # noqa: E402
from ui_client import Stream, http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NIGHT = (23 * 3600, 7 * 3600)  # the default window, 23:00-07:00


def at(hour, minute=0, second=0.0):
    """A second of the day."""
    return hour * 3600 + minute * 60 + second


# --- rigs ------------------------------------------------------------------------------------


class Night:
    """A core and a hub on one fake clock, a Pi whose own clock says `second`, and (maybe) a backlight."""

    def __init__(self, tmp_path, *, window=NIGHT, second=at(12), backlight=None, notice=None):
        self.t = 1000.0
        self.wall = 1_800_000_000.0
        self.second = float(second)  # the Pi's own second of the day
        self.sent = []
        self.core = BuddyCore(tmp_path / "state", self.sent.append, clock=lambda: self.t, wall=lambda: self.wall)
        self.hub = UIHub(night=window, backlight=backlight, clock=lambda: self.t, system_time=lambda: self.second, notice=notice)
        self.hub.bind(self.core, Radio())

    def advance(self, seconds):
        self.t += seconds
        self.wall += seconds
        self.second = (self.second + seconds) % 86400

    def mac_says(self, second):
        """Claude desktop tells the buddy what time it is there."""
        self.core.feed(encode_line({"time": [self.wall - self.wall % 86400 + second, 0]}))

    def view(self):
        return self.hub.view()

    def dim(self):
        return self.hub.view()["dim"]

    def ask(self, approval_id="h1", source="hermes"):
        """A permission request from a source that is not Bluetooth (so the link timeout does not drop it)."""
        got = []
        self.core.broker.submit(Approval(id=approval_id, tool="Bash", hint="ls", source=source), got.append)
        return got


@pytest.fixture
def rig(tmp_path):
    return Night(tmp_path, second=at(23, 30))  # night, nobody has touched it


def make_panel(root, name="10-0045", max_level=255, level=None):
    """A backlight laid out like the one in sysfs."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "max_brightness").write_text(f"{max_level}\n")
    (folder / "brightness").write_text(f"{max_level if level is None else level}\n")
    return folder


def level(panel):
    return int((panel / "brightness").read_text())


def poke(panel, value):
    """Somebody else sets the brightness: whatever the daemon does next, a rewrite would undo this."""
    (panel / "brightness").write_text(f"{value}\n")


def break_panel(panel):
    """The brightness file stops being writable (a directory in its place: open() fails for anyone, root included)."""
    (panel / "brightness").unlink()
    (panel / "brightness").mkdir()


def heal_panel(panel, value):
    """break_panel undone: the brightness file is there and writable again, holding `value`."""
    (panel / "brightness").rmdir()
    (panel / "brightness").write_text(f"{value}\n")


async def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it never happened")


def night_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "buddy.ui" and r.getMessage().startswith("noche")]


# --- the night window -------------------------------------------------------------------------


def test_the_flag_is_hh_mm_to_hh_mm_or_off():
    assert parse_night("23:00-07:00") == NIGHT
    assert parse_night("  7:05 - 9:30 ") == (at(7, 5), at(9, 30))
    assert parse_night("00:00-23:59") == (0, at(23, 59))
    assert parse_night("off") is None and parse_night("OFF") is None and parse_night(" Off ") is None
    for bad in ("", "23:00", "23-07", "noche", "24:00-07:00", "23:60-07:00", "23:00-07:00-08:00", "23:00 07:00",
                "23:00–07:00", "-1:00-07:00", "23:00-07:00 extra", "23:0-07:00", "١٢:٠٠-٠٧:٠٠"):
        with pytest.raises(ValueError):
            parse_night(bad)
    assert format_night(NIGHT) == "23:00-07:00" and format_night((at(7, 5), at(9, 30))) == "07:05-09:30"


@pytest.mark.parametrize("second, expected", [
    (at(22, 59, 59.9), False), (at(23), True), (at(23, 0, 0.1), True), (at(0), True), (at(3, 30), True),
    (at(6, 59, 59.9), True), (at(7), False), (at(12), False), (at(0) - 1e-9 + 86400, True),
])
def test_the_window_wraps_over_midnight_and_starts_at_the_start_not_at_the_end(second, expected):
    assert in_night(NIGHT, second) is expected


def test_a_window_inside_one_day_and_the_degenerate_ones():
    day = (at(1), at(5))
    assert [in_night(day, s) for s in (at(0, 59, 59), at(1), at(4, 59, 59), at(5), at(12))] == [False, True, True, False, False]
    for second in (0, at(7), at(12), at(23, 59, 59.9)):  # start == end is always night
        assert in_night((at(8), at(8)), second) is True


@pytest.mark.parametrize("second, expected", [
    (at(22, 59), False), (at(23, 0), True), (at(6, 59), True), (at(7, 0), False),
])
def test_the_hub_dims_inside_the_window(tmp_path, second, expected):
    assert Night(tmp_path, second=second).dim() is expected


def test_without_night_mode_the_screen_is_never_dim_and_the_brightness_is_not_touched(tmp_path):
    panel = make_panel(tmp_path / "bl", level=77)
    rig = Night(tmp_path, window=None, second=at(3), backlight=Backlight.find(str(panel)))
    v = rig.view()
    assert v["dim"] is False and v["backlight"] is False
    rig.hub.refresh()
    rig.hub.wake()
    rig.hub.close()
    assert level(panel) == 77


def test_the_view_always_says_whether_it_is_dim_and_whether_the_panel_does_the_dimming(tmp_path):
    for rig in (Night(tmp_path / "a", window=None), Night(tmp_path / "b"), Night(tmp_path / "c", second=at(1))):
        v = rig.view()
        assert v["dim"] in (True, False) and v["backlight"] in (True, False)
        assert v["backlight"] is False  # no panel behind it
    assert json.loads(Night(tmp_path / "d", second=at(1)).hub.current())["dim"] is True  # and it reaches the wire


def test_the_mac_clock_decides_and_the_pis_clock_covers_for_it(tmp_path):
    rig = Night(tmp_path, second=at(12))
    assert rig.dim() is False
    rig.second = at(23, 30)
    assert rig.dim() is True  # the Mac has not said what time it is: the Pi's own clock
    rig.mac_says(at(12))
    assert rig.dim() is False  # now the Mac's wins, whatever the Pi thinks
    rig.second = at(12)
    rig.mac_says(at(23, 30))
    assert rig.dim() is True
    rig.mac_says(at(22, 59, 30))
    assert rig.dim() is False
    rig.advance(31)  # the Mac's clock runs on from there; the Pi's moves to 12:00:31 and has no say
    rig.second = at(12)
    assert rig.dim() is True


# --- awake ----------------------------------------------------------------------------------


def test_a_wake_keeps_the_screen_awake_for_thirty_seconds(rig):
    assert AWAKE_S == 30.0 and rig.dim() is True
    assert rig.hub.wake() is False  # what it answers to the page: no longer dim
    assert rig.dim() is False
    rig.advance(AWAKE_S - 0.1)
    assert rig.dim() is False
    rig.advance(0.2)
    assert rig.dim() is True


def test_waking_again_restarts_the_time_and_never_shortens_it(rig):
    rig.hub.wake()
    rig.advance(20)
    rig.hub.wake()
    rig.advance(29)  # 49 s after the first, 29 s after the second
    assert rig.dim() is False
    rig.advance(1.1)
    assert rig.dim() is True


def test_waking_outside_the_night_changes_nothing_you_can_see(tmp_path):
    rig = Night(tmp_path, second=at(12))
    assert rig.hub.wake() is False and rig.dim() is False
    rig.second = at(23, 10)  # the night starts while it is still awake from the tap: it stays awake until that runs out
    rig.advance(1)
    assert rig.dim() is False
    rig.advance(AWAKE_S)
    assert rig.dim() is True


def test_a_pending_request_keeps_the_screen_awake_and_the_last_one_to_go_starts_the_countdown(rig):
    assert rig.dim() is True
    rig.ask("h1")
    assert rig.dim() is False and len(rig.view()["approvals"]) == 1  # it wakes up
    rig.advance(600)  # ten minutes unanswered: still awake
    assert rig.dim() is False
    rig.core.broker.decide("hermes:h1", "once")
    rig.advance(AWAKE_S - 0.1)
    assert rig.dim() is False
    rig.advance(0.2)
    assert rig.dim() is True


def test_while_one_request_is_left_the_other_answered_does_not_start_the_countdown(rig):
    rig.ask("h1")
    rig.ask("h2")
    rig.core.broker.decide("hermes:h1", "deny")
    rig.advance(120)
    assert rig.dim() is False  # h2 is still waiting
    rig.core.broker.decide("hermes:h2", "once")
    rig.advance(AWAKE_S + 1)
    assert rig.dim() is True


def test_a_request_that_expires_or_is_withdrawn_also_leaves_thirty_seconds_behind(rig):
    rig.ask("h1")
    rig.advance(400)
    assert rig.core.broker.withdraw("hermes", "h1") is True  # what Hermes' ttl or the Mac dropping the prompt do
    rig.advance(AWAKE_S - 1)
    assert rig.dim() is False
    rig.advance(2)
    assert rig.dim() is True


def test_every_way_to_wake_it_goes_through_hub_wake(rig, monkeypatch):
    """A future feature (Hermes notices) must only need to call hub.wake(): a tap and a request both do."""
    seen = []
    monkeypatch.setattr(rig.hub, "wake", lambda reason="toque": seen.append(reason) or False)
    rig.ask("h1")
    rig.core.broker.decide("hermes:h1", "once")
    assert seen == ["permiso", "permiso"]  # the request, and its answer (which starts the countdown)
    server = UIServer(rig.hub, port=0)

    async def scenario():
        await server.start()
        try:
            status, _, body = await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token})
            assert status == 200 and json.loads(body) == {"ok": True, "dim": False}
        finally:
            await server.stop()

    run(scenario)
    assert seen == ["permiso", "permiso", "toque"]


def test_dim_and_wake_are_logged_once_each_and_not_on_every_refresh(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="buddy.ui")
    rig = Night(tmp_path, second=at(22, 59, 59))
    rig.hub.refresh()
    assert night_lines(caplog) == []  # day, awake: nothing to say
    rig.advance(1)
    for _ in range(4):
        rig.hub.refresh()
    assert night_lines(caplog) == ["noche: pantalla tenue"]
    rig.hub.wake()
    rig.hub.refresh()
    rig.advance(AWAKE_S + 1)
    rig.hub.refresh()
    rig.ask("h1")
    rig.advance(5)
    rig.core.broker.decide("hermes:h1", "once")
    rig.advance(AWAKE_S + 1)
    rig.hub.refresh()
    rig.second = at(6, 59, 59)
    rig.hub.refresh()
    rig.advance(2)  # 07:00:01
    rig.hub.refresh()
    assert night_lines(caplog) == [
        "noche: pantalla tenue",
        "noche: despierta (toque)",
        "noche: pantalla tenue",
        "noche: despierta (permiso)",
        "noche: pantalla tenue",
        "noche: terminó, pantalla normal",
    ]


def test_the_transitions_go_to_the_notice_when_there_is_one_so_the_service_journal_shows_them(tmp_path, caplog):
    """The service runs without -v, where INFO never reaches the journal: the daemon hands the hub a way to say it out loud."""
    caplog.set_level(logging.INFO, logger="buddy.ui")
    said = []
    rig = Night(tmp_path, second=at(23, 30), notice=said.append)
    rig.hub.refresh()
    rig.hub.wake()
    rig.advance(AWAKE_S + 1)
    rig.hub.refresh()
    rig.ask("h1")
    assert said == ["noche: pantalla tenue", "noche: despierta (toque)", "noche: pantalla tenue", "noche: despierta (permiso)"]
    assert night_lines(caplog) == []  # said once, not twice


def test_a_notice_that_fails_cannot_break_the_screen(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="buddy.ui")

    def broken(text):
        raise BrokenPipeError("stdout is gone")

    rig = Night(tmp_path, second=at(23, 30), notice=broken)
    rig.hub.refresh()
    rig.hub.wake()
    assert rig.dim() is False  # still works
    assert night_lines(caplog) == ["noche: pantalla tenue", "noche: despierta (toque)"]  # and the log has it after all


# --- pairing ---------------------------------------------------------------------------------------


def test_a_pairing_code_wakes_the_screen_and_keeps_it_awake_while_it_is_shown(rig, caplog):
    caplog.set_level(logging.INFO, logger="buddy.ui")
    rig.hub.refresh()  # it has been dark since before the code came
    assert rig.dim() is True
    rig.hub.set_passkey("482915")
    v = rig.view()
    assert v["dim"] is False and v["pairing"] == {"code": "482915", "entered": 0}
    rig.advance(10 * 60)  # the person takes their time at the Mac
    assert rig.dim() is False
    rig.hub.set_passkey("482915", 3)  # a digit typed
    rig.advance(AWAKE_S * 3)
    assert rig.dim() is False
    rig.hub.set_passkey(None)  # paired (or given up): the result stays on screen for the usual half minute
    assert rig.view()["pairing"] is None
    rig.advance(AWAKE_S - 0.1)
    assert rig.dim() is False
    rig.advance(0.2)
    assert rig.dim() is True
    assert "noche: despierta (emparejar)" in night_lines(caplog)


def test_the_radio_saying_there_is_no_code_wakes_nothing(rig):
    rig.hub.set_passkey(None)  # BlueZ cancels "the code" whenever a link drops, pairing or not
    rig.hub.set_passkey(None, 0)
    assert rig.dim() is True


def test_a_pairing_code_by_day_changes_nothing_about_the_dim_state(tmp_path):
    rig = Night(tmp_path, second=at(12))
    rig.hub.set_passkey("123456")
    assert rig.dim() is False
    rig.second = at(23, 10)
    rig.advance(1)
    assert rig.dim() is False  # the night started with the code on screen: still awake
    rig.hub.set_passkey(None)
    rig.advance(AWAKE_S + 1)
    assert rig.dim() is True


def test_a_code_on_screen_has_no_awake_timer_but_the_end_of_the_code_starts_one(tmp_path, monkeypatch):
    monkeypatch.setattr(buddy_ui, "AWAKE_S", 0.25)

    async def scenario():
        hub, _, published = live_hub(tmp_path, lambda: at(23, 30))
        hub.refresh()
        assert (await next_state(published, lambda v: True))["dim"] is True
        hub.set_passkey("111222")
        assert (await next_state(published, lambda v: v["pairing"] is not None))["dim"] is False
        await asyncio.sleep(0.6)  # more than twice the awake time, with nothing else happening
        assert hub.view()["dim"] is False and hub.current() and json.loads(hub.current())["dim"] is False
        hub.set_passkey(None)
        assert (await next_state(published, lambda v: v["pairing"] is None))["dim"] is False
        assert (await next_state(published, lambda v: v["dim"] is True))["dim"] is True  # and then it goes dark by itself
        hub.close()

    run(scenario)


# --- the timer ----------------------------------------------------------------------------------


def ticking(second):
    """A Pi clock that starts at `second` and keeps going with the real time."""
    start = time.monotonic()
    return lambda: second + (time.monotonic() - start)


def live_hub(tmp_path, system_time, **kw):
    """A hub on the real clock (the fake ones cannot say when a timer fires), bound to a plain core."""
    core = BuddyCore(tmp_path / "state", lambda msg: None)
    hub = UIHub(night=NIGHT, system_time=system_time, **kw)
    hub.bind(core, Radio())
    published = asyncio.Queue()
    hub.subscribe(lambda text: published.put_nowait(json.loads(text)))
    return hub, core, published


async def next_state(published, predicate, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        state = await asyncio.wait_for(published.get(), max(0.01, end - asyncio.get_running_loop().time()))
        if predicate(state):
            return state


def test_the_screen_goes_dim_again_by_itself_when_the_awake_time_runs_out(tmp_path, monkeypatch):
    monkeypatch.setattr(buddy_ui, "AWAKE_S", 0.3)

    async def scenario():
        hub, _, published = live_hub(tmp_path, lambda: at(23, 30))
        hub.refresh()
        assert (await next_state(published, lambda v: True))["dim"] is True
        started = time.monotonic()
        hub.wake()
        assert (await next_state(published, lambda v: v["dim"] is False))["dim"] is False
        # nothing else touches the hub from here on: no tick, no event, no request
        assert (await next_state(published, lambda v: v["dim"] is True))["dim"] is True
        assert 0.25 < time.monotonic() - started < 1.5
        hub.close()

    run(scenario)


def test_the_screen_goes_dim_when_the_night_starts_and_awake_when_it_ends_without_any_help(tmp_path):
    async def scenario():
        hub, _, published = live_hub(tmp_path, ticking(at(22, 59, 59.7)))
        hub.refresh()
        assert (await next_state(published, lambda v: True))["dim"] is False
        assert (await next_state(published, lambda v: v["dim"] is True))["dim"] is True  # 23:00:00 came
        hub.close()
        morning, _, published = live_hub(tmp_path / "m", ticking(at(6, 59, 59.7)))
        morning.refresh()
        assert (await next_state(published, lambda v: True))["dim"] is True
        assert (await next_state(published, lambda v: v["dim"] is False))["dim"] is False  # 07:00:00 came
        morning.close()

    run(scenario)


def test_there_is_at_most_one_timer_and_a_change_replaces_it(tmp_path):
    async def scenario():
        loop = asyncio.get_running_loop()
        made = []
        real = loop.call_later

        def spy(delay, callback, *args):
            handle = real(delay, callback, *args)
            if getattr(callback, "__name__", "") == "_on_timer":  # the hub's timers, not asyncio.sleep's
                made.append(handle)
            return handle

        loop.call_later = spy
        try:
            hub, _, _ = live_hub(tmp_path, ticking(at(23, 30)))
            hub.refresh()  # night, awake never: the next change is 07:00
            first = hub._timer
            assert first is not None and [h for h in made if not h.cancelled()] == [first]
            for _ in range(5):  # the daemon refreshes every second: the timer is already right, so it stays
                await asyncio.sleep(0.03)
                hub.refresh()
            assert hub._timer is first and len(made) == 1
            hub.wake()
            hub.refresh()
            assert first.cancelled() and [h for h in made if not h.cancelled()] == [hub._timer]
            await asyncio.sleep(0.05)
            hub.wake()  # later wake: the end of the awake time moved, so the timer does too
            hub.refresh()
            assert [h for h in made if not h.cancelled()] == [hub._timer] and len(made) == 3
            hub.close()
            assert hub._timer is None and all(h.cancelled() for h in made)  # cancelled on shutdown
            hub.wake()
            hub.refresh()
            await asyncio.sleep(0.02)
            assert hub._timer is None and len(made) == 3  # and nothing arms it again afterwards
        finally:
            del loop.call_later

    run(scenario)


def test_no_timers_without_night_mode_or_without_a_loop(tmp_path):
    async def scenario():
        core = BuddyCore(tmp_path / "state", lambda msg: None)
        hub = UIHub(night=None)
        hub.bind(core, Radio())
        hub.wake()
        hub.refresh()
        assert hub._timer is None

    run(scenario)
    rig = Night(tmp_path / "sync")  # no running loop: whoever refreshes by hand gets the state, and nothing is armed
    rig.hub.wake()
    rig.hub.refresh()
    assert rig.hub._timer is None


# --- the backlight ----------------------------------------------------------------------------


def test_it_finds_the_first_panel_it_can_write_and_skips_the_rest(tmp_path):
    root = tmp_path / "backlight"
    bad_max = make_panel(root, "a-no-max")
    (bad_max / "max_brightness").write_text("not a number\n")
    make_panel(root, "b-zero", max_level=0)
    no_brightness = make_panel(root, "c-no-file")
    (no_brightness / "brightness").unlink()
    (root / "d-a-file").write_text("not a panel")
    good = make_panel(root, "e-panel", max_level=200)
    make_panel(root, "f-second", max_level=100)
    found = Backlight.find("auto", root=root)
    assert found is not None and found.directory == good and found.day == 200
    assert Backlight.find("auto", root=tmp_path / "nothing here") is None  # the Mac has no /sys/class/backlight
    (tmp_path / "a-file").write_text("not a folder")
    assert Backlight.find("auto", root=tmp_path / "a-file") is None
    assert Backlight.find("off", root=root) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
def test_a_panel_this_user_cannot_write_is_not_picked(tmp_path):
    root = tmp_path / "backlight"
    locked = make_panel(root, "a-locked")
    os.chmod(locked / "brightness", 0o444)
    mine = make_panel(root, "b-mine")
    assert Backlight.find("auto", root=root).directory == mine
    os.chmod(mine / "brightness", 0o444)
    assert Backlight.find("auto", root=root) is None


def test_an_explicit_directory_is_used_as_given_or_refused_with_a_warning(tmp_path, caplog):
    panel = make_panel(tmp_path / "anywhere", "my-panel", max_level=100)
    found = Backlight.find(str(panel), night_level=30)
    assert found.directory == panel and found.day == 100 and found.night == 30
    caplog.set_level(logging.WARNING, logger="buddy.backlight")
    assert Backlight.find(str(tmp_path / "missing")) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]  # it asked for that one, so it hears about it


def test_day_is_the_maximum_and_night_is_clamped_between_the_floor_and_the_maximum(tmp_path):
    assert (FLOOR, DEFAULT_NIGHT_LEVEL) == (4, 12)
    panel = make_panel(tmp_path, max_level=255)
    levels = {n: Backlight(panel, 255, n).night for n in (-5, 0, 1, 3, 4, 5, 12, 255, 256, 9999)}
    assert levels == {-5: 4, 0: 4, 1: 4, 3: 4, 4: 4, 5: 5, 12: 12, 255: 255, 256: 255, 9999: 255}
    assert Backlight.find(str(panel)).night == 12 and Backlight.find(str(panel)).day == 255
    found = Backlight(make_panel(tmp_path / "small", max_level=3), 3, 1)  # a panel that cannot even reach the floor: its maximum
    assert (found.night, found.day) == (3, 3)


def test_a_panel_with_nowhere_to_dim_to_is_not_taken_so_the_page_darkens_itself(tmp_path, caplog):
    """If night would equal day (an on/off backlight, a tiny range, a night level at the maximum) the screen would never get
    dimmer and the view would still claim the panel does the dimming, so the page would leave it bright all night."""
    for name, top in (("gpio", 1), ("tiny", 3), ("floor", FLOOR), ("at-night-level", DEFAULT_NIGHT_LEVEL)):
        assert Backlight.find(str(make_panel(tmp_path / name, max_level=top))) is None, name
    one_up = make_panel(tmp_path / "one-up", max_level=DEFAULT_NIGHT_LEVEL + 1)
    assert Backlight.find(str(one_up)).night == DEFAULT_NIGHT_LEVEL
    full = make_panel(tmp_path / "full", max_level=255)
    assert Backlight.find(str(full), night_level=255) is None and Backlight.find(str(full), night_level=9999) is None
    assert Backlight.find(str(full), night_level=254).night == 254
    root = tmp_path / "sys"
    make_panel(root, "a-gpio", max_level=1)
    good = make_panel(root, "b-real", max_level=255)
    assert Backlight.find("auto", root=root).directory == good  # auto goes on to the next panel
    only = tmp_path / "only"
    make_panel(only, "gpio", max_level=1)
    assert Backlight.find("auto", root=only) is None
    caplog.set_level(logging.WARNING, logger="buddy.backlight")
    caplog.clear()
    assert Backlight.find(str(tmp_path / "gpio" / "10-0045")) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]  # asked for by name, so it hears why not
    assert "bajar" in caplog.records[0].getMessage()


def test_it_writes_only_when_the_level_changes(tmp_path):
    panel = make_panel(tmp_path)
    back = Backlight.find(str(panel), night_level=12)
    assert back.set_dim(True) is True and level(panel) == 12
    poke(panel, 99)
    back.set_dim(True)  # same target: not written again
    assert level(panel) == 99
    back.set_dim(False)
    assert level(panel) == 255
    poke(panel, 98)
    back.set_dim(False)
    assert level(panel) == 98
    back.set_dim(True)
    assert level(panel) == 12


def test_the_first_call_always_writes_so_a_crash_while_dim_is_repaired_at_the_next_start(tmp_path):
    panel = make_panel(tmp_path, level=12)  # the last run died dim: 12 is what is in the file
    back = Backlight.find(str(panel))
    back.set_dim(False)
    assert level(panel) == 255  # day is the maximum, not "what it was when we started"
    panel2 = make_panel(tmp_path / "other", level=7)
    Backlight.find(str(panel2)).set_dim(True)
    assert level(panel2) == 12


def test_restore_puts_the_day_level_back_for_good(tmp_path):
    panel = make_panel(tmp_path)
    back = Backlight.find(str(panel))
    back.set_dim(True)
    assert level(panel) == 12 and back.active is True
    back.restore()
    assert level(panel) == 255 and back.active is False
    assert back.set_dim(True) is False and level(panel) == 255  # nothing dims it after the daemon is done
    back.restore()  # twice is fine
    assert level(panel) == 255


def test_restore_leaves_the_panel_alone_when_the_daemon_never_moved_it(tmp_path):
    panel = make_panel(tmp_path, level=77)
    Backlight.find(str(panel)).restore()
    assert level(panel) == 77
    back = Backlight.find(str(panel))
    back.set_dim(False)
    poke(panel, 77)
    back.restore()  # it was already at day when it stopped: nothing to put back
    assert level(panel) == 77


def test_a_write_that_fails_disables_it_with_one_log_line_and_never_raises(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="buddy.backlight")
    panel = make_panel(tmp_path)
    back = Backlight.find(str(panel))
    caplog.clear()
    break_panel(panel)
    assert back.set_dim(True) is False and back.enabled is False and back.active is False
    for dim in (False, True, False):
        assert back.set_dim(dim) is False
    back.restore()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "no puedo escribir" in warnings[0].getMessage()


def test_a_failing_restore_does_not_raise_either(tmp_path):
    panel = make_panel(tmp_path)
    back = Backlight.find(str(panel))
    back.set_dim(True)
    break_panel(panel)
    back.restore()  # the panel went away while it was dim
    assert back.active is False


def test_a_day_write_that_fails_is_tried_again_until_the_panel_is_back_at_full_light(tmp_path, caplog):
    """The write that fails can be the one that wakes the panel: it must not stay at the night level for good."""
    caplog.set_level(logging.INFO, logger="buddy.backlight")
    panel = make_panel(tmp_path)
    t = [100.0]
    back = Backlight(panel, 255, 12, clock=lambda: t[0])
    back.set_dim(True)
    assert level(panel) == 12
    break_panel(panel)
    assert back.set_dim(False) is False and back.active is False  # no more dimming from here on...
    heal_panel(panel, 12)
    back.set_dim(False)
    assert level(panel) == 12  # ...but not tried again in the same breath
    t[0] += RETRY_S - 0.01
    back.set_dim(False)
    assert level(panel) == 12
    t[0] += 0.02
    assert back.set_dim(False) is False  # still off for dimming: the contract says any failure turns it off
    assert level(panel) == 255 and back.active is False
    poke(panel, 99)
    t[0] += 10
    back.set_dim(False)
    back.set_dim(True)
    assert level(panel) == 99  # repaired once, then left alone for good
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "no puedo escribir" in warnings[0].getMessage()


def test_a_panel_that_stays_broken_is_tried_for_a_while_then_left_with_one_more_line(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="buddy.backlight")
    panel = make_panel(tmp_path)
    t = [0.0]
    back = Backlight(panel, 255, 12, clock=lambda: t[0])
    back.set_dim(True)
    break_panel(panel)
    back.set_dim(False)
    for _ in range(REPAIR_TRIES + 20):
        t[0] += RETRY_S
        back.set_dim(False)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2 and "no puedo escribir" in warnings[0] and "dejo de intentar" in warnings[1]
    heal_panel(panel, 12)
    t[0] += 10 * RETRY_S
    back.set_dim(False)
    assert level(panel) == 12  # it gave up: nothing writes any more (the shutdown still tries once, as before)
    back.restore()
    assert level(panel) == 255


def test_a_failed_night_write_owes_nothing(tmp_path):
    """The panel never left the day level, so there is nothing to put back."""
    panel = make_panel(tmp_path)
    t = [0.0]
    back = Backlight(panel, 255, 12, clock=lambda: t[0])
    back.set_dim(False)
    break_panel(panel)
    assert back.set_dim(True) is False
    heal_panel(panel, 77)
    t[0] += 100
    back.set_dim(False)
    back.set_dim(True)
    assert level(panel) == 77


def test_a_first_day_write_that_fails_is_repaired_too(tmp_path):
    """The start-up write is the one that repairs a panel the last run left dim; if it fails, it is tried again."""
    panel = make_panel(tmp_path, level=12)
    break_panel(panel)
    t = [0.0]
    back = Backlight(panel, 255, 12, clock=lambda: t[0])
    assert back.set_dim(False) is False
    heal_panel(panel, 12)
    t[0] += RETRY_S
    back.set_dim(False)
    assert level(panel) == 255


def test_after_a_restore_nothing_is_repaired_or_written(tmp_path):
    panel = make_panel(tmp_path)
    t = [0.0]
    back = Backlight(panel, 255, 12, clock=lambda: t[0])
    back.set_dim(True)
    break_panel(panel)
    back.set_dim(False)
    back.restore()  # one last try, which fails
    heal_panel(panel, 12)
    t[0] += 100
    back.set_dim(False)
    assert level(panel) == 12


# --- the backlight and the hub ---------------------------------------------------------------


def test_the_backlight_follows_the_dim_state(tmp_path):
    panel = make_panel(tmp_path / "bl")
    rig = Night(tmp_path, second=at(22, 59, 50), backlight=Backlight.find(str(panel), night_level=12))
    rig.hub.refresh()
    assert level(panel) == 255 and (rig.view()["dim"], rig.view()["backlight"]) == (False, True)
    rig.advance(11)  # 23:00:01
    rig.hub.refresh()
    assert level(panel) == 12 and (rig.view()["dim"], rig.view()["backlight"]) == (True, True)
    rig.hub.wake()  # no loop here, so the hub publishes at once
    assert level(panel) == 255 and rig.view()["dim"] is False  # dim off: day brightness immediately
    rig.advance(AWAKE_S + 1)
    rig.hub.refresh()
    assert level(panel) == 12
    rig.ask("h1")
    assert level(panel) == 255  # a permission request lights it up
    rig.core.broker.decide("hermes:h1", "once")
    rig.advance(AWAKE_S + 1)
    rig.hub.refresh()
    assert level(panel) == 12
    rig.advance(8 * 3600)  # the morning
    rig.hub.refresh()
    assert level(panel) == 255 and rig.view()["dim"] is False


def test_an_unchanged_state_does_not_write_again(tmp_path):
    panel = make_panel(tmp_path / "bl")
    rig = Night(tmp_path, backlight=Backlight.find(str(panel)))
    rig.hub.refresh()
    assert level(panel) == 255
    poke(panel, 99)
    for _ in range(10):  # the daemon refreshes every second
        rig.advance(1)
        rig.hub.refresh()
    assert level(panel) == 99


def test_the_screen_never_goes_below_the_floor_whatever_is_asked(tmp_path):
    panel = make_panel(tmp_path / "bl")
    rig = Night(tmp_path, second=at(2), backlight=Backlight.find(str(panel), night_level=0))
    rig.hub.refresh()
    assert level(panel) == FLOOR and rig.view()["dim"] is True


def test_when_the_backlight_stops_working_the_view_says_so_so_the_page_darkens_itself(tmp_path, caplog):
    panel = make_panel(tmp_path / "bl")
    rig = Night(tmp_path, second=at(22, 59, 50), backlight=Backlight.find(str(panel)))
    rig.hub.refresh()
    break_panel(panel)
    rig.advance(11)
    caplog.set_level(logging.INFO)
    rig.hub.refresh()
    v = rig.view()
    assert v["dim"] is True and v["backlight"] is False
    assert json.loads(rig.hub.current())["backlight"] is False  # and that is what reaches the page
    assert len([r for r in caplog.records if r.name == "buddy.backlight" and r.levelno >= logging.WARNING]) == 1
    rig.advance(AWAKE_S)
    rig.hub.wake()
    rig.hub.refresh()
    assert rig.view()["dim"] is False  # the rest of night mode carries on


def test_a_wake_whose_write_fails_does_not_leave_the_panel_dark_under_a_permission_request(tmp_path):
    panel = make_panel(tmp_path / "bl")
    rig = Night(tmp_path, second=at(23, 30))
    rig.hub.backlight = Backlight(panel, 255, 12, clock=lambda: rig.t)
    rig.hub.refresh()
    assert level(panel) == 12 and rig.view()["dim"] is True
    break_panel(panel)  # a hiccup on the way to the panel exactly when somebody taps it
    rig.hub.wake()
    v = rig.view()
    assert v["dim"] is False and v["backlight"] is False  # the page may not count on the panel any more
    heal_panel(panel, 12)
    rig.advance(RETRY_S)
    rig.hub.refresh()  # the daemon refreshes every second: that is all the retry needs
    assert level(panel) == 255
    rig.advance(AWAKE_S + 1)
    rig.hub.refresh()
    v = rig.view()
    assert level(panel) == 255 and v["dim"] is True and v["backlight"] is False  # it does not dim again: the page does it
    rig.ask("h1")
    assert level(panel) == 255 and rig.view()["dim"] is False


def test_the_morning_write_that_fails_is_repaired_too(tmp_path):
    panel = make_panel(tmp_path / "bl")
    rig = Night(tmp_path, second=at(6, 59, 50))
    rig.hub.backlight = Backlight(panel, 255, 12, clock=lambda: rig.t)
    rig.hub.refresh()
    assert level(panel) == 12
    break_panel(panel)
    rig.advance(11)  # 07:00:01
    rig.hub.refresh()
    assert rig.view()["dim"] is False
    heal_panel(panel, 12)
    rig.advance(RETRY_S)
    rig.hub.refresh()
    assert level(panel) == 255


def test_closing_the_hub_puts_the_brightness_back_and_nothing_dims_it_afterwards(tmp_path):
    panel = make_panel(tmp_path / "bl")
    rig = Night(tmp_path, second=at(1), backlight=Backlight.find(str(panel)))
    rig.hub.refresh()
    assert level(panel) == 12
    rig.hub.close()
    assert level(panel) == 255
    rig.hub.close()
    rig.hub.refresh()  # a page that asks for the state while the daemon is shutting down
    assert level(panel) == 255 and rig.hub.current()


# --- POST /wake -------------------------------------------------------------------------------


def test_posting_to_wake_wakes_the_screen_and_answers_with_the_new_state(rig):
    async def scenario():
        async with serving(rig) as server:
            assert rig.dim() is True
            status, headers, body = await http(server, "POST", "/wake", body=b"{}", headers={
                "Content-Type": "application/json", "X-Buddy-Token": server.token})
            assert status == 200 and json.loads(body) == {"ok": True, "dim": False}
            assert headers["content-type"] == "application/json" and headers["cache-control"] == "no-store"
            assert rig.dim() is False
            rig.advance(AWAKE_S + 1)
            assert rig.dim() is True

    run(scenario)


def test_the_body_is_optional_and_ignored(rig):
    async def scenario():
        async with serving(rig) as server:
            token = {"X-Buddy-Token": server.token}
            json_type = {"Content-Type": "application/json", **token}
            assert (await http(server, "POST", "/wake", headers=token))[0] == 200  # nothing at all
            for body in (b"{}", b"", b"null", b"[1,2]", b'{"dim": true, "key": "claude:req_1", "decision": "once"}', b"not json", b"\xff"):
                status, _, answer = await http(server, "POST", "/wake", headers=json_type, body=body)
                assert status == 200 and json.loads(answer)["ok"] is True, body

    run(scenario)


def test_it_wakes_and_does_nothing_else(tmp_path):
    rig = Night(tmp_path, second=at(23, 30))
    rig.core.feed(encode_line(heartbeat(waiting=1, prompt=PROMPT)))

    async def scenario():
        async with serving(rig) as server:
            body = json.dumps({"key": "claude:req_1", "decision": "once"}).encode()
            for payload in (body, b"{}"):
                status, _, _ = await http(server, "POST", "/wake", body=payload, headers={
                    "Content-Type": "application/json", "X-Buddy-Token": server.token})
                assert status == 200
            assert [a["key"] for a in rig.view()["approvals"]] == ["claude:req_1"]  # still waiting for a person
            assert not [m for m in rig.sent if m.get("cmd") == "permission"]  # the Mac heard nothing
            assert rig.core.state.approvals == 0 and rig.core.state.denials == 0

    run(scenario)


def test_without_the_right_token_it_does_not_wake(rig):
    async def scenario():
        async with serving(rig) as server:
            for token in ("", "wrong", server.token[:-1], server.token + "x", "ñandú"):
                headers = {"Content-Type": "application/json", "X-Buddy-Token": token}
                assert (await http(server, "POST", "/wake", headers=headers, body=b"{}"))[0] == 403, token
            assert (await http(server, "POST", "/wake", headers={"Content-Type": "application/json"}, body=b"{}"))[0] == 403
            status, _, body = await http(server, "POST", "/wake", headers={"X-Buddy-Token": "no"})
            assert status == 403 and json.loads(body) == {"ok": False, "error": "bad token"}  # the same shape as /decide
            assert rig.dim() is True

    run(scenario)


def test_only_post_wakes_the_screen(rig):
    async def scenario():
        async with serving(rig) as server:
            for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
                status, _, _ = await http(server, method, "/wake", headers={"X-Buddy-Token": server.token})
                assert status == 405, method
            assert rig.dim() is True

    run(scenario)


def test_a_plain_html_form_cannot_wake_it_either(rig):
    async def scenario():
        async with serving(rig) as server:
            for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
                headers = {"X-Buddy-Token": server.token, "Content-Type": ctype}
                assert (await http(server, "POST", "/wake", headers=headers, body=b"a=b"))[0] == 415, ctype
                assert (await http(server, "POST", "/wake", headers=headers))[0] == 415, ctype  # not even without a body
            assert (await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token}, body=b"a=b"))[0] == 415
            assert rig.dim() is True

    run(scenario)


def test_the_host_and_the_origin_are_checked_like_for_decide(rig):
    async def scenario():
        async with serving(rig) as server:
            port = server.port
            for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", "", f"10.0.0.5:{port}"):
                status, _, _ = await http(server, "POST", "/wake", host=host or None, body=b"{}", headers={
                    "Host": host, "Content-Type": "application/json", "X-Buddy-Token": server.token})
                assert status == 403, host
            for origin in ("http://evil.example", "https://127.0.0.1:%d" % port, "http://127.0.0.1:%d" % (port + 1), "null"):
                status, _, _ = await http(server, "POST", "/wake", body=b"{}", headers={
                    "Origin": origin, "Content-Type": "application/json", "X-Buddy-Token": server.token})
                assert status == 403, origin
            assert rig.dim() is True
            for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):  # the page itself, which sends its own origin
                status, _, _ = await http(server, "POST", "/wake", body=b"{}", headers={
                    "Host": host, "Origin": f"http://{host}", "Content-Type": "application/json", "X-Buddy-Token": server.token})
                assert status == 200, host

    run(scenario)


def test_an_oversized_or_chunked_body_is_refused_before_anything_wakes(rig):
    async def scenario():
        async with serving(rig) as server:
            host = f"Host: 127.0.0.1:{server.port}\r\nX-Buddy-Token: {server.token}\r\n".encode()
            assert (await http(server, raw=b"POST /wake HTTP/1.1\r\n" + host + b"Content-Length: 99999999\r\n\r\n"))[0] == 413
            assert (await http(server, raw=b"POST /wake HTTP/1.1\r\n" + host + b"Transfer-Encoding: chunked\r\n\r\n"))[0] == 400
            assert rig.dim() is True

    run(scenario)


def test_it_only_listens_on_loopback_like_the_rest_of_the_screen(rig):
    for host in ("0.0.0.0", "192.168.1.5", "buddy.local", ""):
        with pytest.raises(ValueError):
            UIServer(rig.hub, host=host)

    async def scenario():
        async with serving(rig) as server:
            assert server._server.sockets[0].getsockname()[0] == "127.0.0.1"

    run(scenario)


def test_a_flood_of_wakes_is_throttled_and_leaves_decide_and_the_stream_alone(rig):
    async def scenario():
        now = [500.0]
        got = rig.ask("h1")
        async with serving(rig, clock=lambda: now[0]) as server:
            stream = await Stream.open(server)
            try:
                first = await stream.state()
                assert first["dim"] is False  # the request is pending, so it is awake
                for _ in range(30):  # bad tokens spend nothing of the budget
                    assert (await http(server, "POST", "/wake", headers={"X-Buddy-Token": "no"}))[0] == 403
                statuses = [(await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token}))[0]
                            for _ in range(WAKE_RATE_MAX + 5)]
                assert statuses == [200] * WAKE_RATE_MAX + [429] * 5
                status, _, body = await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token})
                assert status == 429 and json.loads(body) == {"ok": False, "error": "too many requests"}
                # the flood did not starve the rest: the stream is open, the state is served, the answer goes through
                assert (await http(server, "GET", "/state"))[0] == 200
                other = await Stream.open(server)
                assert other.status == 200
                other.close()
                assert (await decide_request(server, "hermes:h1", "once"))[0] == 200 and got == ["once"]
                assert (await stream.state(lambda v: v["approvals"] == []))["approvals"] == []
                now[0] += buddy_ui.WAKE_RATE_WINDOW_S + 0.5  # and it is back when the window has passed
                assert (await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token}))[0] == 200
            finally:
                stream.close()

    run(scenario)


def test_a_wake_reaches_the_open_page_through_the_stream(rig):
    async def scenario():
        async with serving(rig) as server:
            stream = await Stream.open(server)
            try:
                assert (await stream.state())["dim"] is True
                await http(server, "POST", "/wake", headers={"X-Buddy-Token": server.token})
                assert (await stream.state(lambda v: v["dim"] is False))["dim"] is False
            finally:
                stream.close()

    run(scenario)


# --- the flags --------------------------------------------------------------------------------


def parse(*argv):
    return buddy.parse_args(["--state-dir", "/nonexistent/buddy-state", *argv])


def test_the_defaults_are_night_from_23_to_7_at_level_12_with_the_backlight_on_auto():
    args = parse()
    assert (args.night, args.night_level, args.backlight) == (NIGHT, 12, "auto")
    assert parse("check").command == "check" and parse("check").night == NIGHT  # the other commands carry them along unharmed
    assert parse("forget-all").night_level == 12


def test_the_flags_take_their_values():
    args = parse("--night", "22:30-06:15", "--night-level", "30", "--backlight", "/sys/class/backlight/x")
    assert (args.night, args.night_level, args.backlight) == ((at(22, 30), at(6, 15)), 30, "/sys/class/backlight/x")
    assert parse("--night", "off").night is None
    assert parse("--night", "08:00-08:00").night == (at(8), at(8))
    assert parse("--night-level", "-3").night_level == -3  # clamped by the backlight, not refused here
    assert parse("--backlight", "off").backlight == "off" and parse("--backlight", "auto").backlight == "auto"
    assert parse("check", "--night", "off").command == "check"


@pytest.mark.parametrize("argv", [
    ["--night", "nope"], ["--night", "25:00-07:00"], ["--night", "23:00"], ["--night", ""], ["--night"],
    ["--night-level", "abc"], ["--night-level", "1.5"], ["--night-level"],
    ["--backlight"], ["--backlight", ""], ["--backlight", "  "],
])
def test_a_bad_flag_is_an_argparse_error_with_exit_code_2(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        parse(*argv)
    assert stop.value.code == 2
    assert "error" in capsys.readouterr().err


def run_buddy(*argv, timeout=60):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(ROOT / "buddy.py"), *argv], capture_output=True, text=True,
                          timeout=timeout, env=env)


def test_the_real_script_refuses_a_bad_night_with_exit_2_and_says_how_to_write_it():
    done = run_buddy("--night", "25:00-07:00")
    assert done.returncode == 2 and "usá HH:MM-HH:MM" in done.stderr and "--night" in done.stderr
    assert run_buddy("--night-level", "much").returncode == 2


def test_the_real_script_lists_the_flags_and_check_still_runs():
    done = run_buddy("--help")
    assert done.returncode == 0
    for text in ("--night", "--night-level", "--backlight", "23:00-07:00"):
        assert text in done.stdout
    assert "—" not in done.stdout  # no em dashes
    nothing = run_buddy("check", "--bus", "unix:path=/nonexistent/bus", "--state-dir", "/nonexistent/buddy-state")
    assert nothing.returncode == 1 and "[ERROR]" in nothing.stdout  # it got to the diagnosis, as before
    assert "Traceback" not in nothing.stderr


def test_the_daemon_only_gets_a_backlight_when_night_mode_is_on_and_asks_for_one(tmp_path):
    panel = make_panel(tmp_path / "bl")
    on = parse("--backlight", str(panel))
    assert buddy.make_backlight(on).directory == panel
    assert buddy.make_backlight(parse("--backlight", str(panel), "--night", "off")) is None  # no night, no hands on the panel
    assert buddy.make_backlight(parse("--backlight", "off")) is None
    assert buddy.make_backlight(parse("--backlight", str(tmp_path / "missing"))) is None
    assert buddy.make_backlight(parse("--backlight", str(panel), "--night-level", "40")).night == 40


def test_the_start_up_line_says_what_night_mode_will_do(tmp_path):
    panel = make_panel(tmp_path / "bl")
    args = parse("--backlight", str(panel), "--night", "22:00-06:30", "--night-level", "20")
    text, _ = buddy.describe_night(args, buddy.make_backlight(args))
    assert "22:00-06:30" in text and "20 de 255" in text and str(panel) in text
    text, _ = buddy.describe_night(args, None)
    assert "22:00-06:30" in text and "oscurece sola" in text
    assert buddy.describe_night(parse("--night", "off"), None) is None
    assert "—" not in text


# --- run_daemon: the restore on every way out ----------------------------------------------------


class FakeTransport:
    """What run_daemon needs from the Bluetooth transport, without Bluetooth."""

    ready = True
    device_name = "Claude-1234"
    not_ready_reason = ""
    secure = True
    address = "00:00:00:00:00:00"

    def __init__(self, panel, start=None):
        self.panel = panel
        self.on_start = start
        self.seen = []
        self.at_stop = []  # the brightness the moment the transport is asked to stop

    def send(self, message):
        pass

    def unpair(self):
        pass

    def is_secure(self):
        return True

    async def start(self):
        if self.on_start is not None:
            await asyncio.sleep(0.2)  # long enough for the first publication to put the panel at night
            self.seen.append(level(self.panel))
            self.on_start()

    async def stop(self):
        self.at_stop.append(level(self.panel))

    async def forget_all(self):
        return 0


def daemon_args(tmp_path, panel, *extra):
    return buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--no-keys", "--ui-port", "0", "--night", "08:00-08:00",
                             "--backlight", str(panel), *extra])


def raise_(exc):
    def thrower():
        raise exc
    return thrower


@pytest.fixture
def hangup_unclaimed():
    """SIGHUP as a shell leaves it for a daemon (not ignored), with a harmless handler so a stray one cannot kill the test run."""
    saved = signal.signal(signal.SIGHUP, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGHUP, saved)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_the_daemon_dims_wakes_on_a_tap_dims_again_and_restores_on_a_signal(tmp_path, monkeypatch, capsys, hangup_unclaimed, sig):
    """systemd's SIGTERM, Ctrl-C and a closed terminal or ssh session (SIGHUP): the screen must come back to full brightness,
    and a tap on the real server must light it."""
    monkeypatch.setattr(buddy_ui, "AWAKE_S", 0.4)
    panel = make_panel(tmp_path / "bl")
    fake = FakeTransport(panel)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    args = daemon_args(tmp_path, panel)

    async def scenario():
        task = asyncio.ensure_future(buddy.run_daemon(args))
        out = ""
        try:
            await wait_until(lambda: level(panel) == 12)  # always night (08:00-08:00): it dims as soon as it starts
            while "Pantalla en http://127.0.0.1:" not in out:
                out += capsys.readouterr().out
                await asyncio.sleep(0.01)
            port = int(re.search(r"Pantalla en http://127\.0\.0\.1:(\d+)", out).group(1))
            page = (await http(port))[2].decode("utf-8")
            token = re.search(r'<meta name="buddy-token" content="([^"]+)">', page).group(1)
            status, _, body = await http(port, "POST", "/wake", headers={"X-Buddy-Token": token})
            assert status == 200 and json.loads(body) == {"ok": True, "dim": False}
            await wait_until(lambda: level(panel) == 255)  # the tap lights the panel at once
            await wait_until(lambda: level(panel) == 12)  # and it goes back down by itself
            os.kill(os.getpid(), sig)
            assert await asyncio.wait_for(task, 5) == 0
        finally:
            if not task.done():
                task.cancel()
        assert level(panel) == 255
        out += capsys.readouterr().out
        assert "Cerrando" in out
        # the service has no -v: what happened to the screen has to be in what it prints, not only in the INFO log
        assert out.index("noche: pantalla tenue") < out.index("noche: despierta (toque)") < out.rindex("noche: pantalla tenue")
        assert fake.at_stop == [255]  # the light was back before the Bluetooth teardown (which may take its time) began

    run(scenario)


def test_a_hangup_does_not_stop_a_daemon_started_under_nohup(tmp_path, monkeypatch):
    """nohup starts the process with SIGHUP ignored on purpose: the daemon must respect that, and still stop on SIGTERM."""
    panel = make_panel(tmp_path / "bl")
    fake = FakeTransport(panel)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    saved = signal.signal(signal.SIGHUP, signal.SIG_IGN)

    async def scenario():
        task = asyncio.ensure_future(buddy.run_daemon(daemon_args(tmp_path, panel)))
        try:
            await wait_until(lambda: level(panel) == 12)
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.sleep(0.4)
            assert not task.done() and level(panel) == 12
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 5) == 0
        finally:
            if not task.done():
                task.cancel()
        assert level(panel) == 255

    try:
        run(scenario)
    finally:
        signal.signal(signal.SIGHUP, saved)


def test_the_daemon_restores_the_brightness_when_it_is_cancelled(tmp_path, monkeypatch):
    panel = make_panel(tmp_path / "bl")
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: FakeTransport(panel))

    async def scenario():
        task = asyncio.ensure_future(buddy.run_daemon(daemon_args(tmp_path, panel)))
        await wait_until(lambda: level(panel) == 12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert level(panel) == 255

    run(scenario)


def test_the_daemon_restores_the_brightness_when_bluetooth_fails_to_start(tmp_path, monkeypatch):
    panel = make_panel(tmp_path / "bl")
    fake = FakeTransport(panel, start=raise_(buddy.TransportError("boom")))
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    assert run_returning(buddy.run_daemon(daemon_args(tmp_path, panel))) == 1
    assert fake.seen == [12]  # it had dimmed the panel by then
    assert level(panel) == 255


def test_the_daemon_restores_the_brightness_on_a_keyboard_interrupt(tmp_path, monkeypatch):
    panel = make_panel(tmp_path / "bl")
    fake = FakeTransport(panel, start=raise_(KeyboardInterrupt()))
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    code = buddy.main(["--state-dir", str(tmp_path / "state"), "--no-keys", "--ui-port", "0", "--night", "08:00-08:00",
                       "--backlight", str(panel)])
    assert code == 130
    assert fake.seen == [12]
    assert level(panel) == 255


def test_a_daemon_with_no_screen_leaves_the_panel_alone_and_does_not_promise_a_tap(tmp_path, monkeypatch, capsys):
    """With --no-ui there is no page to tap awake: dimming the panel would only make it dark for nothing."""
    panel = make_panel(tmp_path / "bl")
    fake = FakeTransport(panel, start=lambda: None)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    assert run_returning(buddy.run_daemon(daemon_args(tmp_path, panel, "--no-ui")), stop_after=0.5) == 0
    assert fake.seen == [255] and level(panel) == 255
    out = capsys.readouterr().out
    assert "un toque" not in out and "noche" not in out  # night mode has no job here, so it says nothing about dimming either


def test_a_daemon_whose_screen_port_is_taken_leaves_the_panel_alone_too(tmp_path, monkeypatch, capsys):
    import socket

    panel = make_panel(tmp_path / "bl")
    fake = FakeTransport(panel, start=lambda: None)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        args = daemon_args(tmp_path, panel, "--ui-port", str(taken.getsockname()[1]))
        assert run_returning(buddy.run_daemon(args), stop_after=0.5) == 0
    out = capsys.readouterr().out
    assert "No pude abrir la pantalla" in out and "un toque" not in out and "noche" not in out
    assert fake.seen == [255] and level(panel) == 255


def test_the_start_up_line_comes_with_the_screen_it_describes(tmp_path, monkeypatch, capsys):
    panel = make_panel(tmp_path / "bl")
    fake = FakeTransport(panel, start=lambda: None)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    assert run_returning(buddy.run_daemon(daemon_args(tmp_path, panel)), stop_after=0.5) == 0
    out = capsys.readouterr().out
    assert out.index("Pantalla en http://127.0.0.1:") < out.index("Modo noche 08:00-08:00: la luz baja a 12 de 255")


def test_a_daemon_without_night_mode_never_touches_the_panel(tmp_path, monkeypatch):
    panel = make_panel(tmp_path / "bl", level=77)
    fake = FakeTransport(panel, start=lambda: None)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: fake)
    args = daemon_args(tmp_path, panel, "--no-ui", "--night", "off")
    assert run_returning(buddy.run_daemon(args), stop_after=0.5) == 0
    assert fake.seen == [77] and level(panel) == 77


def run_returning(coro, stop_after=None):
    """Run a coroutine to its return value; with stop_after, ask the daemon to stop with SIGTERM that many seconds in."""
    async def scenario():
        task = asyncio.ensure_future(coro)
        if stop_after is not None:
            await asyncio.sleep(stop_after)
            os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(task, 10)

    return asyncio.run(scenario())
