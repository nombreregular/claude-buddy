"""The closet: the costumes Buddy may wear, and the dice that pick one. Pure standard library.

A costume is a complete character pack in a folder of its own, `<closet>/<id>/manifest.json` plus the GIFs that
manifest lists, with the costume already drawn into every frame (an overlay on the page would drift off the bobbing
body). They are made on a computer with Pillow (tools/make_closet.py) and copied to the closet of the Pi, which lives
in the state dir and never in git; this module only reads them and never imports anything that draws.

    Closet.scan()     reads the folder and keeps only the costumes that pass every check in `_load`.
    Closet.pick()     chooses what Buddy wears next: one costume or none ("sin disfraz"), never the same twice in a row.
    Closet.recheck()  is a costume still what scan() accepted? The screen asks before it shows one, every time.
    Closet.read()     the bytes of one of its images, for the /character route.

The closet is somebody's folder, not a trusted pack store, so nothing in it is believed: no links anywhere (a link could
point a manifest at any file of the Pi), plain regular files of a sane size, names that cannot leave the folder. A folder
that fails any check is skipped as a whole and counted by the kind of problem; what was in it is never logged."""
import json
import os
import random
import re
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

CLOSET_DIRNAME = "closet"  # `--closet auto` is <state dir>/closet
DEFAULT_EVERY_H = 3.0  # `--closet-every`: how many hours Buddy keeps a look
MIN_EVERY_H = 0.25
MAX_EVERY_H = 168.0
MAX_COSTUMES = 64
MAX_ENTRIES = 256  # folders looked at per scan, valid or not: a closet full of junk cannot make a scan slow
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_NAME_CHARS = 40  # the Spanish name of a costume
MAX_BASE_CHARS = 64
MAX_STATES = 16
MAX_FILES = 32  # images a manifest may list, in all
MAX_FILE_NAME_CHARS = 64
IMAGE_SUFFIXES = (".gif", ".png", ".webp", ".jpg", ".jpeg")  # what the /character route serves (buddy_ui.IMAGE_TYPES)
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")  # a folder name, and so the name of the pack the screen is told about

# The kinds of problem a skipped folder is counted under (what is logged is these words and a count, nothing else).
STRAY = "suelto"        # not a folder at all
LINK = "enlace"         # a link: the folder, its manifest or one of its images
BAD_ID = "nombre"       # a folder name that is not an id
BAD_MANIFEST = "manifest"  # missing, not a plain file, too big, not an object, or states that make no sense
BAD_COSTUME = "disfraz"    # no `costume` block, or its name or base are not text we would show
BAD_FILES = "archivos"     # a listed image is missing, not a plain file, empty or too big
OVER = "de más"         # past MAX_COSTUMES or MAX_ENTRIES
UNREADABLE = "closet"   # the closet folder itself could not be read


class Costume(NamedTuple):
    id: str  # the folder name; also the pack name the page is told
    name: str  # in Spanish, what the log says ("gorro de fiesta coral")
    base: str  # the pack it was drawn over: worn only while that one is the active pack
    folder: Path  # real path, inside the closet
    files: Tuple[str, ...]  # every image the manifest lists
    stamps: Tuple[Tuple[str, int, int], ...]  # (file, size, mtime_ns) for the manifest and each image, as accepted


def stamp(path: Any, limit: int = MAX_FILE_BYTES) -> Optional[Tuple[int, int]]:
    """(size, mtime_ns) of a plain, non-empty file of at most `limit` bytes that is not a link; None for anything else."""
    try:
        info = os.lstat(path)
    except (OSError, ValueError):  # ValueError: a name the OS cannot even be asked about
        return None
    if not S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        return None
    return info.st_size, info.st_mtime_ns


def _is_link(path: Any) -> bool:
    try:
        return S_ISLNK(os.lstat(path).st_mode)
    except (OSError, ValueError):
        return False


def _text(value: Any, limit: int) -> bool:
    """Text we would put in a log line: 1 to `limit` characters, all printable (no control, bidi or zero-width
    characters), nothing padded."""
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() and value == value.strip()


def _image_name(value: Any) -> bool:
    """A file name an image may have: one plain component, not hidden, with an image suffix."""
    return (_text(value, MAX_FILE_NAME_CHARS) and "/" not in value and "\\" not in value and not value.startswith(".")
            and Path(value).suffix.lower() in IMAGE_SUFFIXES)


def _listed_files(states: Any) -> Optional[List[str]]:
    """Every image a manifest's `states` lists, once each in order; None when the states make no sense (not an object,
    none or too many of them, a value that is neither a name nor a list of names, a name that could leave the folder)."""
    if not isinstance(states, dict) or not 0 < len(states) <= MAX_STATES:
        return None
    files: List[str] = []
    for state, value in states.items():
        names = value if isinstance(value, list) else [value]
        if not isinstance(state, str) or not names:
            return None
        for name in names:
            if not _image_name(name):
                return None
            if name not in files:
                files.append(name)
    return files if len(files) <= MAX_FILES else None


def _load(root_real: str, name: str) -> Tuple[Optional[Costume], str]:
    """The costume in `<root_real>/<name>` if every check passes: (costume, ""), else (None, the kind of problem).
    `root_real` is the real path of the closet."""
    folder = os.path.join(root_real, name)
    try:
        info = os.lstat(folder)
    except (OSError, ValueError):
        return None, STRAY  # it went away between the listing and now
    if S_ISLNK(info.st_mode):
        return None, LINK
    if not S_ISDIR(info.st_mode):
        return None, STRAY
    if ID.fullmatch(name) is None:
        return None, BAD_ID
    folder_real = os.path.realpath(folder)
    if os.path.dirname(folder_real) != root_real or os.path.basename(folder_real) != name:
        return None, LINK  # something between the closet and the folder is a link
    manifest = os.path.join(folder_real, "manifest.json")
    seen = stamp(manifest, MAX_MANIFEST_BYTES)
    if seen is None:
        return None, LINK if _is_link(manifest) else BAD_MANIFEST
    try:
        with open(manifest, "rb") as handle:
            blob = handle.read(MAX_MANIFEST_BYTES + 1)
        raw = json.loads(blob.decode("utf-8")) if len(blob) <= MAX_MANIFEST_BYTES else None
    except (OSError, ValueError, RecursionError):  # ValueError: a bad UTF-8 byte or bad JSON; RecursionError: 30000 brackets deep
        return None, BAD_MANIFEST
    if not isinstance(raw, dict):
        return None, BAD_MANIFEST
    files = _listed_files(raw.get("states"))
    if files is None:
        return None, BAD_MANIFEST
    block = raw.get("costume")
    if not isinstance(block, dict) or not _text(block.get("name"), MAX_NAME_CHARS) or not _text(block.get("base"), MAX_BASE_CHARS):
        return None, BAD_COSTUME
    stamps = [("manifest.json", *seen)]
    for fname in files:
        path = os.path.join(folder_real, fname)
        got = stamp(path)
        if got is None:
            return None, LINK if _is_link(path) else BAD_FILES
        stamps.append((fname, *got))
    return Costume(name, block["name"], block["base"], Path(folder_real), tuple(files), tuple(stamps)), ""


class Closet:
    """The costumes in one folder and the choice among them. Hold one per daemon; the hub scans it when it starts and
    again each time it picks. `rng` is anything with `randrange(n)`; the default is a fresh `random.Random`."""

    def __init__(self, root: Any, rng: Optional[Any] = None) -> None:
        self.root = Path(root)
        self._rng = rng if rng is not None else random.Random()
        self.costumes: List[Costume] = []  # what the last scan() accepted, by id
        self.skipped: Dict[str, int] = {}  # what it did not, by kind of problem
        self._picked = False
        self._last = ""  # the id the last pick() chose, "" for "sin disfraz"

    @staticmethod
    def problem(root: Any) -> str:
        """Why `root` cannot be a closet, in Spanish; "" when it can (a folder this user can list)."""
        path = Path(root)
        if not path.exists():
            return f"no existe la carpeta {path}"
        if not path.is_dir():
            return f"{path} no es una carpeta"
        if not os.access(path, os.R_OK | os.X_OK):
            return f"no puedo leer la carpeta {path}"
        return ""

    def scan(self) -> List[Costume]:
        """Read the folder again: `costumes` becomes the valid ones (sorted by id, at most MAX_COSTUMES) and `skipped`
        counts the rest by kind of problem. Hidden names (.DS_Store) are not counted. Never raises."""
        found: List[Costume] = []
        skipped: Dict[str, int] = {}

        def skip(why: str) -> None:
            skipped[why] = skipped.get(why, 0) + 1

        try:
            root_real = os.path.realpath(self.root)
            with os.scandir(root_real) as listing:
                names = sorted(entry.name for entry in listing if not entry.name.startswith("."))
        except OSError:
            self.costumes, self.skipped = [], {UNREADABLE: 1}
            return []
        for position, name in enumerate(names):
            if position >= MAX_ENTRIES or len(found) >= MAX_COSTUMES:
                for _ in names[position:]:
                    skip(OVER)
                break
            try:
                costume, why = _load(root_real, name)
            except Exception:  # whatever a hostile folder provokes, scan() promises not to raise
                costume, why = None, BAD_MANIFEST
            if costume is None:
                skip(why)
            else:
                found.append(costume)
        self.costumes, self.skipped = found, skipped
        return list(found)

    def skipped_text(self) -> str:
        """What the last scan() left out, for one log line: counts and kinds of problem only; "" when nothing."""
        if UNREADABLE in self.skipped:
            return "no pude leer la carpeta del closet"
        total = sum(self.skipped.values())
        if not total:
            return ""
        kinds = ", ".join(f"{why} {count}" for why, count in sorted(self.skipped.items()))
        return "descarté %d %s (%s)" % (total, "carpeta" if total == 1 else "carpetas", kinds)

    def pick(self) -> Optional[Costume]:
        """What Buddy wears next, from the costumes of the last scan(): one of them or None for "sin disfraz", all
        equally likely, except that it is never what the previous pick() chose while there is anything else to choose."""
        options: List[Optional[Costume]] = [None, *self.costumes]
        if self._picked:
            options = [o for o in options if (o.id if o is not None else "") != self._last] or [None]
        choice = options[self._rng.randrange(len(options))]
        self._picked, self._last = True, choice.id if choice is not None else ""
        return choice

    def recheck(self, costume: Costume) -> Optional[Costume]:
        """`costume` as it is on disk now: the same one when no file moved since scan() accepted it, a fresh look when
        one did (a GIF copied over it, a new manifest), None when it is not sound any more (anything missing, a link, too
        big, the closet gone). Never raises."""
        try:
            if self._unchanged(costume):
                return costume
            fresh, _ = _load(os.path.realpath(self.root), costume.id)
            return fresh
        except OSError:
            return None

    @staticmethod
    def _unchanged(costume: Costume) -> bool:
        try:
            if not S_ISDIR(os.lstat(costume.folder).st_mode):
                return False
        except (OSError, ValueError):
            return False
        for fname, size, mtime in costume.stamps:
            limit = MAX_MANIFEST_BYTES if fname == "manifest.json" else MAX_FILE_BYTES
            if stamp(os.path.join(costume.folder, fname), limit) != (size, mtime):
                return False
        return True

    @staticmethod
    def read(costume: Costume, fname: str) -> Optional[bytes]:
        """The bytes of one image the manifest of `costume` lists, None for any other name or a file that is no longer a
        plain file of a sane size. Opened without following a link, so a swap after recheck() cannot lead elsewhere."""
        if fname not in costume.files:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(os.path.join(costume.folder, fname), flags)
        except (OSError, ValueError):
            return None
        try:
            handle = os.fdopen(fd, "rb")
        except OSError:
            os.close(fd)
            return None
        try:
            with handle:
                info = os.fstat(handle.fileno())
                if not S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    return None
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            return None
        return data if 0 < len(data) <= MAX_FILE_BYTES else None

