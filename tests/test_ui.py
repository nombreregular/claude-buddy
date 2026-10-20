"""The screen: its view model, its local web server and the page (no Bluetooth, no D-Bus)."""
import asyncio
import contextlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy_ui  # noqa: E402
from buddy_core import BuddyCore, encode_line  # noqa: E402
from buddy_ui import UIHub, UIServer  # noqa: E402
# The page runner lives in page_runner.py; these names stay so test_hermes.py and the tests here keep working.
from page_runner import attr as _attr  # noqa: E402,F401
from page_runner import chromium as _chromium  # noqa: E402,F401
from page_runner import dump as _dump  # noqa: E402,F401
from page_runner import needs_chromium  # noqa: E402,F401
from ui_client import Stream, http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "ui" / "index.html"
PERSONAS = ["sleep", "idle", "busy", "attention", "celebrate", "dizzy", "heart"]


def heartbeat(**kw):
    base = {"total": 1, "running": 0, "waiting": 0, "msg": "idle", "entries": [], "tokens": 0, "tokens_today": 0}
    base.update(kw)
    return base


PROMPT = {"id": "req_1", "tool": "Bash", "hint": "rm -rf /tmp/foo"}


class Radio:
    """What the hub reads from the Bluetooth transport."""

    ready = True
    device_name = "Claude-1234"
    not_ready_reason = ""
    secure = True


class Rig:
    """A core with a fake clock, a hub and a stand-in for the radio."""

    def __init__(self, state_dir):
        self.t = 1000.0
        self.wall = 1_800_000_000.0
        self.sent = []
        self.radio = Radio()
        self.core = BuddyCore(state_dir, self.sent.append, clock=lambda: self.t, wall=lambda: self.wall)
        self.hub = UIHub()
        self.hub.bind(self.core, self.radio)

    def rx(self, obj):
        self.core.feed(encode_line(obj))

    def view(self):
        return self.hub.view()

    def advance(self, seconds):
        self.t += seconds
        self.core.tick()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


def run(coro_fn):
    return asyncio.run(coro_fn())


# --- the view model ---------------------------------------------------------------------


def test_a_fresh_buddy_is_asleep_with_nothing_to_show(rig):
    rig.radio.ready, rig.radio.not_ready_reason = False, "starting"
    v = rig.view()
    assert v["ble"] == {"ready": False, "name": "Claude-1234", "reason": "starting", "secure": True}
    assert v["linked"] is False and v["mood"] == "sleep"
    assert v["pairing"] is None and v["approvals"] == []
    assert v["pet"] == {"name": "Buddy", "owner": "", "level": 0, "xp": 0.0, "approvals": 0, "denials": 0}
    assert v["sessions"]["total"] == 0 and v["sessions"]["entries"] == []
    assert v["clock"] is None


def test_every_boot_has_its_own_id_so_stale_pages_can_reload(rig, tmp_path):
    first = rig.view()["boot"]
    assert first and first == rig.view()["boot"]  # stable while the daemon lives
    other = Rig(tmp_path / "other")
    assert other.view()["boot"] != first  # a restarted daemon is a different one


def test_the_reason_is_only_reported_while_bluetooth_is_down(rig):
    rig.radio.not_ready_reason = "left over from before"
    assert rig.view()["ble"]["reason"] == ""
    rig.radio.ready, rig.radio.not_ready_reason = False, "bluetoothd se detuvo"
    assert rig.view()["ble"]["reason"] == "bluetoothd se detuvo"


def test_a_heartbeat_with_a_prompt_puts_the_buddy_on_alert(rig):
    rig.rx(heartbeat(total=3, running=2, waiting=1, msg="approve: Bash", entries=["10:42 Read a.py", "10:41 Bash ls"],
                     tokens_today=48230, prompt=PROMPT))
    v = rig.view()
    assert v["linked"] is True and v["mood"] == "attention"
    assert v["sessions"] == {"total": 3, "running": 2, "waiting": 1, "msg": "approve: Bash",
                             "entries": ["10:42 Read a.py", "10:41 Bash ls"], "tokens_today": 48230}
    assert v["approvals"] == [{"key": "claude:req_1", "tool": "Bash", "hint": "rm -rf /tmp/foo", "source": "claude",
                              "always": ""}]


def test_only_four_entries_reach_the_screen(rig):
    rig.rx(heartbeat(entries=[f"line {i}" for i in range(8)]))
    assert rig.view()["sessions"]["entries"] == ["line 0", "line 1", "line 2", "line 3"]


def test_the_view_is_plain_json_with_ascii_only_on_the_wire(rig):
    rig.rx(heartbeat(msg="¿Aprobás? ñandú ✓", prompt={"id": "x", "tool": "Bash", "hint": "echo 'ü' <script>alert(1)</script>"}))
    rig.hub.refresh()
    text = rig.hub.current()
    text.encode("ascii")  # the SSE frame and /state are ASCII: no encoding surprises on the way to the page
    assert json.loads(text)["sessions"]["msg"] == "¿Aprobás? ñandú ✓"
    assert "\n" not in text  # one line, so it fits in one SSE data: field


def test_the_level_and_progress_bar_come_from_the_tokens(rig):
    rig.core.state.tokens = 125_000
    pet = rig.view()["pet"]
    assert pet["level"] == 2 and pet["xp"] == 0.5


def test_the_clock_is_the_desktops_local_time(rig):
    assert rig.view()["clock"] is None
    rig.rx({"time": [1_800_000_000, -21600]})  # 2027-01-15 08:00:00 UTC, the desktop is six hours behind
    assert rig.view()["clock"] == "02:00"
    rig.wall += 3 * 3600 + 25 * 60
    assert rig.view()["clock"] == "05:25"


def test_the_pairing_code_is_shown_as_six_digits(rig):
    rig.hub.set_passkey("123456", 0)
    assert rig.view()["pairing"] == {"code": "123456", "entered": 0}
    rig.hub.set_passkey(42, 3)  # an int is padded the way the Mac shows it
    assert rig.view()["pairing"] == {"code": "000042", "entered": 3}
    rig.hub.set_passkey("123456", 99)  # never more typed digits than the code has
    assert rig.view()["pairing"]["entered"] == 6
    rig.hub.set_passkey(None)
    assert rig.view()["pairing"] is None


def test_the_buddy_falls_asleep_when_the_desktop_goes_quiet(rig):
    rig.rx(heartbeat(running=1))
    assert rig.view()["linked"] is True
    rig.advance(31)
    v = rig.view()
    assert v["linked"] is False and v["mood"] == "sleep"


def test_every_mood_the_core_can_pick_has_a_look_on_the_page(rig):
    page = PAGE.read_text(encoding="utf-8")
    for mood in PERSONAS:
        assert f"data-mood={mood}]" in page, f"no CSS for {mood}"
        assert re.search(rf"\b{mood}:\s*'", page), f"no label for {mood}"
    seen = set()
    rig.rx(heartbeat())
    seen.add(rig.core.persona())
    rig.rx(heartbeat(running=2))
    seen.add(rig.core.persona())
    rig.rx(heartbeat(waiting=1))
    seen.add(rig.core.persona())
    rig.rx(heartbeat(completed=True))
    seen.add(rig.core.persona())
    assert seen <= set(PERSONAS)


def test_refresh_reports_only_real_changes(rig):
    first = rig.hub.refresh()
    assert first is True  # the first look is always news
    assert rig.hub.refresh() is False
    rig.rx(heartbeat(msg="hello"))
    assert rig.hub.refresh() is True
    assert rig.hub.refresh() is False


def test_listeners_hear_about_changes_and_can_leave(rig):
    seen = []
    leave = rig.hub.subscribe(seen.append)
    rig.hub.refresh()
    rig.rx(heartbeat(msg="one"))
    rig.hub.refresh()
    assert [json.loads(s)["sessions"]["msg"] for s in seen] == ["", "one"]
    leave()
    rig.rx(heartbeat(msg="two"))
    rig.hub.refresh()
    assert len(seen) == 2
    leave()  # leaving twice is harmless


def test_a_broken_listener_does_not_stop_the_others(rig):
    seen = []

    def broken(_):
        raise RuntimeError("boom")

    rig.hub.subscribe(broken)
    rig.hub.subscribe(seen.append)
    rig.hub.refresh()
    assert len(seen) == 1


def test_a_hub_that_is_not_bound_yet_is_inert():
    hub = UIHub()
    assert hub.refresh() is False
    hub.touch()
    hub.set_passkey("123456")  # the transport may speak before the core exists


def test_touch_turns_a_burst_into_one_refresh(rig):
    async def scenario():
        seen = []
        rig.hub.subscribe(seen.append)
        rig.rx(heartbeat(msg="burst"))
        computed = []
        original = rig.hub.view
        rig.hub.view = lambda: (computed.append(1), original())[1]
        for _ in range(50):
            rig.hub.touch()
        assert seen == [] and computed == []  # nothing yet: it waits for the end of this loop turn
        await asyncio.sleep(0)
        assert len(seen) == 1 and len(computed) == 1  # fifty touches, one recomputation
        rig.hub.touch()
        await asyncio.sleep(0)
        assert len(seen) == 1  # nothing changed, nothing to say

    run(scenario)


def test_a_new_prompt_reaches_the_listeners_by_itself(rig):
    async def scenario():
        seen = []
        rig.hub.subscribe(seen.append)
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        await asyncio.sleep(0)
        assert json.loads(seen[-1])["approvals"][0]["key"] == "claude:req_1"
        rig.hub.unbind()
        rig.core.broker.decide("claude:req_1", "once")
        await asyncio.sleep(0)
        assert json.loads(seen[-1])["approvals"]  # unbound: it no longer follows the broker

    run(scenario)


def test_deciding_from_the_screen_answers_the_desktop(rig):
    rig.rx(heartbeat(waiting=1, prompt=PROMPT))
    assert rig.hub.decide("claude:req_1", "once") is True
    assert {"cmd": "permission", "id": "req_1", "decision": "once"} in rig.sent
    assert rig.hub.decide("claude:req_1", "once") is False  # already answered
    assert rig.view()["approvals"] == []
    assert rig.view()["pet"]["approvals"] == 1


def test_deciding_something_else_is_refused(rig):
    rig.rx(heartbeat(waiting=1, prompt=PROMPT))
    with pytest.raises(ValueError):
        rig.hub.decide("claude:req_1", "always")
    assert rig.view()["approvals"]


# --- the page ----------------------------------------------------------------------------


BUFO_MANIFEST = {
    "name": "bufo",
    "render": "pixel",
    "colors": {"body": "#6B8E23", "bg": "#000000", "text": "#FFFFFF", "textDim": "#808080", "ink": "bad"},
    "accents": {"idle": "#FF9E7A", "busy": "nope"},
    "states": {"sleep": "sleep.gif", "idle": ["idle_0.gif", "idle_1.gif"], "busy": "missing.gif"},
}


def install_pack(rig, manifest=BUFO_MANIFEST, name="bufo"):
    """A pack already on disk, the way the core leaves it after a folder push."""
    folder = rig.core.state_dir / "characters" / name
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for fname in ("sleep.gif", "idle_0.gif", "idle_1.gif"):
        (folder / fname).write_bytes(b"GIF89a" + fname.encode())
    rig.core.state.character = name
    return folder


def rewrite(path, data, tick):
    """New bytes under the same name, with a modification time of its own (`tick` whole seconds): two writes in
    the same clock tick would look alike on a file system with a coarse clock, and a test must not depend on that."""
    path.write_bytes(data)
    os.utime(path, (1_900_000_000 + tick, 1_900_000_000 + tick))


REV = re.compile(r"[0-9a-f]{12}")


def test_without_a_pack_the_page_draws_its_own_buddy(rig):
    assert rig.view()["character"] is None


def test_the_active_pack_reaches_the_page_with_only_what_it_can_use(rig):
    install_pack(rig)
    c = rig.view()["character"]
    assert c["name"] == "bufo"
    assert c["colors"] == {"body": "#6B8E23", "bg": "#000000", "text": "#FFFFFF", "textDim": "#808080"}  # "bad" is no color
    assert c["accents"] == {"idle": "#FF9E7A"}  # our extension, same filter
    assert c["render"] == "pixel"
    assert c["states"] == {"sleep": ["sleep.gif"], "idle": ["idle_0.gif", "idle_1.gif"]}  # a missing file drops its state


def test_a_pack_that_changes_on_disk_is_read_again(rig):
    import os

    folder = install_pack(rig)
    assert "sleep" in rig.view()["character"]["states"]
    (folder / "manifest.json").write_text(json.dumps({"name": "bufo", "states": {"idle": "idle_0.gif"}}), encoding="utf-8")
    os.utime(folder / "manifest.json", (2_000_000_000, 2_000_000_000))
    c = rig.view()["character"]
    assert c["states"] == {"idle": ["idle_0.gif"]} and c["accents"] == {} and c["render"] == "smooth"
    rig.core.state.character = "gone"
    assert rig.view()["character"] is None


def test_the_pack_carries_a_rev_that_stays_put_while_nothing_changes(rig):
    folder = install_pack(rig)
    rev = rig.view()["character"]["rev"]
    assert REV.fullmatch(rev)
    assert [rig.view()["character"]["rev"] for _ in range(3)] == [rev] * 3
    other = UIHub()
    other.bind(rig.core, rig.radio)
    try:
        assert other.view()["character"]["rev"] == rev  # it comes from the disk, not from a counter or a dice
    finally:
        other.unbind()
    (folder / "notes.txt").write_text("a file the manifest does not list", encoding="utf-8")
    manifest = folder / "manifest.json"
    rewrite(manifest, manifest.read_bytes(), 7)  # the same bytes written again are not a new pack
    assert rig.view()["character"]["rev"] == rev


def test_a_gif_replaced_under_the_same_name_changes_the_rev_though_the_manifest_did_not(rig):
    folder = install_pack(rig)
    manifest_time = (folder / "manifest.json").stat().st_mtime_ns
    before = rig.view()["character"]
    revs = [before["rev"]]
    for data, tick in ((b"GIF89a-one", 1),    # other bytes, other size, other time
                       (b"GIF89a-two", 2),    # same size as the one before: only the time moved
                       (b"GIF89a-three", 2)):  # same time as the one before: only the size moved
        rewrite(folder / "idle_0.gif", data, tick)
        revs.append(rig.view()["character"]["rev"])
    assert len(set(revs)) == 4 and all(REV.fullmatch(r) for r in revs)
    assert (folder / "manifest.json").stat().st_mtime_ns == manifest_time  # the manifest was never touched
    after = rig.view()["character"]
    assert {k: v for k, v in after.items() if k != "rev"} == {k: v for k, v in before.items() if k != "rev"}


def test_a_listed_file_that_appears_or_vanishes_changes_the_rev_and_the_states(rig):
    folder = install_pack(rig)  # the manifest lists missing.gif for busy, and it is not there
    base = rig.view()["character"]
    assert "busy" not in base["states"]
    (folder / "missing.gif").write_bytes(b"GIF89abusy")
    with_busy = rig.view()["character"]
    assert with_busy["states"]["busy"] == ["missing.gif"] and with_busy["rev"] != base["rev"]
    (folder / "idle_1.gif").unlink()
    fewer = rig.view()["character"]
    assert fewer["states"]["idle"] == ["idle_0.gif"]  # a vanished file drops out of its state at once
    assert len({base["rev"], with_busy["rev"], fewer["rev"]}) == 3
    (folder / "sleep.gif").unlink()
    assert "sleep" not in rig.view()["character"]["states"]
    for left in ("idle_0.gif", "missing.gif"):
        (folder / left).unlink()
    assert rig.view()["character"] is None  # nothing left to draw: the page falls back to its own buddy


def test_a_changed_manifest_changes_the_rev_even_when_its_clock_did_not_move(rig):
    folder = install_pack(rig)
    manifest = folder / "manifest.json"
    first = rig.view()["character"]
    second_manifest = dict(BUFO_MANIFEST, colors=dict(BUFO_MANIFEST["colors"], body="#112233"))
    rewrite(manifest, json.dumps(second_manifest).encode(), 1)
    second = rig.view()["character"]
    assert second["colors"]["body"] == "#112233" and second["rev"] != first["rev"]
    third_manifest = dict(second_manifest, accents={"idle": "#FF9E7A", "sleep": "#112233"})
    rewrite(manifest, json.dumps(third_manifest).encode(), 1)  # the same time as before, but not the same size
    third = rig.view()["character"]
    assert third["accents"] == {"idle": "#FF9E7A", "sleep": "#112233"}
    assert len({first["rev"], second["rev"], third["rev"]}) == 3


def test_the_manifest_is_parsed_once_until_it_changes(rig, monkeypatch):
    folder = install_pack(rig)
    parsed = []
    real = UIHub._read_manifest

    def counting(manifest):
        parsed.append(manifest)
        return real(manifest)

    monkeypatch.setattr(UIHub, "_read_manifest", staticmethod(counting))
    for _ in range(5):
        rig.view()
    assert len(parsed) == 1
    rewrite(folder / "manifest.json", json.dumps(dict(BUFO_MANIFEST, render="smooth")).encode(), 1)
    assert rig.view()["character"]["render"] == "smooth" and len(parsed) == 2


def test_the_screen_hears_about_a_replaced_gif_on_the_next_refresh_and_not_before(rig):
    folder = install_pack(rig)
    assert rig.hub.refresh() is True
    assert rig.hub.refresh() is False  # nothing changed: nobody is told, no page reloads its images
    rewrite(folder / "sleep.gif", b"GIF89afresh", 1)
    assert rig.hub.refresh() is True
    assert rig.hub.refresh() is False


@pytest.mark.parametrize("manifest", [
    b"[]", b"not json", b"\xff\xfe\x00", b"{}", json.dumps({"states": {}}).encode(),
    json.dumps({"states": {"idle": "nope.gif", "busy": ["../sleep.gif", ".idle_0.gif", 7, None]}}).encode(),
])
def test_an_unusable_manifest_means_no_pack_and_so_no_rev(rig, manifest):
    folder = install_pack(rig)
    rewrite(folder / "manifest.json", manifest, 1)
    assert rig.view()["character"] is None


def test_odd_file_names_in_a_manifest_cost_the_pack_only_those_files(rig):
    states = {"sleep": ["sleep.gif", "a\x00b.gif", "x" * 300 + ".gif", "\ud800.gif", "folder.gif", "../up.gif", ".hidden.gif"],
              "idle": "folder.gif"}
    folder = install_pack(rig, {"states": states})
    (folder / "folder.gif").mkdir()  # a directory is not an image
    c = rig.view()["character"]
    assert c["states"] == {"sleep": ["sleep.gif"]} and REV.fullmatch(c["rev"])
    rig.hub.current().encode("ascii")


def test_without_a_closet_the_pack_is_described_with_exactly_the_keys_it_always_had(rig):
    install_pack(rig)
    c = rig.view()["character"]
    assert set(c) == {"name", "colors", "accents", "render", "states", "rev"}  # `costume` only while Buddy wears one
    assert "costume" not in rig.hub.current()
    assert rig.hub.character_bytes(c, "idle_1.gif") == b"GIF89aidle_1.gif"  # the route reads the pack's own folder
    assert rig.hub.character_bytes(c, "nothing.gif") is None
    assert rig.hub._closet is None and rig.hub._closet_timer is None


def test_the_page_has_exactly_one_token_placeholder():
    assert PAGE.read_text(encoding="utf-8").count(buddy_ui.TOKEN_PLACEHOLDER) == 1


def test_the_page_never_turns_received_text_into_markup():
    page = PAGE.read_text(encoding="utf-8")
    script = page[page.index("<script>"):]
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc"):
        assert bad not in script, f"the page uses {bad}"


def test_the_page_loads_nothing_from_outside():
    page = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"https?://", page)
    assert not re.search(r"""(src|href)\s*=""", page)
    assert "@import" not in page and "url(" not in page.replace("url(#", "")


def test_every_element_the_script_looks_up_exists():
    page = PAGE.read_text(encoding="utf-8")
    html, script = page[:page.index("<script>")], page[page.index("<script>"):]
    ids = set(re.findall(r'id="([\w-]+)"', html))
    wanted = set(re.findall(r"\$\('([\w-]+)'\)", script))
    assert wanted, "the script no longer uses $()"
    assert wanted <= ids, f"missing in the HTML: {sorted(wanted - ids)}"


def _keyframe_blocks(css):
    for found in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", css):
        depth, i = 1, found.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        yield found.group(1), css[found.end():i - 1]


def test_the_page_only_animates_transform_and_opacity():
    page = PAGE.read_text(encoding="utf-8")
    css = page[page.index("<style>"):page.index("</style>")]
    blocks = dict(_keyframe_blocks(css))
    assert len(blocks) >= 15
    for name, body in blocks.items():
        assert set(re.findall(r"([a-z-]+)\s*:", body)) <= {"transform", "opacity"}, name
    for found in re.finditer(r"transition:([^;}]*)", css):  # transitions the same: nothing that makes the Pi re-layout
        assert set(re.findall(r"(?:^|,)\s*([a-z-]+)", found.group(1))) <= {"transform", "opacity"}, found.group(0)


# --- the server --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def serving(rig, **kw):
    server = UIServer(rig.hub, port=0, **kw)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def decide_request(server, key, decision, **kw):
    headers = {"Content-Type": "application/json", "X-Buddy-Token": server.token, **kw.pop("headers", {})}
    body = kw.pop("body", json.dumps({"key": key, "decision": decision}).encode())
    return http(server, "POST", "/decide", headers=headers, body=body, **kw)


def test_the_page_is_served_with_its_token_and_strict_headers(rig):
    async def scenario():
        async with serving(rig) as server:
            for path in ("/", "/index.html"):
                status, headers, body = await http(server, path=path)
                assert status == 200
                assert headers["content-type"] == "text/html; charset=utf-8"
                assert headers["cache-control"] == "no-store"
                assert headers["x-content-type-options"] == "nosniff"
                assert headers["referrer-policy"] == "no-referrer"
                csp = headers["content-security-policy"]
                assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "form-action 'none'" in csp
                html = body.decode("utf-8")
                assert buddy_ui.TOKEN_PLACEHOLDER not in html
                assert f'<meta name="buddy-token" content="{server.token}">' in html
                assert int(headers["content-length"]) == len(body)

    run(scenario)


def test_every_server_start_has_its_own_token(rig):
    async def scenario():
        async with serving(rig) as a, serving(rig) as b:
            assert a.token != b.token and len(a.token) >= 32

    run(scenario)


def test_the_page_is_read_from_disk_each_time(rig, tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<p>one</p> __BUDDY_TOKEN__", encoding="utf-8")

    async def scenario():
        async with serving(rig, page=page) as server:
            assert (await http(server))[2].decode().startswith("<p>one</p>")
            page.write_text("<p>two ñ</p>", encoding="utf-8")  # a design tweak shows up on reload, no restart
            assert (await http(server))[2].decode() == "<p>two ñ</p>"

    run(scenario)


def test_a_missing_page_is_a_clear_error_not_a_crash(rig, tmp_path):
    async def scenario():
        async with serving(rig, page=tmp_path / "nope.html") as server:
            status, _, body = await http(server)
            assert status == 500 and b"page file is missing" in body
            assert (await http(server, path="/healthz"))[0] == 200  # the server is still fine

    run(scenario)


def test_state_returns_what_the_stream_would_say(rig):
    async def scenario():
        rig.rx(heartbeat(total=2, msg="hola"))
        async with serving(rig) as server:
            status, headers, body = await http(server, path="/state")
            assert status == 200 and headers["content-type"] == "application/json"
            assert json.loads(body) == rig.view()

    run(scenario)


def test_unknown_paths_and_wrong_methods(rig):
    async def scenario():
        async with serving(rig) as server:
            assert (await http(server, path="/nope"))[0] == 404
            assert (await http(server, path="/fonts/x.woff2"))[0] == 404
            assert (await http(server, "POST", "/"))[0] == 405
            assert (await http(server, "GET", "/decide"))[0] == 405
            assert (await http(server, "POST", "/events"))[0] == 405
            assert (await http(server, "DELETE", "/state"))[0] == 405
            assert (await http(server, path="/favicon.ico"))[0] == 204
            assert (await http(server, path="/state?x=1"))[0] == 200  # the query string is ignored

    run(scenario)


def test_pack_images_are_served_but_nothing_else_on_the_disk(rig):
    install_pack(rig)
    (rig.core.state_dir / "characters" / "bufo" / "secret.txt").write_text("x", encoding="utf-8")

    async def scenario():
        async with serving(rig) as server:
            status, headers, body = await http(server, path="/character/bufo/idle_1.gif")
            assert status == 200 and headers["content-type"] == "image/gif" and body == b"GIF89aidle_1.gif"
            assert headers["x-content-type-options"] == "nosniff"
            for path in ("/character/bufo/manifest.json", "/character/bufo/secret.txt", "/character/bufo/missing.gif",
                         "/character/other/sleep.gif", "/character/bufo/../state.json", "/character/bufo/..%2Fstate.json",
                         "/character/bufo", "/character/bufo/", "/character/bufo/sleep.gif/x", "/character/"):
                assert (await http(server, path=path))[0] == 404, path
            assert (await http(server, "POST", "/character/bufo/sleep.gif"))[0] == 405

    run(scenario)


def test_pack_images_are_served_under_the_rev_in_the_query_and_the_query_decides_nothing(rig):
    folder = install_pack(rig)
    rev = rig.view()["character"]["rev"]

    async def scenario():
        async with serving(rig) as server:
            for query in (f"?v={rev}", "?v=abc", "?v=", "?", "?v=1&x=2", "?v=%zz"):
                status, headers, body = await http(server, path="/character/bufo/idle_1.gif" + query)
                assert status == 200 and headers["content-type"] == "image/gif" and body == b"GIF89aidle_1.gif", query
            for path in (f"/character/bufo/missing.gif?v={rev}", f"/character/other/sleep.gif?v={rev}",
                         f"/character/bufo/manifest.json?v={rev}", f"/character/bufo/..%2Fstate.json?v={rev}",
                         f"/character/bufo?v={rev}", f"/character/?v={rev}", "/character/bufo/idle_1.gif%3Fv=abc"):
                assert (await http(server, path=path))[0] == 404, path  # a query neither opens a door nor makes a name
            rewrite(folder / "idle_1.gif", b"GIF89anewer", 1)
            new = rig.view()["character"]["rev"]
            assert new != rev
            assert (await http(server, path=f"/character/bufo/idle_1.gif?v={new}"))[2] == b"GIF89anewer"
            assert (await http(server, path=f"/character/bufo/idle_1.gif?v={rev}"))[2] == b"GIF89anewer"  # the disk wins

    run(scenario)


def test_the_rev_and_the_names_travel_as_ascii_and_the_images_still_come(rig):
    manifest = {"states": {"sleep": "sleep.gif", "idle": ["cola-ñ.gif"]}}
    folder = install_pack(rig, manifest, name="mochí")
    (folder / "cola-ñ.gif").write_bytes(b"GIF89acola")
    rev = rig.view()["character"]["rev"]
    text = rig.hub.current()
    text.encode("ascii")
    sent = json.loads(text)["character"]
    assert sent["rev"] == rev and REV.fullmatch(rev)
    assert sent["name"] == "mochí" and sent["states"]["idle"] == ["cola-ñ.gif"]

    async def scenario():
        async with serving(rig) as server:
            status, _, body = await http(server, path=f"/character/moch%C3%AD/cola-%C3%B1.gif?v={rev}")
            assert status == 200 and body == b"GIF89acola"

    run(scenario)


def test_without_a_pack_no_image_is_served(rig):
    async def scenario():
        async with serving(rig) as server:
            assert (await http(server, path="/character/bufo/sleep.gif"))[0] == 404

    run(scenario)


def test_it_refuses_to_listen_beyond_the_machine(rig):
    for host in ("0.0.0.0", "::", "192.168.1.20", "buddy.local"):
        with pytest.raises(ValueError):
            UIServer(rig.hub, host=host)

    async def scenario():
        async with serving(rig) as server:
            assert server._server.sockets[0].getsockname()[0] == "127.0.0.1"
            assert server.url == f"http://127.0.0.1:{server.port}"

    run(scenario)


def test_a_port_that_is_taken_is_an_error_the_caller_can_handle(rig):
    async def scenario():
        async with serving(rig) as first:
            second = UIServer(rig.hub, port=first.port)
            with pytest.raises(OSError):
                await second.start()

    run(scenario)


# -- /events --


def test_the_stream_starts_with_the_state_and_then_follows_every_change(rig):
    async def scenario():
        async with serving(rig) as server:
            stream = await Stream.open(server)
            try:
                assert stream.status == 200
                assert stream.headers["content-type"] == "text/event-stream"
                assert stream.headers["cache-control"] == "no-store"
                assert (await stream.raw_block()) == f"retry: {buddy_ui.RETRY_MS}\n\n"
                first = await stream.state()
                assert first["linked"] is False and first["approvals"] == []

                rig.rx(heartbeat(waiting=1, msg="approve: Bash", prompt=PROMPT))
                v = await stream.state(lambda v: v["linked"])
                assert v["mood"] == "attention" and v["approvals"][0]["tool"] == "Bash"

                rig.hub.set_passkey("987654", 2)
                v = await stream.state(lambda v: v["pairing"])
                assert v["pairing"] == {"code": "987654", "entered": 2}
            finally:
                stream.close()

    run(scenario)


def test_the_stream_sends_an_event_as_a_keepalive(rig, monkeypatch):
    monkeypatch.setattr(buddy_ui, "PING_S", 0.05)

    async def scenario():
        async with serving(rig) as server:
            stream = await Stream.open(server)
            try:
                await stream.state()
                block = await stream.raw_block(1.0)
                assert block == "event: ping\ndata: 1\n\n"  # a real event, so the page can tell a hung daemon from a quiet one
            finally:
                stream.close()

    run(scenario)


def test_every_screen_gets_the_same_news(rig):
    async def scenario():
        async with serving(rig) as server:
            streams = [await Stream.open(server) for _ in range(3)]
            try:
                for s in streams:
                    await s.state()
                rig.rx(heartbeat(msg="for all"))
                rig.hub.touch()  # the daemon does this on every core event and once a second
                for s in streams:
                    assert (await s.state(lambda v: v["sessions"]["msg"] == "for all"))["linked"] is True
            finally:
                for s in streams:
                    s.close()

    run(scenario)


def test_there_is_a_limit_to_how_many_screens_can_watch(rig, monkeypatch):
    monkeypatch.setattr(buddy_ui, "MAX_CLIENTS", 2)

    async def scenario():
        async with serving(rig) as server:
            a, b = await Stream.open(server), await Stream.open(server)
            try:
                await a.state()
                await b.state()
                status, _, body = await http(server, path="/events")
                assert status == 503 and b"too many screens" in body
                a.close()
                for _ in range(100):  # the server notices the closed connection
                    if server._streams == 1:
                        break
                    await asyncio.sleep(0.02)
                again = await Stream.open(server)
                assert again.status == 200
                again.close()
            finally:
                a.close()
                b.close()

    run(scenario)


def test_a_screen_that_leaves_is_forgotten(rig):
    async def scenario():
        async with serving(rig) as server:
            stream = await Stream.open(server)
            await stream.state()
            assert server._streams == 1 and len(rig.hub._listeners) == 1
            stream.close()
            for _ in range(100):
                if server._streams == 0:
                    break
                await asyncio.sleep(0.02)
            assert server._streams == 0 and rig.hub._listeners == []

    run(scenario)


def test_stopping_the_server_ends_the_streams(rig):
    async def scenario():
        server = UIServer(rig.hub, port=0)
        await server.start()
        stream = await Stream.open(server)
        await stream.state()
        await asyncio.wait_for(server.stop(), 3)
        with pytest.raises(asyncio.IncompleteReadError):
            await stream.raw_block(1.0)  # the connection was closed under it
        assert rig.hub._listeners == []

    run(scenario)


# -- /decide --


def test_tapping_approve_answers_the_desktop(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            status, headers, body = await decide_request(server, "claude:req_1", "once")
            assert status == 200 and json.loads(body) == {"ok": True}
            assert {"cmd": "permission", "id": "req_1", "decision": "once"} in rig.sent
            assert rig.view()["approvals"] == []

    run(scenario)


def test_tapping_deny_answers_the_desktop(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            status, _, _ = await decide_request(server, "claude:req_1", "deny")
            assert status == 200
            assert {"cmd": "permission", "id": "req_1", "decision": "deny"} in rig.sent
            assert rig.view()["pet"]["denials"] == 1

    run(scenario)


def test_a_request_that_was_already_answered_is_a_conflict(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            assert (await decide_request(server, "claude:req_1", "once"))[0] == 200
            status, _, body = await decide_request(server, "claude:req_1", "deny")
            assert status == 409 and json.loads(body)["error"] == "no longer pending"
            assert (await decide_request(server, "claude:never_existed", "once"))[0] == 409
            answers = [m for m in rig.sent if m.get("cmd") == "permission"]
            assert answers == [{"cmd": "permission", "id": "req_1", "decision": "once"}]

    run(scenario)


def test_always_is_refused_where_it_is_not_offered_and_taken_where_it_is(rig):
    from buddy_core import Approval

    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        got = []
        rig.core.broker.submit(Approval(id="c1", tool="Bash", hint="npm run lint", source="cli",
                                        always="Bash(npm run *) en este proyecto"), got.append)
        async with serving(rig) as server:
            assert [a["always"] for a in rig.view()["approvals"]] == ["", "Bash(npm run *) en este proyecto"]
            status, _, body = await decide_request(server, "claude:req_1", "always")
            assert status == 400 and "always" in json.loads(body)["error"]
            assert not [m for m in rig.sent if m.get("cmd") == "permission"]  # the Mac heard nothing
            assert (await decide_request(server, "cli:c1", "always"))[0] == 200 and got == ["always"]
            assert (await decide_request(server, "cli:c1", "always"))[0] == 409
            assert [a["key"] for a in rig.view()["approvals"]] == ["claude:req_1"]

    run(scenario)


def _nothing_was_answered(rig):
    return not [m for m in rig.sent if m.get("cmd") == "permission"] and rig.view()["approvals"]


def test_without_the_right_token_nothing_is_answered(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            ok_body = json.dumps({"key": "claude:req_1", "decision": "once"}).encode()
            for token in ("", "wrong", server.token[:-1], server.token + "x", "ñandú"):
                headers = {"Content-Type": "application/json", "X-Buddy-Token": token}
                status, _, _ = await http(server, "POST", "/decide", headers=headers, body=ok_body)
                assert status == 403, token
            status, _, _ = await http(server, "POST", "/decide", headers={"Content-Type": "application/json"}, body=ok_body)
            assert status == 403
            assert _nothing_was_answered(rig)

    run(scenario)


def test_a_plain_html_form_cannot_answer_a_prompt(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            body = b"key=claude%3Areq_1&decision=once"
            for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x", None):
                headers = {"X-Buddy-Token": server.token}
                if ctype:
                    headers["Content-Type"] = ctype
                status, _, _ = await http(server, "POST", "/decide", headers=headers, body=body)
                assert status == 415, ctype
            assert _nothing_was_answered(rig)

    run(scenario)


def test_malformed_answers_are_refused(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            bad = [
                b"", b"not json", b"\xff\xfe", b"[]", b"null", b"42", b'"once"',
                b'{"decision": "once"}', b'{"key": "claude:req_1"}',
                b'{"key": "claude:req_1", "decision": "always"}',
                b'{"key": "claude:req_1", "decision": "ONCE"}',
                b'{"key": "claude:req_1", "decision": ["once"]}',
                b'{"key": 7, "decision": "once"}',
                b'{"key": ["claude:req_1"], "decision": "once"}',
            ]
            for body in bad:
                status, _, _ = await decide_request(server, "", "", body=body)
                assert status == 400, body
            assert _nothing_was_answered(rig)

    run(scenario)


def test_the_host_must_be_the_screen_itself_against_dns_rebinding(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            port = server.port
            for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", "", f"10.0.0.5:{port}"):
                assert (await decide_request(server, "claude:req_1", "once", host=host or None, headers={"Host": host}))[0] == 403, host
                assert (await http(server, path="/", headers={"Host": host}))[0] == 403, host
            for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"LOCALHOST:{port}"):
                assert (await http(server, path="/state", headers={"Host": host}))[0] == 200, host
            assert _nothing_was_answered(rig)

    run(scenario)


def test_a_cross_site_page_cannot_answer_a_prompt_even_with_the_token(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, prompt=PROMPT))
        async with serving(rig) as server:
            port = server.port
            for origin in ("http://evil.example", "https://127.0.0.1:%d" % port, "http://127.0.0.1:%d" % (port + 1), "null"):
                status, _, _ = await decide_request(server, "claude:req_1", "once", headers={"Origin": origin})
                assert status == 403, origin
            assert _nothing_was_answered(rig)
            # the page itself, which does send its own origin, still works
            status, _, _ = await decide_request(server, "claude:req_1", "once", headers={"Origin": f"http://127.0.0.1:{port}"})
            assert status == 200

    run(scenario)


def test_rude_clients_get_errors_and_the_server_keeps_going(rig):
    async def scenario():
        async with serving(rig) as server:
            host = f"Host: 127.0.0.1:{server.port}\r\n"
            cases = {
                "garbage": (b"\x00\x01\x02garbage\r\n\r\n", 400),
                "no version": (b"GET /\r\n" + host.encode() + b"\r\n", 400),
                "http/2": (b"GET / HTTP/2\r\n" + host.encode() + b"\r\n", 400),
                "bad header": (b"GET / HTTP/1.1\r\n" + host.encode() + b"no colon here\r\n\r\n", 400),
                "chunked": (b"POST /decide HTTP/1.1\r\n" + host.encode() + b"Transfer-Encoding: chunked\r\n\r\n", 400),
                "bad length": (b"POST /decide HTTP/1.1\r\n" + host.encode() + b"Content-Length: abc\r\n\r\n", 400),
                "negative length": (b"POST /decide HTTP/1.1\r\n" + host.encode() + b"Content-Length: -5\r\n\r\n", 400),
                "huge body": (b"POST /decide HTTP/1.1\r\n" + host.encode() + b"Content-Length: 99999999\r\n\r\n", 413),
                "huge header": (b"GET / HTTP/1.1\r\n" + host.encode() + b"X-Pad: " + b"a" * 20000 + b"\r\n\r\n", 431),
            }
            for name, (raw, expected) in cases.items():
                status, _, _ = await http(server, raw=raw)
                assert status == expected, name
            assert (await http(server, path="/healthz"))[0] == 200

    run(scenario)


def test_a_client_that_never_finishes_is_dropped(rig, monkeypatch):
    monkeypatch.setattr(buddy_ui, "HEADER_TIMEOUT_S", 0.2)

    async def scenario():
        async with serving(rig) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1")  # and then nothing
            await writer.drain()
            assert await asyncio.wait_for(reader.read(-1), 2) == b""  # closed by the server, no reply
            writer.close()
            # a body that stops half way
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(f"POST /decide HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nContent-Length: 50\r\n\r\n{{".encode())
            await writer.drain()
            assert await asyncio.wait_for(reader.read(-1), 2) == b""
            writer.close()
            assert (await http(server, path="/healthz"))[0] == 200

    run(scenario)


def test_a_client_that_hangs_up_mid_request_does_not_hurt(rig):
    async def scenario():
        async with serving(rig) as server:
            for _ in range(5):
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                writer.write(b"GET /state HTTP/1.1\r\nHost: x")
                await writer.drain()
                writer.close()
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)  # asks and leaves before the answer
            writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode())
            await writer.drain()
            writer.close()
            for _ in range(100):  # every handler ends, none is left hanging on a dead socket
                if not server._tasks:
                    break
                await asyncio.sleep(0.02)
            assert not server._tasks
            assert (await http(server, path="/healthz"))[0] == 200

    run(scenario)


# --- the page, run for real --------------------------------------------------------------


@needs_chromium
@pytest.mark.parametrize("demo,screen,mood", [
    ("boot", "boot", "sleep"),
    ("starting", "down", "sleep"),
    ("notready", "down", "sleep"),
    ("waiting", "wait", "sleep"),
    ("pairing", "pair", "sleep"),
    ("idle", "main", "idle"),
    ("busy", "main", "busy"),
    ("celebrate", "main", "celebrate"),
    ("dizzy", "main", "dizzy"),
    ("heart", "main", "heart"),
    ("approval", "ask", "attention"),
    ("approval-queue", "ask", "attention"),
])
def test_the_page_picks_the_right_screen_for_each_demo(demo, screen, mood):
    dom = _dump(f"{PAGE.as_uri()}?demo={demo}&tag=0")
    assert _attr(dom, "data-screen") == screen
    assert _attr(dom, "data-mood") == mood


@needs_chromium
def test_the_page_shows_what_the_buddy_sends_and_escapes_it(rig):
    async def scenario():
        rig.rx(heartbeat(waiting=1, msg="<b>msg</b>", entries=["<img src=x onerror=alert(1)>"],
                         prompt={"id": "r1", "tool": "<i>Bash</i>", "hint": "<script>boom()</script> & \"quotes\""}))
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, _dump, server.url + "/?poll=300")

    dom = run(scenario)
    assert _attr(dom, "data-screen") == "ask"
    assert "&lt;script&gt;boom()&lt;/script&gt; &amp; \"quotes\"" in dom  # shown as text...
    assert "&lt;i&gt;Bash&lt;/i&gt;" in dom
    assert "<script>boom()" not in dom and "<img src=x" not in dom and "<i>Bash" not in dom  # ...never as markup


@needs_chromium
def test_the_page_shows_the_pack_character_and_takes_its_colors(rig):
    async def scenario():
        install_pack(rig)
        rig.rx(heartbeat())
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, _dump, server.url + "/?poll=300")

    dom = run(scenario)
    assert _attr(dom, "data-screen") == "main"
    assert re.search(r'<body[^>]*\bclass="[^"]*\bpack\b', dom)
    assert re.search(r'<body[^>]*style="[^"]*--ground:\s*#000000', dom, re.I)
    # The images may carry the pack's rev (?v=<12 hex>); the page tests say when they must.
    assert re.search(r'src="/character/bufo/idle_0\.gif(\?v=[0-9a-f]{12})?"', dom)
    assert re.search(r'src="/character/bufo/sleep\.gif(\?v=[0-9a-f]{12})?"', dom)
    assert re.search(r'<img[^>]*class="on"[^>]*data-state="idle"', dom) or re.search(r'<img[^>]*data-state="idle"[^>]*class="on"', dom)


@needs_chromium
def test_the_page_shows_the_pairing_code_from_a_live_server(rig):
    async def scenario():
        rig.hub.set_passkey("246810", 2)
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, _dump, server.url + "/?poll=300")

    dom = run(scenario)
    assert _attr(dom, "data-screen") == "pair"
    digits = re.findall(r'<div class="digit(?: done)?">(\d)</div>', dom)
    assert "".join(digits) == "246810"
    assert len(re.findall(r'class="digit done"', dom)) == 2


def test_the_view_lists_the_tasks_the_sessions_report(rig):
    assert rig.view()["tasks"] == []
    rig.core.tasks.update("s1", project="relay", tool="Bash", what="npm test")
    assert rig.view()["tasks"] == [{"id": "s1", "project": "relay", "tool": "Bash", "what": "npm test", "state": "working", "age": 0,
                                    "source": "hook"}]


def test_the_view_adds_title_group_parent_and_said_only_when_non_empty(rig):
    board = rig.core.tasks
    board.update("plain", project="relay", tool="Bash", what="npm test")
    rig.t += 1
    board.update("titled", project="~", tool="Edit", what="x.py", title="Hilos en la pantalla", group="Buddy")
    rig.t += 1
    board.update("kid", project="~", tool="Read", what="y.py", title="Subtarea", group="Buddy", parent="titled",
                 said="Listo.\n\nFalta probarlo.")
    by_id = {t["id"]: t for t in rig.view()["tasks"]}
    assert by_id["plain"] == {"id": "plain", "project": "relay", "tool": "Bash", "what": "npm test", "state": "working",
                              "age": 2, "source": "hook"}
    assert by_id["titled"] == {"id": "titled", "project": "~", "tool": "Edit", "what": "x.py", "state": "working", "age": 1,
                               "title": "Hilos en la pantalla", "group": "Buddy", "source": "hook"}
    assert by_id["kid"] == {"id": "kid", "project": "~", "tool": "Read", "what": "y.py", "state": "working", "age": 0,
                            "title": "Subtarea", "group": "Buddy", "parent": "titled", "said": "Listo.\n\nFalta probarlo.",
                            "source": "hook"}
    board.update("kid", tool="Read", what="y.py", title="", group="", parent="", said="")  # cleared: no keys, not ""
    assert {t["id"]: t for t in rig.view()["tasks"]}["kid"] == {"id": "kid", "project": "~", "tool": "Read", "what": "y.py",
                                                               "state": "working", "age": 0, "source": "hook"}


def test_the_view_lists_at_most_eight_threads_working_first_then_the_newest(rig):
    from buddy_core import MAX_TASKS

    assert UIHub.MAX_TASKS_SHOWN == 8 and MAX_TASKS == 12
    board = rig.core.tasks
    for n in range(MAX_TASKS):
        rig.t += 1
        board.update(f"t{n}", project="p", title=f"hilo {n}", state="idle" if n in (3, 9) else "working")
    shown = rig.view()["tasks"]
    assert len(shown) == 8
    assert [t["id"] for t in shown] == ["t11", "t10", "t8", "t7", "t6", "t5", "t4", "t2"]  # working, newest first
    assert all(t["state"] == "working" for t in shown)
    assert len(board.tasks()) == MAX_TASKS  # the rest are still on the board, just not shown


def test_the_view_with_threads_is_one_json_document_and_text_stays_text(rig):
    hostile = '<img src=x onerror="alert(1)">'
    rig.core.tasks.update("constructor", project=hostile, title=hostile, group="__proto__", parent="__proto__",
                          said=hostile + "\n" + hostile)
    rig.core.tasks.update("__proto__", project="p", title="</script><script>1</script>", group="constructor")
    state = json.loads(rig.hub.current())
    by_id = {t["id"]: t for t in state["tasks"]}
    assert by_id["constructor"]["title"] == hostile and by_id["constructor"]["group"] == "__proto__"
    assert by_id["__proto__"]["title"] == "</script><script>1</script>"
    assert rig.hub.current().isascii()  # the stream carries ASCII only, whatever the sessions said



# --- the census in the view: runs and jobs beside the threads ------------------------------------------


def run_rows(n, first=0, **kw):
    """n census rows, run-<first>, run-<first+1>...; the lower the number, the older the run."""
    return [{"id": f"run-{i}", "name": f"corrida {i}", "group": "Corridas", "state": "working", "elapsed": 5000 - i, **kw}
            for i in range(first, first + n)]


def totals(view):
    return (view["working_total"], view["tasks_total"], view["census_total"], view["tasks_more"])


def test_the_totals_are_taken_before_the_cut_twelve_threads_and_eight_runs(rig):
    board = rig.core.tasks
    for n in range(12):
        rig.t += 1
        board.update(f"t{n}", project="p", title=f"hilo {n}", state="idle" if n in (2, 5) else "working")
    board.replace_census("mac", run_rows(8), 60)
    view = rig.view()
    assert len(view["tasks"]) == 8
    assert [t["id"] for t in view["tasks"]] == ["run-0", "run-1", "run-2", "run-3", "run-4", "t11", "t10", "t9"]  # 5 runs, 3 threads
    assert totals(view) == (18, 20, 8, 12)  # 8 runs and 10 working threads work; two threads wait; 12 are not drawn
    assert len(board.tasks()) == 20  # all of them are still on the board


@pytest.mark.parametrize("hooks,runs,drawn_runs,drawn_hooks", [
    (12, 8, 5, 3), (12, 5, 5, 3), (12, 4, 4, 4), (12, 3, 3, 5), (12, 1, 1, 7), (12, 0, 0, 8),
    (8, 8, 5, 3), (4, 8, 5, 3), (3, 8, 5, 3), (2, 8, 5, 2), (1, 8, 5, 1), (0, 8, 5, 0),
    (6, 6, 5, 3), (5, 2, 2, 5), (0, 3, 3, 0), (0, 0, 0, 0)])
def test_at_most_five_slots_go_to_runs_and_at_least_three_stay_for_threads(rig, hooks, runs, drawn_runs, drawn_hooks):
    assert (UIHub.MAX_TASKS_SHOWN, UIHub.MAX_CENSUS_SHOWN, UIHub.MIN_HOOK_SHOWN) == (8, 5, 3)
    for n in range(hooks):
        rig.t += 1
        rig.core.tasks.update(f"t{n}", project="p", title=f"hilo {n}")
    rig.core.tasks.replace_census("mac", run_rows(runs), 60)
    view = rig.view()
    shown = view["tasks"]
    assert len([t for t in shown if t["source"] == "census"]) == drawn_runs
    assert len([t for t in shown if t["source"] == "hook"]) == drawn_hooks
    assert totals(view) == (hooks + runs, hooks + runs, runs, hooks + runs - len(shown))
    assert [t["source"] for t in shown] == ["census"] * drawn_runs + ["hook"] * drawn_hooks  # runs first, then the threads


def test_runs_come_first_then_jobs_oldest_first_and_threads_keep_their_order(rig):
    board = rig.core.tasks
    board.update("wait-old", project="p", state="idle")
    rig.t += 1
    board.update("work-old", project="p")
    rig.t += 1
    board.update("work-new", project="p")
    rig.t += 1
    board.update("wait-new", project="p", state="idle")
    rows = [{"id": "job-largo", "name": "largo", "elapsed": 9000}, {"id": "run-nueva", "name": "nueva", "elapsed": 30},
            {"id": "run-vieja", "name": "vieja", "elapsed": 7000}, {"id": "job-corto", "name": "corto", "elapsed": 90}]
    board.replace_census("mac", rows, 60)
    assert [t["id"] for t in rig.view()["tasks"]] == [
        "run-vieja", "run-nueva", "job-largo", "job-corto", "work-new", "work-old", "wait-new", "wait-old"]


def test_a_census_row_in_the_view_carries_its_clocks_group_source_and_no_hook_text(rig):
    board = rig.core.tasks
    board.update("plain", project="relay", tool="Bash", what="npm test")
    board.replace_census("mac", [{"id": "run-quietud", "name": "quietud", "group": "Corridas", "state": "working",
                                  "elapsed": 3643, "idle": 538}, {"id": "job-noche13", "name": "noche13", "elapsed": 3048}], 60)
    rig.t += 7
    by_id = {t["id"]: t for t in rig.view()["tasks"]}
    assert by_id["run-quietud"] == {"id": "run-quietud", "project": "quietud", "title": "quietud", "group": "Corridas", "tool": "",
                                    "what": "", "state": "working", "age": 545, "source": "census", "elapsed": 3650, "idle": 545}
    assert by_id["job-noche13"] == {"id": "job-noche13", "project": "noche13", "title": "noche13", "group": "Corridas", "tool": "",
                                    "what": "", "state": "working", "age": 0, "source": "census", "elapsed": 3055}  # no idle key
    plain = by_id["plain"]  # a thread has neither clock
    assert plain["source"] == "hook" and "elapsed" not in plain and "idle" not in plain and plain["age"] == 7


def test_a_run_that_is_a_thread_is_one_row_in_the_view(rig):
    sid = "3f2a9c1e-7b44-4d0a-9e51-0c8d6a2b1f77"
    rig.core.tasks.update(sid, project="relay-x", tool="Bash", what="pnpm e2e", said="Voy bien.", title="Hilo", group="Buddy")
    rig.core.tasks.replace_census("mac", [{"id": "run-x", "name": "x", "elapsed": 60, "idle": 5, "session": sid}], 60)
    view = rig.view()
    (row,) = view["tasks"]
    assert (row["id"], row["title"], row["group"], row["tool"], row["what"], row["said"], row["source"]) == (
        "run-x", "x", "Corridas", "Bash", "pnpm e2e", "Voy bien.", "census")
    assert totals(view) == (1, 1, 1, 0)  # counted once


def test_census_total_adds_what_the_sources_could_not_list_and_forgets_it_with_the_source(rig):
    board = rig.core.tasks
    board.update("t", project="p")
    board.replace_census("mac", run_rows(8), 60, more=4)
    board.replace_census("linux", run_rows(2, first=20), 5, more=1)
    view = rig.view()
    assert view["census_total"] == 8 + 4 + 2 + 1
    # the runs nobody listed work too: they are in every total, so the header, the group count and "+K" agree
    assert (view["working_total"], view["tasks_total"], view["tasks_more"]) == (16, 16, 16 - len(view["tasks"]))
    rig.t += 6  # the linux source expired
    assert rig.view()["census_total"] == 8 + 4
    rig.t += 60
    assert totals(rig.view()) == (1, 1, 0, 0)


def test_the_totals_with_only_threads_count_the_whole_board_too(rig):
    from buddy_core import MAX_TASKS

    board = rig.core.tasks
    assert totals(rig.view()) == (0, 0, 0, 0)
    for n in range(MAX_TASKS):
        rig.t += 1
        board.update(f"t{n}", project="p", state="idle" if n in (3, 9) else "working")
    view = rig.view()
    assert len(view["tasks"]) == 8 and totals(view) == (10, 12, 0, 4)  # "10 working", not the 8 that are drawn


def test_without_a_board_the_view_has_no_threads_and_zero_totals():
    hub = UIHub()
    hub.core = object()
    assert hub._tasks_view() == ([], {"working_total": 0, "tasks_total": 0, "census_total": 0, "tasks_more": 0})


def test_a_running_clock_changes_the_view_every_second_without_any_new_post(rig):
    rig.core.tasks.replace_census("mac", run_rows(1), 60)
    seen = []
    rig.hub.subscribe(seen.append)
    assert rig.hub.refresh() is True
    assert rig.hub.refresh() is False  # nothing moved
    rig.t += 1
    assert rig.hub.refresh() is True and json.loads(seen[-1])["tasks"][0]["elapsed"] == 5001  # the page sees it tick


def test_census_names_in_the_view_are_text_and_the_stream_stays_ascii(rig):
    hostile = '<img src=x onerror="alert(1)">'
    rig.core.tasks.replace_census("mac", [
        {"id": "run-a", "name": hostile, "group": "</script><script>1</script>", "elapsed": 1, "project": hostile},
        {"id": "job-b", "name": "ñandú ✓", "elapsed": 2},
        {"id": "run-constructor", "name": "constructor", "group": "__proto__", "elapsed": 3, "session": "__proto__"}], 60)
    rig.core.tasks.update("__proto__", project="p", title="</script>", group="constructor")
    state = json.loads(rig.hub.current())
    by_id = {t["id"]: t for t in state["tasks"]}
    assert by_id["run-a"]["title"] == hostile and by_id["run-a"]["group"].startswith("</script>")
    assert by_id["job-b"]["title"] == "ñandú ✓" and by_id["run-constructor"]["group"] == "__proto__"
    assert "__proto__" not in by_id  # the thread of that name is the run
    assert rig.hub.current().isascii()  # the stream carries ASCII only, whatever the names were


def test_the_view_always_carries_a_list_of_notices(rig):
    assert rig.view()["notices"] == [] and json.loads(rig.hub.current())["notices"] == []
    rig.core.notices.add(title="La tanda terminó", body="Quedó en verde", kind="ok", notice_id="tanda")
    rig.t += 12
    assert rig.view()["notices"] == [
        {"id": "tanda", "title": "La tanda terminó", "body": "Quedó en verde", "kind": "ok", "source": "hermes", "age": 12, "rev": 1}]
    rig.core.notices = None  # a core with no board: still a list
    assert rig.view()["notices"] == []
