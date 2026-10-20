"""The closet of costumes in the daemon: what the folder must look like to count, how Buddy picks, when it picks again,
what the screen is told, which files the /character route serves, and the flags. The Pillow tool that draws real
costumes (tools/make_closet.py) is tested apart; here the costumes are fake closets built in temporary directories with
tiny fake GIFs, the clock is a number the test moves and the dice are a list.

The daemon never imports Pillow: the last tests read its source to prove it."""
import ast
import asyncio
import copy
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy  # noqa: E402
import buddy_closet  # noqa: E402
import buddy_ui  # noqa: E402
from buddy_closet import (BAD_COSTUME, BAD_FILES, BAD_ID, BAD_MANIFEST, LINK, MAX_COSTUMES, MAX_ENTRIES,  # noqa: E402
                          MAX_FILE_BYTES, MAX_MANIFEST_BYTES, OVER, STRAY, UNREADABLE, Closet)
from buddy_core import BuddyCore  # noqa: E402
from buddy_ui import UIHub  # noqa: E402
from test_night import run_returning  # noqa: E402
from test_ui import REV, Radio, run, serving  # noqa: E402
from ui_client import http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3600.0
EVERY = 3 * HOUR

FILES = ["sleep.gif", "idle_0.gif", "idle_1.gif", "idle_2.gif", "busy.gif", "attention.gif", "celebrate.gif", "dizzy.gif", "heart.gif"]
STATES = {"sleep": "sleep.gif", "idle": ["idle_0.gif", "idle_1.gif", "idle_2.gif"], "busy": "busy.gif",
          "attention": "attention.gif", "celebrate": "celebrate.gif", "dizzy": "dizzy.gif", "heart": "heart.gif"}
MANIFEST = {
    "render": "pixel",
    "colors": {"body": "#FF9E7A", "bg": "#0E0D14", "text": "#F3EEF7", "textDim": "#9A93A4", "ink": "#1B1626"},
    "accents": {"sleep": "#A78BFA", "idle": "#FF9E7A", "busy": "#4FD1C5", "attention": "#FFCF5C", "celebrate": "#8BE76A",
                "dizzy": "#A9B8CF", "heart": "#FF8FA3"},
    "states": STATES,
}
IDS = ("a-coral", "b-menta", "c-lila")


def gif(cid, fname):
    """The fake bytes of an image: distinct per costume and per file, and not a real GIF."""
    return b"GIF89a" + f"{cid}/{fname}".encode()


def make_costume(root, cid, name=None, base="buddy", states=None, edit=None):
    """A costume the way tools/make_closet.py leaves it: <root>/<cid>/manifest.json plus the images it lists."""
    folder = Path(root) / cid
    folder.mkdir(parents=True)
    manifest = copy.deepcopy(MANIFEST)
    manifest["name"] = cid
    manifest["costume"] = {"name": name or f"gorro {cid}", "template": "gorro", "color": "coral", "base": base}
    if states is not None:
        manifest["states"] = states
    if edit is not None:
        edit(manifest)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    listed = manifest["states"] if isinstance(manifest["states"], dict) else {}
    for value in listed.values():
        for fname in value if isinstance(value, list) else [value]:
            if isinstance(fname, str) and "/" not in fname and fname and not fname.startswith("."):
                (folder / fname).write_bytes(gif(cid, fname))
    return folder


def install_base(core, name="buddy"):
    """The pack the costumes are drawn over, installed the way a folder push leaves it, and made the active one."""
    folder = core.state_dir / "characters" / name
    folder.mkdir(parents=True, exist_ok=True)
    manifest = copy.deepcopy(MANIFEST)
    manifest["name"] = name
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for fname in FILES:
        (folder / fname).write_bytes(b"GIF89abase/" + fname.encode())
    core.state.character = name
    return folder


class ScriptedCloset(Closet):
    """A closet whose dice are a list: pick() returns the costume called by the next id ("" is "sin disfraz"), and
    "sin disfraz" once the list runs out. The scanning, the checks and everything else are the real ones."""

    def __init__(self, root, script=(), rng=None):
        super().__init__(root, rng)
        self.script = list(script)
        self.picks = 0

    def pick(self):
        self.picks += 1
        want = self.script.pop(0) if self.script else ""
        return next((c for c in self.costumes if c.id == want), None)


class Wardrobe:
    """A core on a fake clock with the pack `buddy` active, a closet of three costumes and a hub that wears them."""

    def __init__(self, tmp_path, script=("a-coral",), ids=IDS, every=EVERY, base="buddy", pack="buddy", notice=True,
                 closet=True, clock=None):
        self.t = 1000.0
        self.said = []
        self.root = Path(tmp_path) / "closet"
        self.root.mkdir(parents=True, exist_ok=True)
        for cid in ids:
            make_costume(self.root, cid, base=base)
        self.core = BuddyCore(Path(tmp_path) / "state", lambda msg: None, clock=lambda: self.t, wall=lambda: 1_800_000_000.0)
        if pack:
            install_base(self.core, pack)
        self.closet = ScriptedCloset(self.root, script) if closet else None
        self.hub = UIHub(clock=clock or (lambda: self.t), notice=self.said.append if notice else None, closet=self.closet,
                         closet_every=every)
        self.hub.bind(self.core, Radio())

    def view(self):
        return self.hub.view()

    def character(self):
        return self.hub.view()["character"]

    def advance(self, seconds):
        """Time passes and the daemon's 1 Hz refresh notices."""
        self.t += seconds
        self.hub.refresh()

    def close(self):
        self.hub.close()


@pytest.fixture
def closet_root(tmp_path):
    root = tmp_path / "closet"
    root.mkdir()
    return root


def ids_of(closet):
    return [c.id for c in closet.costumes]


# --- the folder: what counts as a costume ----------------------------------------------------------------------------


def test_a_costume_the_tool_leaves_is_accepted_with_what_the_manifest_says(closet_root):
    make_costume(closet_root, "gorro-coral", name="gorro de fiesta coral", base="buddy")
    closet = Closet(closet_root)
    found = closet.scan()
    assert closet.skipped == {} and closet.skipped_text() == ""
    assert [c.id for c in found] == ["gorro-coral"] and found == closet.costumes
    c = found[0]
    assert (c.name, c.base) == ("gorro de fiesta coral", "buddy")
    assert c.folder == Path(os.path.realpath(closet_root / "gorro-coral"))
    assert set(c.files) == set(FILES) and c.files[0] == "sleep.gif"
    assert [s[0] for s in c.stamps] == ["manifest.json", *c.files]


def test_costumes_come_sorted_whatever_order_they_were_made_in(closet_root):
    for cid in ("zeta-x", "alfa-x", "mu-x"):
        make_costume(closet_root, cid)
    closet = Closet(closet_root)
    closet.scan()
    assert ids_of(closet) == ["alfa-x", "mu-x", "zeta-x"]


def pad_to(manifest, size):
    """JSON text of `manifest` with a filler key so it is exactly `size` bytes."""
    plain = len(json.dumps(dict(manifest, pad="")).encode())
    return json.dumps(dict(manifest, pad="x" * (size - plain)))


def write_manifest(folder, data):
    (folder / "manifest.json").write_bytes(data if isinstance(data, bytes) else data.encode())


def edit_manifest(fn):
    def damage(folder, root, outside):
        path = folder / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        fn(data)
        path.write_text(json.dumps(data), encoding="utf-8")
    return damage


def set_states(states):
    return edit_manifest(lambda m: m.__setitem__("states", states))


def set_costume(**fields):
    def apply(m):
        for key, value in fields.items():
            if value is DROP:
                m["costume"].pop(key, None)
            else:
                m["costume"][key] = value
    return edit_manifest(apply)


DROP = object()


def to_symlink(folder, name, target):
    (folder / name).unlink()
    (folder / name).symlink_to(target)


def link_outside(folder, root, outside):
    (outside / "secret.gif").write_bytes(b"GIF89asecret")
    to_symlink(folder, "idle_1.gif", outside / "secret.gif")


def move_manifest_out(folder, root, outside):
    shutil.move(str(folder / "manifest.json"), str(outside / "manifest.json"))
    (folder / "manifest.json").symlink_to(outside / "manifest.json")


def move_folder_out(folder, root, outside):
    shutil.move(str(folder), str(outside / "real"))
    folder.symlink_to(outside / "real")


DAMAGE = [
    # the manifest itself
    ("manifest missing", BAD_MANIFEST, lambda f, r, o: (f / "manifest.json").unlink()),
    ("manifest is not json", BAD_MANIFEST, lambda f, r, o: write_manifest(f, b"{nope")),
    ("manifest is a list", BAD_MANIFEST, lambda f, r, o: write_manifest(f, b"[]")),
    ("manifest is not utf-8", BAD_MANIFEST, lambda f, r, o: write_manifest(f, b"\xff\xfe\x00")),
    ("manifest is empty", BAD_MANIFEST, lambda f, r, o: write_manifest(f, b"")),
    ("manifest is nested 30000 brackets deep", BAD_MANIFEST, lambda f, r, o: write_manifest(f, b"[" * 30000 + b"]" * 30000)),
    ("manifest is too big", BAD_MANIFEST, lambda f, r, o: write_manifest(f, pad_to(MANIFEST, MAX_MANIFEST_BYTES + 1))),
    ("manifest is a folder", BAD_MANIFEST, lambda f, r, o: ((f / "manifest.json").unlink(), (f / "manifest.json").mkdir())),
    # the states it lists
    ("no states", BAD_MANIFEST, edit_manifest(lambda m: m.pop("states"))),
    ("states is empty", BAD_MANIFEST, set_states({})),
    ("states is a list", BAD_MANIFEST, set_states(["idle_0.gif"])),
    ("a state is a number", BAD_MANIFEST, set_states({"idle": 7})),
    ("a state holds a number", BAD_MANIFEST, set_states({"idle": ["idle_0.gif", 7]})),
    ("a state with no files", BAD_MANIFEST, set_states({"idle": []})),
    ("a file in a subfolder", BAD_MANIFEST, set_states({"idle": "sub/idle_0.gif"})),
    ("a file going up", BAD_MANIFEST, set_states({"idle": "../idle_0.gif"})),
    ("a file that is a path", BAD_MANIFEST, set_states({"idle": "/etc/passwd.gif"})),
    ("a hidden file", BAD_MANIFEST, set_states({"idle": ".idle_0.gif"})),
    ("a file with a backslash", BAD_MANIFEST, set_states({"idle": "a\\b.gif"})),
    ("a file with a nul", BAD_MANIFEST, set_states({"idle": "a\x00b.gif"})),
    ("a file with a newline", BAD_MANIFEST, set_states({"idle": "a\nb.gif"})),
    ("a file that is not an image", BAD_MANIFEST, set_states({"idle": "idle_0.txt"})),
    ("a file with no suffix", BAD_MANIFEST, set_states({"idle": "idle_0"})),
    ("a svg", BAD_MANIFEST, set_states({"idle": "idle_0.svg"})),
    ("a file name too long", BAD_MANIFEST, set_states({"idle": "x" * 70 + ".gif"})),
    ("too many files", BAD_MANIFEST, set_states({"idle": [f"f{i}.gif" for i in range(33)]})),
    ("too many states", BAD_MANIFEST, set_states({f"s{i}": "idle_0.gif" for i in range(17)})),
    # the costume block
    ("no costume block", BAD_COSTUME, edit_manifest(lambda m: m.pop("costume"))),
    ("costume block is a list", BAD_COSTUME, edit_manifest(lambda m: m.__setitem__("costume", ["gorro"]))),
    ("costume name missing", BAD_COSTUME, set_costume(name=DROP)),
    ("costume name is a number", BAD_COSTUME, set_costume(name=7)),
    ("costume name is empty", BAD_COSTUME, set_costume(name="")),
    ("costume name is 41 characters", BAD_COSTUME, set_costume(name="x" * 41)),
    ("costume name has a newline", BAD_COSTUME, set_costume(name="gorro\ncoral")),
    ("costume name has a bidi override", BAD_COSTUME, set_costume(name="gorro ‮coral")),
    ("costume name has a zero-width space", BAD_COSTUME, set_costume(name="gorro​coral")),
    ("costume name is padded", BAD_COSTUME, set_costume(name=" gorro")),
    ("costume name has a control character", BAD_COSTUME, set_costume(name="go\x07rro")),
    ("costume base missing", BAD_COSTUME, set_costume(base=DROP)),
    ("costume base is empty", BAD_COSTUME, set_costume(base="")),
    ("costume base is a number", BAD_COSTUME, set_costume(base=3)),
    ("costume base is 65 characters", BAD_COSTUME, set_costume(base="b" * 65)),
    ("costume base has a newline", BAD_COSTUME, set_costume(base="bud\ndy")),
    # the images
    ("an image is missing", BAD_FILES, lambda f, r, o: (f / "idle_1.gif").unlink()),
    ("an image is a folder", BAD_FILES, lambda f, r, o: ((f / "idle_1.gif").unlink(), (f / "idle_1.gif").mkdir())),
    ("an image is empty", BAD_FILES, lambda f, r, o: (f / "idle_1.gif").write_bytes(b"")),
    ("an image is too big", BAD_FILES, lambda f, r, o: (f / "idle_1.gif").write_bytes(b"x" * (MAX_FILE_BYTES + 1))),
    # links, anywhere
    ("an image is a link inside the costume", LINK, lambda f, r, o: to_symlink(f, "idle_1.gif", "sleep.gif")),
    ("an image is a link to a file outside", LINK, link_outside),
    ("the manifest is a link", LINK, move_manifest_out),
    ("the costume folder is a link", LINK, move_folder_out),
]


@pytest.mark.parametrize("label, kind, damage", DAMAGE, ids=[d[0] for d in DAMAGE])
def test_a_folder_that_fails_one_check_is_skipped_whole_and_counted_by_kind(tmp_path, label, kind, damage):
    root, outside = tmp_path / "closet", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    make_costume(root, "good-one")
    make_costume(root, "zz-fine")
    damage(root / "good-one", root, outside)
    closet = Closet(root)
    closet.scan()
    assert ids_of(closet) == ["zz-fine"], label  # the others are not harmed
    assert closet.skipped == {kind: 1}, label


def test_what_sits_exactly_at_each_limit_is_accepted(closet_root):
    make_costume(closet_root, "x" * 63, name="n" * 40, base="b" * 64)
    make_costume(closet_root, "big-image", states={"idle": "idle_0.gif"})
    (closet_root / "big-image" / "idle_0.gif").write_bytes(b"x" * MAX_FILE_BYTES)
    make_costume(closet_root, "many-files", states={"idle": [f"f{i}.gif" for i in range(32)]})
    make_costume(closet_root, "many-states", states={f"s{i}": "idle_0.gif" for i in range(16)})
    big = make_costume(closet_root, "big-manifest")
    write_manifest(big, pad_to(json.loads((big / "manifest.json").read_text()), MAX_MANIFEST_BYTES))
    assert (big / "manifest.json").stat().st_size == MAX_MANIFEST_BYTES
    closet = Closet(closet_root)
    closet.scan()
    assert closet.skipped == {} and len(closet.costumes) == 5


@pytest.mark.parametrize("name, kind", [
    ("Bad-Name", BAD_ID), ("has space", BAD_ID), ("ñandú", BAD_ID), ("a_b", BAD_ID), ("-leading", BAD_ID), ("x" * 64, BAD_ID),
])
def test_a_folder_name_that_is_not_an_id_is_skipped(closet_root, name, kind):
    make_costume(closet_root, name)
    make_costume(closet_root, "fine-one")
    closet = Closet(closet_root)
    closet.scan()
    assert ids_of(closet) == ["fine-one"] and closet.skipped == {kind: 1}


def test_stray_files_are_skipped_and_hidden_ones_are_not_even_counted(closet_root):
    make_costume(closet_root, "fine-one")
    (closet_root / "notes.txt").write_text("hola", encoding="utf-8")
    (closet_root / ".DS_Store").write_bytes(b"\x00\x01")
    make_costume(closet_root, ".hidden-one")
    closet = Closet(closet_root)
    closet.scan()
    assert ids_of(closet) == ["fine-one"] and closet.skipped == {STRAY: 1}


def test_a_link_in_the_closet_is_never_followed_even_to_a_good_costume(tmp_path, closet_root):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_costume(elsewhere, "far-away")
    make_costume(closet_root, "fine-one")
    (closet_root / "alias-one").symlink_to(closet_root / "fine-one")  # to a sibling
    (closet_root / "far-one").symlink_to(elsewhere / "far-away")  # to a costume outside
    (closet_root / "dangling").symlink_to(tmp_path / "nowhere")
    closet = Closet(closet_root)
    closet.scan()
    assert ids_of(closet) == ["fine-one"] and closet.skipped == {LINK: 3}


def test_a_folder_inside_a_folder_is_not_a_costume_and_is_not_looked_into(closet_root):
    (closet_root / "box").mkdir()
    make_costume(closet_root / "box", "inner-one")  # <closet>/box/inner-one/manifest.json
    make_costume(closet_root, "fine-one")
    extra = closet_root / "fine-one" / "deeper"  # junk inside a costume is simply not listed
    extra.mkdir()
    (extra / "manifest.json").write_text("{}", encoding="utf-8")
    (closet_root / "fine-one" / "notes.txt").write_text("hola", encoding="utf-8")
    closet = Closet(closet_root)
    closet.scan()
    assert ids_of(closet) == ["fine-one"] and closet.skipped == {BAD_MANIFEST: 1}  # "box" has no manifest of its own


def test_the_closet_folder_itself_may_be_a_link_the_user_chose(tmp_path, closet_root):
    make_costume(closet_root, "fine-one")
    link = tmp_path / "wardrobe"
    link.symlink_to(closet_root)
    closet = Closet(link)
    closet.scan()
    assert ids_of(closet) == ["fine-one"] and closet.skipped == {}
    c = closet.costumes[0]
    assert str(c.folder).startswith(os.path.realpath(closet_root)) and closet.recheck(c) is c
    assert closet.read(c, "idle_0.gif") == gif("fine-one", "idle_0.gif")


def test_at_most_64_costumes_and_the_rest_are_counted_not_looked_at(closet_root):
    for i in range(70):
        make_costume(closet_root, "c%03d" % i, states={"idle": "idle_0.gif"})
    closet = Closet(closet_root)
    closet.scan()
    assert len(closet.costumes) == MAX_COSTUMES == 64
    assert ids_of(closet) == ["c%03d" % i for i in range(64)]  # the first 64 by id
    assert closet.skipped == {OVER: 6}


def test_a_closet_full_of_junk_costs_a_bounded_look(closet_root, monkeypatch):
    for i in range(MAX_ENTRIES + 44):
        (closet_root / ("j%04d" % i)).mkdir()
    looked = []
    real = buddy_closet._load
    monkeypatch.setattr(buddy_closet, "_load", lambda root, name: looked.append(name) or real(root, name))
    closet = Closet(closet_root)
    closet.scan()
    assert closet.costumes == [] and len(looked) == MAX_ENTRIES
    assert closet.skipped == {BAD_MANIFEST: MAX_ENTRIES, OVER: 44}


@pytest.mark.parametrize("make", ["missing", "file"])
def test_a_closet_that_cannot_be_read_is_an_empty_closet_not_an_error(tmp_path, make):
    root = tmp_path / "nothing"
    if make == "file":
        root.write_text("not a folder", encoding="utf-8")
    closet = Closet(root)
    assert closet.scan() == [] and closet.costumes == [] and closet.skipped == {UNREADABLE: 1}
    assert closet.skipped_text() == "no pude leer la carpeta del closet"


def test_the_skipped_text_is_counts_and_kinds_and_nothing_else(closet_root):
    secret = "MARCA-SECRETA-77"
    (closet_root / "uno").mkdir()
    (closet_root / "uno" / "manifest.json").write_text(json.dumps({"states": {"idle": "a.gif"}, "x": secret}), encoding="utf-8")
    (closet_root / "dos").mkdir()
    (closet_root / "dos" / "manifest.json").write_text("{" + secret, encoding="utf-8")
    (closet_root / f"{secret}.txt").write_text(secret, encoding="utf-8")
    make_costume(closet_root, "tres", edit=lambda m: m["costume"].update(name="x" * 41))
    closet = Closet(closet_root)
    closet.scan()
    text = closet.skipped_text()
    assert text == "descarté 4 carpetas (disfraz 2, manifest 1, suelto 1)"
    assert secret not in text and str(closet_root) not in text
    assert Closet(closet_root / "nope").skipped_text() == ""  # nothing scanned yet, nothing to say


def test_a_manifest_that_exhausts_the_json_parser_is_just_a_bad_manifest(closet_root, monkeypatch):
    """Python 3.9 to 3.13 raise RecursionError on a few thousand brackets (3.14 no longer does): the one error json.loads
    can raise that is neither an OSError nor a ValueError."""
    make_costume(closet_root, "deep-one")

    def boom(text):
        raise RecursionError("maximum recursion depth exceeded")

    closet = Closet(closet_root)
    closet.scan()
    costume = closet.costumes[0]
    monkeypatch.setattr(buddy_closet, "json", types.SimpleNamespace(loads=boom))
    closet.scan()
    assert closet.costumes == [] and closet.skipped == {BAD_MANIFEST: 1}
    rewrite(closet_root / "deep-one" / "manifest.json", (closet_root / "deep-one" / "manifest.json").read_bytes() + b" ", 4)
    assert closet.recheck(costume) is None  # the same parser, asked again for a costume already accepted


def test_whatever_a_folder_provokes_a_scan_does_not_raise(closet_root, monkeypatch):
    make_costume(closet_root, "fine-one")
    make_costume(closet_root, "odd-one")
    real = buddy_closet._load

    def explode(root, name):
        if name == "odd-one":
            raise RuntimeError("something nobody thought of")
        return real(root, name)

    monkeypatch.setattr(buddy_closet, "_load", explode)
    closet = Closet(closet_root)
    closet.scan()
    assert ids_of(closet) == ["fine-one"] and closet.skipped == {BAD_MANIFEST: 1}


def test_a_scan_forgets_what_is_gone_and_finds_what_was_added(closet_root):
    make_costume(closet_root, "a-one")
    closet = Closet(closet_root)
    closet.scan()
    assert ids_of(closet) == ["a-one"]
    make_costume(closet_root, "b-two")
    shutil.rmtree(closet_root / "a-one")
    closet.scan()
    assert ids_of(closet) == ["b-two"]


def test_the_problem_with_a_folder_that_cannot_be_a_closet(tmp_path, closet_root):
    assert Closet.problem(closet_root) == ""
    assert Closet.problem(tmp_path / "missing") == f"no existe la carpeta {tmp_path / 'missing'}"
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert Closet.problem(tmp_path / "file") == f"{tmp_path / 'file'} no es una carpeta"
    if os.geteuid() != 0:  # root reads everything
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            assert Closet.problem(locked) == f"no puedo leer la carpeta {locked}"
        finally:
            locked.chmod(0o700)


# --- recheck and read -------------------------------------------------------------------------------------------------


def one_costume(closet_root, **kw):
    folder = make_costume(closet_root, "gorro-coral", **kw)
    closet = Closet(closet_root)
    closet.scan()
    return closet, closet.costumes[0], folder


def rewrite(path, data, tick):
    """Other bytes under the same name with a modification time of their own (see test_ui.rewrite)."""
    path.write_bytes(data)
    os.utime(path, (1_900_000_000 + tick, 1_900_000_000 + tick))


def test_a_costume_nobody_touched_is_the_very_same_object(closet_root):
    closet, costume, _ = one_costume(closet_root)
    assert closet.recheck(costume) is costume


def test_a_gif_copied_over_a_costume_is_seen_and_the_costume_stays(closet_root):
    closet, costume, folder = one_costume(closet_root)
    rewrite(folder / "idle_0.gif", b"GIF89anewer", 1)
    fresh = closet.recheck(costume)
    assert fresh is not None and fresh.id == costume.id and fresh.stamps != costume.stamps
    assert closet.recheck(fresh) is fresh
    assert closet.read(fresh, "idle_0.gif") == b"GIF89anewer"


def test_a_rewritten_manifest_is_read_again_with_its_new_name_and_base(closet_root):
    closet, costume, folder = one_costume(closet_root)
    data = json.loads((folder / "manifest.json").read_text())
    data["costume"].update(name="gorro nuevo", base="otro")
    rewrite(folder / "manifest.json", json.dumps(data).encode(), 2)
    fresh = closet.recheck(costume)
    assert (fresh.name, fresh.base) == ("gorro nuevo", "otro")


UNSOUND = [
    ("a gif deleted", lambda f, root: (f / "heart.gif").unlink()),
    ("a gif turned into a link", lambda f, root: to_symlink(f, "heart.gif", "sleep.gif")),
    ("a gif grown too big", lambda f, root: rewrite(f / "heart.gif", b"x" * (MAX_FILE_BYTES + 1), 3)),
    ("a gif emptied", lambda f, root: rewrite(f / "heart.gif", b"", 3)),
    ("the manifest corrupted", lambda f, root: rewrite(f / "manifest.json", b"{garbage", 3)),
    ("the manifest gone", lambda f, root: (f / "manifest.json").unlink()),
    ("the manifest losing its costume block", lambda f, root: rewrite(f / "manifest.json", b'{"states": {"idle": "idle_0.gif"}}', 3)),
    ("the folder deleted", lambda f, root: shutil.rmtree(f)),
    ("the whole closet deleted", lambda f, root: shutil.rmtree(root)),
    ("the folder replaced by a link", lambda f, root: (shutil.move(str(f), str(root.parent / "kept")), f.symlink_to(root.parent / "kept"))),
]


@pytest.mark.parametrize("label, damage", UNSOUND, ids=[u[0] for u in UNSOUND])
def test_a_costume_that_stopped_being_sound_is_none_and_never_an_exception(closet_root, label, damage):
    closet, costume, folder = one_costume(closet_root)
    damage(folder, closet_root)
    assert closet.recheck(costume) is None, label


def test_read_gives_only_the_images_the_manifest_lists(closet_root):
    closet, costume, folder = one_costume(closet_root)
    (folder / "extra.gif").write_bytes(b"GIF89aextra")
    (folder / "notes.txt").write_text("hola", encoding="utf-8")
    assert closet.read(costume, "idle_1.gif") == gif("gorro-coral", "idle_1.gif")
    for name in ("manifest.json", "extra.gif", "notes.txt", "missing.gif", "../gorro-coral/idle_1.gif", "/etc/passwd",
                 "idle_1.gif/", "", ".", ".."):
        assert closet.read(costume, name) is None, name


def test_read_does_not_follow_a_link_swapped_in_after_the_check(tmp_path, closet_root):
    closet, costume, folder = one_costume(closet_root)
    (tmp_path / "secret.gif").write_bytes(b"GIF89asecret")
    to_symlink(folder, "idle_1.gif", tmp_path / "secret.gif")
    assert closet.read(costume, "idle_1.gif") is None
    rewrite(folder / "sleep.gif", b"x" * (MAX_FILE_BYTES + 1), 1)  # and a file that grew
    assert closet.read(costume, "sleep.gif") is None


# --- the dice -----------------------------------------------------------------------------------------------------------


class Dice:
    """Anything with randrange(n) will do: this one answers from a list and remembers what it was asked."""

    def __init__(self, *rolls):
        self.rolls = list(rolls)
        self.asked = []

    def randrange(self, n):
        self.asked.append(n)
        return self.rolls.pop(0) % n


def stocked(closet_root, count, rng=None):
    for i in range(count):
        make_costume(closet_root, "c%02d" % i, states={"idle": "idle_0.gif"})
    closet = Closet(closet_root, rng)
    closet.scan()
    return closet


def test_the_choice_is_among_the_costumes_and_no_costume_at_all(closet_root):
    dice = Dice(0, 0, 1, 2)
    closet = stocked(closet_root, 3, dice)
    assert closet.pick() is None  # four options, bare and the three costumes: roll 0 is bare
    assert closet.pick().id == "c00"  # after bare: the three costumes
    assert closet.pick().id == "c01"  # after c00: bare and the other two
    assert closet.pick().id == "c02"
    assert dice.asked == [4, 3, 3, 3]


def test_every_option_comes_up_about_equally_often_bare_included(closet_root):
    closet = stocked(closet_root, 3, random.Random(20261018))
    counts = {}
    for _ in range(8000):
        choice = closet.pick()
        key = choice.id if choice is not None else ""
        counts[key] = counts.get(key, 0) + 1
    assert set(counts) == {"", "c00", "c01", "c02"}
    for key, n in counts.items():
        assert 1700 < n < 2300, (key, counts)  # a quarter each, give or take


@pytest.mark.parametrize("count", [1, 2, 5])
def test_it_never_picks_the_same_look_twice_in_a_row_when_there_is_another(closet_root, count):
    closet = stocked(closet_root, count, random.Random(7))
    previous = object()
    for _ in range(600):
        choice = closet.pick()
        key = choice.id if choice is not None else ""
        assert key != previous
        previous = key


def test_with_one_costume_the_looks_simply_alternate(closet_root):
    closet = stocked(closet_root, 1)  # the default dice: nothing to choose, so it cannot matter
    keys = [(c.id if c else "") for c in (closet.pick() for _ in range(8))]
    assert keys in (["", "c00"] * 4, ["c00", ""] * 4)


def test_with_nothing_in_the_closet_it_is_bare_every_time(closet_root):
    closet = Closet(closet_root, Dice(0, 0, 0))
    closet.scan()
    assert [closet.pick() for _ in range(3)] == [None, None, None]


def test_a_pick_is_one_costume_or_none_never_a_pile(closet_root):
    closet = stocked(closet_root, 4, random.Random(3))
    for _ in range(50):
        choice = closet.pick()
        assert choice is None or isinstance(choice, buddy_closet.Costume)


def test_a_costume_that_left_the_closet_cannot_be_picked_and_does_not_break_the_rule(closet_root):
    closet = stocked(closet_root, 2, Dice(1, 0, 0))
    assert closet.pick().id == "c00"  # roll 1 of [bare, c00, c01]
    shutil.rmtree(closet_root / "c00")
    closet.scan()
    assert closet.pick() is None  # options now [bare, c01]; roll 0 is bare
    assert closet.pick().id == "c01"  # the only costume left, and it is not what was picked last


# --- the screen: off by default ------------------------------------------------------------------------------------------


def view_without_boot(hub):
    view = hub.view()
    view.pop("boot")
    return json.dumps(view, sort_keys=True)


def test_without_a_closet_the_character_is_exactly_what_it_always_was(tmp_path):
    rig = Wardrobe(tmp_path, closet=False)
    c = rig.character()
    assert set(c) == {"name", "colors", "accents", "render", "states", "rev"} and c["name"] == "buddy"
    assert "costume" not in rig.hub.current() and rig.hub._closet is None and rig.said == []
    assert rig.hub.character_bytes(c, "idle_0.gif") == b"GIF89abase/idle_0.gif"


def test_a_closet_that_chose_no_costume_looks_exactly_like_no_closet(tmp_path):
    rig = Wardrobe(tmp_path, script=[""])
    plain = UIHub()
    plain.bind(rig.core, Radio())
    assert rig.hub._pick is None and rig.character() == plain.view()["character"]
    assert view_without_boot(rig.hub) == view_without_boot(plain)
    assert json.dumps(rig.character(), sort_keys=True) == json.dumps(plain.character(), sort_keys=True)
    plain.unbind()


def test_with_the_closet_off_nothing_is_scanned_and_no_timer_ever_exists(tmp_path, monkeypatch):
    scanned = []
    monkeypatch.setattr(Closet, "scan", lambda self: scanned.append(1) or [])
    monkeypatch.setattr(buddy, "Closet", lambda root: pytest.fail("the closet must not even be built"))
    args = buddy.parse_args(["--state-dir", str(tmp_path / "state")])
    assert args.closet == "off" and args.closet_every == 3.0
    term = FakeTerm()
    assert buddy.make_closet(args, term) is None and term.lines == []
    made = []

    async def scenario():
        loop = asyncio.get_running_loop()
        real = loop.call_later

        def spy(delay, callback, *a):
            made.append(getattr(callback, "__name__", ""))
            return real(delay, callback, *a)

        loop.call_later = spy
        try:
            rig = Wardrobe(tmp_path, closet=False)
            for _ in range(3):
                rig.advance(HOUR)
            rig.hub.touch()
            await asyncio.sleep(0.02)
            assert rig.hub._closet_timer is None
            rig.hub.close()
        finally:
            del loop.call_later

    run(scenario)
    assert "_on_closet_timer" not in made and scanned == []


# --- the screen: who wears what and when ---------------------------------------------------------------------------------


def test_the_first_look_is_picked_when_the_hub_binds_and_the_screen_hears_of_it(tmp_path):
    rig = Wardrobe(tmp_path, script=["b-menta"])
    assert rig.closet.picks == 1 and rig.character()["name"] == "b-menta"
    assert rig.said == ["Closet: 3 disfraces, cambia cada 3 h", "Buddy se puso gorro b-menta"]


def test_the_costume_is_described_like_a_pack_with_its_own_name_and_the_spanish_name(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral"])
    c = rig.character()
    assert c == {
        "name": "a-coral", "costume": "gorro a-coral", "render": "pixel", "rev": c["rev"],
        "colors": MANIFEST["colors"], "accents": MANIFEST["accents"],
        "states": {"sleep": ["sleep.gif"], "idle": ["idle_0.gif", "idle_1.gif", "idle_2.gif"], "busy": ["busy.gif"],
                   "attention": ["attention.gif"], "celebrate": ["celebrate.gif"], "dizzy": ["dizzy.gif"], "heart": ["heart.gif"]},
    }
    assert REV.fullmatch(c["rev"])
    rig.hub.current().encode("ascii")  # the wire is ASCII


def test_the_rev_of_a_costume_is_worked_out_exactly_like_the_rev_of_any_pack(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral"])
    worn = rig.character()
    twin = rig.core.state_dir / "characters" / "a-coral"
    shutil.copytree(rig.root / "a-coral", twin)
    for path in twin.iterdir():
        stat = (rig.root / "a-coral" / path.name).stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    rig.core.state.character = "a-coral"  # the same folder, installed as the active pack of a hub without a closet
    plain = UIHub()
    plain.bind(rig.core, Radio())
    as_a_pack = plain.character()
    plain.unbind()
    assert {k: v for k, v in worn.items() if k != "costume"} == as_a_pack


def test_the_rev_holds_still_and_moves_with_every_change_to_the_costume(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"])
    rev = rig.character()["rev"]
    assert [rig.character()["rev"] for _ in range(3)] == [rev] * 3
    rewrite(rig.root / "a-coral" / "idle_0.gif", b"GIF89a-other", 1)  # a gif replaced under the same name
    replaced = rig.character()["rev"]
    manifest = rig.root / "a-coral" / "manifest.json"
    data = json.loads(manifest.read_text())
    data["colors"]["body"] = "#112233"
    rewrite(manifest, json.dumps(data).encode(), 2)  # a new manifest
    recolored = rig.character()
    assert recolored["colors"]["body"] == "#112233"
    rig.advance(EVERY)  # another costume
    assert rig.character()["name"] == "b-menta"
    assert len({rev, replaced, recolored["rev"], rig.character()["rev"]}) == 4


def test_what_the_core_keeps_is_never_touched_the_costume_is_only_a_view(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "", "b-menta", "c-lila"])
    state_file = rig.core.state_dir / "state.json"
    before = state_file.read_bytes() if state_file.exists() else None
    for _ in range(3):
        rig.advance(EVERY)
        assert rig.core.state.character == "buddy" and not rig.core.store.dirty
    assert (state_file.read_bytes() if state_file.exists() else None) == before


def test_a_new_look_reaches_the_listeners_in_one_publication_with_a_new_name_and_rev(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"])
    rig.hub.refresh()
    heard = []
    rig.hub.subscribe(lambda text: heard.append(json.loads(text)["character"]))
    first = rig.character()
    rig.advance(EVERY - 1)
    assert heard == []  # nothing to say before the time is up
    rig.advance(1)
    assert len(heard) == 1 and heard[0]["name"] == "b-menta" and heard[0]["rev"] != first["rev"]
    assert heard[0]["costume"] == "gorro b-menta"
    rig.advance(5)
    assert len(heard) == 1  # and nothing more until the next pick


def test_it_picks_again_every_so_many_hours_and_not_a_second_before(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta", "", "c-lila"])
    assert rig.closet.picks == 1
    rig.advance(EVERY - 0.5)
    assert rig.closet.picks == 1 and rig.character()["name"] == "a-coral"
    rig.advance(0.5)
    assert rig.closet.picks == 2 and rig.character()["name"] == "b-menta"
    rig.advance(EVERY - 1)  # the clock for the next one started at the second pick
    assert rig.closet.picks == 2
    rig.advance(1)
    assert rig.closet.picks == 3 and rig.character()["name"] == "buddy"  # "sin disfraz": the pack underneath
    assert "costume" not in rig.character()
    rig.advance(EVERY)
    assert rig.closet.picks == 4 and rig.character()["name"] == "c-lila"


def test_a_long_gap_picks_once_not_once_for_each_missed_turn(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta", "c-lila"])
    rig.advance(100 * EVERY)
    assert rig.closet.picks == 2 and rig.character()["name"] == "b-menta"
    rig.advance(EVERY - 1)
    assert rig.closet.picks == 2


def test_every_zero_picks_once_at_the_start_and_never_again(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"], every=0)
    for _ in range(3):
        rig.advance(10 ** 7)
    assert rig.closet.picks == 1 and rig.character()["name"] == "a-coral"
    assert rig.said[0] == "Closet: 3 disfraces, elige uno solo al arrancar"

    async def scenario():
        again = Wardrobe(tmp_path / "again", script=["a-coral"], every=0)
        again.advance(HOUR)
        assert again.hub._closet_timer is None  # not even a timer: there is nothing to wake up for
        again.close()

    run(scenario)


def test_a_closet_that_grew_since_the_last_look_is_in_the_dice_at_the_next_one(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "d-lima"], ids=("a-coral",))
    assert rig.character()["name"] == "a-coral"
    make_costume(rig.root, "d-lima")  # copied in from the Mac
    rig.advance(EVERY)
    assert rig.character()["name"] == "d-lima"


def test_an_empty_closet_says_so_and_wears_nothing(tmp_path):
    rig = Wardrobe(tmp_path, ids=())
    assert rig.said == ["Closet: sin disfraces todavía"]
    assert rig.character()["name"] == "buddy" and "costume" not in rig.character()
    rig.advance(EVERY)
    assert rig.said == ["Closet: sin disfraces todavía"]  # and does not repeat itself


@pytest.mark.parametrize("every_hours, costumes, expected", [
    (3, IDS, "Closet: 3 disfraces, cambia cada 3 h"),
    (3, IDS[:1], "Closet: 1 disfraz, cambia cada 3 h"),
    (0.25, IDS, "Closet: 3 disfraces, cambia cada 0.25 h"),
    (1.5, IDS, "Closet: 3 disfraces, cambia cada 1.5 h"),
    (168, IDS, "Closet: 3 disfraces, cambia cada 168 h"),
    (0, IDS, "Closet: 3 disfraces, elige uno solo al arrancar"),
    (0, IDS[:1], "Closet: 1 disfraz, elige uno solo al arrancar"),
    (3, (), "Closet: sin disfraces todavía"),
])
def test_the_start_line_says_how_many_and_how_often(tmp_path, every_hours, costumes, expected):
    rig = Wardrobe(tmp_path, script=[""], ids=costumes, every=every_hours * HOUR)
    assert rig.said == [expected]
    assert "—" not in expected


def test_skipped_folders_are_reported_once_by_kind_and_again_only_if_they_change(tmp_path):
    rig = Wardrobe(tmp_path, script=[""] * 8, ids=("a-coral",))
    assert rig.said == ["Closet: 1 disfraz, cambia cada 3 h"]
    (rig.root / "mala").mkdir()
    rig.advance(EVERY)
    rig.advance(EVERY)
    assert rig.said.count("Closet: descarté 1 carpeta (manifest 1)") == 1  # scanned twice, said once
    (rig.root / "peor").mkdir()
    rig.advance(EVERY)
    assert rig.said[-1] == "Closet: descarté 2 carpetas (manifest 2)"
    shutil.rmtree(rig.root / "mala")
    shutil.rmtree(rig.root / "peor")
    n = len(rig.said)
    rig.advance(EVERY)
    assert len(rig.said) == n  # all clear is not a line of its own
    (rig.root / "peor").mkdir()
    rig.advance(EVERY)
    assert rig.said[-1] == "Closet: descarté 1 carpeta (manifest 1)"


def test_one_line_when_a_costume_goes_on_one_when_it_comes_off_and_one_when_it_is_swapped(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta", "", "", "c-lila"])
    assert rig.said[-1] == "Buddy se puso gorro a-coral"
    rig.advance(EVERY)
    assert rig.said[-1] == "Buddy se puso gorro b-menta" and rig.said.count("Buddy se sacó el disfraz") == 0  # a swap is one line
    rig.advance(EVERY)
    assert rig.said[-1] == "Buddy se sacó el disfraz"
    n = len(rig.said)
    rig.advance(EVERY)  # bare after bare: nothing to say
    assert len(rig.said) == n
    rig.advance(EVERY)
    assert rig.said[-1] == "Buddy se puso gorro c-lila"
    for _ in range(5):
        rig.advance(1)
    assert rig.said[-1] == "Buddy se puso gorro c-lila" and len(rig.said) == n + 1  # refreshing says nothing new


def test_without_a_notice_the_lines_go_to_the_log(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="buddy.ui")
    rig = Wardrobe(tmp_path, script=["a-coral", ""], notice=False)
    rig.advance(EVERY)
    lines = [r.getMessage() for r in caplog.records if r.name == "buddy.ui"]
    assert lines == ["Closet: 3 disfraces, cambia cada 3 h", "Buddy se puso gorro a-coral", "Buddy se sacó el disfraz"]


def test_the_lines_never_carry_a_path_or_a_word_from_inside_a_file(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "MARCA-SECRETA-91"
    rig = Wardrobe(tmp_path, script=["a-coral"])
    (rig.root / "mala").mkdir()
    (rig.root / "mala" / "manifest.json").write_text(json.dumps({"states": {"idle": secret + ".gif"}, "x": secret}))
    (rig.root / f"{secret}.txt").write_text(secret)
    rig.advance(EVERY)
    rig.advance(EVERY)
    everything = "\n".join(rig.said) + "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in everything and str(tmp_path) not in everything and "closet/" not in everything


# -- the costume is worn only over the pack it was drawn for --


def test_a_costume_is_worn_only_while_its_base_is_the_active_pack(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral"])
    assert rig.character()["name"] == "a-coral"
    install_base(rig.core, "otro")  # another pack becomes the active one
    c = rig.character()
    assert c["name"] == "otro" and "costume" not in c
    rig.hub.refresh()
    assert rig.said[-1] == "Buddy se sacó el disfraz"
    rig.core.state.character = ""  # no pack at all
    assert rig.character() is None
    rig.core.state.character = "buddy"  # and back: the look picked is still the look
    assert rig.character()["name"] == "a-coral"
    rig.hub.refresh()
    assert rig.said[-1] == "Buddy se puso gorro a-coral"
    assert rig.closet.picks == 1  # none of that was a new pick


def test_a_costume_drawn_for_another_pack_never_shows_over_this_one(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"], base="otro")
    for _ in range(3):
        assert rig.character()["name"] == "buddy" and "costume" not in rig.character()
        rig.advance(EVERY)
    assert not [line for line in rig.said if "se puso" in line]


def test_no_pack_means_nothing_is_worn_and_the_page_keeps_drawing_its_own_buddy(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral"], pack=None)
    assert rig.character() is None and not [line for line in rig.said if "se puso" in line]


@pytest.mark.parametrize("base", ["Buddy", "budd", "buddy2", "buddy-2"])
def test_the_base_name_is_compared_whole_not_by_prefix_or_case(tmp_path, base):
    rig = Wardrobe(tmp_path, script=["a-coral"], base=base)
    assert rig.character()["name"] == "buddy" and "costume" not in rig.character()


# -- the timer --


def live(made):
    return [h for h in made if not h.cancelled()]


def test_there_is_at_most_one_timer_and_a_change_replaces_it(tmp_path):
    async def scenario():
        loop = asyncio.get_running_loop()
        made = []
        real = loop.call_later

        def spy(delay, callback, *args):
            handle = real(delay, callback, *args)
            if getattr(callback, "__name__", "") == "_on_closet_timer":  # this one, not the night timer or asyncio.sleep's
                made.append(handle)
            return handle

        loop.call_later = spy
        try:
            rig = Wardrobe(tmp_path, script=["a-coral", "b-menta", "c-lila"])
            first = rig.hub._closet_timer
            assert first is not None and live(made) == [first]
            for _ in range(5):  # the daemon refreshes every second: the timer is already right, so it stays
                rig.advance(1)
            assert rig.hub._closet_timer is first and len(made) == 1
            rig.advance(EVERY)  # time is up: a new look, a new moment, a new timer
            assert first.cancelled() and live(made) == [rig.hub._closet_timer] and len(made) == 2
            second = rig.hub._closet_timer
            rig.hub.touch()
            await asyncio.sleep(0.01)
            assert rig.hub._closet_timer is second and len(made) == 2
            rig.close()
            assert rig.hub._closet_timer is None and all(h.cancelled() for h in made)  # nothing leaks past close()
            rig.advance(EVERY)
            rig.hub.refresh()
            assert rig.hub._closet_timer is None and len(made) == 2 and rig.closet.picks == 2  # and nothing arms it again
        finally:
            del loop.call_later

    run(scenario)


def test_the_timer_wakes_the_screen_by_itself_and_the_new_look_is_published(tmp_path):
    async def scenario():
        rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"], every=0.3, clock=time.monotonic)  # the real clock
        published = asyncio.Queue()
        rig.hub.subscribe(lambda text: published.put_nowait(json.loads(text)["character"]))
        rig.hub.refresh()
        assert (await asyncio.wait_for(published.get(), 2))["name"] == "a-coral"
        started = time.monotonic()
        # nothing touches the hub from here on: no tick, no event, no request
        got = await asyncio.wait_for(published.get(), 3)
        assert got["name"] == "b-menta" and got["costume"] == "gorro b-menta"
        assert time.monotonic() - started < 2.0
        assert rig.said[-1] == "Buddy se puso gorro b-menta"
        rig.close()

    run(scenario)


def test_without_a_running_loop_there_is_no_timer_and_a_manual_refresh_still_picks(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"])
    assert rig.hub._closet_timer is None
    rig.advance(EVERY)
    assert rig.hub._closet_timer is None and rig.character()["name"] == "b-menta"


def test_dropping_the_closet_forgets_the_costume_and_the_timer(tmp_path):
    async def scenario():
        rig = Wardrobe(tmp_path, script=["a-coral"])
        assert rig.hub._closet_timer is not None and rig.character()["name"] == "a-coral"
        assert rig.hub.drop_closet() is True and rig.hub.drop_closet() is False
        assert rig.hub._closet_timer is None
        assert rig.character()["name"] == "buddy"
        rig.advance(10 * EVERY)
        assert rig.hub._closet_timer is None and rig.closet.picks == 1
        rig.close()

    run(scenario)


# -- the closet fails under the screen's feet --


def base_view(rig):
    plain = UIHub()
    plain.bind(rig.core, Radio())
    try:
        return plain.character()
    finally:
        plain.unbind()


DAMAGED = [
    ("the closet deleted", lambda rig: shutil.rmtree(rig.root)),
    ("the costume folder deleted", lambda rig: shutil.rmtree(rig.root / "a-coral")),
    ("the manifest corrupted", lambda rig: rewrite(rig.root / "a-coral" / "manifest.json", b"{garbage", 5)),
    ("a gif deleted", lambda rig: (rig.root / "a-coral" / "dizzy.gif").unlink()),
    ("a gif turned into a link", lambda rig: to_symlink(rig.root / "a-coral", "dizzy.gif", rig.root / "b-menta" / "dizzy.gif")),
    ("a gif grown past the limit", lambda rig: rewrite(rig.root / "a-coral" / "dizzy.gif", b"x" * (MAX_FILE_BYTES + 1), 5)),
    ("the folder swapped for a link", lambda rig: (shutil.move(str(rig.root / "a-coral"), str(rig.root.parent / "kept")),
                                                  (rig.root / "a-coral").symlink_to(rig.root.parent / "kept"))),
]


@pytest.mark.parametrize("label, damage", DAMAGED, ids=[d[0] for d in DAMAGED])
def test_a_closet_damaged_while_running_leaves_the_base_pack_showing_without_an_exception(tmp_path, label, damage):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"])
    assert rig.character()["name"] == "a-coral"
    damage(rig)
    c = rig.character()
    assert c == base_view(rig) and "costume" not in c, label
    rig.hub.current().encode("ascii")  # this refresh is also when the screen says the costume came off
    assert rig.said[-1] == "Buddy se sacó el disfraz" and rig.hub._pick is None
    checks = []
    real = rig.closet.recheck
    rig.closet.recheck = lambda costume: checks.append(costume) or real(costume)
    for _ in range(3):
        rig.advance(1)
    assert checks == []  # a costume that went bad is dropped, not looked at again every second


def test_after_a_wipe_the_next_pick_says_the_closet_cannot_be_read_and_a_restored_closet_is_used(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "", "d-lima"])
    shutil.rmtree(rig.root)
    rig.advance(1)
    rig.advance(EVERY)
    assert rig.said[-2:] == ["Buddy se sacó el disfraz", "Closet: no pude leer la carpeta del closet"]
    assert rig.character()["name"] == "buddy"
    rig.root.mkdir()
    make_costume(rig.root, "d-lima")
    rig.advance(EVERY)
    assert rig.character()["name"] == "d-lima"


def test_a_closet_that_raises_is_a_closet_without_costumes(tmp_path, caplog):
    rig = Wardrobe(tmp_path, script=["a-coral"])
    assert rig.character()["name"] == "a-coral"

    def boom(costume):
        raise RuntimeError("the disk is on fire")

    rig.closet.recheck = boom
    assert rig.character() == base_view(rig)
    rig.hub.refresh()
    assert rig.hub._pick is None and any("could not look at the closet" in r.getMessage() for r in caplog.records)


# -- the route --


def test_the_route_serves_the_worn_costumes_images_and_nothing_else(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral"])
    (rig.root / "a-coral" / "extra.gif").write_bytes(b"GIF89aextra")
    (rig.root / "a-coral" / "notes.txt").write_text("hola", encoding="utf-8")
    rev = rig.character()["rev"]

    async def scenario():
        async with serving(rig) as server:
            for fname in FILES:
                status, headers, body = await http(server, path=f"/character/a-coral/{fname}?v={rev}")
                assert status == 200 and headers["content-type"] == "image/gif", fname
                assert body == gif("a-coral", fname) and headers["x-content-type-options"] == "nosniff"
            for path in ("/character/a-coral/manifest.json", "/character/a-coral/extra.gif", "/character/a-coral/notes.txt",
                         "/character/a-coral/missing.gif", "/character/a-coral/../b-menta/idle_0.gif",
                         "/character/a-coral/..%2Fb-menta%2Fidle_0.gif", "/character/a-coral/%2e%2e/b-menta/idle_0.gif",
                         "/character/a-coral/idle_0.gif/x", "/character/a-coral/idle_0.gif%3Fv=1", "/character/a-coral",
                         "/character/a-coral/", "/character/",
                         "/character/buddy/idle_0.gif",  # the pack underneath is not what the page was told about
                         "/character/b-menta/idle_0.gif", "/character/closet/a-coral/idle_0.gif",
                         "/character/..%2Fclosets/a-coral/idle_0.gif"):
                assert (await http(server, path=path))[0] == 404, path
            assert (await http(server, "POST", "/character/a-coral/sleep.gif"))[0] == 405
        rig.close()

    run(scenario)


def test_after_a_new_pick_the_old_name_is_gone_and_the_new_one_answers(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta", ""])

    async def scenario():
        async with serving(rig) as server:
            assert (await http(server, path="/character/a-coral/idle_0.gif"))[2] == gif("a-coral", "idle_0.gif")
            rig.advance(EVERY)
            assert (await http(server, path="/character/a-coral/idle_0.gif"))[0] == 404
            assert (await http(server, path="/character/b-menta/idle_0.gif"))[2] == gif("b-menta", "idle_0.gif")
            rig.advance(EVERY)  # bare: the pack underneath answers, no costume does
            assert (await http(server, path="/character/buddy/idle_0.gif"))[2] == b"GIF89abase/idle_0.gif"
            assert (await http(server, path="/character/b-menta/idle_0.gif"))[0] == 404
        rig.close()

    run(scenario)


def test_the_route_refuses_a_costume_file_swapped_for_a_link_between_two_requests(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral"])
    (tmp_path / "secret.gif").write_bytes(b"GIF89asecret")

    async def scenario():
        async with serving(rig) as server:
            assert (await http(server, path="/character/a-coral/heart.gif"))[0] == 200
            to_symlink(rig.root / "a-coral", "heart.gif", tmp_path / "secret.gif")
            status, _, body = await http(server, path="/character/a-coral/heart.gif")
            assert status == 404 and b"secret" not in body
            assert (await http(server, path="/character/buddy/heart.gif"))[2] == b"GIF89abase/heart.gif"  # the base shows now
            assert rig.character()["name"] == "buddy"
        rig.close()

    run(scenario)


def test_the_hub_hands_out_a_costumes_bytes_only_for_the_costume_that_is_on(tmp_path):
    rig = Wardrobe(tmp_path, script=["a-coral", "b-menta"])
    worn = rig.character()
    assert rig.hub.character_bytes(worn, "idle_0.gif") == gif("a-coral", "idle_0.gif")
    assert rig.hub.character_bytes(dict(worn, name="b-menta"), "idle_0.gif") is None  # another costume of the closet
    assert rig.hub.character_bytes(dict(worn, name="buddy"), "idle_0.gif") is None
    assert rig.hub.character_bytes(worn, "manifest.json") is None
    rig.advance(EVERY)
    assert rig.hub.character_bytes(worn, "idle_0.gif") is None  # the page's old description of a look that went off


def test_with_no_costume_on_the_route_serves_the_pack_exactly_as_before(tmp_path):
    rig = Wardrobe(tmp_path, script=[""])

    async def scenario():
        async with serving(rig) as server:
            assert (await http(server, path="/character/buddy/sleep.gif"))[2] == b"GIF89abase/sleep.gif"
            for name in IDS:
                assert (await http(server, path=f"/character/{name}/sleep.gif"))[0] == 404
        rig.close()

    run(scenario)


# --- the flags ----------------------------------------------------------------------------------------------------------


class FakeTerm:
    """What buddy.make_closet needs from the terminal."""

    def __init__(self):
        self.lines = []

    def say(self, text, style=""):
        self.lines.append(text)


def parse(*argv):
    return buddy.parse_args(["--state-dir", "/nonexistent/buddy-state", *argv])


def test_the_defaults_are_off_and_every_three_hours():
    args = parse()
    assert (args.closet, args.closet_every) == ("off", 3.0)
    assert parse("check").closet == "off" and parse("forget-all").closet_every == 3.0  # the other commands carry them unharmed


def test_the_flags_take_their_values():
    assert parse("--closet", "auto").closet == "auto" and parse("--closet", "AUTO").closet == "auto"
    assert parse("--closet", "off").closet == "off" and parse("--closet", "Off").closet == "off"
    assert parse("--closet", " /some/where ").closet == "/some/where"
    assert parse("--closet", "./auto").closet == "./auto"  # a folder that happens to be called auto
    for text, expected in (("0", 0.0), ("0.25", 0.25), ("1.5", 1.5), ("3", 3.0), ("168", 168.0), ("1e1", 10.0), ("-0", 0.0)):
        got = parse("--closet-every", text).closet_every
        assert got == expected and str(got) != "-0.0", text
    assert parse("check", "--closet", "auto", "--closet-every", "6").command == "check"


@pytest.mark.parametrize("argv", [
    ["--closet", ""], ["--closet", "   "], ["--closet"],
    ["--closet-every", "0.1"], ["--closet-every", "0.24"], ["--closet-every", "169"], ["--closet-every", "-1"],
    ["--closet-every", "-0.5"], ["--closet-every", "abc"], ["--closet-every", ""], ["--closet-every", "nan"],
    ["--closet-every", "inf"], ["--closet-every", "-inf"], ["--closet-every", "1e999"], ["--closet-every", "3h"],
    ["--closet-every"],
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


def test_the_real_script_refuses_a_bad_closet_flag_with_exit_2_and_says_what_to_use():
    done = run_buddy("--closet-every", "0.1")
    assert done.returncode == 2 and "usá un número de horas entre 0.25 y 168" in done.stderr
    done = run_buddy("--closet", "")
    assert done.returncode == 2 and "off, auto o la carpeta" in done.stderr


def test_the_real_script_lists_the_flags_and_check_still_runs():
    done = run_buddy("--help")
    assert done.returncode == 0
    for text in ("--closet", "--closet-every", "off|auto|CARPETA", "HORAS", "make_closet"):
        assert text in done.stdout
    assert "—" not in done.stdout  # no em dashes
    nothing = run_buddy("check", "--closet", "auto", "--closet-every", "6", "--bus", "unix:path=/nonexistent/bus",
                        "--state-dir", "/nonexistent/buddy-state")
    assert nothing.returncode == 1 and "[ERROR]" in nothing.stdout  # it got to the diagnosis, as before
    assert "Traceback" not in nothing.stderr and "Closet" not in nothing.stdout  # and the closet had no part in it


def test_the_real_script_reads_the_closet_and_says_so_before_it_fails_for_want_of_a_bus(tmp_path, closet_root):
    make_costume(closet_root, "a-coral")
    done = run_buddy("--closet", str(closet_root), "--closet-every", "0", "--no-keys", "--ui-port", "0", "--night", "off",
                     "--bus", "unix:path=/nonexistent/bus", "--state-dir", str(tmp_path / "state"))
    assert done.returncode == 1 and "Traceback" not in done.stderr  # the missing bus, nothing to do with the closet
    lines = done.stdout.splitlines()
    assert "Closet: 1 disfraz, elige uno solo al arrancar" in done.stdout
    assert [i for i, line in enumerate(lines) if "Claude Buddy" in line][0] < [i for i, line in enumerate(lines) if "Closet:" in line][0]
    missing = run_buddy("--closet", str(tmp_path / "nope"), "--no-keys", "--ui-port", "0", "--night", "off",
                        "--bus", "unix:path=/nonexistent/bus", "--state-dir", str(tmp_path / "state"))
    assert missing.stdout.count("Closet apagado: no existe la carpeta") == 1 and "Closet:" not in missing.stdout
    assert missing.returncode == 1 and "Traceback" not in missing.stderr


def test_auto_is_the_closet_folder_of_the_state_dir(tmp_path):
    args = buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--closet", "auto"])
    folder = tmp_path / "state" / "closet"
    term = FakeTerm()
    assert buddy.make_closet(args, term) is None  # not made yet: one warning
    assert term.lines == [f"Closet apagado: no existe la carpeta {folder}"]
    folder.mkdir(parents=True)
    make_costume(folder, "a-coral")
    closet = buddy.make_closet(args, FakeTerm())
    assert closet is not None and closet.root == folder


def test_an_explicit_folder_is_used_as_given_with_the_home_expanded(tmp_path, monkeypatch, closet_root):
    monkeypatch.setenv("HOME", str(tmp_path))
    args = buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--closet", "~/closet"])
    closet = buddy.make_closet(args, FakeTerm())
    assert closet is not None and closet.root == tmp_path / "closet"


@pytest.mark.parametrize("what", ["missing", "a file"])
def test_a_closet_that_is_not_there_is_one_warning_and_the_closet_stays_off(tmp_path, what):
    path = tmp_path / "nothing"
    if what == "a file":
        path.write_text("x", encoding="utf-8")
    term = FakeTerm()
    args = buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--closet", str(path)])
    assert buddy.make_closet(args, term) is None
    assert len(term.lines) == 1 and term.lines[0].startswith("Closet apagado: ") and str(path) in term.lines[0]


def test_a_closet_the_user_cannot_read_is_a_warning_too(tmp_path, closet_root):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    closet_root.chmod(0)
    try:
        term = FakeTerm()
        args = buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--closet", str(closet_root)])
        assert buddy.make_closet(args, term) is None and "no puedo leer la carpeta" in term.lines[0]
    finally:
        closet_root.chmod(0o700)


def test_without_a_screen_there_is_nobody_to_dress_and_the_closet_is_not_even_opened(tmp_path, closet_root, monkeypatch):
    opened = []
    monkeypatch.setattr(buddy, "Closet", lambda root: opened.append(root))
    term = FakeTerm()
    args = buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--closet", str(closet_root), "--no-ui"])
    assert buddy.make_closet(args, term) is None and opened == []
    assert term.lines == ["Closet apagado: sin pantalla no hay quién vea los disfraces"]


# -- run_daemon in this process, with a stand-in for the radio --


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


def daemon_state(tmp_path):
    """A state dir where the pack `buddy` is installed and active, the way the Pi has it."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text(json.dumps({"character": "buddy"}), encoding="utf-8")
    core = BuddyCore(state, lambda msg: None)
    install_base(core, "buddy")
    return state


def daemon_args(tmp_path, *extra):
    return buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--no-keys", "--ui-port", "0", "--night", "off",
                             "--backlight", "off", *extra])


def test_the_daemon_wears_a_costume_tells_the_screen_and_serves_its_images(tmp_path, closet_root, monkeypatch, capsys):
    make_costume(closet_root, "a-coral")
    make_costume(closet_root, "b-menta")
    daemon_state(tmp_path)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: Transport())
    monkeypatch.setattr(buddy, "Closet", WearsFirst)
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(buddy.run_daemon(daemon_args(tmp_path, "--closet", str(closet_root), "--closet-every", "0")))
        out = ""
        try:
            while "Pantalla en http://127.0.0.1:" not in out:
                out += capsys.readouterr().out
                await asyncio.sleep(0.01)
            port = int(re.search(r"Pantalla en http://127\.0\.0\.1:(\d+)", out).group(1))
            status, _, body = await http(port, path="/state")
            seen["character"] = json.loads(body)["character"]
            rev = seen["character"]["rev"]
            seen["image"] = await http(port, path=f"/character/a-coral/idle_0.gif?v={rev}")
            seen["base"] = await http(port, path="/character/buddy/idle_0.gif")
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 5) == 0
        finally:
            if not task.done():
                task.cancel()
        seen["out"] = out + capsys.readouterr().out

    run(scenario)
    out = seen["out"]
    assert seen["character"]["name"] == "a-coral" and seen["character"]["costume"] == "gorro a-coral"
    assert seen["image"][0] == 200 and seen["image"][2] == gif("a-coral", "idle_0.gif") and seen["base"][0] == 404
    order = [out.index(text) for text in ("Claude Buddy", "Closet: 2 disfraces, elige uno solo al arrancar",
                                           "Buddy se puso gorro a-coral", "Pantalla en http://127.0.0.1:")]
    assert order == sorted(order)
    assert out.count("Closet:") == 1 and out.count("Buddy se puso") == 1 and "Traceback" not in out


def test_the_daemon_with_a_closet_off_says_nothing_about_closets(tmp_path, closet_root, monkeypatch, capsys):
    make_costume(closet_root, "a-coral")
    daemon_state(tmp_path)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: Transport())
    monkeypatch.setattr(buddy, "Closet", lambda root: pytest.fail("the closet must not even be built"))
    assert run_returning(buddy.run_daemon(daemon_args(tmp_path)), stop_after=0.4) == 0
    out = capsys.readouterr().out
    assert "Closet" not in out and "disfraz" not in out and "Pantalla en http://127.0.0.1:" in out


def test_a_missing_closet_is_one_warning_and_the_daemon_carries_on(tmp_path, monkeypatch, capsys):
    daemon_state(tmp_path)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: Transport())
    assert run_returning(buddy.run_daemon(daemon_args(tmp_path, "--closet", str(tmp_path / "nope"))), stop_after=0.4) == 0
    out = capsys.readouterr().out
    assert out.count("Closet apagado: no existe la carpeta") == 1 and "Closet:" not in out
    assert "Pantalla en http://127.0.0.1:" in out and "Traceback" not in out and "Cerrando" in out


def test_auto_finds_the_closet_in_the_state_dir(tmp_path, monkeypatch, capsys):
    state = daemon_state(tmp_path)
    (state / "closet").mkdir()
    make_costume(state / "closet", "a-coral")
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: Transport())
    assert run_returning(buddy.run_daemon(daemon_args(tmp_path, "--closet", "auto", "--closet-every", "6")), stop_after=0.4) == 0
    assert "Closet: 1 disfraz, cambia cada 6 h" in capsys.readouterr().out


def test_a_screen_that_could_not_open_takes_the_closet_with_it(tmp_path, closet_root, monkeypatch, capsys):
    import socket

    make_costume(closet_root, "a-coral")
    daemon_state(tmp_path)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: Transport())
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        args = daemon_args(tmp_path, "--closet", str(closet_root))
        args.ui_port = taken.getsockname()[1]
        assert run_returning(buddy.run_daemon(args), stop_after=0.4) == 0
    out = capsys.readouterr().out
    assert out.index("Closet: 1 disfraz") < out.index("No pude abrir la pantalla") < out.index("Closet apagado: sin pantalla")


def test_a_daemon_stopped_with_a_costume_on_leaves_no_timer_behind(tmp_path, closet_root, monkeypatch):
    make_costume(closet_root, "a-coral")
    daemon_state(tmp_path)
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: Transport())
    monkeypatch.setattr(buddy, "Closet", WearsFirst)
    made = []

    async def scenario():
        loop = asyncio.get_running_loop()
        real = loop.call_later

        def spy(delay, callback, *a):
            handle = real(delay, callback, *a)
            if getattr(callback, "__name__", "") == "_on_closet_timer":
                made.append(handle)
            return handle

        loop.call_later = spy
        try:
            task = asyncio.ensure_future(buddy.run_daemon(daemon_args(tmp_path, "--closet", str(closet_root))))
            await asyncio.sleep(0.4)
            assert len(live(made)) == 1
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 5) == 0
            assert live(made) == []
        finally:
            del loop.call_later

    run(scenario)
    assert len(made) >= 1


# --- the daemon stays stdlib: no Pillow, nothing from tools/ ---------------------------------------------------------------

STDLIB_OK = {"argparse", "asyncio", "collections", "dataclasses", "functools", "getpass", "hashlib", "hmac", "json", "logging", "os",
             "pathlib", "random", "re", "secrets", "signal", "socket", "stat", "sys", "time", "typing", "urllib", "termios", "tty",
             "__future__", "enum", "struct", "base64", "shutil", "subprocess", "itertools", "contextlib", "errno", "ipaddress",
             "math", "unicodedata", "binascii", "string", "threading", "weakref", "tempfile", "select", "uuid", "textwrap", "zlib"}
LOCAL = {"buddy", "buddy_backlight", "buddy_bluez", "buddy_closet", "buddy_core", "buddy_hermes", "buddy_ui"}


def imports_of(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("name", ["buddy.py", "buddy_ui.py", "buddy_closet.py"])
def test_the_daemon_never_imports_pillow_or_anything_from_tools(name):
    found = imports_of(ROOT / name)
    assert not {"PIL", "Pillow", "tools", "make_pixel_buddy", "make_closet", "closet_costumes"} & found


def test_the_closet_module_uses_only_the_standard_library_and_imports_nothing_of_ours():
    found = imports_of(ROOT / "buddy_closet.py")
    assert found <= STDLIB_OK, found - STDLIB_OK
    assert not found & LOCAL  # buddy_ui imports it, never the other way round


def test_the_kinds_of_image_the_closet_accepts_are_the_ones_the_route_serves():
    assert set(buddy_closet.IMAGE_SUFFIXES) == set(buddy_ui.IMAGE_TYPES)
