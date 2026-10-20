"""tools/closet_costumes.py and tools/make_closet.py: Mochi's closet of costumes.

A costume is the Mochi generator drawn with ONE head-top piece baked into every frame through the COSTUME hook of
tools/make_pixel_buddy.py. The frame-level tests run the generator's own frame functions with the hook set and look at
what every layer put on the 32x32 grid (the costume, the extras, the plain body), so they hold for every frame of every
state, the highest pose included. The tool tests build real pack folders into tmp dirs (a few: each build is ~1 s) and
the logic tests (count, seed, exhaustion, flags) replace the drawing by a stub that only makes the folder.

Needs Pillow, which only the tool uses (never the daemon): without it these tests are skipped."""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="Pillow is only needed to draw the costumes")
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
PACK = ROOT / "characters" / "buddy-pixel"
sys.path.insert(0, str(TOOLS))

import closet_costumes as cc  # noqa: E402
import make_closet as mc  # noqa: E402
import make_pixel_buddy as mpb  # noqa: E402

MOODS = ["sleep", "idle", "busy", "attention", "celebrate", "dizzy", "heart"]
FILES = ["sleep", "idle_0", "idle_1", "idle_2", "busy", "attention", "celebrate", "dizzy", "heart"]
ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
STATE_FRAMES = {"busy": mpb.frames_busy, "attention": mpb.frames_attention, "celebrate": mpb.frames_celebrate,
                "dizzy": mpb.frames_dizzy, "heart": mpb.frames_heart}


# --- what every layer put on every frame -----------------------------------------------------------------------

def render(hook=None):
    """{file name: (frames, ms)}: every frame of the pack the way build() draws them, each frame as (image, info) where
    info is the generator's anchor info plus "extras", the pixels the per-state extra put on the grid. Nothing global
    is left changed."""
    original, previous = mpb.make_frame, mpb.COSTUME

    def make_frame(pal, pose, face, fx=None, shear=None, dx=0):
        extras = {}

        def record(ex):
            fx(ex)
            extras.update(ex.px)
        img, info = original(pal, pose, face, record if fx is not None else None, shear, dx)
        return img, dict(info, extras=extras)

    mpb.make_frame, mpb.COSTUME = make_frame, hook
    try:
        out = {"sleep": mpb.frames_sleep(mpb.Pal("sleep"))}
        for v in range(3):
            out[f"idle_{v}"] = mpb.frames_idle(mpb.Pal("idle"), v)
        for state, fn in STATE_FRAMES.items():
            out[state] = fn(mpb.Pal(state))
        return out
    finally:
        mpb.make_frame, mpb.COSTUME = original, previous


def grid(img):
    """The 32x32 grid behind a 96x96 frame."""
    return img.resize((mpb.W, mpb.H), Image.NEAREST)


@pytest.fixture(scope="module")
def plain():
    return render(None)


@pytest.fixture(scope="module")
def infos(plain):
    """The anchor info of every frame of every state (the costume does not change them)."""
    return [(name, i, f[1]) for name, (frames, _) in plain.items() for i, f in enumerate(frames)]


def snapshot(root):
    """{relative path: bytes} of every file under root, and the folders too (None)."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        out[str(p.relative_to(root))] = None if p.is_dir() else p.read_bytes()
    return out


def stub_drawing(monkeypatch):
    """Replace the drawing by a function that only makes the folder and its manifest (the logic tests need no GIFs)."""
    made = []

    def fake(closet, template, color, base="buddy"):
        ident = cc.costume_id(template, color)
        folder = Path(closet) / ident
        if os.path.lexists(str(folder)):
            raise FileExistsError(ident)
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"name": ident, "costume": {
            "name": cc.costume_name(template, color), "template": template, "color": color, "base": base}}), encoding="utf-8")
        made.append((template, color, base))
        return folder
    monkeypatch.setattr(mc, "make_costume", fake)
    return made


# --- the library -------------------------------------------------------------------------------------------------

def test_the_library_follows_its_own_rules():
    cc.validate()
    assert len(cc.TEMPLATES) >= 8 and len(cc.COLORS) == 7
    assert 8 <= len(cc.combos()) <= 64  # the daemon takes at most 64 costumes from a closet
    for t in cc.TEMPLATES:
        w, h = cc.size(t.id)
        assert w <= 14 and h <= 7 and w % 2 == 0, t.id
        assert 1 <= t.sink <= 3, t.id


def test_ids_and_names_are_well_formed():
    ids = set()
    for template, color in cc.combos():
        ident, name = cc.costume_id(template, color), cc.costume_name(template, color)
        assert ident == f"{template}-{color}" and ID_RE.fullmatch(ident) and ident.isascii(), ident
        assert name == f"{cc.BY_ID[template].name_es} {color}", name
        assert name == name.strip() and name.isprintable() and 0 < len(name) <= 40, name
        ids.add(ident)
    assert len(ids) == len(cc.combos())  # every look has its own id
    assert len({cc.BY_ID[t].name_es for t in cc.BY_ID}) == len(cc.TEMPLATES)


def test_the_colours_come_from_the_mochi_palette():
    allowed = {h.upper() for h in mpb.BODY_HEX.values()} | {"#7CC6FF"}  # the accents of the moods and the sweat drop (cielo)
    for color, hexa in cc.COLORS.items():
        assert hexa.upper() in allowed, color
        assert color.isascii() and color.isalpha() and color == color.lower()


def test_every_costume_has_a_one_pixel_ink_outline_and_only_its_roles():
    for template, color in cc.combos():
        shape = cc.shape(template, color)
        roles = cc.roles(color)
        assert set(shape.values()) <= set(roles.values())
        fill = {p for p, c in shape.items() if c != mpb.INK}
        ink = {p for p, c in shape.items() if c == mpb.INK}
        assert fill and ink, (template, color)
        for (x, y) in fill:   # a fill pixel never touches the empty ground: the ink is always in between
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                assert (x + dx, y + dy) in shape, (template, color, x, y)
        for (x, y) in ink:    # and the ink hugs the fill, one pixel wide, never floating
            assert any((x + dx, y + dy) in fill for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))), (template, color, x, y)


# --- the hook of the generator -----------------------------------------------------------------------------------

def test_the_generator_without_a_hook_is_what_it_was(plain):
    assert mpb.COSTUME is None
    again = render(lambda cv, info: None)   # a hook that paints nothing changes nothing
    for name, (frames, ms) in plain.items():
        assert [f[0].tobytes() for f in frames] == [f[0].tobytes() for f in again[name][0]], name
        assert ms == again[name][1]


def test_the_hook_runs_over_body_and_face_and_under_the_extras(plain):
    mark = (255, 0, 255, 255)

    def hook(cv, info):
        for x in range(0, mpb.W):          # a stripe across the whole grid: the face, the body and any extra
            cv.put(x, 5, mark)
            cv.put(x, 20, mark)
    drawn = render(hook)
    for name, (frames, _) in drawn.items():
        for i, (img, info) in enumerate(frames):
            g = grid(img)
            for x in range(mpb.W):
                for y in (5, 20):
                    want = info["extras"].get((x, y), mark)    # an extra on the stripe wins; anything else is the hook's
                    assert g.getpixel((x, y)) == want, (name, i, x, y)
    # and the hook gets the anchor info of the frame, the lean of the dizzy ones included
    seen = []
    render(lambda cv, info: seen.append(dict(info)))
    assert seen and all({"top_y", "flat_x0", "flat_x1", "cx", "dx", "pose"} <= set(info) for info in seen)
    assert {info["cx"] for info in seen} > {15.5}  # the leaning frames move the middle of the head


def test_the_highest_pose_is_what_the_library_was_sized_for(infos):
    assert min(info["top_y"] for _, _, info in infos) == cc.MIN_TOP
    for t in cc.TEMPLATES:   # whatever the costume, its top row stays on the grid at the highest head
        w, h = cc.size(t.id)
        assert cc.MIN_TOP + t.sink - h >= 0, t.id


def test_every_look_fits_the_grid_in_every_frame_of_every_state(infos):
    assert len(infos) >= 50
    for template, color in cc.combos():
        for name, i, info in infos:
            pixels = cc.place(template, color, info)
            assert pixels, (template, color, name, i)
            assert all(0 <= x < mpb.W and 0 <= y < mpb.H for x, y in pixels), (template, color, name, i)


# --- every template drawn into every frame -------------------------------------------------------------------------

@pytest.mark.parametrize("template", [t.id for t in cc.TEMPLATES])
def test_a_costume_is_the_plain_frame_plus_the_costume_and_nothing_else(plain, template):
    """Per frame: the grid is the plain frame with the costume over it, under the extras. Checked with every colour of
    the library for this template (the colour changes no position), which also proves the stays-on-the-head rules."""
    colors = [c for t, c in cc.combos() if t == template]
    sink = cc.BY_ID[template].sink
    w, h = cc.size(template)
    for color in colors:
        drawn = render(cc.hook(template, color))
        for name, (frames, _) in drawn.items():
            for i, (img, info) in enumerate(frames):
                base_img, base_info = plain[name][0][i]
                assert {k: v for k, v in info.items() if k != "extras"} == {k: v for k, v in base_info.items() if k != "extras"}
                piece = cc.place(template, color, info)
                where = (template, color, name, i)
                g, base = grid(img), grid(base_img)
                extras = info["extras"]
                # anchored: centred on the middle of the head, its bottom row over the flat top, riding the pose and the lean
                xs = [x for x, _ in piece]
                ys = [y for _, y in piece]
                cx = info["cx"] + info["dx"]
                assert min(xs) + max(xs) == 2 * cx, where
                assert max(ys) == info["top_y"] + sink - 1 and min(ys) == max(ys) - h + 1, where
                assert max(xs) - min(xs) + 1 == w, where
                bottom = [x for x, y in piece if y == max(ys)]
                assert any(info["flat_x0"] + info["dx"] - 1 <= x <= info["flat_x1"] + info["dx"] + 1 for x in bottom), where
                assert max(ys) < info["pose"]["b"] - 10, where   # well clear of the face (the tall eyes start at b - 10)
                # the extras win, and in this library they are nowhere near the costume
                assert not set(piece) & set(extras), where
                assert min(x for x, _ in extras) >= 25 if extras else True
                # the picture: plain frame + costume, extras on top, the rest untouched
                want = base.copy()
                for p, c in piece.items():
                    want.putpixel(p, c)
                for p, c in extras.items():
                    want.putpixel(p, c)
                if g.tobytes() != want.tobytes():
                    bad = [(x, y) for y in range(mpb.H) for x in range(mpb.W) if g.getpixel((x, y)) != want.getpixel((x, y))]
                    raise AssertionError(f"{where}: the frame is not plain + costume + extras at {bad[:5]}")
                # the ink outline is on the frame (the shape's own ink pixels, all of them)
                assert all(g.getpixel(p) == mpb.INK for p, c in piece.items() if c == mpb.INK and p not in extras), where
                # the generator's own rules (one body, the extra clear of the head, mirrored unless it leans on purpose);
                # the colour moves no pixel, so one colour per template is enough for the slow check
                if color == colors[0]:
                    mpb.check_frame(name, i, img, info["dx"])


def test_every_colour_draws_its_own_costume_over_the_same_place(infos):
    name, i, info = infos[0]
    shapes = [cc.place("fiesta", c, info) for c in cc.COLORS]
    assert all(set(s) == set(shapes[0]) for s in shapes)
    assert len({tuple(sorted(s.items())) for s in shapes}) == len(cc.COLORS)


def test_a_look_that_would_leave_the_grid_is_refused_and_never_clipped():
    info = {"top_y": 0, "cx": 15.5, "dx": 0}
    paint = cc.hook("mago", "lila")
    with pytest.raises(AssertionError):
        paint(mpb.Canvas(), info)


# --- the tool ----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """A closet with three real costumes (about 3 s) made by the command, and what it printed."""
    closet = tmp_path_factory.mktemp("closet") / "closet"
    out = subprocess.run([sys.executable, str(TOOLS / "make_closet.py"), str(closet), "--count", "3", "--seed", "1"],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-800:]
    return closet, out.stdout


def test_the_command_makes_whole_pack_folders_and_says_what_it_made(made):
    closet, stdout = made
    expected = mc.choose([], 3, 1)
    ids = [cc.costume_id(t, c) for t, c in expected]
    assert sorted(p.name for p in closet.iterdir()) == sorted(ids)   # nothing else, no dot folders left behind
    assert stdout.splitlines() == [f"Buddy se hizo: {cc.costume_name(t, c)}" for t, c in expected]
    for ident in ids:
        assert sorted(p.name for p in (closet / ident).iterdir()) == sorted(["manifest.json"] + [f"{n}.gif" for n in FILES])


def test_the_manifest_is_the_mochi_manifest_with_the_costume_block(made):
    closet, _ = made
    base = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    for template, color in mc.choose([], 3, 1):
        ident = cc.costume_id(template, color)
        manifest = json.loads((closet / ident / "manifest.json").read_text(encoding="utf-8"))
        assert manifest.pop("costume") == {"name": cc.costume_name(template, color), "template": template, "color": color,
                                           "base": "buddy"}
        assert manifest.pop("name") == ident and base.pop("name") == "buddy"
        assert manifest == base   # render, colors, accents and states are the base's
        base["name"] = "buddy"


def test_the_gifs_decode_and_match_the_generators_own_checks(made, plain):
    closet, _ = made
    for template, color in mc.choose([], 3, 1):
        folder = closet / cc.costume_id(template, color)
        drawn = render(cc.hook(template, color))
        for name, (frames, ms) in drawn.items():
            path = folder / f"{name}.gif"
            with Image.open(path) as gif:
                assert gif.size == (96, 96) and gif.n_frames >= 2, path
                gif.seek(0)
                assert gif.convert("RGBA").getpixel((0, 0))[3] == 0, f"{path} must be transparent at the corner"
            mpb.check_gif(path, [f[0] for f in frames], ms)   # every decoded pixel is the pixel that was drawn
            for i, (img, info) in enumerate(frames):
                mpb.check_frame(name, i, img, info["dx"])
        assert sum(p.stat().st_size for p in folder.iterdir()) < int(1.8 * 1024 * 1024)  # what a pushed pack may weigh


def test_the_same_seed_gives_the_same_bytes(made, tmp_path):
    closet, _ = made
    again = tmp_path / "again"
    done = subprocess.run([sys.executable, str(TOOLS / "make_closet.py"), str(again), "--count", "3", "--seed", "1"],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-800:]
    assert snapshot(again) == snapshot(closet)


def test_the_committed_pack_is_untouched_and_the_hook_is_put_back(tmp_path):
    before = snapshot(PACK)
    mc.make_costume(tmp_path / "c", "gato", "lila")
    assert snapshot(PACK) == before
    assert mpb.COSTUME is None
    assert sorted(p.name for p in (tmp_path / "c").iterdir()) == ["gato-lila"]


def test_a_costume_that_fails_a_check_leaves_nothing_and_puts_the_hook_back(tmp_path, monkeypatch):
    def broken(template, color):
        def paint(cv, info):
            raise AssertionError("a frame broke a rule")
        return paint
    monkeypatch.setattr(cc, "hook", broken)
    with pytest.raises(AssertionError):
        mc.make_costume(tmp_path / "c", "gato", "lila")
    assert mpb.COSTUME is None
    assert list((tmp_path / "c").iterdir()) == []      # no half-made folder, no dot folder


def test_a_costume_is_never_overwritten_and_a_stale_dot_folder_is_replaced(tmp_path):
    closet = tmp_path / "c"
    (closet / ".gato-lila.tmp").mkdir(parents=True)
    (closet / ".gato-lila.tmp" / "junk").write_text("a crashed run", encoding="utf-8")
    first = mc.make_costume(closet, "gato", "lila")
    assert first.name == "gato-lila" and not (closet / ".gato-lila.tmp").exists()
    stamp = (first / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        mc.make_costume(closet, "gato", "lila")
    assert (first / "manifest.json").read_bytes() == stamp


def test_the_tool_writes_nowhere_but_the_closet_and_the_sheet_it_was_asked_for(tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "keep.txt").write_text("mine", encoding="utf-8")
    before = snapshot(tmp_path)
    assert mc.main([str(tmp_path / "deep" / "er" / "closet"), "--seed", "2", "--sheet", str(tmp_path / "out" / "sheet.png")]) == 0
    after = snapshot(tmp_path)
    new = {k for k in after if k not in before}
    assert all(k.startswith(("deep", "out")) for k in new), sorted(new)
    assert {k: v for k, v in after.items() if k in before} == before
    assert (tmp_path / "out" / "sheet.png").is_file()


def test_the_default_is_one_costume_and_the_default_seed_is_how_many_the_closet_holds(tmp_path, monkeypatch, capsys):
    made = stub_drawing(monkeypatch)
    closet = tmp_path / "c"
    assert mc.main([str(closet)]) == 0
    assert made == [mc.choose([], 1, 0)[0] + ("buddy",)]
    first = cc.costume_id(*made[0][:2])
    assert mc.main([str(closet)]) == 0
    assert made[1][:2] == mc.choose([first], 1, 1)[0]      # one costume in the closet: seed 1
    assert capsys.readouterr().out.count("Buddy se hizo:") == 2
    assert sorted(p.name for p in closet.iterdir()) == sorted(cc.costume_id(*m[:2]) for m in made)


def test_count_and_seed_choose_the_costumes(tmp_path, monkeypatch, capsys):
    made = stub_drawing(monkeypatch)
    assert mc.main([str(tmp_path / "a"), "--count", "4", "--seed", "9"]) == 0
    assert [m[:2] for m in made] == mc.choose([], 4, 9)
    assert len(capsys.readouterr().out.splitlines()) == 4          # one line per costume
    other = tmp_path / "b"
    assert mc.main([str(other), "--count", "4", "--seed", "10"]) == 0
    assert [m[:2] for m in made[4:]] == mc.choose([], 4, 10)
    assert len({cc.costume_id(*m[:2]) for m in made[:4]}) == 4   # never the same look twice in a run
    assert mc.main([str(tmp_path / "a"), "--count", "2", "--seed", "9"]) == 0   # and never one the closet already has
    taken = {cc.costume_id(*m[:2]) for m in made[:4]}
    assert not {cc.costume_id(*m[:2]) for m in made[8:]} & taken


def test_the_choice_is_deterministic_balanced_and_never_repeats_a_look():
    assert mc.choose([], 5, 3) == mc.choose([], 5, 3) == mc.choose(set(), 5, 3)
    firsts = {mc.choose([], 1, s)[0] for s in range(12)}
    assert len(firsts) > 3                                    # the seed moves the choice
    first = mc.choose([], 7, 0)
    assert len({t for t, _ in first}) == 7                    # the least used template goes first...
    assert len({c for _, c in first}) == 7                    # ...and then the least used colour
    eight = mc.choose([], 8, 0)
    assert {t for t, _ in eight} == {t.id for t in cc.TEMPLATES}
    taken = [cc.costume_id(*tc) for tc in eight]
    more = mc.choose(taken, 8, 1)
    assert not {cc.costume_id(*tc) for tc in more} & set(taken)
    assert {t for t, _ in more} == {t.id for t in cc.TEMPLATES}   # the second round dresses every template once more


def test_choosing_from_the_whole_library_never_repeats_and_runs_out(tmp_path):
    everything = mc.choose([], 1000, 0)
    assert len(everything) == len(cc.combos()) == len({cc.costume_id(*tc) for tc in everything})
    ids = [cc.costume_id(*tc) for tc in everything]
    assert mc.choose(ids, 3, 0) == []
    assert mc.choose(ids[:-2], 5, 0) == [tc for tc in everything[-2:]] or sorted(mc.choose(ids[:-2], 5, 0)) == sorted(everything[-2:])


def test_when_the_closet_is_full_it_says_so_and_exits_zero(tmp_path, monkeypatch, capsys):
    made = stub_drawing(monkeypatch)
    closet = tmp_path / "c"
    assert mc.main([str(closet), "--count", "64"]) == 0
    assert len(made) == len(cc.combos())
    assert "Eso era todo" in capsys.readouterr().out
    count = len(made)
    assert mc.main([str(closet)]) == 0
    out = capsys.readouterr().out
    assert len(made) == count and "Buddy se hizo" not in out and f"({len(cc.combos())})" in out
    assert len(list(closet.iterdir())) == count


def test_a_folder_with_the_name_of_a_look_is_never_touched_or_replaced(tmp_path, monkeypatch):
    made = stub_drawing(monkeypatch)
    closet = tmp_path / "c"
    (closet / "gato-lila").mkdir(parents=True)
    (closet / "gato-lila" / "mine.txt").write_text("not a costume", encoding="utf-8")
    link = closet / "flor-sol"
    link.symlink_to(tmp_path)
    assert mc.main([str(closet), "--count", "64"]) == 0
    assert not {("gato", "lila"), ("flor", "sol")} & {m[:2] for m in made}
    assert (closet / "gato-lila" / "mine.txt").read_text(encoding="utf-8") == "not a costume"
    assert link.is_symlink() and not list(tmp_path.glob("manifest.json"))


def test_flags_are_checked_and_the_base_name_goes_into_the_manifest(tmp_path, monkeypatch):
    made = stub_drawing(monkeypatch)
    for bad in (["--count", "0"], ["--count", "65"], ["--count", "x"], ["--seed", "x"], ["--base-name", "a b"],
                ["--base-name", ".hidden"], ["--base-name", "a/b"], ["--base-name", ""], ["--nope"]):
        with pytest.raises(SystemExit) as stop:
            mc.main([str(tmp_path / "c"), *bad])
        assert stop.value.code == 2, bad
    assert not (tmp_path / "c").exists() and not made
    assert mc.main([str(tmp_path / "c"), "--base-name", "mochi_2.0-x"]) == 0
    assert made[0][2] == "mochi_2.0-x"


def test_a_closet_that_is_a_file_is_an_error_not_a_crash(tmp_path, capsys):
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert mc.main([str(tmp_path / "file")]) == 1
    assert "no es una carpeta" in capsys.readouterr().err


def test_list_shows_what_the_closet_holds_and_makes_nothing(tmp_path, monkeypatch, capsys):
    made = stub_drawing(monkeypatch)
    closet = tmp_path / "nothing-here"
    assert mc.main([str(closet), "--list"]) == 0
    assert "vacío" in capsys.readouterr().out and not closet.exists()
    assert mc.main([str(closet), "--count", "3", "--seed", "4"]) == 0
    capsys.readouterr()
    (closet / "stray.txt").write_text("x", encoding="utf-8")
    (closet / "empty-folder").mkdir()
    (closet / ".hidden").mkdir()
    (closet / "broken").mkdir()
    (closet / "broken" / "manifest.json").write_text("{", encoding="utf-8")
    (closet / "nocostume").mkdir()
    (closet / "nocostume" / "manifest.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    before = snapshot(closet)
    assert mc.main([str(closet), "--list", "--count", "5"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert snapshot(closet) == before and len(made) == 3
    assert out[0] == "El closet de Buddy tiene 3 disfraces:"
    want = sorted((cc.costume_id(*m[:2]), cc.costume_name(*m[:2])) for m in made)
    assert out[1:] == [f"  {i}  {n}" for i, n in want]


def test_read_closet_skips_what_is_not_a_costume(tmp_path):
    closet = tmp_path / "c"
    mc.make_costume(closet, "corona", "sol", "otro")
    (closet / "sub").mkdir()
    (closet / "sub" / "manifest.json").write_text(json.dumps({"costume": {"name": 5}}), encoding="utf-8")   # no text name
    (closet / "list").mkdir()
    (closet / "list" / "manifest.json").write_text("[1]", encoding="utf-8")
    (closet / "link").symlink_to(closet / "corona-sol")
    rows = mc.read_closet(closet)
    assert [r["id"] for r in rows] == ["corona-sol"]
    row = rows[0]
    assert row["name"] == "corona sol" and row["template"] == "corona" and row["color"] == "sol" and row["base"] == "otro"


def test_the_sheet_is_a_png_with_a_row_per_costume_and_the_seven_moods(made, tmp_path, capsys):
    closet, _ = made
    sheet = tmp_path / "sheet.png"
    assert mc.main([str(closet), "--list", "--sheet", str(sheet)]) == 0
    with Image.open(sheet) as im:
        assert im.format == "PNG"
        rows, cell, pad = 3, 96 * 3, 12
        assert im.size == (mc.LABEL_W + (cell + pad) * 7 + pad, (cell + pad) * rows + pad)
        ground = mpb.hexa(mc.SHEET_BG)[:3]
        assert im.convert("RGB").getpixel((im.width - 1, im.height - 1)) == ground     # the dark ground of the screen
        for j in range(rows):    # every mood cell of every row has a Mochi in it (not the bare ground)
            for i in range(7):
                x0, y0 = mc.LABEL_W + pad + i * (cell + pad), pad + j * (cell + pad)
                crop = im.convert("RGB").crop((x0, y0, x0 + cell, y0 + cell))
                assert len(crop.getcolors(maxcolors=cell * cell)) > 6, (j, i)
    assert str(sheet) not in capsys.readouterr().err


def test_a_sheet_of_an_empty_closet_says_so(tmp_path, capsys):
    sheet = tmp_path / "s.png"
    assert mc.main([str(tmp_path / "none"), "--list", "--sheet", str(sheet)]) == 0
    assert not sheet.exists() and "No hay disfraces" in capsys.readouterr().err


def test_without_pillow_it_says_so_in_spanish_and_exits_one(tmp_path):
    code = ("import sys, runpy\nsys.modules['PIL'] = None\nsys.path.insert(0, %r)\nsys.argv = ['make_closet.py', %r]\n"
            "runpy.run_path(%r, run_name='__main__')\n" % (str(TOOLS), str(tmp_path / "closet"), str(TOOLS / "make_closet.py")))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "Pillow" in done.stderr and "Hace falta" in done.stderr
    assert not (tmp_path / "closet").exists() and done.stdout == ""


def test_the_tools_stay_python_39_clean_and_the_daemon_never_meets_them():
    for name in ("closet_costumes.py", "make_closet.py", "make_pixel_buddy.py"):
        tree = ast.parse((TOOLS / name).read_text(encoding="utf-8"), feature_version=(3, 9))
        for node in ast.walk(tree):
            notes = []
            if isinstance(node, ast.arg):
                notes.append(node.annotation)
            elif isinstance(node, ast.AnnAssign):
                notes.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                notes.append(node.returns)
            for note in notes:
                for sub in ast.walk(note) if note is not None else []:
                    assert not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)), f"{name}: X | Y in an annotation"
    for daemon in ROOT.glob("buddy*.py"):
        source = daemon.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(PIL|make_pixel_buddy|make_closet|closet_costumes)\b", source, re.M), daemon.name


def test_nothing_personal_in_the_tools():
    text = "".join((TOOLS / n).read_text(encoding="utf-8") for n in ("closet_costumes.py", "make_closet.py"))
    assert not re.search(r"/Users/|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|hermes\.token|someuser", text)
