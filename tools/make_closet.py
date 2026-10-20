#!/usr/bin/env python3
"""Mochi's closet: costumes that Buddy "makes himself", each one a complete pack folder.

    python3 tools/make_closet.py CLOSET_DIR                       # one new costume
    python3 tools/make_closet.py CLOSET_DIR --count 8 --sheet closet.png
    python3 tools/make_closet.py CLOSET_DIR --list                # what the closet holds
    python3 tools/make_closet.py CLOSET_DIR [--count N] [--seed S] [--base-name buddy] [--sheet PNG] [--list]

A costume is Mochi with ONE head-top piece (a hat, a bow, a flower, ears) baked into the nine GIFs by the COSTUME
hook of tools/make_pixel_buddy.py, so it follows the bob and the lean of every frame. It lands in
CLOSET_DIR/<template>-<color>/ with a manifest.json (the Mochi manifest, `name` = the costume id, plus a `costume`
block: Spanish name, template, color and `base`, the pack it dresses). Every frame goes through the generator's own
checks. The closet lives outside git; copy it to the state dir of the Pi (the daemon picks from it when it runs with
`--closet auto`) and a new costume is just another folder to copy. Needs Pillow; the daemon never imports this.

The costumes made by a run are chosen from the seed and from what the closet already holds (the least used template
and colour first), so repeated runs keep adding different ones; the default seed is the number of costumes already
there. When every look of the library is in the closet it says so and stops (exit 0).
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import unicodedata
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont

    import closet_costumes as cc
    import make_pixel_buddy as mpb
    PILLOW = True
except ModuleNotFoundError as err:  # only a missing Pillow is told to the user; anything else is a real bug
    if err.name != "PIL":
        raise
    PILLOW = False

NO_PILLOW = ("Hace falta Pillow para dibujar los disfraces de Buddy (solo esta herramienta lo usa, el daemon no): "
             "python3 -m pip install Pillow")
BASE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}")   # what the screen accepts as a pack folder name
MAX_COUNT = 64
SHEET_BG = "#0E0D14"
LABEL_W = 230


# --- what the closet holds --------------------------------------------------------------------------------------

def read_closet(closet: Path) -> List[Dict[str, object]]:
    """The costumes in CLOSET_DIR, sorted by id: {"id", "name", "template", "color", "base", "dir"}. A folder that is
    not a costume pack (no manifest, a broken one, no `costume` block) is left out. Nothing is created or changed."""
    out: List[Dict[str, object]] = []
    if not closet.is_dir():
        return out
    for d in sorted(closet.iterdir(), key=lambda p: p.name):
        if d.name.startswith(".") or d.is_symlink() or not d.is_dir():
            continue
        try:
            manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            costume = manifest["costume"]
            if not isinstance(costume["name"], str):
                continue
            out.append({"id": d.name, "name": costume["name"], "template": str(costume.get("template", "")),
                        "color": str(costume.get("color", "")), "base": str(costume.get("base", "")), "dir": d})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def _digest(seed: int, costume: str) -> str:
    return hashlib.sha256(f"{seed}:{costume}".encode("ascii")).hexdigest()


def choose(taken: Iterable[str], count: int, seed: int) -> List[Tuple[str, str]]:
    """Up to `count` looks (template, color) that are not in `taken` (costume ids already in the closet), one after
    the other: the least used template first, then the least used colour, and the seed breaks the ties (a hash, so
    it does not depend on the Python version). The same closet and seed always give the same answer."""
    used_t: Dict[str, int] = {}
    used_c: Dict[str, int] = {}
    pool = []
    have = set(taken)
    for t, c in cc.combos():
        if cc.costume_id(t, c) in have:
            used_t[t] = used_t.get(t, 0) + 1
            used_c[c] = used_c.get(c, 0) + 1
        else:
            pool.append((t, c))
    out: List[Tuple[str, str]] = []
    while pool and len(out) < count:
        best = min(pool, key=lambda tc: (used_t.get(tc[0], 0), used_c.get(tc[1], 0), _digest(seed, cc.costume_id(*tc))))
        pool.remove(best)
        out.append(best)
        used_t[best[0]] = used_t.get(best[0], 0) + 1
        used_c[best[1]] = used_c.get(best[1], 0) + 1
    return out


# --- making one -------------------------------------------------------------------------------------------------

def make_costume(closet: Path, template: str, color: str, base: str = "buddy") -> Path:
    """Draw Mochi dressed as `template` in `color` into CLOSET_DIR/<id>/ and return that folder. It is drawn in a
    dot folder next to it and renamed when every frame has passed the generator's checks, so the closet never holds
    a half-made costume. Raises AssertionError when a frame breaks a rule (nothing is left behind)."""
    ident = cc.costume_id(template, color)
    final = closet / ident
    if os.path.lexists(str(final)):
        raise FileExistsError(ident)
    closet.mkdir(parents=True, exist_ok=True)
    tmp = closet / f".{ident}.tmp"
    if tmp.is_symlink() or tmp.is_file():
        tmp.unlink()
    elif tmp.is_dir():
        shutil.rmtree(str(tmp))
    previous = mpb.COSTUME
    mpb.COSTUME = cc.hook(template, color)
    try:
        with redirect_stdout(io.StringIO()):          # build() reports on stdout; the tool says its own thing
            mpb.build(tmp, name=ident)
        manifest = json.loads((tmp / "manifest.json").read_text(encoding="utf-8"))
        manifest["costume"] = {"name": cc.costume_name(template, color), "template": template, "color": color,
                               "base": base}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.rename(str(tmp), str(final))
    except BaseException:
        shutil.rmtree(str(tmp), ignore_errors=True)
        raise
    finally:
        mpb.COSTUME = previous
    return final


# --- the contact sheet ------------------------------------------------------------------------------------------

def _mood_frame(folder: Path, manifest: Dict[str, object], mood: str) -> Optional["Image.Image"]:
    states = manifest.get("states", {})
    files = states.get(mood) if isinstance(states, dict) else None  # type: ignore[union-attr]
    name = files[0] if isinstance(files, list) and files else files
    if not isinstance(name, str):
        return None
    try:
        with Image.open(folder / name) as im:
            im.seek(min(mpb.MID[mood], im.n_frames - 1))
            return im.convert("RGBA")
    except (OSError, EOFError):
        return None


def _ascii(text: str) -> str:
    """The font that ships with Pillow has no accents: a sheet label drops them (moño reads mono; the id under it is exact)."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def write_sheet(closet: Path, path: Path) -> int:
    """A contact sheet: one row per costume in the closet, the seven moods across (the most telling frame of each),
    on the screen's dark ground, the Spanish name and the id on the left. The GIF is the 32 grid scaled 3x, and the
    cells are drawn 3x that (2x or 1x when the closet is big, so the picture stays a size a person can open).
    Returns how many costumes are on it."""
    rows = read_closet(closet)
    if not rows:
        return 0
    scale = 3 if len(rows) <= 12 else 2 if len(rows) <= 28 else 1
    cell = mpb.W * mpb.SCALE * scale
    pad = 12
    sheet = Image.new("RGBA", (LABEL_W + (cell + pad) * len(mpb.STATE_ORDER) + pad, (cell + pad) * len(rows) + pad),
                      mpb.hexa(SHEET_BG))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=16)    # a sized font needs Pillow 10.1
    except TypeError:
        font = ImageFont.load_default()
    for j, row in enumerate(rows):
        y = pad + j * (cell + pad)
        draw.text((pad, y + cell // 2 - 22), _ascii(str(row["name"])), fill=mpb.hexa("#F3EEF7"), font=font)
        draw.text((pad, y + cell // 2 + 2), str(row["id"]), fill=mpb.hexa("#9A93A4"), font=font)
        try:
            manifest = json.loads((Path(str(row["dir"])) / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for i, mood in enumerate(mpb.STATE_ORDER):
            frame = _mood_frame(Path(str(row["dir"])), manifest, mood)
            if frame is not None:
                big = frame.resize((cell, cell), Image.NEAREST)
                sheet.paste(big, (LABEL_W + pad + i * (cell + pad), y), big)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(path)
    return len(rows)


# --- the command ------------------------------------------------------------------------------------------------

def _count(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("tiene que ser un número entero")
    if not 1 <= n <= MAX_COUNT:
        raise argparse.ArgumentTypeError(f"tiene que estar entre 1 y {MAX_COUNT}")
    return n


def _base(text: str) -> str:
    if not BASE_NAME_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("el nombre de un pack lleva letras, números, punto, guion y guion bajo")
    return text


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Hace disfraces para Mochi y los guarda en un closet (una carpeta por disfraz).")
    ap.add_argument("closet", type=Path, help="la carpeta del closet (se crea si no existe)")
    ap.add_argument("--count", type=_count, default=1, metavar="N", help="cuántos disfraces nuevos hacer (por defecto 1)")
    ap.add_argument("--seed", type=int, default=None,
                    help="semilla de la elección (por defecto, cuántos disfraces ya hay en el closet)")
    ap.add_argument("--base-name", type=_base, default="buddy", metavar="NOMBRE",
                    help="el pack que se disfraza: el daemon solo lo pone mientras ese pack está activo (por defecto buddy)")
    ap.add_argument("--sheet", type=Path, metavar="PNG", help="también escribe una hoja de contacto de todo el closet")
    ap.add_argument("--list", action="store_true", help="solo muestra lo que hay en el closet")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    if not PILLOW:
        print(NO_PILLOW, file=sys.stderr)
        return 1
    cc.validate()
    closet: Path = args.closet
    if closet.exists() and not closet.is_dir():
        print(f"{closet} no es una carpeta.", file=sys.stderr)
        return 1
    if args.list:
        have = read_closet(closet)
        if have:
            print(f"El closet de Buddy tiene {len(have)} disfraces:" if len(have) != 1 else "El closet de Buddy tiene 1 disfraz:")
            for row in have:
                print(f"  {row['id']}  {row['name']}")
        else:
            print("El closet de Buddy está vacío.")
    else:
        have = read_closet(closet)
        taken: Set[str] = {str(r["id"]) for r in have}
        if closet.is_dir():   # anything that already has the name of a look is taken, a costume or not
            taken.update(p.name for p in closet.iterdir())
        seed = len(have) if args.seed is None else args.seed
        total = len(cc.combos())
        todo = choose(sorted(taken), args.count, seed)
        if not todo:
            print(f"Buddy ya tiene todos los disfraces que sabe hacer ({total}): no hay nada nuevo.")
        for template, color in todo:
            try:
                make_costume(closet, template, color, args.base_name)
            except (AssertionError, OSError) as err:
                print(f"No se pudo hacer {cc.costume_name(template, color)}: {type(err).__name__}: {err}", file=sys.stderr)
                return 1
            print(f"Buddy se hizo: {cc.costume_name(template, color)}", flush=True)
        if todo and len(todo) < args.count:
            print(f"Eso era todo lo que le quedaba por hacer: ahora tiene los {total}.")
    if args.sheet is not None:
        try:
            if write_sheet(closet, args.sheet) == 0:
                print("No hay disfraces para la hoja de contacto.", file=sys.stderr)
        except OSError as err:
            print(f"No se pudo escribir la hoja de contacto: {type(err).__name__}: {err}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
