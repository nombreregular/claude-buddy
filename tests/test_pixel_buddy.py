"""tools/make_pixel_buddy.py draws the pixel Buddy pack; the pack committed in characters/ must be what it draws.

Needs Pillow, which only the tool uses (never the daemon): without it these tests are skipped."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="Pillow is only needed to draw the pack")
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "make_pixel_buddy.py"
PACK = ROOT / "characters" / "buddy-pixel"
MOODS = ["sleep", "idle", "busy", "attention", "celebrate", "dizzy", "heart"]


def draw(dest, *extra):
    done = subprocess.run([sys.executable, str(TOOL), str(dest), *extra], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
    return done.stdout


def test_the_pack_has_the_seven_moods_in_the_official_size(tmp_path):
    draw(tmp_path / "p")
    manifest = json.loads((tmp_path / "p" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["render"] == "pixel" and manifest["name"] == "buddy"
    assert set(manifest["states"]) == set(MOODS)
    assert len(manifest["states"]["idle"]) == 3  # a few idle clips so it does not loop one blink
    assert set(manifest["accents"]) == set(MOODS)
    assert all(len(c) == 7 and c.startswith("#") for c in manifest["accents"].values())
    for state, files in manifest["states"].items():
        for name in files if isinstance(files, list) else [files]:
            gif = Image.open(tmp_path / "p" / name)
            assert gif.size == (96, 96), name  # what the official Hardware Buddy packs use
            assert gif.n_frames >= 2, name
            gif.seek(0)
            assert gif.convert("RGBA").getpixel((0, 0))[3] == 0, f"{name} must be transparent at the corner"
    total = sum(p.stat().st_size for p in (tmp_path / "p").iterdir())
    assert total < int(1.8 * 1024 * 1024)  # the protocol's cap for a pushed pack


def test_the_committed_pack_is_exactly_what_the_tool_draws(tmp_path):
    draw(tmp_path / "p")
    drawn = {p.name: p.read_bytes() for p in (tmp_path / "p").iterdir()}
    committed = {p.name: p.read_bytes() for p in PACK.iterdir()}
    assert drawn.keys() == committed.keys()
    stale = sorted(name for name in drawn if drawn[name] != committed[name])
    assert not stale, f"characters/buddy-pixel is out of date for {stale}: run tools/make_pixel_buddy.py characters/buddy-pixel"


def test_the_tool_can_name_the_pack_and_write_the_extras(tmp_path):
    draw(tmp_path / "p", "--name", "otro", "--sheet", str(tmp_path / "s.png"), "--anchors", str(tmp_path / "a.json"))
    assert json.loads((tmp_path / "p" / "manifest.json").read_text(encoding="utf-8"))["name"] == "otro"
    assert (tmp_path / "s.png").stat().st_size > 1000
    anchors = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    assert "sleep" in anchors and anchors["sleep"]["frames"][0]["top_y"] >= 0  # where a hat would sit
