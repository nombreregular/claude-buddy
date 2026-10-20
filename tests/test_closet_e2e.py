"""Mochi's closet from the tool to the screen: the costumes tools/make_closet.py really draws, the daemon that wears them,
the route that serves their images and the page that shows them.

The other test files each take one piece apart (test_closet_tool.py draws, test_closet.py wears with fake GIFs, test_ui.py and
the test_page_* files serve and show). This one joins the pieces with nothing faked on the way: a closet made by running the
command, a core that has the committed Mochi pack active as `buddy`, a real UIHub with a real Closet, a real UIServer on
loopback, the real `buddy.py run_daemon`, and the page in a headless Chromium. Only the dice and the clock are the test's (a
costume is worn when the script says so, and the hub's clock is a number that moves by hand), and the core's clock stands still.

What it proves, in order: the daemon accepts what the tool leaves; the route serves it byte for byte and nothing else of the
closet; a costumed GIF is the plain Mochi frame plus the costume over the head, in every frame of every state; a new look moves
`name` and `rev` (over HTTP, over the event stream, and in a living page that swaps without an empty frame); a costume made while
the daemon runs is in the dice at the next roll; the page draws the costume on the real 800x480 layout in pixels.

CLOSET_E2E_SHOTS=<folder> keeps a copy of every screenshot the page tests take, so a person can look at them. Nothing else is
written outside the tmp dirs. Needs Pillow (the tool and the GIF decoding use it; the daemon never does); the page tests also need
a Chromium and skip without one."""
import asyncio
import html
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="Pillow is only needed to draw the costumes")
from PIL import Image, ImageChops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
PAGE = ROOT / "ui" / "index.html"
PACK = ROOT / "characters" / "buddy-pixel"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy  # noqa: E402
import closet_costumes as cc  # noqa: E402
from buddy_closet import MAX_FILE_BYTES, Closet  # noqa: E402
from buddy_core import BuddyCore, encode_line  # noqa: E402
from buddy_ui import UIHub  # noqa: E402
from page_runner import chromium, needs_chromium  # noqa: E402
from test_ui import PROMPT, REV, Radio, heartbeat, run, serving  # noqa: E402
from ui_client import Stream, http  # noqa: E402

HOUR = 3600.0
EVERY = 3 * HOUR
GRID = 32  # the generator's grid: the GIF is that grid scaled 3x
FILES = ["sleep", "idle_0", "idle_1", "idle_2", "busy", "attention", "celebrate", "dizzy", "heart"]  # the pack's nine GIFs
GROUND = (14, 13, 20)  # #0E0D14, the `bg` of the pack: what shows through a transparent GIF pixel
# Where the page draws Mochi (ui/index.html, #gif: left 74, top 144 on the 800x480 stage, the 96 px GIF shown at 192 px), so one
# grid cell is 6x6 screen pixels. The living-page test ties these numbers to the real DOM, so a layout change fails there first.
FIGURE = (74, 144)
CELL = 6
# What a costume may touch, from the contract (not from the library): at most 7 rows tall and 14 wide, and the highest the head
# top ever gets is row 4, so it rises at most 4 rows above the head; its bottom rows dip at most 2 rows into the dome.
MAX_RISE, MAX_DIP, HALF_WIDTH = 4, 2, 7


# --- what a person does: make a closet, wear from it ---------------------------------------------------------------


def make_closet(root, *args):
    """Run the real command (a subprocess, the way it is typed) and return what it printed."""
    done = subprocess.run([sys.executable, str(TOOLS / "make_closet.py"), str(root), *args], capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, done.stderr[-800:]
    return done.stdout


@pytest.fixture(scope="module")
def closet(tmp_path_factory):
    """Eight real costumes (one of every template), made by the command."""
    root = tmp_path_factory.mktemp("e2e") / "closet"
    out = make_closet(root, "--count", "8", "--seed", "0")
    assert out.count("Buddy se hizo:") == 8
    return root


@pytest.fixture(scope="module")
def anchors(tmp_path_factory):
    """Where the head is in every frame of every GIF, from the generator itself (`--anchors`), after proving that the committed
    pack is what that generator draws (so the anchors are the committed pack's)."""
    out = tmp_path_factory.mktemp("anchors")
    done = subprocess.run([sys.executable, str(TOOLS / "make_pixel_buddy.py"), str(out / "pack"), "--anchors", str(out / "a.json")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
    assert {p.name: p.read_bytes() for p in (out / "pack").glob("*.gif")} == {p.name: p.read_bytes() for p in PACK.glob("*.gif")}, \
        "characters/buddy-pixel is not what tools/make_pixel_buddy.py draws"
    found = json.loads((out / "a.json").read_text(encoding="utf-8"))
    assert list(found) == FILES
    return found


def ids_of(root):
    return sorted(p.name for p in Path(root).iterdir() if not p.name.startswith("."))


def of_template(root, template):
    """The id of the costume of `template` that the closet has."""
    found = [i for i in ids_of(root) if i.startswith(template + "-")]
    assert len(found) == 1, (template, found)
    return found[0]


class Scripted(Closet):
    """The real closet with the dice replaced by a list: pick() answers with the costume the next id names, and "sin disfraz"
    ("") once the list runs out. Scanning, checking and reading are the real ones."""

    def __init__(self, root, script=()):
        super().__init__(root)
        self.script = list(script)

    def pick(self):
        want = self.script.pop(0) if self.script else ""
        return next((c for c in self.costumes if c.id == want), None)


class Dressed:
    """A core with the committed Mochi pack active as `buddy`, and a hub that wears from a real closet. The hub's clock is
    `self.t` (a look lasts EVERY seconds on it) unless a `clock` is given; the core's clock never moves, so the Mac stays
    "linked" whatever the hub's clock does."""

    def __init__(self, tmp_path, root, script=(), every=EVERY, clock=None, packs=("buddy",)):
        self.t = 1000.0
        self.sent, self.said = [], []
        self.core = BuddyCore(Path(tmp_path) / "state", self.sent.append, clock=lambda: 1000.0, wall=lambda: 1_800_000_000.0)
        for name in packs:
            shutil.copytree(PACK, self.core.state_dir / "characters" / name)
        self.core.state.character = packs[0]
        self.closet = Scripted(root, script)
        self.hub = UIHub(clock=clock or (lambda: self.t), closet=self.closet, closet_every=every, notice=self.said.append)
        self.hub.bind(self.core, Radio())

    def rx(self, obj):
        self.core.feed(encode_line(obj))

    def be(self, mood):
        """Put the real core in one of the moods the page is shot in."""
        if mood == "idle":
            self.rx(heartbeat())
        elif mood == "busy":
            self.rx(heartbeat(running=1, msg="working"))
        elif mood == "heart":  # a quick approval makes Buddy throw hearts
            self.rx(heartbeat(waiting=1, msg="approve: Bash", prompt=PROMPT))
            assert self.hub.decide("claude:req_1", "once")
        assert self.hub.view()["mood"] == mood

    def wear(self, ident=""):
        """Roll the dice by hand: the next pick is `ident` ("" is no costume) and a look's time has passed."""
        self.closet.script = [ident]
        self.t += EVERY + 1
        self.hub.refresh()
        return self.hub.character()

    def close(self):
        self.hub.close()


# --- GIFs as grids ---------------------------------------------------------------------------------------------------


def cells(img):
    """{(x, y): (r, g, b) or None} for the 32x32 grid behind a 96x96 frame (None is a transparent cell)."""
    px = img.convert("RGBA").resize((GRID, GRID), Image.NEAREST).load()
    return {(x, y): (px[x, y][:3] if px[x, y][3] else None) for y in range(GRID) for x in range(GRID)}


def timeline(data, ms):
    """The frames of a GIF's bytes as grids, on the generator's timeline (Pillow folds identical neighbours into one longer
    frame; this unfolds them, so frame i is the i-th frame the generator drew)."""
    out = []
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (96, 96)
        for i in range(im.n_frames):
            im.seek(i)
            out.extend([cells(im)] * max(1, round(im.info.get("duration", ms) / ms)))
    return out


def plain_gif(name):
    return (PACK / f"{name}.gif").read_bytes()


def assert_plain_plus_costume(ident, manifest, served, anchors):
    """`served` is {file name: bytes} as the screen's route gave them for the worn costume. In every frame of every one of
    the nine GIFs the picture is the plain Mochi frame plus the costume, and the costume only lies over the head: above it
    and on its top rows, centred on the head whatever the bob or the lean, with the face, the body and the extras untouched."""
    template, color = manifest["costume"]["template"], manifest["costume"]["color"]
    for name in FILES:
        info = anchors[name]
        got = timeline(served[f"{name}.gif"], info["ms"])
        plain = timeline(plain_gif(name), info["ms"])
        assert len(got) == len(plain) == len(info["frames"]), (ident, name)
        for i, (frame, base) in enumerate(zip(got, plain)):
            where = (ident, name, i)
            top, cx = info["frames"][i]["top_y"], info["frames"][i]["cx"]
            changed = sorted(p for p in frame if frame[p] != base[p])
            assert changed, f"{where}: the costume is not on this frame"
            # independent of the library: only the rows around the top of the head, only the columns around its middle
            outside = [(x, y) for x, y in changed if not (top - MAX_RISE <= y <= top + MAX_DIP and abs(x - cx) <= HALF_WIDTH)]
            assert not outside, f"{where}: pixels changed away from the head: {outside[:5]}"
            above = [(x, y) for x, y in changed if y < top]
            assert above, f"{where}: nothing of the costume rises above the head"
            assert min(x for x, _ in above) + max(x for x, _ in above) == 2 * cx, f"{where}: not centred on the head"
            # and exactly what the library says: the plain frame with the costume drawn over it
            want = dict(base)
            want.update({p: c[:3] for p, c in cc.place(template, color, {"top_y": top, "cx": cx, "dx": 0}).items()})
            wrong = sorted(p for p in want if want[p] != frame[p])
            assert not wrong, f"{where}: the frame is not plain + costume at {wrong[:5]}"


def manifest_of(root, ident):
    return json.loads((Path(root) / ident / "manifest.json").read_text(encoding="utf-8"))


async def fetch_all(server, rig, ident):
    """Every GIF of the costume the hub wears, through the route, the way the page asks for them (?v=rev)."""
    character = rig.hub.character()
    assert character["name"] == ident, character["name"]
    served = {}
    for name in FILES:
        status, headers, body = await http(server, path=f"/character/{ident}/{name}.gif?v={character['rev']}")
        assert status == 200 and headers["content-type"] == "image/gif", (ident, name, status)
        served[f"{name}.gif"] = body
    return served


# --- the tool's closet in the daemon -------------------------------------------------------------------------------------


def test_the_daemon_accepts_every_costume_the_tool_made_and_the_tool_leaves_nothing_else(closet):
    found = Closet(closet)
    costumes = found.scan()
    ids = ids_of(closet)
    assert len(ids) == 8 and [c.id for c in costumes] == ids
    assert found.skipped == {} and found.skipped_text() == ""
    assert sorted(p.name for p in Path(closet).iterdir()) == ids  # no dot folder (the tool's work folder) is left behind
    listed = {}
    for line in make_closet(closet, "--list").splitlines()[1:]:
        ident, _, name = line.strip().partition("  ")
        listed[ident] = name
    assert {c.id: c.name for c in costumes} == listed  # what the tool says it has is what the daemon sees, names and all
    for c in costumes:
        assert c.base == "buddy" and set(c.files) == {f"{n}.gif" for n in FILES}, c.id
        manifest = manifest_of(closet, c.id)
        assert c.name == manifest["costume"]["name"] and manifest["name"] == c.id
        for fname in c.files:
            assert 0 < (c.folder / fname).stat().st_size < MAX_FILE_BYTES // 4, (c.id, fname)  # well under the daemon's cap


def test_a_stale_work_folder_of_a_tool_that_died_is_not_a_costume_and_is_not_even_counted(tmp_path, closet):
    root = tmp_path / "closet"
    shutil.copytree(closet, root)
    shutil.copytree(root / of_template(root, "gato"), root / ".gato-lila.tmp")  # what a crashed run leaves
    found = Closet(root)
    assert len(found.scan()) == 8 and found.skipped == {}
    assert make_closet(root, "--list").splitlines()[0] == "El closet de Buddy tiene 8 disfraces:"


# --- the route and the pixels ------------------------------------------------------------------------------------------


def test_every_costume_reaches_the_screen_as_the_plain_mochi_plus_the_costume_over_the_head(tmp_path, closet, anchors):
    rig = Dressed(tmp_path, closet, [""])
    template_of = {i: manifest_of(closet, i)["costume"]["template"] for i in ids_of(closet)}
    assert sorted(template_of.values()) == sorted(t.id for t in cc.TEMPLATES)  # all eight shapes, each once

    async def scenario():
        async with serving(rig) as server:
            assert rig.hub.character()["name"] == "buddy" and "costume" not in rig.hub.character()
            for name in FILES:   # bare: the route serves the pack itself, byte for byte
                status, _, body = await http(server, path=f"/character/buddy/{name}.gif")
                assert status == 200 and body == plain_gif(name), name
            for ident in ids_of(closet):
                character = rig.wear(ident)
                status, _, state = await http(server, path="/state")
                sent = json.loads(state)["character"]
                manifest = manifest_of(closet, ident)
                assert status == 200 and sent == character, ident
                assert sent["name"] == ident and sent["costume"] == manifest["costume"]["name"] and REV.fullmatch(sent["rev"])
                assert sent["render"] == "pixel" and sent["colors"] == manifest["colors"] and sent["accents"] == manifest["accents"]
                assert sent["states"] == {k: v if isinstance(v, list) else [v] for k, v in manifest["states"].items()}
                served = await fetch_all(server, rig, ident)
                for fname, body in served.items():   # byte for byte what the tool wrote
                    assert body == (Path(closet) / ident / fname).read_bytes(), (ident, fname)
                assert_plain_plus_costume(ident, manifest, served, anchors)
            back = rig.wear("")  # and the costume off: the pack is exactly what it was
            assert back["name"] == "buddy" and "costume" not in back
            status, _, body = await http(server, path="/character/buddy/idle_0.gif")
            assert status == 200 and body == plain_gif("idle_0")

    try:
        run(scenario)
    finally:
        rig.close()


def test_the_route_gives_the_costume_that_is_on_and_nothing_else_of_the_closet(tmp_path, closet):
    on, other = of_template(closet, "gato"), of_template(closet, "mago")
    rig = Dressed(tmp_path, closet, [on])

    async def scenario():
        async with serving(rig) as server:
            assert (await http(server, path=f"/character/{on}/idle_0.gif"))[0] == 200
            for path in (f"/character/{on}/manifest.json", f"/character/{on}/nothing.gif", f"/character/{other}/idle_0.gif",
                         "/character/buddy/idle_0.gif",  # the pack under the costume is not what the screen was told about
                         f"/character/{on}/..%2Fmanifest.json", f"/character/{on}/%2e%2e%2f{other}%2fidle_0.gif",
                         f"/character/{on}/idle_0.gif/x", f"/character/{on}", f"/character/{on}/", "/character/",
                         f"/character/.{on}.tmp/idle_0.gif", "/character/closet/gato"):
                assert (await http(server, path=path))[0] == 404, path
            assert (await http(server, "POST", f"/character/{on}/idle_0.gif"))[0] == 405
            rig.wear("")  # the costume comes off: its images are gone from the route and the pack's are back
            assert (await http(server, path=f"/character/{on}/idle_0.gif"))[0] == 404
            assert (await http(server, path="/character/buddy/idle_0.gif"))[0] == 200

    try:
        run(scenario)
    finally:
        rig.close()


def test_a_look_never_touches_what_the_core_keeps_and_the_pack_underneath_is_never_changed(tmp_path, closet):
    rig = Dressed(tmp_path, closet, [of_template(closet, "chef")])
    before = {p.name: p.read_bytes() for p in (rig.core.state_dir / "characters" / "buddy").iterdir()}
    assert rig.hub.character()["name"] != "buddy"
    for ident in ids_of(closet):
        rig.wear(ident)
    rig.wear("")
    rig.core.store.save()
    assert json.loads((rig.core.state_dir / "state.json").read_text(encoding="utf-8"))["character"] == "buddy"
    assert rig.core.state.character == "buddy"
    assert {p.name: p.read_bytes() for p in (rig.core.state_dir / "characters" / "buddy").iterdir()} == before
    assert {p.name: p.read_bytes() for p in PACK.iterdir()} == before   # and the repo's pack is what was installed
    rig.close()


def test_a_costume_is_worn_only_over_the_pack_it_was_drawn_for(tmp_path):
    root = tmp_path / "closet"
    make_closet(root, "--count", "1", "--seed", "3", "--base-name", "otro")
    (ident,) = ids_of(root)
    assert manifest_of(root, ident)["costume"]["base"] == "otro"
    rig = Dressed(tmp_path, root, [ident], packs=("buddy", "otro"))
    assert rig.hub.character()["name"] == "buddy" and "costume" not in rig.hub.character()  # the pack in use is `buddy`
    rig.core.state.character = "otro"
    assert rig.hub.character()["name"] == ident and rig.hub.character()["costume"] == manifest_of(root, ident)["costume"]["name"]
    rig.core.state.character = "buddy"
    assert rig.hub.character()["name"] == "buddy"
    rig.core.state.character = "ninguno"  # no such pack: the page keeps drawing its own Buddy
    assert rig.hub.character() is None
    rig.close()


def test_a_costume_made_while_the_daemon_runs_is_in_the_dice_at_the_next_roll(tmp_path, closet):
    root = tmp_path / "closet"
    shutil.copytree(closet, root)
    rig = Dressed(tmp_path, root, [""])
    assert len(rig.closet.costumes) == 8
    out = make_closet(root, "--count", "1")  # a person runs the tool again, no restart
    (new,) = [i for i in ids_of(root) if i not in {c.id for c in rig.closet.costumes}]
    assert out.strip().startswith("Buddy se hizo:")
    assert rig.hub.character()["name"] == "buddy"  # nothing changes until the next roll...
    character = rig.wear(new)  # ...and then it is in the dice, and on
    assert character["name"] == new and len(rig.closet.costumes) == 9 and rig.closet.skipped == {}
    assert any(line == "Buddy se puso " + manifest_of(root, new)["costume"]["name"] for line in rig.said)
    rig.close()


def test_a_gif_copied_over_the_worn_costume_changes_its_rev_and_what_the_route_serves(tmp_path, closet):
    root = tmp_path / "closet"
    shutil.copytree(closet, root)
    a, b = of_template(root, "gato"), of_template(root, "flor")
    rig = Dressed(tmp_path, root, [a])
    first = rig.hub.character()
    assert first["name"] == a and rig.hub.character()["rev"] == first["rev"]  # still the same while nothing moves

    async def scenario():
        async with serving(rig) as server:
            old = (await http(server, path=f"/character/{a}/idle_0.gif"))[2]
            shutil.copyfile(root / b / "idle_0.gif", root / a / "idle_0.gif")   # what an rsync of a newer costume does
            os.utime(root / a / "idle_0.gif", (1_900_000_000, 1_900_000_000))
            now = rig.hub.character()
            assert now["name"] == a and now["costume"] == first["costume"] and now["rev"] != first["rev"]
            new = (await http(server, path=f"/character/{a}/idle_0.gif?v={now['rev']}"))[2]
            assert new == (root / b / "idle_0.gif").read_bytes() and new != old

    try:
        run(scenario)
    finally:
        rig.close()


# --- a look comes and goes on its own ------------------------------------------------------------------------------------


def test_the_hubs_own_timer_pushes_each_new_look_to_a_screen_that_is_open(tmp_path, closet):
    on = of_template(closet, "corona")

    async def scenario():
        # a real clock and a look that lasts 0.4 s: nobody calls refresh or touch, the timer of the hub is the only thing that moves
        rig = Dressed(tmp_path, closet, [on] + ["", on] * 40, every=0.4, clock=time.monotonic)
        try:
            async with serving(rig) as server:
                stream = await Stream.open(server)
                seen = []
                try:
                    end = asyncio.get_running_loop().time() + 8
                    while len({c["name"] for c in seen}) < 2 or len(seen) < 4:
                        name, data = await stream.event(max(0.1, end - asyncio.get_running_loop().time()))
                        if name == "state":
                            seen.append(json.loads(data)["character"])
                finally:
                    stream.close()
                return seen
        finally:
            rig.close()

    seen = run(scenario)
    names = [c["name"] for c in seen]
    assert set(names) == {on, "buddy"}
    assert all(a != b for a, b in zip(names, names[1:]))  # every event is a change, never a repeat
    worn, bare = [c for c in seen if c["name"] == on], [c for c in seen if c["name"] == "buddy"]
    assert worn[0]["costume"] == manifest_of(closet, on)["costume"]["name"] and all("costume" not in c for c in bare)
    assert len({c["rev"] for c in worn}) == 1 and len({c["rev"] for c in bare}) == 1 and worn[0]["rev"] != bare[0]["rev"]


# --- the daemon as the service runs it -------------------------------------------------------------------------------------


class Transport:
    """What run_daemon needs from the Bluetooth transport, without Bluetooth."""

    ready = True
    device_name = "Claude-1234"
    not_ready_reason = ""
    secure = True
    address = "00:00:00:00:00:00"

    def send(self, message):
        pass

    def unpair(self):
        pass

    def is_secure(self):
        return True

    async def start(self):
        pass

    async def stop(self):
        pass

    async def forget_all(self):
        return 0


class WearsFirst(Closet):
    """The real closet, whose dice always say: the first costume."""

    def pick(self):
        return self.costumes[0] if self.costumes else None


def daemon_args(tmp_path, *extra):
    return buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--no-keys", "--ui-port", "0", "--night", "off",
                             "--backlight", "off", *extra])


def installed_pack(tmp_path):
    """The Pi's state dir: the committed pack installed as `buddy` and the active one."""
    state = tmp_path / "state"
    shutil.copytree(PACK, state / "characters" / "buddy")
    (state / "state.json").write_text(json.dumps({"character": "buddy"}), encoding="utf-8")


def test_the_daemon_run_with_the_flags_wears_a_real_costume_and_the_screen_gets_it_end_to_end(tmp_path, closet, anchors, monkeypatch,
                                                                                         capsys):
    installed_pack(tmp_path)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: Transport())
    monkeypatch.setattr(buddy, "Closet", WearsFirst)
    first = ids_of(closet)[0]
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(buddy.run_daemon(daemon_args(tmp_path, "--closet", str(closet), "--closet-every", "0")))
        out = ""
        try:
            while "Pantalla en http://127.0.0.1:" not in out:
                out += capsys.readouterr().out
                await asyncio.sleep(0.01)
            port = int(re.search(r"Pantalla en http://127\.0\.0\.1:(\d+)", out).group(1))
            seen["state"] = json.loads((await http(port, path="/state"))[2])["character"]
            rev = seen["state"]["rev"]
            seen["files"] = {}
            for name in FILES:
                status, _, body = await http(port, path=f"/character/{first}/{name}.gif?v={rev}")
                assert status == 200, name
                seen["files"][f"{name}.gif"] = body
            seen["pack"] = (await http(port, path="/character/buddy/idle_0.gif"))[0]
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 5) == 0
        finally:
            if not task.done():
                task.cancel()
        seen["out"] = out + capsys.readouterr().out

    run(scenario)
    manifest = manifest_of(closet, first)
    assert seen["state"]["name"] == first and seen["state"]["costume"] == manifest["costume"]["name"] and seen["pack"] == 404
    assert_plain_plus_costume(first, manifest, seen["files"], anchors)
    out = seen["out"]
    order = [out.index(text) for text in ("Closet: 8 disfraces, elige uno solo al arrancar", "Buddy se puso " + manifest["costume"]["name"],
                                           "Pantalla en http://127.0.0.1:")]
    assert order == sorted(order) and "Traceback" not in out
    assert json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))["character"] == "buddy"


# --- the page ------------------------------------------------------------------------------------------------------------------


def spy(rig):
    """Remember every image the screen asked the hub for: a list of (pack name, file name, served)."""
    seen = []
    real = rig.hub.character_bytes

    def character_bytes(pack, fname):
        data = real(pack, fname)
        seen.append((pack["name"], fname, data is not None))
        return data

    rig.hub.character_bytes = character_bytes
    return seen


def serve_page(rig, work, **kw):
    """Run `work(server)` (it blocks: it starts a browser) against the real screen server of `rig`."""
    async def scenario():
        async with serving(rig, **kw) as server:
            return await asyncio.get_running_loop().run_in_executor(None, work, server)

    return run(scenario)


class BrowserDied(Exception):
    """The headless Chromium gave nothing (it crashed or hung); nothing to do with what the page did."""


def browse(url, *flags, budget=3000, timeout=60):
    """A headless Chromium on `url`, in an 800x480 window with one device pixel per CSS pixel (what the Pi's panel is), on the
    page_runner's flags (--password-store=basic and the rest). Returns what the process left; the caller says what makes it good."""
    cmd = [chromium(), "--headless", "--no-sandbox", "--disable-gpu", "--password-store=basic", "--hide-scrollbars",
           "--force-device-scale-factor=1", "--window-size=800,480", f"--virtual-time-budget={int(budget)}", *flags, url]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise BrowserDied("the browser did not finish") from err


def dump_dom(url, budget=3000):
    """The DOM after the page's script ran (page_runner.dump, but a browser that dies while shutting down, after it printed the
    page, still counts: on a busy machine Chromium's own watchdog does that now and then)."""
    done = browse(url, "--dump-dom", budget=budget)
    if "</html>" not in done.stdout:
        raise BrowserDied(done.stderr[-300:])
    return done.stdout


def shoot(url, png, budget=3000):
    """A screenshot of the page (same remark: a screenshot that was written counts)."""
    Path(png).unlink(missing_ok=True)
    browse(url, f"--screenshot={png}", budget=budget)
    if not Path(png).is_file() or Path(png).stat().st_size == 0:
        raise BrowserDied("no screenshot")


def retried(attempt, tries=2):
    """attempt(n) -> result, from scratch each time (n numbers the attempts, for fresh folders). A browser that died runs the
    whole attempt once more before it counts as a failure; anything the page or the daemon did wrong is never retried."""
    for n in range(tries):
        try:
            return attempt(n)
        except BrowserDied:
            if n == tries - 1:
                raise


def keep(png, name):
    """CLOSET_E2E_SHOTS=<folder>: a copy of the screenshot there, for a person to look at."""
    folder = os.environ.get("CLOSET_E2E_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        shutil.copy(png, Path(folder) / name)


def screen_cells(png):
    """The 32x32 cells of the figure in a screenshot (the 192 px figure: a cell is a flat 6x6 block, nothing blurs it)."""
    shot = Image.open(png).convert("RGB")
    assert shot.size == (800, 480), shot.size
    x, y = FIGURE
    figure = shot.crop((x, y, x + GRID * CELL, y + GRID * CELL))
    small = figure.resize((GRID, GRID), Image.NEAREST)
    assert ImageChops.difference(figure, small.resize(figure.size, Image.NEAREST)).getbbox() is None, "the figure is not made of flat blocks"
    px = small.load()
    return {(x, y): px[x, y] for y in range(GRID) for x in range(GRID)}


def on_ground(frame):
    """A GIF frame as the screen shows it: a transparent cell is the ground of the pack."""
    return {p: (GROUND if c is None else c) for p, c in frame.items()}


def shown(screen, candidates):
    """Which of the candidate frames ({"file": [frames]}) the screen shows: (file, index) of the one that matches it cell for
    cell, or None. The GIF animates on its own clock, so any of its frames may be the one in the screenshot."""
    for name, frames in candidates.items():
        for i, frame in enumerate(frames):
            if on_ground(frame) == screen:
                return name, i
    return None


MOOD_FILES = {"idle": ["idle_0", "idle_1", "idle_2"], "busy": ["busy"], "heart": ["heart"]}


@needs_chromium
@pytest.mark.parametrize("template", ["mago", "chef", "gato"])
def test_the_page_draws_the_costume_on_mochis_head_on_the_real_layout_in_pixels(tmp_path, closet, anchors, template):
    """The tallest hat, the widest one and a pair of ears, each in the three moods the screen lives in. The 192 px figure on the
    real 800x480 layout is, cell for cell, a frame of the costume's own GIF on the ground of the pack: nothing (the bubble, the
    plate, the bar, the glow) covers it, nothing clips it, and it is not a frame of the plain pack."""
    ident = of_template(closet, template)
    manifest = manifest_of(closet, ident)
    for mood, names in MOOD_FILES.items():
        def attempt(n):
            rig = Dressed(tmp_path / f"{template}-{mood}-{n}", closet, [ident])
            rig.be(mood)
            served = spy(rig)
            png = tmp_path / f"{template}-{mood}-{n}.png"
            try:
                serve_page(rig, lambda server: shoot(server.url + "/?poll=300", png))
            finally:
                rig.close()
            return rig, served, png

        rig, served, png = retried(attempt)
        keep(png, f"{mood}-{ident}.png")
        assert rig.hub.character()["name"] == ident and rig.hub.view()["mood"] == mood
        wanted = {n: timeline((Path(closet) / ident / f"{n}.gif").read_bytes(), anchors[n]["ms"]) for n in names}
        plain = {n: timeline(plain_gif(n), anchors[n]["ms"]) for n in names}
        screen = screen_cells(png)
        got = shown(screen, wanted)
        if got is None:  # say how close the nearest frame was, so a failure is something to look at
            near = min((sum(1 for p, c in on_ground(f).items() if c != screen[p]), n, i) for n, fs in wanted.items() for i, f in enumerate(fs))
            raise AssertionError(f"{ident} in {mood}: the figure on the screen is no frame of the costume's GIF (nearest: {near})")
        assert shown(screen, plain) is None, f"{ident} in {mood}: the screen shows the plain pack, not the costume"
        name, index = got
        costume = {p for p, c in wanted[name][index].items() if c != plain[name][index][p]}
        assert costume and min(y for _, y in costume) >= 0
        assert {n for pack, n, ok in served if pack == ident and ok} >= {f"{n}.gif" for n in names}  # it asked for these and got them
        assert not [1 for pack, _, _ in served if pack != ident]  # and never for the pack under the costume
        assert manifest["costume"]["name"] in "".join(rig.said)


@needs_chromium
def test_the_page_draws_a_costumed_pack_in_pack_mode_with_the_images_under_the_costumes_rev(tmp_path, closet):
    ident = of_template(closet, "lana")

    def attempt(n):
        rig = Dressed(tmp_path / f"run{n}", closet, [ident])
        rig.be("idle")
        served = spy(rig)
        try:
            dom = serve_page(rig, lambda server: dump_dom(server.url + "/?poll=300"))
        finally:
            rig.close()
        return rig, served, dom

    rig, served, dom = retried(attempt)
    rev = rig.hub.character()["rev"]
    assert REV.fullmatch(rev)
    classes = html.unescape(re.search(r'<body[^>]*\bclass="([^"]*)"', dom).group(1)).split()
    assert "pack" in classes and "pixel" in classes   # the pack's own pixel look, not the page's SVG Buddy
    assert re.search(r'<body[^>]*style="[^"]*--ground:\s*#0e0d14', dom, re.I)
    for name in FILES:
        assert f'src="/character/{ident}/{name}.gif?v={rev}"' in dom, name
    assert "/character/buddy/" not in dom
    assert re.search(r'<img[^>]*class="on"[^>]*data-state="idle"', dom) or re.search(r'<img[^>]*data-state="idle"[^>]*class="on"', dom)
    assert sorted(n for pack, n, ok in served if pack == ident and ok) == sorted(f"{n}.gif" for n in FILES)
    assert all(ok for _, _, ok in served) and not [1 for pack, _, _ in served if pack != ident]


# One script, injected into a copy of the page (the screen's CSP allows inline scripts): it looks at the figure every 20 ms of the
# page's own clock and right after every change to the figure's box, and writes down each change of what is on show: the image's
# address, whether it has loaded, where it is on the stage and where its neighbours are. A frame is "empty" when nothing is on
# show or the image on show has not loaded.
SAMPLER = r"""
(function () {
  var box = document.getElementById('gif'), stage = document.getElementById('stage'), samples = [], last = '';
  function rect(el) {
    var sr = stage.getBoundingClientRect(), k = sr.width / 800, r = el.getBoundingClientRect();
    return [Math.round((r.left - sr.left) / k), Math.round((r.top - sr.top) / k), Math.round(r.width / k), Math.round(r.height / k)];
  }
  function sample() {
    var on = box.querySelector('img.on'), src = on ? on.getAttribute('src') : '';
    var ok = !!on && on.complete && on.naturalWidth > 0, key = src + '|' + ok;
    if (key === last) return;
    last = key;
    var near = [];
    Array.prototype.forEach.call(document.querySelectorAll('.bar, #plate, .col, .bubble'), function (el) {
      var r = rect(el);
      if (r[2] > 0 && r[3] > 0) near.push([(el.id ? '#' + el.id : '.' + String(el.className).split(' ')[0]), r]);   // the ones on show
    });
    samples.push({ src: src, ok: ok, rect: on ? rect(on) : null, near: near });
    document.body.setAttribute('data-e2e', JSON.stringify(samples));
  }
  setInterval(sample, 20);
  /* the browser's virtual time stands still while an image loads, so a timer never sees the wait: this looks right after each change */
  new MutationObserver(sample).observe(box, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'src'] });
  sample();
})();
"""


def page_with(tmp_path, script):
    """A copy of the page with `script` added at the end of its body."""
    page = PAGE.read_text(encoding="utf-8")
    assert page.count("</body>") == 1
    copy = tmp_path / "index.html"
    copy.write_text(page.replace("</body>", f"<script>{script}</script></body>"), encoding="utf-8")
    return copy


def samples_of(dom):
    found = re.search(r'<body[^>]*\bdata-e2e="([^"]*)"', dom)
    assert found, "the sampler wrote nothing"
    return json.loads(html.unescape(found.group(1)))


def meets(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


@needs_chromium
def test_a_living_page_swaps_costume_for_costume_and_back_to_the_bare_pack_without_an_empty_frame(tmp_path, closet):
    a, b = of_template(closet, "mago"), of_template(closet, "corona")

    def attempt(n):
        rig = Dressed(tmp_path / f"run{n}", closet, [a, b, ""])
        rig.be("idle")
        served = spy(rig)
        states = []
        polls = [0]
        real = rig.hub.current

        def current():
            # The page polls /state; at its 6th and 14th poll a look's time is up on the hub's clock, and that very poll brings the new look.
            polls[0] += 1
            if polls[0] in (6, 14):
                rig.t += EVERY + 1
            text = real()
            states.append(json.loads(text)["character"])
            return text

        rig.hub.current = current
        page = page_with(tmp_path / f"run{n}", SAMPLER)
        try:
            dom = serve_page(rig, lambda server: dump_dom(server.url + "/?poll=300", budget=9000), page=page)
        finally:
            rig.close()
        return served, states, dom

    served, states, dom = retried(attempt)
    revs = {}
    for c in states:
        assert revs.setdefault(c["name"], c["rev"]) == c["rev"]   # the rev of a look does not move while it is on
    assert list(revs) == [a, b, "buddy"] and len(set(revs.values())) == 3 and all(REV.fullmatch(r) for r in revs.values())
    assert [c["name"] for c in states if "costume" in c] == [n for n in (c["name"] for c in states) if n != "buddy"]
    samples = samples_of(dom)
    seen = []   # the look each loaded image on show belonged to, in order
    for s in samples:
        if not s["ok"]:
            assert not seen, f"an empty frame after {seen}: {s['src']!r}"   # only the very first paint may wait for its images
            continue
        pack, name, rev = re.fullmatch(r"/character/([^/]+)/([^/?]+)\?v=([0-9a-f]{12})", s["src"]).groups()
        assert rev == revs[pack], (pack, rev)   # the page loads every image under the rev the daemon sent
        if not seen or seen[-1] != pack:
            seen.append(pack)
    assert seen == [a, b, "buddy"], seen    # the costume, the other one and the bare pack, once each and in that order
    ok = [s for s in samples if s["ok"]]
    for s in ok:   # the figure is where the layout says, and nothing sits on it
        assert s["rect"] == [FIGURE[0], FIGURE[1], GRID * CELL, GRID * CELL], s["rect"]
        assert {sel for sel, _ in s["near"]} >= {"#top", "#plate", ".col"}, s["near"]   # the neighbours were really measured
        for sel, box in s["near"]:
            assert not meets(s["rect"], box), f"{sel} {box} overlaps the figure {s['rect']}"
    by_pack = {}
    for pack, name, ok_ in served:
        assert ok_, (pack, name)
        by_pack.setdefault(pack, set()).add(name)
    assert list(by_pack) == [a, b, "buddy"] and all(names == {f"{n}.gif" for n in FILES} for names in by_pack.values())
