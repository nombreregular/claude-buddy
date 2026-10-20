"""The costume library for Mochi's closet: small head-top pixel templates, a short list of colours, and the rule
that puts a costume on the head of every frame.

A costume is ONE piece (a hat, a bow, a flower, a pair of ears) drawn on the generator's 32x32 grid, so the GIF is
that grid scaled 3x like the rest of Mochi. The templates are text rows (M main, S shade, H highlight, T trim, a dot
is empty); the 1 px ink outline (#1B1626, the ink of the eyes) is added around the shape by `shape`, so a
costume reads on all seven body colours and on the dark ground. The letters are the roles of a costume:

    M  main       the colour word of the costume (coral, menta, lila, ...)
    S  shade      main, darker, like the shade of the body
    H  highlight main, lighter, like the rim light of the body
    T  trim       a warm cream for pompoms, bands and centres
    K  outline    the ink, never written in a template

Placement: `anchor` centres the costume on the `cx` of the frame (the middle of the flat top of the head, so it
follows the dizzy lean) and lowers it `sink` rows into the head, so its bottom rows overlap the top of the dome
and it rides the bob of every pose: its pixels lie in the rows top_y + sink - height .. top_y + sink - 1 of the
frame, and in x only over the head and the two pixels beside it. `tools/make_closet.py` runs the generator with
`hook(...)` as its COSTUME layer. Needs Pillow through make_pixel_buddy; the daemon never imports this file.
"""
import colorsys
from typing import Callable, Dict, List, NamedTuple, Tuple

from make_pixel_buddy import INK, RGBA, WHITE, Canvas, hexa, hsv_adjust, mix

GRID = 32                # the generator's grid (make_pixel_buddy.W and H)
MAX_W, MAX_H = 14, 7     # the biggest costume, outline included
# the highest the head top ever gets (celebrate at its peak): a costume may rise only MIN_TOP rows above it
# without leaving the 32x32 grid, i.e. height - sink <= MIN_TOP
MIN_TOP = 4

# invariable Spanish colour words, drawn from Mochi's own accent palette (DESIGN.md)
COLORS: Dict[str, str] = {
    "coral": "#FF9E7A",
    "menta": "#4FD1C5",
    "lila": "#A78BFA",
    "rosa": "#FF8FA3",
    "lima": "#8BE76A",
    "cielo": "#7CC6FF",
    "sol": "#FFCF5C",
}
TRIM_HEX = "#FFF4D6"


class Template(NamedTuple):
    id: str                       # ASCII, [a-z0-9]: half of a costume id
    name_es: str                  # Spanish noun phrase: "gorro de fiesta"
    rows: Tuple[str, ...]         # the shape without the outline (M S H T and dots)
    sink: int                     # how many bottom rows of the finished shape (outline included) lie over the head


TEMPLATES: Tuple[Template, ...] = (
    Template("fiesta", "gorro de fiesta", (
        "....TT....",
        "...HMMS...",
        "..HMTMMS..",
        ".HMMMMMTS.",
        "HMTMMMMMMS",
    ), 3),
    Template("corona", "corona", (
        ".MM..MM..MM.",
        ".HM..HM..SM.",
        "HMMMMMMMMMMS",
        "HTMMTMMTMMTS",
    ), 2),
    Template("lana", "gorro de lana", (
        "....TTTT....",
        "....TTTT....",
        "..HMMMMMMS..",
        ".HMMMMMMMMS.",
        "TTTTTTTTTTTT",
    ), 3),
    Template("mago", "sombrero de mago", (
        ".....MM.....",
        "....HMMS....",
        "...HMTMMS...",
        "...TTTTTT...",
        "HMMMMMMMMMMS",
    ), 3),
    Template("chef", "gorro de chef", (
        "..TT.TT.TT..",
        ".TTTTTTTTTT.",
        "TTTTTTTTTTTT",
        ".HMMMMMMMMS.",
        ".HMMMMMMMMS.",
    ), 3),
    Template("lazo", "moño", (
        ".MMM..MMM.",
        "HMMMTTMMMS",
        "HMMMTTMMMS",
        ".MMM..MMM.",
    ), 2),
    Template("flor", "flor", (
        "..MM..",
        "M.MM.M",
        "MMTTMM",
        ".MMMM.",
        "..MM..",
    ), 3),
    Template("gato", "orejas de gato", (
        ".M........M.",
        ".MM......MM.",
        "MTMM....MMTM",
        "MTTM....MTTM",
    ), 2),
)
BY_ID: Dict[str, Template] = {t.id: t for t in TEMPLATES}


def _letters(t: Template) -> List[str]:
    """The finished shape as rows of letters: the template with the ink outline one pixel around it (4-neighbour
    dilation, so the corners come out rounded like the outline of the body)."""
    w, h = len(t.rows[0]), len(t.rows)
    out = [["." for _ in range(w + 2)] for _ in range(h + 2)]
    for j, row in enumerate(t.rows):
        for i, ch in enumerate(row):
            if ch != ".":
                out[j + 1][i + 1] = ch
    for j in range(h + 2):
        for i in range(w + 2):
            if out[j][i] != ".":
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                x, y = i + dx, j + dy
                if 0 <= x < w + 2 and 0 <= y < h + 2 and out[y][x] not in (".", "K"):
                    out[j][i] = "K"
                    break
    return ["".join(r) for r in out]


def size(template: str) -> Tuple[int, int]:
    """(width, height) of the finished shape, outline included."""
    t = BY_ID[template]
    return len(t.rows[0]) + 2, len(t.rows) + 2


def roles(color: str) -> Dict[str, RGBA]:
    main = hexa(COLORS[color])
    dh = -0.02 if colorsys.rgb_to_hsv(*(c / 255 for c in main[:3]))[0] < 0.2 else 0.02  # like Pal.shade
    return {
        "M": main,
        "S": hsv_adjust(main, dh, 0.10, 0.80),
        "H": mix(main, WHITE, 0.55),
        "T": hexa(TRIM_HEX),
        "K": INK,
    }


def shape(template: str, color: str) -> Dict[Tuple[int, int], RGBA]:
    """The finished costume as {(x, y): rgba} with (0, 0) at its top left corner."""
    pal = roles(color)
    out: Dict[Tuple[int, int], RGBA] = {}
    for y, row in enumerate(_letters(BY_ID[template])):
        for x, ch in enumerate(row):
            if ch != ".":
                out[(x, y)] = pal[ch]
    return out


def anchor(template: str, info: Dict[str, object]) -> Tuple[int, int]:
    """Top left corner on the grid for the frame described by `info` (what make_frame hands to its COSTUME layer):
    centred on the middle of the flat top of the head, `sink` rows over it."""
    w, h = size(template)
    cx = float(info["cx"]) + int(info.get("dx", 0))  # type: ignore[arg-type]
    top = int(info["top_y"])  # type: ignore[call-overload]
    return int(round(cx - (w - 1) / 2)), top + BY_ID[template].sink - h   # w is even, so the left edge is a whole pixel


def place(template: str, color: str, info: Dict[str, object]) -> Dict[Tuple[int, int], RGBA]:
    """The costume's pixels on the grid for one frame."""
    x0, y0 = anchor(template, info)
    return {(x0 + x, y0 + y): c for (x, y), c in shape(template, color).items()}


def hook(template: str, color: str) -> Callable[[Canvas, Dict[str, object]], None]:
    """The COSTUME layer of tools/make_pixel_buddy.py for one look. A costume that would leave the grid (Canvas.put
    would silently clip it) is an error: it must never ship half cut."""
    def paint(cv: Canvas, info: Dict[str, object]) -> None:
        pixels = place(template, color, info)
        for x, y in pixels:
            if not (0 <= x < GRID and 0 <= y < GRID):
                raise AssertionError(f"{template}: the costume leaves the {GRID}x{GRID} grid at {x},{y}")
        for (x, y), c in pixels.items():
            cv.put(x, y, c)
    return paint


# --- ids and names ----------------------------------------------------------------------------------------------

def combos() -> List[Tuple[str, str]]:
    """Every look the library can make, in a fixed order: templates in the order above, colours in the order of COLORS."""
    return [(t.id, c) for t in TEMPLATES for c in COLORS]


def costume_id(template: str, color: str) -> str:
    return f"{template}-{color}"


def costume_name(template: str, color: str) -> str:
    return f"{BY_ID[template].name_es} {color}"


def validate() -> None:
    """Raise ValueError when the library breaks its own rules (the tests and make_closet call it)."""
    seen = set()
    for t in TEMPLATES:
        if not t.id.isascii() or not t.id.isalnum() or t.id != t.id.lower():
            raise ValueError(f"template id {t.id!r} is not [a-z0-9]")
        if t.id in seen:
            raise ValueError(f"template id {t.id!r} twice")
        seen.add(t.id)
        widths = {len(r) for r in t.rows}
        if len(widths) != 1:
            raise ValueError(f"{t.id}: rows of different widths")
        if set("".join(t.rows)) - set("MSHT."):
            raise ValueError(f"{t.id}: unknown letters")
        w, h = size(t.id)
        if w > MAX_W or h > MAX_H or w % 2:
            raise ValueError(f"{t.id}: {w}x{h} does not fit {MAX_W}x{MAX_H} with an even width")
        if not 1 <= t.sink <= h or h - t.sink > MIN_TOP:
            raise ValueError(f"{t.id}: sink {t.sink} leaves it out of the grid at the highest pose")
        for j, row in enumerate(_letters(t)):
            if any((c == ".") != (d == ".") for c, d in zip(row, row[::-1])):
                raise ValueError(f"{t.id}: row {j} is not symmetric")
        if not t.name_es or t.name_es != t.name_es.strip():
            raise ValueError(f"{t.id}: bad name")
    for c, hx in COLORS.items():
        if not c.isascii() or not c.isalpha() or c != c.lower() or len(hx) != 7:
            raise ValueError(f"colour {c!r}")
