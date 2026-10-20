"""kiosk.sh: finds Chromium and the Pi's screen, waits for the daemon, opens the right URL."""
import asyncio
import os
import shutil
import socket
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buddy_ui import UIHub, UIServer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "kiosk.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_CHROMIUM = """#!/bin/bash
{
  echo "ARGS:$*"
  echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
  echo "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}"
} > "$FAKE_CHROMIUM_LOG"
"""


def run(coro_fn):
    return asyncio.run(coro_fn())


def tools_dir(tmp_path, *, chromium=True, name="chromium"):
    """A PATH with only what the script needs, so the machine's own Chromium never leaks in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("python3", "id", "basename"):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    if chromium:
        fake = bin_dir / name
        fake.write_text(FAKE_CHROMIUM)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def environment(tmp_path, bin_dir, **extra):
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path / "home"), "FAKE_CHROMIUM_LOG": str(tmp_path / "chromium.log"),
           "WAYLAND_DISPLAY": "wayland-0", "BUDDY_WAIT": "5"}
    env.update(extra)
    return env


async def kiosk(tmp_path, *args, **kw):
    env = environment(tmp_path, kw.pop("bin_dir", None) or tools_dir(tmp_path), **kw.pop("env", {}))
    for gone in kw.pop("unset", ()):
        env.pop(gone, None)
    proc = await asyncio.create_subprocess_exec(
        shutil.which("bash"), str(SCRIPT), *args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    return proc.returncode, out.decode()


def logged(tmp_path):
    return dict(line.split(":", 1) if line.startswith("ARGS:") else line.split("=", 1)
                for line in (tmp_path / "chromium.log").read_text().splitlines())


async def with_daemon(scenario):
    server = UIServer(UIHub(), port=0)
    await server.start()
    try:
        return await scenario(server)
    finally:
        await server.stop()


def test_it_opens_chromium_full_screen_on_the_buddy_page(tmp_path):
    async def scenario(server):
        code, out = await kiosk(tmp_path, env={"BUDDY_PORT": str(server.port)})
        assert code == 0, out
        args = logged(tmp_path)["ARGS"].split()
        assert "--kiosk" in args
        assert args[-1] == f"http://127.0.0.1:{server.port}/?kiosk=1"  # the page knows it is a kiosk: hides the cursor
        assert f"--user-data-dir={tmp_path / 'home' / '.cache' / 'claude-buddy-kiosk'}" in args  # its own profile
        assert "--noerrdialogs" in args and "--disable-session-crashed-bubble" in args and "--password-store=basic" in args
        assert "--ozone-platform=wayland" in args  # from SSH Chromium would otherwise look for an X server

    run(lambda: with_daemon(scenario))


def test_window_mode_is_a_normal_window_on_the_plain_page(tmp_path):
    async def scenario(server):
        code, out = await kiosk(tmp_path, "--window", env={"BUDDY_PORT": str(server.port)})
        assert code == 0, out
        args = logged(tmp_path)["ARGS"].split()
        assert "--kiosk" not in args
        assert args[-1] == f"http://127.0.0.1:{server.port}/"

    run(lambda: with_daemon(scenario))


def test_chromium_browser_works_too(tmp_path):
    async def scenario(server):
        code, out = await kiosk(tmp_path, bin_dir=tools_dir(tmp_path, name="chromium-browser"),
                                env={"BUDDY_PORT": str(server.port)})
        assert code == 0, out
        assert "--kiosk" in logged(tmp_path)["ARGS"]

    run(lambda: with_daemon(scenario))


def test_it_finds_the_pis_screen_when_started_from_ssh(tmp_path):
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "wayland-0.lock").write_text("")  # a lock file is not a socket
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(runtime / "wayland-0"))
    sock.listen()

    async def scenario(server):
        code, out = await kiosk(tmp_path, env={"BUDDY_PORT": str(server.port), "XDG_RUNTIME_DIR": str(runtime)},
                                unset=["WAYLAND_DISPLAY"])
        assert code == 0, out
        seen = logged(tmp_path)
        assert seen["WAYLAND_DISPLAY"] == "wayland-0" and seen["XDG_RUNTIME_DIR"] == str(runtime)
        assert "--ozone-platform=wayland" in seen["ARGS"]

    try:
        run(lambda: with_daemon(scenario))
    finally:
        sock.close()


@pytest.mark.skipif(os.path.exists("/tmp/.X11-unix/X0"), reason="this machine has an X11 display")
def test_it_says_so_when_there_is_no_screen(tmp_path):
    runtime = tmp_path / "run"
    runtime.mkdir()

    async def scenario(server):
        code, out = await kiosk(tmp_path, env={"BUDDY_PORT": str(server.port), "XDG_RUNTIME_DIR": str(runtime)},
                                unset=["WAYLAND_DISPLAY", "DISPLAY"])
        assert code == 1 and "no encuentro la pantalla del Pi" in out
        assert not (tmp_path / "chromium.log").exists()

    run(lambda: with_daemon(scenario))


def test_it_says_so_when_chromium_is_missing(tmp_path):
    code, out = run(lambda: kiosk(tmp_path, bin_dir=tools_dir(tmp_path, chromium=False)))
    assert code == 1 and "no encuentro Chromium" in out and "apt install chromium" in out


def test_it_waits_for_a_daemon_that_starts_late(tmp_path):
    async def scenario():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        task = asyncio.ensure_future(kiosk(tmp_path, env={"BUDDY_PORT": str(port), "BUDDY_WAIT": "15"}))
        await asyncio.sleep(1.5)
        assert not (tmp_path / "chromium.log").exists()  # still waiting: no error page for Chromium to show
        server = UIServer(UIHub(), port=port)
        await server.start()
        try:
            code, out = await task
            assert code == 0, out
            assert (tmp_path / "chromium.log").exists()
        finally:
            await server.stop()

    run(scenario)


def test_it_gives_up_when_the_daemon_never_answers(tmp_path):
    async def scenario():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        code, out = await kiosk(tmp_path, env={"BUDDY_PORT": str(port), "BUDDY_WAIT": "2"})
        assert code == 1 and "el daemon no contesta" in out
        assert not (tmp_path / "chromium.log").exists()

    run(scenario)


@pytest.mark.skipif(os.path.exists("/tmp/.X11-unix/X0"), reason="this machine has an X11 display")
def test_an_x11_session_gets_the_x11_platform(tmp_path):
    async def scenario(server):
        code, out = await kiosk(tmp_path, env={"BUDDY_PORT": str(server.port), "DISPLAY": ":0"}, unset=["WAYLAND_DISPLAY"])
        assert code == 0, out
        assert "--ozone-platform=x11" in logged(tmp_path)["ARGS"]

    run(lambda: with_daemon(scenario))
