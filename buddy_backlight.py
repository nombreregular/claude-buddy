"""The panel's own backlight, so night mode dims the screen for real.

A black overlay on an LCD still burns the backlight; the Pi's 7" DSI panel has a real one in sysfs:
/sys/class/backlight/<name>/{brightness,max_brightness} (0..max, group `video` can write it). The
daemon owns two levels and nothing in between:

    day     max_brightness. Never "what it was when we started": a crash while dim must not make the
            next start treat the dim level as the normal one.
    night   `--night-level` (default 12), kept between FLOOR and max_brightness so the screen can
            never go black and leave a permission request unreadable.

Pure standard library. Nothing here raises into the daemon: the first OSError turns the backlight off
(one log line) and the page falls back to darkening itself. One thing it does not give up on: if the write that
failed was the one back to the day level, the panel may be stuck dim, so that write is tried again (see `_repair`).
"""
import logging
import os
import time
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("buddy.backlight")

SYSFS = Path("/sys/class/backlight")
FLOOR = 4  # the dimmest the screen is ever allowed to get
DEFAULT_NIGHT_LEVEL = 12
RETRY_S = 1.0  # a write back to the day level that failed is tried again this often (the daemon refreshes every second)...
REPAIR_TRIES = 60  # ...this many times (about a minute), and then the panel is left as it is


class Backlight:
    """One panel backlight. `set_dim` moves it between the two levels and writes only when the level changes
    (no polling loop); `restore` puts it back to day for good when the daemon stops."""

    def __init__(self, directory: Path, max_level: int, night_level: int = DEFAULT_NIGHT_LEVEL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.directory = Path(directory)
        self.day = max_level
        self.night = max(1, min(max(night_level, FLOOR), max_level))  # [FLOOR, max]; a panel under FLOOR just gets its max
        self.enabled = True  # False after the first failed write
        self._released = False  # restore() was called: nothing may dim the screen again
        self._level: Optional[int] = None  # what was last written (None: nothing yet, so the first call always writes)
        self._clock = clock
        self._owed = False  # the write back to day failed: the panel may be stuck at the night level (see _repair)
        self._tries = 0  # repairs attempted since then
        self._next_try = 0.0  # on self._clock

    @property
    def active(self) -> bool:
        """True while the daemon controls the panel: the page then leaves the darkening to it."""
        return self.enabled and not self._released

    # -- finding the panel ------------------------------------------------------------

    @classmethod
    def find(cls, spec: str = "auto", night_level: int = DEFAULT_NIGHT_LEVEL, root: Path = SYSFS) -> Optional["Backlight"]:
        """The backlight `spec` names: "off" (None), "auto" (the first one under `root` whose brightness this user can
        write) or a directory (a trusted path from the command line, the same layout). None when there is none."""
        if spec == "off":
            return None
        if spec == "auto":
            try:
                candidates: List[Path] = sorted(p for p in Path(root).iterdir() if p.is_dir())
            except OSError:
                log.info("backlight: no hay %s, el modo noche oscurece la página", root)
                return None
        else:
            try:
                candidates = [Path(spec).expanduser()]
            except RuntimeError:  # "~" with no home to expand
                candidates = []
        for directory in candidates:
            found = cls._open(directory, night_level)
            if found is not None:
                log.info("backlight: %s, día %d, noche %d", found.directory, found.day, found.night)
                return found
        if spec == "auto":
            log.info("backlight: ninguna luz de %s se puede escribir y bajar, el modo noche oscurece la página", root)
        else:
            log.warning("backlight: no puedo usar %s (falta max_brightness, brightness no se puede escribir o no tiene a dónde bajar)", spec)
        return None

    @classmethod
    def _open(cls, directory: Path, night_level: int) -> Optional["Backlight"]:
        try:
            top = int((directory / "max_brightness").read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None
        if top < 1 or not os.access(directory / "brightness", os.W_OK):
            return None
        found = cls(directory, top, night_level)
        if found.night >= found.day:  # an on/off backlight, a tiny range, a night level at the maximum: nothing to dim to
            return None
        return found

    # -- moving it ----------------------------------------------------------------------

    def set_dim(self, dim: bool) -> bool:
        """Night level when `dim`, day level otherwise. Writes only if that is not where it already is. False once
        the backlight is off (a write failed, or restore() ran): the caller then knows it is not dimming anything."""
        if not self.active:
            if self._owed and not self._released:
                self._repair()  # off for dimming, but not done with putting the light back
            return False
        level = self.night if dim else self.day
        if level != self._level:
            self._write(level)
        return self.active

    def restore(self) -> None:
        """Back to the day level, for good: the screen must not stay dim once the daemon is gone. Safe to call twice
        and when nothing was ever dimmed. Tries once even after a failed write (the last level that did go through
        may have been the night one)."""
        if self._released:
            return
        self._released = True
        if self._owed or (self._level is not None and self._level != self.day):
            self._write(self.day)

    def _repair(self) -> None:
        """The write that failed was the one back to day (a tap, a permission request, the morning), so the panel is
        probably still at the night level and the page, told the backlight is gone, shows an awake screen on a dark
        one. Try that one write again, at most once per RETRY_S and REPAIR_TRIES times, and nothing else: the
        backlight stays off for dimming (any failure turns it off), and the log stays quiet unless it gives up."""
        now = self._clock()
        if now < self._next_try:
            return
        self._tries += 1
        if self._write(self.day):
            log.info("backlight: la luz volvió al día")
        elif self._tries >= REPAIR_TRIES:
            self._owed = False
            log.warning("backlight: sigo sin poder devolver la luz al día en %s, dejo de intentar", self.directory / "brightness")

    def _write(self, level: int) -> bool:
        path = self.directory / "brightness"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC)  # no O_CREAT: sysfs files exist or the panel is gone
            try:
                os.write(fd, b"%d\n" % level)
            finally:
                os.close(fd)
        except OSError as exc:
            if self.enabled:  # one line, however many times it is asked again
                log.warning("backlight: no puedo escribir en %s (%s); la página se oscurece sola", path, exc.strerror or exc)
            self.enabled = False
            if level == self.day:  # the panel may be stuck at the night level
                self._owed = True
                self._next_try = self._clock() + RETRY_S
            return False
        self._level = level
        self._owed = False
        self._tries = 0
        return True
