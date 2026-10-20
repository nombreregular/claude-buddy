#!/usr/bin/env python3
"""Mochi: Buddy as one soft rice-cake squircle, drawn in pixel art.

A single silhouette (wider than tall, flat-ish top, flat bottom), no antenna, no arms, no ears. Big glossy eyes
set low on the face, two small blush marks and a tiny mouth. Two nub feet are optional (FEET). Every mood changes
the body colour, the eyes and the mouth, and adds at most ONE small element off to the upper right (a Z, a sweat
drop, a spark, a heart); the top of the head stays clear so costumes can be baked on later.

Drawn on a 32x32 grid and scaled 3x with nearest neighbour to 96x96 transparent animated GIFs (the size the
official Hardware Buddy packs use). Needs Pillow; the daemon itself never imports this file.

    python3 tools/make_pixel_buddy.py characters/buddy-pixel                    # the pack
    python3 tools/make_pixel_buddy.py characters/buddy-pixel --sheet sheet.png  # plus a contact sheet
    python3 tools/make_pixel_buddy.py DEST [--name buddy] [--sheet PNG] [--sheet-mid PNG] [--anchors JSON] [--debug DIR]

The art is procedural, so it stays editable: change a colour or a shape here and run it again. Every GIF is
decoded back and checked (size, transparency, one body, extras clear of the head), and the run fails if a frame
breaks a rule. `--anchors` writes where the top of the head is in every frame (for baking costumes on later);
tools/make_closet.py bakes them on through the COSTUME hook of make_frame.
"""
import argparse
import colorsys
import json
import math
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image

W = H = 32
SCALE = 3
GROUND = 28          # bottom row of the resting body (a foot, if any, would still fit below)
FEET = "none"        # "none": the body sits on the ground itself (cleanest); "nub" or "short" add tiny feet
BODY_W, BODY_H = 26, 19

RGBA = Tuple[int, int, int, int]
TRANSPARENT: RGBA = (0, 0, 0, 0)


def hexa(s: str, a: int = 255) -> RGBA:
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), a)


def mix(a: RGBA, b: RGBA, t: float) -> RGBA:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3)) + (255,)  # type: ignore


def hsv_adjust(c: RGBA, dh: float, ds: float, vmul: float) -> RGBA:
    h, s, v = colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)
    h = (h + dh) % 1.0
    s = max(0.0, min(1.0, s + ds))
    v = max(0.0, min(1.0, v * vmul))
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)), 255)


# the screen's mood colours (DESIGN.md); light / shade / line are derived from them, hue kept within ~10 degrees
BODY_HEX = {
    "sleep": "#A78BFA", "idle": "#FF9E7A", "busy": "#4FD1C5", "attention": "#FFCF5C",
    "celebrate": "#8BE76A", "dizzy": "#A9B8CF", "heart": "#FF8FA3",
}
# blush is picked by hand per mood: mixing pink into lime or teal turns it brown
BLUSH_HEX = {
    "sleep": "#F27FCB", "idle": "#FF6B86", "busy": "#FF8DB0", "attention": "#FF8A5C",
    "celebrate": "#FF86B0", "dizzy": "#FF9DB8", "heart": "#F4527E",
}
WHITE = hexa("#FFFFFF")
INK = hexa("#1B1626")
TONGUE = hexa("#FF6F7D")
SWEAT = hexa("#7CC6FF")
SPARK = hexa("#FFF3B8")
HEART_RED = hexa("#E23A66")
ZZ = hexa("#E4DAFF")


class Pal:
    def __init__(self, mood: str) -> None:
        base = hexa(BODY_HEX[mood])
        h = colorsys.rgb_to_hsv(*(c / 255 for c in base[:3]))[0]
        dh = -0.02 if h < 0.2 else 0.02
        self.base = base
        self.light = mix(base, WHITE, 0.55)
        self.shade = hsv_adjust(base, dh, 0.10, 0.80)
        self.line = hsv_adjust(base, dh * 1.5, 0.22, 0.46)
        self.blush = hexa(BLUSH_HEX[mood])
        self.foot = hsv_adjust(base, dh * 1.5, 0.16, 0.62)


class Canvas:
    def __init__(self) -> None:
        self.px: Dict[Tuple[int, int], RGBA] = {}

    def put(self, x: int, y: int, c: RGBA) -> None:
        if 0 <= x < W and 0 <= y < H:
            self.px[(x, y)] = c

    def rect(self, x: int, y: int, w: int, h: int, c: RGBA) -> None:
        for j in range(h):
            for i in range(w):
                self.put(x + i, y + j, c)

    def glyph(self, x: int, y: int, rows: Sequence[str], c: RGBA, alt: Optional[RGBA] = None,
              alt2: Optional[RGBA] = None) -> None:
        """Paint a tiny bitmap: '#' in c, '+' in alt, 'o' in alt2; anything else is skipped."""
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch == "#":
                    self.put(x + i, y + j, c)
                elif ch == "+" and alt is not None:
                    self.put(x + i, y + j, alt)
                elif ch == "o" and alt2 is not None:
                    self.put(x + i, y + j, alt2)

    def shear(self, fn: Callable[[int], int]) -> "Canvas":
        out = Canvas()
        for (x, y), c in self.px.items():
            out.put(x + fn(y), y, c)
        return out

    def merge(self, other: "Canvas") -> None:
        self.px.update(other.px)

    def image(self) -> Image.Image:
        img = Image.new("RGBA", (W, H), TRANSPARENT)
        for (x, y), c in self.px.items():
            img.putpixel((x, y), c)
        return img.resize((W * SCALE, H * SCALE), Image.NEAREST)


# --- the body --------------------------------------------------------------------------------------

# the neutral silhouette, row by row from the top: a soft dome, the widest part low, a flat-ish base.
# Every step is 2 px (1 per side) or 4 px (2 per side) near the top and bottom, so the outline stays even.
BASE_ROWS = [12, 16, 18, 20, 22, 24, 24, 26, 26, 26, 26, 26, 26, 26, 24, 24, 22, 20, 16]
BASE_H, BASE_W = len(BASE_ROWS), 26


def row_widths(w: int, h: int) -> List[int]:
    """Even width of every row for a body w wide and h tall, resampled from BASE_ROWS (so a squash or a
    stretch keeps the same dome). Mirrors around x = 15.5, hence even."""
    out = []
    for r in range(h):
        src = BASE_ROWS[min(BASE_H - 1, int((r + 0.5) * BASE_H / h))]
        out.append(max(2, 2 * int(math.floor(src * w / BASE_W / 2 + 0.5))))
    return out


class Pose:
    """Where the body is on this frame. b is the bottom outline row; w, h its size."""

    def __init__(self, b: int, w: int = BODY_W, h: int = BODY_H) -> None:
        self.b, self.w, self.h = b, w, h

    @property
    def top(self) -> int:
        return self.b - self.h + 1


def body(cv: Canvas, pal: Pal, pose: Pose, shift: Optional[Callable[[int], int]] = None) -> Dict[str, object]:
    """Fill, outline, a thin light rim (top-left) and shade (bottom, right). `shift` moves each row sideways
    (the dizzy lean) BEFORE the outline is found, so a leaning body still has a clean one pixel outline.
    Returns the anchor info for costumes."""
    widths = row_widths(pose.w, pose.h)
    mask = set()
    spans = {}
    for r, wr in enumerate(widths):
        y = pose.top + r
        sh = shift(y) if shift else 0
        x0, x1 = 16 - wr // 2 + sh, 15 + wr // 2 + sh
        spans[y] = (x0, x1)
        for x in range(x0, x1 + 1):
            mask.add((x, y))

    outline = set()
    for (x, y) in mask:
        if not all((x + dx, y + dy) in mask for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
            outline.add((x, y))
    cy = pose.top + pose.h / 2
    for (x, y) in mask:
        if (x, y) in outline:
            cv.put(x, y, pal.line)
            continue
        cx = 15.5 + (shift(y) if shift else 0)
        up_line = (x, y - 1) in outline
        left_line = (x - 1, y) in outline
        diag_line = (x - 1, y - 1) in outline or (x - 1, y - 1) not in mask
        down_line = (x, y + 1) in outline
        right_line = (x + 1, y) in outline
        c = pal.base
        if (up_line and x <= cx + 2.5) or (left_line and y <= cy + 1) or (diag_line and x <= cx - 1.5 and y <= cy - 2):
            c = pal.light
        if down_line or (right_line and y >= cy):
            c = pal.shade
        cv.put(x, y, c)
    x0, x1 = spans[pose.top]
    return {"top_y": pose.top, "flat_x0": x0, "flat_x1": x1, "cx": (x0 + x1) / 2}


def feet(cv: Canvas, pal: Pal, pose: Pose) -> None:
    if FEET == "nub":
        for fx in (8, 20):
            cv.rect(fx, pose.b + 1, 4, 2, pal.foot)
    elif FEET == "short":
        for fx in (9, 20):
            cv.rect(fx, pose.b + 1, 3, 1, pal.foot)


# --- the face: positions hang off the bottom row of the body, so a squash keeps the face steady ----------

EYE_L, EYE_R = 8, 19     # left x of each 5-wide eye (mirror: 8+4 = 12, 19+4 = 23 -> 12 + 19 = 31)


def eye_top(pose: Pose) -> int:
    return pose.b - 9


def eye_open(cv: Canvas, pose: Pose, look: int = 0, tall: int = 0, dy: int = 0) -> None:
    """Big glossy dark oval, 5 wide, 6 tall (7 with tall=1), a 2x2 shine and a small second glint."""
    y = eye_top(pose) + dy - tall
    for ex in (EYE_L, EYE_R):
        x = ex + look
        rows = [".###."] + ["#####"] * (4 + tall) + [".###."]
        cv.glyph(x, y, rows, INK)
        cv.rect(x + 1, y + 1, 2, 2, WHITE)
        cv.put(x + 3, y + 3 + tall, WHITE)


def eye_focus(cv: Canvas, pose: Pose, look: int = 0) -> None:
    """Busy: the same glossy eyes with a flat lid, so it reads 'concentrating' and not 'wide open'."""
    y = eye_top(pose) + 1
    for ex in (EYE_L, EYE_R):
        x = ex + look
        cv.glyph(x, y, ["#####", "#####", "#####", "#####", ".###."], INK)
        cv.rect(x + 1, y + 1, 2, 2, WHITE)
        cv.put(x + 3, y + 3, WHITE)


def eye_blink(cv: Canvas, pose: Pose, look: int = 0, dy: int = 0) -> None:
    y = eye_top(pose) + dy
    for ex in (EYE_L, EYE_R):
        cv.glyph(ex + look, y + 4, ["#####", ".###."], INK)


def eye_sleep(cv: Canvas, pose: Pose, dy: int = 0) -> None:
    """Closed, relaxed: a little U."""
    y = eye_top(pose) + dy + 3
    for ex in (EYE_L, EYE_R):
        cv.glyph(ex, y, ["#...#", ".###."], INK)


def eye_happy(cv: Canvas, pose: Pose, dy: int = 0) -> None:
    """Joyful ^ ^ : an arch."""
    y = eye_top(pose) + dy + 2
    for ex in (EYE_L, EYE_R):
        cv.glyph(ex, y, [".###.", "#...#", "#...#"], INK)


def eye_heart(cv: Canvas, pose: Pose, dy: int = 0) -> None:
    y = eye_top(pose) + dy
    for ex in (EYE_L, EYE_R):
        cv.glyph(ex, y, [".#.#.", "#####", "#####", ".###.", "..#.."], HEART_RED)
        cv.put(ex + 1, y + 1, hexa("#FFC2CF"))


# a round swirl; the second frame is the first turned half a turn, so the eyes seem to spin
SWIRL_A = [".###.", "#...#", "#.#.#", "#..#.", ".##.."]
SWIRL_B = ["..##.", ".#..#", "#.#.#", "#...#", ".###."]


def eye_dizzy(cv: Canvas, pose: Pose, phase: int, dy: int = 0) -> None:
    y = eye_top(pose) + dy + 1
    for i, ex in enumerate((EYE_L, EYE_R)):
        cv.glyph(ex, y, SWIRL_A if (phase + i) % 2 == 0 else SWIRL_B, INK)


def mouth(cv: Canvas, pose: Pose, kind: str, dx: int = 0, dy: int = 0) -> None:
    y = pose.b - 5 + dy
    x = 14 + dx
    if kind == "smile":
        cv.glyph(x, y, ["#..#", ".##."], INK)
    elif kind == "dash":
        cv.glyph(x + 1, y + 1, ["##"], INK)
    elif kind == "o":
        cv.glyph(x + 1, y - 1, ["##", "##", "##"], INK)
    elif kind == "big":
        cv.glyph(x, y, ["####", "####", ".++."], INK, TONGUE)
    elif kind == "wavy":
        cv.glyph(x, y, ["##..", "..##"], INK)
    elif kind == "wavy2":
        cv.glyph(x, y, ["..##", "##.."], INK)


def blush(cv: Canvas, pose: Pose, pal: Pal, dx: int = 0, dy: int = 0, big: bool = False) -> None:
    y = pose.b - 3 + dy
    for bx in (6, 23):
        cv.rect(bx + dx, y, 3, 2 if big else 1, pal.blush)


# --- the one small extra per mood: always up and to the right, clear of the top of the head ------------

Z3 = ["###", ".#.", "###"]
Z4 = ["####", "..#.", ".#..", "####"]
Z5 = ["#####", "...#.", "..#..", ".#...", "#####"]
PLUS3 = [".#.", "###", ".#."]
STAR5 = ["..#..", ".###.", "#####", ".###.", "..#.."]
HEART_G = [".#.#.", "#####", ".###.", "..#.."]
DROP = [".##.", ".##.", "####", "####", ".##."]


def extra_z(cv: Canvas, step: int) -> None:
    seq = [(Z5, 25, 2), (Z5, 26, 1), (Z5, 26, 0), None, (Z3, 25, 6), (Z4, 25, 4)]
    z = seq[step % len(seq)]
    if z:
        cv.glyph(z[1], z[2], z[0], ZZ)


def extra_drop(cv: Canvas, step: int) -> None:
    cv.glyph(26, 1 + step, DROP, SWEAT)
    cv.put(27, 3 + step, WHITE)


def extra_spark(cv: Canvas, step: int, x: int = 28, y: int = 3) -> None:
    """Twinkle between a small plus and a four point star, centred on (x, y)."""
    if step % 2 == 0:
        cv.glyph(x - 2, y - 2, STAR5, SPARK)
    else:
        cv.glyph(x - 1, y - 1, PLUS3, SPARK)


def extra_heart(cv: Canvas, step: int) -> None:
    cv.glyph(26 + (1 if step % 4 in (1, 2) else 0), 7 - step, HEART_G, hexa("#FF4F6E"))


# --- states -----------------------------------------------------------------------------------------------

Frame = Tuple[Image.Image, Dict[str, object]]

# Optional costume layer, None for the plain Mochi (what characters/buddy-pixel holds). When set, make_frame calls it
# with the canvas and the anchor info of the frame (top_y, flat_x0, flat_x1, cx, pose, dx) once body and face are
# drawn and before the extra, so a costume sits over the head and the extra still wins. tools/make_closet.py sets it.
COSTUME: Optional[Callable[[Canvas, Dict[str, object]], None]] = None


def make_frame(pal: Pal, pose: Pose, face: Callable[[Canvas, Pose], None],
               fx: Optional[Callable[[Canvas], None]] = None,
               shear: Optional[Callable[[int], int]] = None, dx: int = 0) -> Frame:
    """Body and face are separate layers: a shear leans the body row by row, while the face slides as one block
    (an eye cut by a one pixel jog looks broken). The extra goes on last, so it never leans with the body."""
    cv = Canvas()
    feet(cv, pal, pose)
    info = body(cv, pal, pose, shear)
    fc = Canvas()
    face(fc, pose)
    face_dx = dx + (shear(eye_top(pose) + 3) if shear else 0)
    cv.merge(fc.shear(lambda y: face_dx))
    if dx:
        cv = cv.shear(lambda y: dx)
    info["pose"] = {"b": pose.b, "w": pose.w, "h": pose.h}
    info["dx"] = dx
    if COSTUME is not None:
        COSTUME(cv, info)
    if fx is not None:
        ex = Canvas()
        fx(ex)
        cv.merge(ex)
    return cv.image(), info


def neutral_b() -> int:
    """Bottom outline row of the resting body: the ground, less the height of the feet."""
    return GROUND - {"nub": 2, "short": 1}.get(FEET, 0)


def P(lift: int = 0, w: int = BODY_W, h: int = BODY_H) -> Pose:
    return Pose(neutral_b() - lift, w, h)


def frames_sleep(pal: Pal) -> Tuple[List[Frame], int]:
    out = []
    hs = [19, 19, 18, 18, 19, 19]          # a slow breath
    for i in range(6):
        pose = P(0, BODY_W, hs[i])

        def face(cv: Canvas, p: Pose) -> None:
            eye_sleep(cv, p)
            mouth(cv, p, "dash")
            blush(cv, p, pal)
        out.append(make_frame(pal, pose, face, lambda cv, i=i: extra_z(cv, i)))
    return out, 260


def frames_idle(pal: Pal, variant: int) -> Tuple[List[Frame], int]:
    out = []
    looks = {0: [0] * 8, 1: [0, 0, -2, -2, -2, -2, 0, 0], 2: [0, 0, 2, 2, 2, 2, 0, 0]}[variant]
    for i in range(8):
        pose = P(0, BODY_W, 18 if i in (3, 4) else 19)
        look = looks[i]

        def face(cv: Canvas, p: Pose, i: int = i, look: int = look) -> None:
            if variant == 0 and i == 6:
                eye_blink(cv, p)
            else:
                eye_open(cv, p, look=look)
            mouth(cv, p, "smile", dx=look // 2)
            blush(cv, p, pal, dx=look // 2)
        out.append(make_frame(pal, pose, face))
    return out, 170


def frames_busy(pal: Pal) -> Tuple[List[Frame], int]:
    out = []
    looks = [-1, 0, 1, 1, 0, -1]
    for i in range(6):
        pose = P(0, BODY_W, 18 if i % 2 else 19)

        def face(cv: Canvas, p: Pose, i: int = i) -> None:
            eye_focus(cv, p, look=looks[i])
            mouth(cv, p, "dash", dx=looks[i])
            blush(cv, p, pal, dx=looks[i])
        out.append(make_frame(pal, pose, face, lambda cv, i=i: extra_drop(cv, i)))
    return out, 150


def frames_attention(pal: Pal) -> Tuple[List[Frame], int]:
    out = []
    # a hop: stand, stretch up, peak, coming down, land (squash), settle
    plan = [(0, 26, 19), (2, 25, 20), (4, 24, 20), (2, 25, 20), (0, 28, 17), (0, 26, 19)]
    for i, (lift, w, h) in enumerate(plan):
        pose = P(lift, w, h)

        def face(cv: Canvas, p: Pose) -> None:
            eye_open(cv, p, tall=1)
            mouth(cv, p, "o")
            blush(cv, p, pal)
        out.append(make_frame(pal, pose, face, lambda cv, i=i: extra_spark(cv, i)))
    return out, 140


def frames_celebrate(pal: Pal) -> Tuple[List[Frame], int]:
    out = []
    plan = [(3, 25, 20), (5, 24, 20), (3, 25, 20), (0, 28, 17), (0, 26, 19), (2, 25, 20), (0, 26, 19), (0, 28, 17)]
    for i, (lift, w, h) in enumerate(plan):
        pose = P(lift, w, h)

        def face(cv: Canvas, p: Pose) -> None:
            eye_happy(cv, p)
            mouth(cv, p, "big")
            blush(cv, p, pal, big=True)
        out.append(make_frame(pal, pose, face, lambda cv, i=i: extra_spark(cv, i)))
    return out, 120


def frames_dizzy(pal: Pal) -> Tuple[List[Frame], int]:
    out = []
    tilt = [-4, -2, 0, 2, 4, 2]
    for i in range(6):
        pose = P(0, BODY_W, 19)
        t = tilt[i]
        # shear: the top leans, the feet stay
        fn = (lambda y, t=t, pose=pose: int(round(t * (pose.b - y) / pose.h)))

        def face(cv: Canvas, p: Pose, i: int = i) -> None:
            eye_dizzy(cv, p, i)
            mouth(cv, p, "wavy" if i % 2 == 0 else "wavy2")
            blush(cv, p, pal)
        out.append(make_frame(pal, pose, face, None, shear=fn))
    return out, 160


def frames_heart(pal: Pal) -> Tuple[List[Frame], int]:
    out = []
    lifts = [0, 0, 1, 1, 0, 0, 1, 1]
    for i in range(8):
        pose = P(lifts[i], BODY_W, 19)

        def face(cv: Canvas, p: Pose) -> None:
            eye_heart(cv, p)
            mouth(cv, p, "smile")
            blush(cv, p, pal)
        out.append(make_frame(pal, pose, face, lambda cv, i=i: extra_heart(cv, i)))
    return out, 180


# --- gif + pack ---------------------------------------------------------------------------------------------

def save_gif(frames: List[Image.Image], ms: int, path: Path) -> None:
    """Animated GIF with real transparency: one palette per frame, disposal 2 (restore to background)."""
    quantized = []
    for f in frames:
        alpha = f.getchannel("A")
        q = f.convert("RGB").quantize(colors=63, method=Image.Quantize.MEDIANCUT).convert("P")
        px, mask = q.load(), alpha.load()
        for y in range(f.height):
            for x in range(f.width):
                if mask[x, y] == 0:
                    px[x, y] = 255
        quantized.append(q)
    quantized[0].save(path, save_all=True, append_images=quantized[1:], duration=ms, loop=0,
                      transparency=255, disposal=2, optimize=False)


def check_gif(path: Path, frames: List[Image.Image], ms: int) -> None:
    """Decode the GIF again: 96x96 and, on the timeline (Pillow folds identical neighbours into one longer
    frame), the same pixels as what we drew."""
    timeline: List[Image.Image] = []
    with Image.open(path) as im:
        assert im.size == (W * SCALE, H * SCALE), (path, im.size)
        for i in range(im.n_frames):
            im.seek(i)
            got = im.convert("RGBA")
            timeline.extend([got] * max(1, round(im.info.get("duration", ms) / ms)))
    assert len(timeline) == len(frames), (path, len(timeline), len(frames))
    for i, (got, want) in enumerate(zip(timeline, frames)):
        for y in range(got.height):
            for x in range(got.width):
                g, w_ = got.getpixel((x, y)), want.getpixel((x, y))
                if (g[3] == 0) != (w_[3] == 0) or (g[3] and g[:3] != w_[:3]):
                    raise AssertionError(f"{path.name} frame {i} differs at {x},{y}: {g} vs {w_}")


def components(img: Image.Image) -> List[List[Tuple[int, int]]]:
    """8-connected blobs of opaque pixels on the 32x32 grid (the GIF is that grid scaled 3x)."""
    grid = img.resize((W, H), Image.NEAREST)
    seen, blobs = set(), []
    for y in range(H):
        for x in range(W):
            if grid.getpixel((x, y))[3] == 0 or (x, y) in seen:
                continue
            stack, blob = [(x, y)], []
            seen.add((x, y))
            while stack:
                cx, cy = stack.pop()
                blob.append((cx, cy))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < W and 0 <= ny < H and (nx, ny) not in seen and grid.getpixel((nx, ny))[3]:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
            blobs.append(blob)
    return sorted(blobs, key=len, reverse=True)


def check_frame(name: str, i: int, img: Image.Image, dx: int) -> None:
    """The rules, as asserts: one body plus at most ONE small extra, the extra clear of the head, no stray
    pixels, and (unless the frame leans on purpose) a body that mirrors around x = 15.5."""
    blobs = components(img)
    assert 1 <= len(blobs) <= 2, f"{name} frame {i}: {len(blobs)} blobs (stray pixels?)"
    body_px = set(blobs[0])
    if len(blobs) == 2:
        extra = blobs[1]
        assert len(extra) <= 25, f"{name} frame {i}: the extra has {len(extra)} px"
        assert min(x for x, _ in extra) >= 25, f"{name} frame {i}: the extra reaches over the head"
    assert len(body_px) > 300, f"{name} frame {i}: body too small"
    if dx == 0 and not name.startswith("dizzy"):
        for (x, y) in body_px:
            assert (31 - x, y) in body_px, f"{name} frame {i}: body not symmetric at {x},{y}"


STATE_ORDER = ["sleep", "idle", "busy", "attention", "celebrate", "dizzy", "heart"]
# which frame to show on the mid sheet: the most telling pose in the middle of each loop
MID = {"sleep": 2, "idle": 3, "busy": 4, "attention": 2, "celebrate": 1, "dizzy": 4, "heart": 4}


def contact_sheet(cells: List[Image.Image], path: Path, scale: int = 3) -> None:
    cw = W * SCALE * scale
    cols = len(cells)
    pad = 24
    img = Image.new("RGBA", (cw * cols + pad * (cols + 1), cw + pad * 2), hexa("#0E0D14"))
    for i, cell in enumerate(cells):
        big = cell.resize((cw, cw), Image.NEAREST)
        img.paste(big, (pad + i * (cw + pad), pad), big)
    img.convert("RGB").save(path)


def build(dest: Path, name: str = "buddy", sheet: Optional[Path] = None, sheet_mid: Optional[Path] = None,
          anchors_path: Optional[Path] = None, debug_dir: Optional[Path] = None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.gif"):
        old.unlink()
    states: Dict[str, object] = {}
    first: Dict[str, Image.Image] = {}
    mid: Dict[str, Image.Image] = {}
    anchors: Dict[str, object] = {}

    def emit(state: str, name: str, frames: List[Frame], ms: int) -> None:
        imgs = [f[0] for f in frames]
        save_gif(imgs, ms, dest / f"{name}.gif")
        check_gif(dest / f"{name}.gif", imgs, ms)
        for i, f in enumerate(frames):
            check_frame(name, i, f[0], f[1]["dx"])
        if name in ("sleep", "idle_0", "busy", "attention", "celebrate", "dizzy", "heart"):
            first[state] = imgs[0]
            mid[state] = imgs[min(MID[state], len(imgs) - 1)]
        anchors[name] = {"ms": ms, "frames": [
            {"top_y": f[1]["top_y"], "cx": f[1]["cx"] + f[1]["dx"], "flat_x": [f[1]["flat_x0"] + f[1]["dx"], f[1]["flat_x1"] + f[1]["dx"]],
             "body": f[1]["pose"]} for f in frames]}
        if debug_dir is not None:
            strip = Image.new("RGBA", (W * SCALE * 2 * len(imgs) + 8 * (len(imgs) + 1), W * SCALE * 2 + 16), hexa("#0E0D14"))
            for i, im in enumerate(imgs):
                big = im.resize((W * SCALE * 2, W * SCALE * 2), Image.NEAREST)
                strip.paste(big, (8 + i * (W * SCALE * 2 + 8), 8), big)
            debug_dir.mkdir(parents=True, exist_ok=True)
            strip.convert("RGB").save(debug_dir / f"{name}.png")

    emit("sleep", "sleep", *frames_sleep(Pal("sleep")))
    states["sleep"] = "sleep.gif"
    idle_files = []
    for v in range(3):
        emit("idle", f"idle_{v}", *frames_idle(Pal("idle"), v))
        idle_files.append(f"idle_{v}.gif")
    states["idle"] = idle_files
    for state, fn in (("busy", frames_busy), ("attention", frames_attention), ("celebrate", frames_celebrate),
                      ("dizzy", frames_dizzy), ("heart", frames_heart)):
        emit(state, state, *fn(Pal(state)))
        states[state] = f"{state}.gif"

    manifest = {
        "name": name,
        "render": "pixel",
        "colors": {"body": BODY_HEX["idle"], "bg": "#0E0D14", "text": "#F3EEF7", "textDim": "#9A93A4", "ink": "#1B1626"},
        "accents": {state: BODY_HEX[state] for state in STATE_ORDER},
        "states": states,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if anchors_path is not None:
        anchors_path.write_text(json.dumps(anchors, indent=1) + "\n", encoding="utf-8")
    if sheet is not None:
        contact_sheet([first[s] for s in STATE_ORDER], sheet)
    if sheet_mid is not None:
        contact_sheet([mid[s] for s in STATE_ORDER], sheet_mid)
    total = sum(p.stat().st_size for p in dest.iterdir())
    print(f"{dest}: {len(list(dest.glob('*.gif')))} GIFs, {total // 1024}K")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dest", type=Path, help="folder for the pack (created; old GIFs in it are replaced)")
    ap.add_argument("--name", default="buddy", help="name in manifest.json (the screen uses the folder name)")
    ap.add_argument("--sheet", type=Path, help="also write a PNG contact sheet with the first frame of each state")
    ap.add_argument("--sheet-mid", type=Path, help="also write a contact sheet with a mid-animation frame of each state")
    ap.add_argument("--anchors", type=Path, help="also write JSON with the top of the head in every frame")
    ap.add_argument("--debug", type=Path, metavar="DIR", help="also write per-state frame strips into DIR")
    args = ap.parse_args()
    build(args.dest, args.name, args.sheet, args.sheet_mid, args.anchors, args.debug)


if __name__ == "__main__":
    main()
