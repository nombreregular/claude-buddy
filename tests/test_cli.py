"""End to end: the real `buddy.py` process, a real pty for the keyboard, and the fake bluetoothd
on a private D-Bus playing the Mac."""
import asyncio
import json
import os
import re
import shutil
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_bluez import DBusDaemon, FakeBlueZ, FakeCentral  # noqa: E402
from ui_client import Stream, http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


@pytest.fixture
def dbus():
    daemon = DBusDaemon()
    yield daemon
    daemon.close()


class Proc:
    """A buddy.py child process whose output we can wait on."""

    def __init__(self, proc, master=None):
        self.proc = proc
        self.master = master
        self.lines = []
        self._reader = asyncio.ensure_future(self._read())

    @classmethod
    async def start(cls, dbus, tmp_path, *args, keyboard=True):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "HOME": str(tmp_path / "home")}
        master = slave = None
        if keyboard:
            master, slave = os.openpty()
        cmd = [sys.executable, str(ROOT / "buddy.py"), "--bus", dbus.address, "--state-dir", str(tmp_path / "state"),
               "--ui-port", "0", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=slave if keyboard else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        if slave is not None:
            os.close(slave)
        return cls(proc, master)

    async def _read(self):
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            self.lines.append(line.decode("utf-8", "replace").rstrip("\n"))

    @property
    def text(self):
        return "\n".join(self.lines)

    async def screen_port(self):
        """The port the daemon says its screen listens on."""
        await self.expect("Pantalla en http://127.0.0.1:")
        return int(re.search(r"Pantalla en http://127\.0\.0\.1:(\d+)", self.text).group(1))

    async def expect(self, fragment, timeout=5.0, times=1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.text.count(fragment) >= times:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"never saw {fragment!r} x{times}; output was:\n{self.text}")

    def press(self, keys):
        assert self.master is not None
        os.write(self.master, keys.encode())

    async def finish(self, timeout=6.0):
        code = await asyncio.wait_for(self.proc.wait(), timeout)
        await asyncio.wait_for(self._reader, 2)
        if self.master is not None:
            os.close(self.master)
            self.master = None
        return code

    async def kill(self):
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self._reader.cancel()
        if self.master is not None:
            os.close(self.master)
            self.master = None


def heartbeat(**kw):
    base = {"total": 1, "running": 1, "waiting": 0, "msg": "working", "entries": [], "tokens": 0, "tokens_today": 0}
    base.update(kw)
    return base


def run(coro_fn):
    asyncio.run(coro_fn())


def test_daemon_pairs_shows_the_code_and_answers_prompts_from_the_keyboard(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path)
        try:
            await buddy.expect("Bluetooth listo. Anunciando como Claude-3456")
            await buddy.expect("a aprobar | d denegar")

            mac = FakeCentral(fake)
            await mac.connect()
            await mac.pair(123456)
            await buddy.expect("CÓDIGO DE EMPAREJAMIENTO:  123 456")
            await buddy.expect("Emparejado con Ana's MacBook Pro")
            await mac.start_notify()

            await mac.send(heartbeat(waiting=1, prompt={"id": "req_1", "tool": "Bash", "hint": "rm -rf /tmp/x"}))
            await buddy.expect("Claude desktop conectado")
            await buddy.expect("¿APROBAR?  Bash: rm -rf /tmp/x")
            buddy.press("a")
            assert await mac.next() == {"cmd": "permission", "id": "req_1", "decision": "once"}
            await buddy.expect("APROBADO en")

            await mac.send(heartbeat(waiting=1, prompt={"id": "req_2", "tool": "Edit", "hint": "main.py"}))
            await buddy.expect("¿APROBAR?  Edit: main.py")
            buddy.press("d")
            assert await mac.next() == {"cmd": "permission", "id": "req_2", "decision": "deny"}
            await buddy.expect("DENEGADO en")

            buddy.press("a")  # nothing is pending any more
            await buddy.expect("No hay nada pendiente")

            buddy.press("s")
            await buddy.expect("aprobadas: 1 | denegadas: 1")
            await buddy.expect("cifrado: sí")

            buddy.press("q")
            assert await buddy.finish() == 0
            # a clean shutdown leaves BlueZ as it found it
            assert fake.alias == "" and fake.adv is None and fake.app is None and fake.agent is None
            state = json.loads((tmp_path / "state" / "state.json").read_text())
            assert state["approvals"] == 1 and state["denials"] == 1
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_arrow_keys_and_pastes_never_answer_a_prompt(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path)
        try:
            await buddy.expect("Bluetooth listo")
            mac = await _paired_mac(fake, buddy)
            await mac.send(heartbeat(waiting=1, prompt={"id": "req_1", "tool": "Bash", "hint": "ls"}))
            await buddy.expect("¿APROBAR?  Bash: ls")
            for junk in (b"\x1b[A", b"\x1b[D", b"\x1bOP", b"add", b"\x1b"):  # up, left, F1, a paste, lone Escape
                buddy.press(junk.decode())
                await asyncio.sleep(0.15)
            await mac.expect_silence(0.3)
            assert "APROBADO" not in buddy.text and "DENEGADO" not in buddy.text
            buddy.press("d")  # a real keypress still works
            assert await mac.next() == {"cmd": "permission", "id": "req_1", "decision": "deny"}
            buddy.press("q")
            assert await buddy.finish() == 0
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_the_u_key_forgets_the_buddy_pairings_after_confirmation(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path)
        try:
            await buddy.expect("Bluetooth listo")
            keyboard = fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")
            mac = await _paired_mac(fake, buddy)
            await mac.send(heartbeat())
            await buddy.expect("Claude desktop conectado")

            buddy.press("u")
            await buddy.expect("¿Borrar los emparejamientos hechos con el buddy?")
            buddy.press("n")
            await buddy.expect("Cancelado")
            assert fake.removed == []

            buddy.press("u")
            await buddy.expect("¿Borrar los emparejamientos hechos con el buddy?", times=2)
            buddy.press("y")
            await buddy.expect("Emparejamientos del buddy borrados: 1")
            assert fake.removed == [mac.path] and keyboard.path not in fake.removed

            buddy.press("q")
            assert await buddy.finish() == 0
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


async def _paired_mac(fake, buddy):
    mac = FakeCentral(fake)
    await mac.connect()
    await mac.pair(123456)
    await mac.start_notify()
    return mac


def test_sigterm_shuts_down_cleanly_and_without_a_keyboard_says_so(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, keyboard=False)
        try:
            await buddy.expect("Bluetooth listo")
            await buddy.expect("Sin teclado")
            mac = await _paired_mac(fake, buddy)
            await mac.send(heartbeat(waiting=1, prompt={"id": "req_1", "tool": "Bash", "hint": "ls"}))
            await buddy.expect("¿APROBAR?  Bash: ls")
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            assert "Cerrando" in buddy.text
            assert fake.alias == "" and fake.adv is None and fake.app is None and fake.agent is None
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_log_file_captures_the_bluez_conversation(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        log_file = tmp_path / "buddy.log"
        buddy = await Proc.start(dbus, tmp_path, "-vv", "--log-file", str(log_file), keyboard=False)
        try:
            await buddy.expect("Bluetooth listo")
            mac = await _paired_mac(fake, buddy)
            await mac.send({"cmd": "status"})
            await mac.next()
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            text = log_file.read_text()
            assert "Bluetooth ready: Claude-3456" in text
            assert "RX " in text and "TX notify" in text
            assert "Bluetooth listo" in buddy.text and "TX notify" not in buddy.text  # the screen stays clean
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_insecure_flag_is_announced(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, "--insecure", keyboard=False)
        try:
            await buddy.expect("INSEGURO")
            assert fake.agent is None  # no pairing agent in insecure mode
            buddy.proc.send_signal(signal.SIGINT)
            assert await buddy.finish() == 0
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_waits_for_bluetoothd_and_reports_what_is_missing(dbus, tmp_path):
    async def scenario():
        buddy = await Proc.start(dbus, tmp_path, keyboard=False)  # no fake bluetoothd yet
        try:
            await buddy.expect("Bluetooth no está listo: bluetoothd no está corriendo")
            fake = FakeBlueZ(dbus.address)
            await fake.start()
            try:
                await buddy.expect("Bluetooth listo", timeout=12)
            finally:
                buddy.proc.send_signal(signal.SIGTERM)
                assert await buddy.finish() == 0
                fake.stop()
        finally:
            await buddy.kill()

    run(scenario)


def test_check_command_tries_the_registration(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, "check", keyboard=False)
        try:
            assert await buddy.finish() == 0
            assert "[ok]    D-Bus: bluetoothd responde" in buddy.text
            assert "BlueZ aceptó todo. Anuncio como Claude-3456" in buddy.text
            assert fake.adv is None and fake.app is None  # the trial cleaned up after itself
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_check_command_fails_loudly_without_bluetoothd(dbus, tmp_path):
    async def scenario():
        buddy = await Proc.start(dbus, tmp_path, "check", keyboard=False)
        try:
            assert await buddy.finish() == 1
            assert "[ERROR] bluetoothd no está corriendo" in buddy.text
        finally:
            await buddy.kill()

    run(scenario)


def test_forget_all_command(dbus, tmp_path):
    async def scenario():
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "peers.json").write_text(json.dumps(["11:22:33:44:55:66"]))
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        try:
            mine = fake.add_bonded_device("11:22:33:44:55:66")
            fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")
            await asyncio.sleep(0.1)
            buddy = await Proc.start(dbus, tmp_path, "forget-all", keyboard=False)
            assert await buddy.finish() == 0
            assert "Emparejamientos del buddy borrados: 1" in buddy.text
            assert "No toqué 1 dispositivo(s)" in buddy.text
            assert fake.removed == [mine.path]
        finally:
            fake.stop()

    run(scenario)


def test_the_screen_shows_the_pairing_code_and_answers_prompts(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, keyboard=False)
        try:
            await buddy.expect("Bluetooth listo")
            await buddy.expect("Sin teclado: los pedidos de aprobación se responden en la pantalla táctil")
            port = await buddy.screen_port()

            status, _, page = await http(port, path="/")
            assert status == 200
            token = re.search(rb'name="buddy-token" content="([^"]+)"', page).group(1).decode()
            stream = await Stream.open(port)
            try:
                v = await stream.state()
                assert v["ble"] == {"ready": True, "name": "Claude-3456", "reason": "", "secure": True}
                assert v["linked"] is False and v["pairing"] is None and v["mood"] == "sleep"

                # the Mac asks to pair: the code appears on the screen, digit by digit
                mac = FakeCentral(fake)
                await mac.connect()
                agent = fake.agent
                show = lambda entered: fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", "DisplayPasskey",  # noqa: E731
                                                 "ouq", [mac.path, 123456, entered])
                await show(0)
                v = await stream.state(lambda v: v["pairing"])
                assert v["pairing"] == {"code": "123456", "entered": 0}
                await show(3)
                v = await stream.state(lambda v: v["pairing"] and v["pairing"]["entered"] == 3)
                mac.device.set_paired(True)
                v = await stream.state(lambda v: v["pairing"] is None)  # paired: the code goes away
                await mac.start_notify()

                # a permission prompt arrives
                await mac.send(heartbeat(total=2, running=1, waiting=1, msg="approve: Bash",
                                         prompt={"id": "req_1", "tool": "Bash", "hint": "rm -rf /tmp/x"}))
                v = await stream.state(lambda v: v["approvals"])
                assert v["linked"] is True and v["mood"] == "attention"
                assert v["sessions"]["waiting"] == 1
                approval = v["approvals"][0]
                assert approval == {"key": "claude:req_1", "tool": "Bash", "hint": "rm -rf /tmp/x", "source": "claude",
                                    "always": ""}

                # tapping the button is what answers it
                headers = {"Content-Type": "application/json", "X-Buddy-Token": token}
                status, _, _ = await http(port, "POST", "/decide", headers=headers,
                                          body=json.dumps({"key": approval["key"], "decision": "once"}).encode())
                assert status == 200
                assert await mac.next() == {"cmd": "permission", "id": "req_1", "decision": "once"}
                await buddy.expect("APROBADO en")
                v = await stream.state(lambda v: not v["approvals"])
                assert v["pet"]["approvals"] == 1

                # and a second one, denied
                await mac.send(heartbeat(waiting=1, prompt={"id": "req_2", "tool": "Edit", "hint": "main.py"}))
                v = await stream.state(lambda v: v["approvals"])
                status, _, _ = await http(port, "POST", "/decide", headers=headers,
                                          body=json.dumps({"key": v["approvals"][0]["key"], "decision": "deny"}).encode())
                assert status == 200
                assert await mac.next() == {"cmd": "permission", "id": "req_2", "decision": "deny"}
                v = await stream.state(lambda v: v["pet"]["denials"] == 1)

                # if the Mac goes away the screen says so
                mac.disconnect()
                v = await stream.state(lambda v: not v["linked"])
                assert v["approvals"] == []  # (the mood may still be a happy one-shot from the quick approval)
            finally:
                stream.close()
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            # the screen server is gone with the daemon
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", port)
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_screen_and_keyboard_answer_the_same_queue(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path)
        try:
            await buddy.expect("Bluetooth listo")
            port = await buddy.screen_port()
            mac = await _paired_mac(fake, buddy)
            stream = await Stream.open(port)
            try:
                await mac.send(heartbeat(waiting=1, prompt={"id": "req_1", "tool": "Bash", "hint": "ls"}))
                await stream.state(lambda v: v["approvals"])
                buddy.press("a")  # answered on the keyboard...
                assert await mac.next() == {"cmd": "permission", "id": "req_1", "decision": "once"}
                v = await stream.state(lambda v: not v["approvals"])  # ...and the screen drops it by itself
                assert v["pet"]["approvals"] == 1
                buddy.press("q")
                assert await buddy.finish() == 0
            finally:
                stream.close()
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_no_ui_runs_without_a_screen(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, "--no-ui", keyboard=False)
        try:
            await buddy.expect("Bluetooth listo")
            await buddy.expect("Sin teclado ni pantalla")
            assert "Pantalla en" not in buddy.text
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_a_busy_screen_port_does_not_stop_the_buddy(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        buddy = await Proc.start(dbus, tmp_path, "--ui-port", str(port), keyboard=False)
        try:
            await buddy.expect(f"No pude abrir la pantalla en el puerto {port}")
            await buddy.expect("Bluetooth listo")
            await buddy.expect("Sin teclado ni pantalla")
            mac = await _paired_mac(fake, buddy)  # the radio side works as usual
            await mac.send({"cmd": "status"})
            assert (await mac.next())["ack"] == "status"
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
        finally:
            taken.close()
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_the_screen_is_up_before_bluetooth_is_and_says_so(dbus, tmp_path):
    async def scenario():
        buddy = await Proc.start(dbus, tmp_path, keyboard=False)  # no bluetoothd yet
        try:
            port = await buddy.screen_port()
            stream = await Stream.open(port)
            try:
                v = await stream.state(lambda v: not v["ble"]["ready"] and "bluetoothd" in v["ble"]["reason"])
                assert v["linked"] is False and v["approvals"] == []
                fake = FakeBlueZ(dbus.address)
                await fake.start()
                try:
                    v = await stream.state(lambda v: v["ble"]["ready"], timeout=12)
                    assert v["ble"]["name"] == "Claude-3456"
                finally:
                    buddy.proc.send_signal(signal.SIGTERM)
                    assert await buddy.finish() == 0
                    fake.stop()
            finally:
                stream.close()
        finally:
            await buddy.kill()

    run(scenario)


# -- night mode: the real process, a panel that is a temporary directory laid out like sysfs --


def _panel(tmp_path):
    panel = tmp_path / "panel"
    panel.mkdir()
    (panel / "max_brightness").write_text("255\n")
    (panel / "brightness").write_text("255\n")
    return panel


def _brightness(panel):
    return int((panel / "brightness").read_text())


async def _until(predicate, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("it never happened")


def test_night_mode_dims_the_panel_at_start_and_sigterm_puts_it_back(dbus, tmp_path):
    panel = _panel(tmp_path)

    async def scenario():
        buddy = await Proc.start(dbus, tmp_path, "--night", "00:00-00:00", "--backlight", str(panel), keyboard=False)
        try:
            await buddy.expect("Modo noche 00:00-00:00: la luz baja a 12 de 255")
            await _until(lambda: _brightness(panel) == 12)  # always night: dim as soon as the screen is up
            await buddy.expect("noche: pantalla tenue")  # the service runs without -v: this has to be in what it prints
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            assert _brightness(panel) == 255  # the screen does not stay dim when the daemon is gone
        finally:
            await buddy.kill()

    run(scenario)


def test_a_tap_on_the_dark_screen_lights_the_panel_through_post_wake(dbus, tmp_path):
    panel = _panel(tmp_path)

    async def scenario():
        buddy = await Proc.start(dbus, tmp_path, "--night", "00:00-00:00", "--backlight", str(panel), keyboard=False)
        try:
            port = await buddy.screen_port()
            await _until(lambda: _brightness(panel) == 12)
            _, _, page = await http(port, path="/")
            token = re.search(rb'name="buddy-token" content="([^"]+)"', page).group(1).decode()
            stream = await Stream.open(port)
            try:
                v = await stream.state()
                assert v["dim"] is True and v["backlight"] is True
                status, _, _ = await http(port, "POST", "/wake", headers={"X-Buddy-Token": "wrong"})
                assert status == 403 and _brightness(panel) == 12
                status, _, body = await http(port, "POST", "/wake", headers={"X-Buddy-Token": token})
                assert status == 200 and json.loads(body) == {"ok": True, "dim": False}
                v = await stream.state(lambda v: v["dim"] is False)
                assert v["backlight"] is True
                await _until(lambda: _brightness(panel) == 255)
                await buddy.expect("noche: despierta (toque)")
            finally:
                stream.close()
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            assert _brightness(panel) == 255
        finally:
            await buddy.kill()

    run(scenario)


def test_without_night_mode_the_daemon_leaves_the_panel_alone(dbus, tmp_path):
    panel = _panel(tmp_path)
    (panel / "brightness").write_text("77\n")

    async def scenario():
        buddy = await Proc.start(dbus, tmp_path, "--night", "off", "--backlight", str(panel), keyboard=False)
        try:
            port = await buddy.screen_port()
            stream = await Stream.open(port)
            try:
                v = await stream.state()
                assert v["dim"] is False and v["backlight"] is False
            finally:
                stream.close()
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            assert _brightness(panel) == 77
        finally:
            await buddy.kill()

    run(scenario)


# -- the closet of costumes: the real process (the flags and the lines are also tested in test_closet.py, in this process) --


def _closet(tmp_path, *ids):
    """A closet folder with one fake costume per id: a manifest and a fake image each."""
    root = tmp_path / "closet"
    root.mkdir()
    for cid in ids:
        folder = root / cid
        folder.mkdir()
        manifest = {"name": cid, "states": {"idle": "idle_0.gif"}, "costume": {"name": f"gorro {cid}", "base": "buddy"}}
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "idle_0.gif").write_bytes(b"GIF89a" + cid.encode())
    return root


def test_the_closet_is_off_unless_asked_for(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, keyboard=False)
        try:
            await buddy.expect("Bluetooth listo")
            assert "Closet" not in buddy.text and "disfraz" not in buddy.text
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_a_closet_that_is_asked_for_is_read_and_the_screen_keeps_its_own_buddy_without_a_pack(dbus, tmp_path):
    root = _closet(tmp_path, "a-coral", "b-menta")

    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, "--closet", str(root), "--closet-every", "0.25", keyboard=False)
        try:
            await buddy.expect("Closet: 2 disfraces, cambia cada 0.25 h")
            await buddy.expect("Bluetooth listo")
            stream = await Stream.open(await buddy.screen_port())
            try:
                assert (await stream.state())["character"] is None  # no pack is active, so no costume has anything to go over
            finally:
                stream.close()
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            assert "Traceback" not in buddy.text
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_a_closet_that_is_not_there_is_one_warning_and_the_buddy_carries_on(dbus, tmp_path):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, "--closet", str(tmp_path / "nope"), keyboard=False)
        try:
            await buddy.expect("Closet apagado: no existe la carpeta")
            await buddy.expect("Bluetooth listo")
            assert buddy.text.count("Closet apagado") == 1 and "Closet:" not in buddy.text
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
        finally:
            await buddy.kill()
            fake.stop()

    run(scenario)


def test_a_bad_closet_flag_stops_with_exit_code_2(dbus, tmp_path):
    async def scenario():
        buddy = await Proc.start(dbus, tmp_path, "--closet-every", "0.1", keyboard=False)
        try:
            assert await buddy.finish() == 2
            assert "usá un número de horas entre 0.25 y 168" in buddy.text
        finally:
            await buddy.kill()

    run(scenario)
