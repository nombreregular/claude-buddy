"""Run the screen's page in a headless Chromium and read the DOM back (shared by the page tests).

A plain helper module like ui_client.py. The browser is found on the Pi (chromium on PATH), on the Mac
(Google Chrome in /Applications) and in the CI image (/opt/pw-browsers), or through CLAUDE_BUDDY_CHROMIUM
when someone wants a specific binary."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def chromium():
    """Path of a Chromium to run the page in, or None when there is none."""
    chosen = os.environ.get("CLAUDE_BUDDY_CHROMIUM")
    if chosen and os.path.isfile(chosen) and os.access(chosen, os.X_OK):
        return chosen
    for name in ("chromium", "chromium-browser", "chrome", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    if os.path.isfile(MAC_CHROME) and os.access(MAC_CHROME, os.X_OK):
        return MAC_CHROME
    for base in sorted(Path("/opt/pw-browsers").glob("chromium_headless_shell-*/chrome-linux/headless_shell")):
        return str(base)
    return None


needs_chromium = pytest.mark.skipif(chromium() is None, reason="no Chromium to run the page in")


def dump(url, budget=1500, timeout=40):
    """Run the page in a headless Chromium and return the DOM after its script ran.

    budget is the virtual time (ms) the page gets to run: timers and animations fast-forward, so a test
    can see a 25 s auto close in well under a second of real time. timeout is the real time limit (s)."""
    # --password-store=basic: on a Pi with a desktop session, Chromium would otherwise ask GNOME keyring to unlock
    # (a dialog on the Pi's screen) and hang here until the timeout.
    cmd = [chromium(), "--headless", "--no-sandbox", "--disable-gpu", "--password-store=basic", "--dump-dom",
           f"--virtual-time-budget={int(budget)}", url]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    assert done.returncode == 0, done.stderr[-500:]
    return done.stdout


def attr(dom, name):
    """The value of attribute name on <body> in a dumped DOM."""
    found = re.search(rf'<body[^>]*\b{name}="([^"]*)"', dom)
    assert found, f"no {name} on <body> in:\n{dom[:400]}"
    return found.group(1)
