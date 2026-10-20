"""Integration tests: the real daemon code (core + BlueZ transport) against a fake
bluetoothd on a private D-Bus. Needs `dbus-daemon` installed."""
import asyncio
import base64
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import pytest
from dbus_fast import DBusError, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.service import method

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy_bluez  # noqa: E402
from buddy_bluez import (  # noqa: E402
    ADV_PATH, AGENT_PATH, RX_PATH, SERVICE_PATH, TX_PATH, BlueZTransport, PeerBook, TransportError,
    diagnose, forget_all_devices,
)
from buddy_core import NUS_SERVICE_UUID, BuddyCore  # noqa: E402
import fake_bluez  # noqa: E402
from fake_bluez import DBusDaemon, FakeBlueZ, FakeCentral  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


@pytest.fixture
def dbus():
    daemon = DBusDaemon()
    yield daemon
    daemon.close()


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(buddy_bluez, "READVERTISE_DELAY_S", 0.05)
    monkeypatch.setattr(buddy_bluez, "RESETUP_DELAY_S", 0.05)
    monkeypatch.setattr(buddy_bluez, "SUPERVISE_NOT_READY_S", 0.1)
    monkeypatch.setattr(buddy_bluez, "SUPERVISE_READY_S", 0.15)


async def wait_for(predicate, timeout=3.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


class Stack:
    """The daemon exactly as buddy.py wires it, plus the fake bluetoothd."""

    def __init__(self, dbus, tmp_path, *, secure=True, adv_mode="name", fake_kw=None, adapter="hci0"):
        self.fake = FakeBlueZ(dbus.address, **(fake_kw or {}))
        self.passkeys = []
        self.events = []
        self.core_events = []
        self.link_downs = 0
        self.transport = BlueZTransport(
            on_data=lambda data: self.core.feed(data),
            on_link_down=self._link_down,
            adapter=adapter,
            bus_address=dbus.address,
            secure=secure,
            adv_mode=adv_mode,
            peers_path=tmp_path / "state" / "peers.json",
            on_passkey=lambda pk, entered: self.passkeys.append((pk, entered)),
            on_event=lambda name, data: self.events.append((name, data)),
        )
        self.core = BuddyCore(
            tmp_path / "state",
            self.transport.send,
            unpair=self.transport.unpair,
            secure=self.transport.is_secure,
            on_event=lambda name, data: self.core_events.append((name, data)),
        )

    def _link_down(self):
        self.link_downs += 1
        self.core.link_down()

    async def start(self, own_name=True):
        await self.fake.start(own_name=own_name)
        await self.transport.start()

    async def stop(self):
        await self.transport.stop()
        self.fake.stop()
        self.core.close()

    async def connect(self, *, pair=True, notify=True, mtu=185, passkey=123456, **kw):
        central = FakeCentral(self.fake, mtu=mtu, **kw)
        await central.connect()
        if pair:
            await central.pair(passkey)
        if notify:
            await central.start_notify()
        return central


def run(coro_fn):
    asyncio.run(coro_fn())


def heartbeat(**kw):
    base = {"total": 1, "running": 0, "waiting": 0, "msg": "idle", "entries": [], "tokens": 0, "tokens_today": 0}
    base.update(kw)
    return base


# --- registration -----------------------------------------------------------------


def test_registers_service_agent_and_advertisement_like_the_reference_device(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            f = s.fake
            assert s.transport.ready and f.powered and f.pairable
            assert f.alias == "Claude-3456"  # last two bytes of DC:A6:32:12:34:56
            assert f.app["services"] == {SERVICE_PATH: NUS_SERVICE_UUID}
            assert f.app["rx_path"] == RX_PATH and f.app["tx_path"] == TX_PATH
            assert f.app["rx_flags"] == ["write", "write-without-response", "encrypt-authenticated-write"]
            assert f.app["tx_flags"] == ["notify", "encrypt-authenticated-notify"]
            assert f.agent == {"sender": f.client, "path": AGENT_PATH, "capability": "DisplayOnly", "default": True}
            adv = f.adv
            assert adv["path"] == ADV_PATH
            assert adv["props"]["Type"] == "peripheral"
            assert adv["props"]["ServiceUUIDs"] == [NUS_SERVICE_UUID]
            assert adv["props"]["Discoverable"] is True
            assert adv["props"]["LocalName"] == "Claude-3456"
            assert "Includes" not in adv["props"]  # the kernel would cut the name to 10 characters
            assert "DiscoverableTimeout" not in adv["props"]  # BlueZ 5.66 would strip the Flags with it
            assert adv["flags_present"] is True
            assert adv["scan_rsp_name"] == "Claude-3456" and adv["scan_rsp_complete"] is True
            assert adv["adv_bytes"] == 21  # flags + 128-bit UUID, well under 31
            assert s.transport.effective_adv_mode == "name"
        finally:
            await s.stop()

    run(scenario)


def test_alias_is_restored_on_clean_shutdown(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        assert s.fake.alias == "Claude-3456"
        await s.stop()
        assert s.fake.alias == ""  # back to "system provided"
        assert s.fake.app is None and s.fake.adv is None and s.fake.agent is None

    run(scenario)


def test_the_default_mode_does_not_depend_on_what_the_kernel_can_include(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path, fake_kw={"supported_includes": ["tx-power"]})
        await s.start()
        try:
            assert s.transport.ready
            assert s.fake.adv["scan_rsp_name"] == "Claude-3456" and s.fake.adv["scan_rsp_complete"] is True
        finally:
            await s.stop()

    run(scenario)


def test_kernel_mode_is_the_fallback_and_shows_why_it_is_not_the_default(dbus, tmp_path):
    """Includes=local-name makes the kernel append the adapter name, but it only keeps 10
    characters: "Claude-3456" goes out as "Claude-345", which a scanner matching by name misses."""

    async def scenario():
        s = Stack(dbus, tmp_path, adv_mode="kernel")
        await s.start()
        try:
            adv = s.fake.adv
            assert s.transport.effective_adv_mode == "kernel"
            assert adv["props"]["Includes"] == ["local-name"] and "LocalName" not in adv["props"]
            assert "DiscoverableTimeout" not in adv["props"] and adv["flags_present"] is True
            assert adv["scan_rsp_name"] == "Claude-345" and adv["scan_rsp_complete"] is False
        finally:
            await s.stop()

    run(scenario)


def test_kernel_mode_says_so_when_the_kernel_cannot_include_the_name(dbus, tmp_path, caplog):
    async def scenario():
        s = Stack(dbus, tmp_path, adv_mode="kernel", fake_kw={"supported_includes": ["tx-power"]})
        await s.start()
        try:
            assert not s.transport.ready
            assert "NotSupported" in s.transport.not_ready_reason
            assert "local-name" in caplog.text
        finally:
            await s.stop()

    run(scenario)


def test_an_unknown_adv_mode_is_refused():
    with pytest.raises(ValueError):
        BlueZTransport(lambda d: None, lambda: None, adv_mode="inline")


def test_insecure_mode_has_no_encryption_flags_and_no_agent(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path, secure=False)
        await s.start()
        try:
            assert s.fake.app["rx_flags"] == ["write", "write-without-response"]
            assert s.fake.app["tx_flags"] == ["notify"]
            assert s.fake.agent is None
            c = await s.connect(pair=False)
            await c.send({"cmd": "status"})
            ack = await c.next()
            assert ack["data"]["sec"] is False
        finally:
            await s.stop()

    run(scenario)


# --- pairing and access control ---------------------------------------------------


def test_unpaired_central_is_refused_then_pairs_with_the_passkey_shown(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = FakeCentral(s.fake)
            await c.connect()
            with pytest.raises(DBusError):
                await c.write(b"{}\n")
            with pytest.raises(DBusError):
                await c.start_notify()
            await c.pair(passkey=4242)
            assert s.passkeys[0] == ("004242", 0)  # zero padded to six digits
            assert ("004242", 6) in s.passkeys  # progress while the user types
            assert s.passkeys[-1] == (None, 0)  # cleared once paired
            await c.start_notify()
            await c.send({"cmd": "status"})
            ack = await c.next()
            assert ack["ack"] == "status" and ack["data"]["sec"] is True
        finally:
            await s.stop()

    run(scenario)


def test_cancelled_pairing_clears_the_passkey(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = FakeCentral(s.fake)
            await c.connect()
            await c.pair(passkey=999999, cancel=True)
            assert s.passkeys[0] == ("999999", 0) and s.passkeys[-1] == (None, 0)
        finally:
            await s.stop()

    run(scenario)


def test_agent_refuses_everything_but_displaying_a_passkey(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            agent = s.fake.agent
            dev = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
            for member, sig, body in (
                ("RequestConfirmation", "ou", [dev, 1]),
                ("RequestPasskey", "o", [dev]),
                ("RequestPinCode", "o", [dev]),
                ("RequestAuthorization", "o", [dev]),
                ("AuthorizeService", "os", [dev, NUS_SERVICE_UUID]),
            ):
                with pytest.raises(DBusError) as err:
                    await s.fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", member, sig, body)
                assert err.value.type == "org.bluez.Error.Rejected", member
        finally:
            await s.stop()

    run(scenario)


# --- the conversation -------------------------------------------------------------


def test_full_conversation_at_the_worst_case_mtu(dbus, tmp_path):
    """MTU 23: every write and notification is 20 bytes. Heartbeat with a prompt in,
    approval out, status poll in between."""

    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect(mtu=23)
            await c.send({"time": [1_775_731_234, -21600]})
            await c.send({"cmd": "owner", "name": "Ana"})
            assert (await c.next()) == {"ack": "owner", "ok": True, "n": 0}
            await c.send(heartbeat(total=2, running=1, waiting=1, msg="approve: Bash", entries=["10:42 git push"],
                                   tokens=184502, tokens_today=31200,
                                   prompt={"id": "req_abc123", "tool": "Bash", "hint": "rm -rf /tmp/foo"}))
            await wait_for(lambda: s.core.broker.current() is not None)
            assert s.core.persona() == "attention"
            s.core.broker.decide("claude:req_abc123", "once")
            assert (await c.next()) == {"cmd": "permission", "id": "req_abc123", "decision": "once"}
            await c.send({"cmd": "status"})
            ack = await c.next()
            assert ack["data"]["owner"] == "Ana" and ack["data"]["stats"]["appr"] == 1
            assert not c.violations
            assert max(len(n) for n in c.notifications) <= 20
        finally:
            await s.stop()

    run(scenario)


@pytest.mark.parametrize("mtu,expected_max", [(23, 20), (100, 97), (185, 180), (517, 180)])
def test_notifications_follow_the_mtu_and_never_exceed_180(dbus, tmp_path, mtu, expected_max):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect(mtu=mtu)
            await c.send({"cmd": "name", "name": "Clawd"})  # teach the daemon the MTU
            await c.next()
            await c.send({"cmd": "status"})
            ack = await c.next()
            assert ack["ok"] is True and ack["data"]["name"] == "Clawd"  # reassembled intact
            assert not c.violations
            sizes = [len(n) for n in c.notifications[1:]]  # skip the name ack
            assert max(sizes) == expected_max
            assert len(sizes) >= 2
        finally:
            await s.stop()

    run(scenario)


def test_non_ascii_names_survive_chunking(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect(mtu=23)
            await c.send({"cmd": "name", "name": "Ñandú ✓ 🐕"})
            await c.next()
            await c.send({"cmd": "status"})
            ack = await c.next()
            assert ack["data"]["name"] == "Ñandú ✓ 🐕"
        finally:
            await s.stop()

    run(scenario)


def test_messages_are_dropped_while_the_desktop_is_not_subscribed(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect(notify=False)
            await c.send({"cmd": "status"})
            await c.expect_silence(0.3)
            assert c.notifications == []
            await c.start_notify()
            await c.send({"cmd": "status"})
            assert (await c.next())["ack"] == "status"
        finally:
            await s.stop()

    run(scenario)


def test_folder_push_over_ble_installs_the_pack(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect(mtu=185)
            manifest = json.dumps({"name": "bufo", "states": {"sleep": "sleep.gif"}}).encode()
            gif = b"GIF89a" + bytes(range(256)) * 4
            files = {"manifest.json": manifest, "sleep.gif": gif}
            await c.send({"cmd": "char_begin", "name": "bufo", "total": sum(map(len, files.values()))})
            assert (await c.next()) == {"ack": "char_begin", "ok": True, "n": 0}
            for fname, data in files.items():
                await c.send({"cmd": "file", "path": fname, "size": len(data)})
                assert (await c.next())["ok"] is True
                written = 0
                for i in range(0, len(data), 200):  # lines longer than one write
                    part = data[i : i + 200]
                    await c.send({"cmd": "chunk", "d": base64.b64encode(part).decode()})
                    written += len(part)
                    assert (await c.next()) == {"ack": "chunk", "ok": True, "n": written}
                await c.send({"cmd": "file_end"})
                assert (await c.next()) == {"ack": "file_end", "ok": True, "n": len(data)}
            await c.send({"cmd": "char_end"})
            assert (await c.next()) == {"ack": "char_end", "ok": True, "n": 0}
            installed = tmp_path / "state" / "characters" / "bufo"
            assert (installed / "sleep.gif").read_bytes() == gif
            assert s.core.state.character == "bufo"
        finally:
            await s.stop()

    run(scenario)


def test_hostile_push_is_refused_over_ble(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect()
            await c.send({"cmd": "char_begin", "name": "evil", "total": 10})
            await c.next()
            await c.send({"cmd": "file", "path": "../../.ssh/authorized_keys", "size": 10})
            ack = await c.next()
            assert ack["ok"] is False
            assert not (tmp_path / ".ssh").exists() and not list((tmp_path / "state").rglob("authorized_keys"))
        finally:
            await s.stop()

    run(scenario)


# --- connection lifecycle ---------------------------------------------------------


def test_disconnect_resets_the_link_and_reconnect_needs_no_new_pairing(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect()
            await c.send(heartbeat(prompt={"id": "r1", "tool": "Bash", "hint": "x"}))
            await wait_for(lambda: s.core.broker.current() is not None)
            c.disconnect()
            await wait_for(lambda: s.link_downs == 1)
            assert not s.core.connected and s.core.broker.current() is None
            # same Mac comes back: bonded, so no passkey this time
            s.passkeys.clear()
            c.device.set_connected(True)
            await c.start_notify()
            await c.send(heartbeat())
            await wait_for(lambda: s.core.connected)
            await c.send({"cmd": "status"})
            assert (await c.next())["ack"] == "status"
            assert s.passkeys == []
        finally:
            await s.stop()

    run(scenario)


def test_stop_notify_without_disconnect_stops_sending(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect()
            await c.stop_notify()
            await c.send({"cmd": "status"})
            await c.expect_silence(0.3)
        finally:
            await s.stop()

    run(scenario)


def write_peers(tmp_path, *addresses):
    path = tmp_path / "state" / "peers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(addresses)))
    return path


def test_unpair_forgets_buddy_bonds_only_and_acks_before_dropping_the_connected_mac(dbus, tmp_path):
    async def scenario():
        write_peers(tmp_path, "11:22:33:44:55:66")
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            old_mac = s.fake.add_bonded_device("11:22:33:44:55:66", "Old iPad")  # paired with the buddy before
            keyboard = s.fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")  # nothing to do with us
            await asyncio.sleep(0.1)
            c = await s.connect()
            await c.send({"cmd": "unpair"})
            ack = await c.next()
            assert ack == {"ack": "unpair", "ok": True, "n": 0}
            assert old_mac.path in s.fake.removed  # idle buddy bonds go first
            assert c.path not in s.fake.removed  # the Mac still has its link, the ack got out
            await wait_for(lambda: c.path in s.fake.removed)
            await wait_for(lambda: s.link_downs == 1)
            assert keyboard.path not in s.fake.removed  # never touched
            assert PeerBook(tmp_path / "state" / "peers.json").addresses == set()
        finally:
            await s.stop()

    run(scenario)


def test_forget_all_for_the_keyboard_only_touches_buddy_devices(dbus, tmp_path):
    async def scenario():
        write_peers(tmp_path, "11:22:33:44:55:66", "11:22:33:44:55:77")
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            a = s.fake.add_bonded_device("11:22:33:44:55:66")
            b = s.fake.add_bonded_device("11:22:33:44:55:77")
            keyboard = s.fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")
            await asyncio.sleep(0.1)
            assert await s.transport.forget_all() == 2
            assert set(s.fake.removed) == {a.path, b.path}
            assert keyboard.path not in s.fake.removed
        finally:
            await s.stop()

    run(scenario)


def test_a_desktop_that_pairs_with_the_buddy_is_remembered_for_later(dbus, tmp_path):
    async def scenario():
        peers = tmp_path / "state" / "peers.json"
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            keyboard = s.fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")
            c = FakeCentral(s.fake)
            await c.connect()
            assert PeerBook(peers).addresses == set()  # just connected: could be anybody
            await c.pair(4242)
            assert PeerBook(peers).addresses == {c.mac}  # the code was shown for it, so it is the buddy's
            assert keyboard.mac not in PeerBook(peers)
        finally:
            await s.stop()

    run(scenario)


def test_the_first_write_also_remembers_the_desktop(dbus, tmp_path):
    async def scenario():
        peers = tmp_path / "state" / "peers.json"
        s = Stack(dbus, tmp_path, secure=False)  # no pairing here, so the write is the only sign
        await s.start()
        try:
            c = await s.connect(pair=False)
            assert PeerBook(peers).addresses == set()
            await c.send({"cmd": "status"})
            await c.next()
            assert PeerBook(peers).addresses == {c.mac}
        finally:
            await s.stop()

    run(scenario)


def test_the_identity_address_replaces_the_private_one_in_the_peer_book(dbus, tmp_path):
    """A Mac connects from a private address; once pairing hands over its identity key BlueZ keeps
    the same Device1 object (the path still holds the private address) and swaps Address."""

    async def scenario():
        peers = tmp_path / "state" / "peers.json"
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            keyboard = s.fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")
            c = await s.connect(mac="4B:11:22:33:44:55")
            await c.send({"cmd": "status"})
            await c.next()
            assert PeerBook(peers).addresses == {"4B:11:22:33:44:55"}
            c.resolve_identity("AA:BB:CC:DD:EE:FF")
            await wait_for(lambda: PeerBook(peers).addresses == {"AA:BB:CC:DD:EE:FF"})  # the old one is gone
            keyboard.resolve_identity("11:22:33:44:55:AA")  # not ours: nothing to record
            await asyncio.sleep(0.2)
            assert PeerBook(peers).addresses == {"AA:BB:CC:DD:EE:FF"}
            assert await s.transport.forget_all() == 1  # still found, through the path and the new address
            assert s.fake.removed == [c.path]
            assert PeerBook(peers).addresses == set()
        finally:
            await s.stop()

    run(scenario)


def test_a_desktop_that_paired_but_never_wrote_is_found_after_its_address_changes(dbus, tmp_path):
    async def scenario():
        peers = tmp_path / "state" / "peers.json"
        s = Stack(dbus, tmp_path)
        await s.start()
        c = FakeCentral(s.fake, mac="4B:11:22:33:44:55")
        try:
            await c.connect()
            await c.pair(4242)  # the passkey was shown for this device, so it belongs to the buddy
            assert PeerBook(peers).addresses == {"4B:11:22:33:44:55"}
            c.resolve_identity("AA:BB:CC:DD:EE:FF")
            await wait_for(lambda: PeerBook(peers).addresses == {"AA:BB:CC:DD:EE:FF"})
            await s.transport.stop()  # the daemon is gone; BlueZ still has the bond
            assert await forget_all_devices("hci0", dbus.address, peers) == (1, 0)
            assert s.fake.removed == [c.path]
        finally:
            s.fake.stop()
            s.core.close()

    run(scenario)


def test_classic_devices_are_not_announced_as_buddy_connections(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            mouse = s.fake.add_classic_device()
            await asyncio.sleep(0.2)
            mouse.disconnect()
            await asyncio.sleep(0.2)
            assert not [e for e in s.events if e[0] in ("device_connected", "device_disconnected")]
            phone = FakeCentral(s.fake, mac="11:22:33:44:55:77", alias="nRF Connect")  # a BLE scanner
            await phone.connect()
            await wait_for(lambda: any(e[0] == "device_connected" for e in s.events))
        finally:
            await s.stop()

    run(scenario)


def test_an_unrelated_device_disconnecting_does_not_end_the_link(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            mouse = s.fake.add_classic_device()
            c = await s.connect()
            await c.send(heartbeat())  # from the first write on, the buddy knows which device is its desktop
            await wait_for(lambda: s.core.connected)
            mouse.disconnect()
            await asyncio.sleep(0.3)
            assert s.link_downs == 0 and s.core.connected
            await c.send({"cmd": "status"})
            assert (await c.next())["ack"] == "status"  # notifications still flow
            c.disconnect()  # but its own desktop leaving does end it
            await wait_for(lambda: s.link_downs == 1)
        finally:
            await s.stop()

    run(scenario)


def test_an_unrelated_disconnect_does_not_cancel_the_passkey_on_screen(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = FakeCentral(s.fake)
            await c.connect()
            agent = s.fake.agent
            await s.fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", "DisplayPasskey", "ouq",
                              [c.path, 123456, 0])
            mouse = s.fake.add_classic_device()
            await asyncio.sleep(0.1)
            mouse.disconnect()  # nobody has written yet, so the buddy cannot tell who its desktop is
            await asyncio.sleep(0.3)
            assert s.passkeys == [("123456", 0)]  # the code is still on screen
            await c.pair(123456)
            assert s.passkeys[-1] == (None, 0)
        finally:
            await s.stop()

    run(scenario)


def test_a_pairing_attempt_that_walks_away_clears_the_passkey(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = FakeCentral(s.fake)
            await c.connect()
            agent = s.fake.agent
            await s.fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", "DisplayPasskey", "ouq",
                              [c.path, 123456, 0])
            assert s.passkeys == [("123456", 0)]
            c.disconnect()
            await wait_for(lambda: s.passkeys[-1] == (None, 0))
            assert s.link_downs == 0  # there never was a link to end
        finally:
            await s.stop()

    run(scenario)


# --- a desktop that is connected but never opens the channel ------------------------


class KickableDevice1(fake_bluez.Device1):
    """The fake's Device1 plus org.bluez.Device1.Disconnect, the call behind `bluetoothctl
    disconnect` and the one the daemon makes on a silent desktop. BlueZ drops the link and
    Connected goes false."""

    @method()
    def Disconnect(self):
        self.central.fake.calls.append(("Disconnect", self.central.path))
        self.central.disconnect()


@pytest.fixture
def quiet_watchdog(monkeypatch):
    monkeypatch.setattr(fake_bluez, "Device1", KickableDevice1)
    monkeypatch.setattr(buddy_bluez, "QUIET_PEER_TIMEOUT_S", 0.3)


def kicks(central):
    return central.fake.calls.count(("Disconnect", central.path))


async def bonded_mac(s, mac="AA:BB:CC:DD:EE:FF"):
    """The real case: a Mac that paired before (BlueZ lists it bonded) connects without pairing again."""
    c = FakeCentral(s.fake, mac=mac)
    c.attach(connected=True, paired=True)
    await asyncio.sleep(0.05)
    return c


def test_a_desktop_still_connected_when_the_daemon_starts_is_dropped_if_it_stays_silent(dbus, tmp_path, quiet_watchdog, caplog):
    """Seen with the real Mac on 17 Oct 2026: after the daemon (or the Pi) restarts, the Mac is still
    connected at the Bluetooth level but never subscribes to TX again, so the buddy sleeps for good.
    What fixed it by hand was `bluetoothctl disconnect`: the Mac reconnects by itself and opens the
    channel. The daemon now does that itself, after QUIET_PEER_TIMEOUT_S."""

    async def scenario():
        caplog.set_level(logging.INFO, logger="buddy.bluez")
        write_peers(tmp_path, "AA:BB:CC:DD:EE:FF")
        s = Stack(dbus, tmp_path)
        await s.fake.start()
        c = await bonded_mac(s)  # connected before the daemon comes up
        await s.transport.start()
        try:
            t0 = time.monotonic()
            await wait_for(lambda: kicks(c) == 1)
            assert time.monotonic() - t0 >= 0.25  # after the timeout, not on sight
            assert c.device.connected is False
            assert "callado" in caplog.text and "lo desconecto" in caplog.text
            # the Mac comes back on its own and this time opens the channel
            c.device.set_connected(True)
            await c.start_notify()
            await c.send(heartbeat())
            await wait_for(lambda: s.core.connected)
            await asyncio.sleep(0.5)  # longer than the watchdog
            assert kicks(c) == 1 and c.device.connected is True
        finally:
            await s.stop()

    run(scenario)


def test_a_known_desktop_that_reconnects_and_stays_silent_is_dropped_too(dbus, tmp_path, quiet_watchdog):
    async def scenario():
        write_peers(tmp_path, "AA:BB:CC:DD:EE:FF")
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await bonded_mac(s)
            await wait_for(lambda: kicks(c) == 1)
            assert s.link_downs == 0  # there never was a link to end
        finally:
            await s.stop()

    run(scenario)


@pytest.mark.parametrize("activity", ["notify", "write"])
def test_a_desktop_that_opens_the_channel_in_time_is_left_alone(dbus, tmp_path, quiet_watchdog, activity):
    async def scenario():
        write_peers(tmp_path, "AA:BB:CC:DD:EE:FF")
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await bonded_mac(s)
            if activity == "notify":
                await c.start_notify()
            else:
                await c.send(heartbeat())
                await wait_for(lambda: s.core.connected)
            await asyncio.sleep(0.6)
            assert kicks(c) == 0 and c.device.connected is True
        finally:
            await s.stop()

    run(scenario)


def test_silent_devices_that_are_not_the_buddys_are_never_dropped(dbus, tmp_path, quiet_watchdog, caplog):
    async def scenario():
        caplog.set_level(logging.INFO, logger="buddy.bluez")
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            mouse = s.fake.add_classic_device()  # the Pi's own, connected and silent by nature
            phone = FakeCentral(s.fake, mac="11:22:33:44:55:77", alias="nRF Connect")  # a scanner, unknown
            await phone.connect()
            await asyncio.sleep(0.6)
            assert kicks(mouse) == 0 and kicks(phone) == 0
            assert "callado" not in caplog.text
        finally:
            await s.stop()

    run(scenario)


def test_the_watchdog_gives_up_after_a_few_tries_until_the_desktop_leaves_on_its_own(dbus, tmp_path, quiet_watchdog, caplog):
    async def scenario():
        write_peers(tmp_path, "AA:BB:CC:DD:EE:FF")
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await bonded_mac(s)
            limit = buddy_bluez.QUIET_PEER_MAX_KICKS
            for n in range(1, limit + 1):
                await wait_for(lambda: kicks(c) == n)
                c.device.set_connected(True)  # back, still silent
            await asyncio.sleep(0.6)
            assert kicks(c) == limit and "no insisto" in caplog.text
            # it leaves by itself (Claude quit) and returns later: a fresh spell
            c.disconnect()
            await asyncio.sleep(0.05)
            c.device.set_connected(True)
            await wait_for(lambda: kicks(c) == limit + 1)
        finally:
            await s.stop()

    run(scenario)


def test_a_passkey_on_screen_pauses_the_watchdog_until_the_desktop_is_paired(dbus, tmp_path, quiet_watchdog):
    """The Mac forgot the bond but the Pi still has it: it connects as a known peer and pairs again.
    Dropping it while the person types the code would be the one thing worse than the bug."""

    async def scenario():
        write_peers(tmp_path, "AA:BB:CC:DD:EE:FF")
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = FakeCentral(s.fake)
            await c.connect()
            agent = s.fake.agent
            await s.fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", "DisplayPasskey", "ouq",
                              [c.path, 123456, 0])
            await asyncio.sleep(0.6)  # the person is slow at the keyboard
            assert kicks(c) == 0 and c.device.connected is True
            c.device.set_paired(True)  # done; now it has to open the channel like any other time
            await wait_for(lambda: kicks(c) == 1)
        finally:
            await s.stop()

    run(scenario)


def test_forget_all_without_the_daemon_removes_only_buddy_devices(dbus, tmp_path):
    async def scenario():
        peers = write_peers(tmp_path, "11:22:33:44:55:66", "AA:AA:AA:AA:AA:AA")  # the second is not listed by BlueZ now
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        try:
            mine = fake.add_bonded_device("11:22:33:44:55:66")
            keyboard = fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")
            await asyncio.sleep(0.1)
            assert await forget_all_devices("hci0", dbus.address, peers) == (1, 1)
            assert fake.removed == [mine.path]
            assert keyboard.path not in fake.removed
            # What was removed leaves the list. The rest stays: that address may belong to a desktop
            # that BlueZ simply has not listed yet, and forgetting it would lose track of its bond.
            assert PeerBook(peers).addresses == {"AA:AA:AA:AA:AA:AA"}
        finally:
            fake.stop()

    run(scenario)


def test_forget_all_without_the_daemon_needs_the_adapter_and_bluetoothd(dbus, tmp_path):
    async def scenario():
        with pytest.raises(TransportError, match="bluetoothd"):
            await forget_all_devices("hci0", dbus.address, tmp_path / "peers.json")
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        try:
            with pytest.raises(TransportError, match="hci3"):
                await forget_all_devices("hci3", dbus.address, tmp_path / "peers.json")
        finally:
            fake.stop()

    run(scenario)


# --- check (diagnose) ---------------------------------------------------------------


@pytest.fixture
def quiet_probes(monkeypatch):
    monkeypatch.setattr(buddy_bluez, "_probe_rfkill", lambda: "0: hci0: Bluetooth\n\tSoft blocked: no\n\tHard blocked: no\n")
    monkeypatch.setattr(buddy_bluez, "_probe_bluetoothd", lambda: "5.82")


def levels(findings):
    return [level for level, _ in findings]


def texts(findings):
    return "\n".join(text for _, text in findings)


def test_diagnose_healthy_adapter(dbus, tmp_path, quiet_probes):
    async def scenario():
        peers = write_peers(tmp_path, "11:22:33:44:55:66")
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        try:
            fake.add_bonded_device("11:22:33:44:55:66", "Old iPad")
            fake.add_bonded_device("11:22:33:44:55:99", "Pi keyboard")
            await asyncio.sleep(0.1)
            findings, facts = await diagnose("hci0", dbus.address, peers)
            report = texts(findings)
            assert "fail" not in levels(findings)
            assert "DC:A6:32:12:34:56" in report and "bluetoothd 5.82" in report
            assert "servidor GATT y anuncios BLE" in report
            assert "Old iPad: emparejado, desconectado, del buddy" in report
            assert "Pi keyboard: emparejado, desconectado" in report and "Pi keyboard: emparejado, desconectado, del buddy" not in report
            assert "forget-all" in report  # there is a leftover buddy bond: tell them how to clear it
            assert facts["active_instances"] == 0
        finally:
            fake.stop()

    run(scenario)


def test_diagnose_reports_a_busy_advertiser(dbus, tmp_path, quiet_probes):
    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        try:
            findings, facts = await diagnose("hci0", dbus.address, tmp_path / "peers.json")
            assert facts["active_instances"] == 0 and "cerralo" not in texts(findings)
            transport = BlueZTransport(lambda d: None, lambda: None, bus_address=dbus.address)
            await transport.start()
            try:
                findings, facts = await diagnose("hci0", dbus.address, tmp_path / "peers.json")
                assert facts["active_instances"] == 1
                assert "cerralo" in texts(findings)
            finally:
                await transport.stop()
        finally:
            fake.stop()

    run(scenario)


def test_diagnose_flags_an_adapter_that_cannot_be_a_peripheral(dbus, tmp_path, quiet_probes):
    async def scenario():
        fake = FakeBlueZ(dbus.address, roles=["central"])
        await fake.start()
        try:
            findings, _ = await diagnose("hci0", dbus.address, tmp_path / "peers.json")
            assert ("fail", "el adaptador no puede actuar como periférico (roles: central)") in findings
        finally:
            fake.stop()

    run(scenario)


def test_diagnose_without_bluetoothd(dbus, tmp_path, quiet_probes):
    async def scenario():
        findings, _ = await diagnose("hci0", dbus.address, tmp_path / "peers.json")
        assert levels(findings)[-1] == "fail" and "bluetoothd no está corriendo" in findings[-1][1]

    run(scenario)


def test_diagnose_missing_adapter_lists_what_exists(dbus, tmp_path, quiet_probes):
    async def scenario():
        fake = FakeBlueZ(dbus.address, adapter="hci0")
        await fake.start()
        try:
            findings, _ = await diagnose("hci1", dbus.address, tmp_path / "peers.json")
            assert findings[-1][0] == "fail"
            assert "hci1" in findings[-1][1] and "hci0" in findings[-1][1]
        finally:
            fake.stop()

    run(scenario)


def test_a_dbus_policy_that_denies_the_call_gets_a_message_that_says_what_to_try(dbus, tmp_path, quiet_probes):
    async def scenario():
        bus = await MessageBus(bus_address=dbus.address).connect()
        try:
            def deny(msg):
                if msg.message_type == MessageType.METHOD_CALL and msg.member == "GetManagedObjects":
                    return Message.new_error(msg, "org.freedesktop.DBus.Error.AccessDenied", "Rejected send message")
                return None

            bus.add_message_handler(deny)
            await bus.request_name("org.bluez")
            findings, _ = await diagnose("hci0", dbus.address, tmp_path / "peers.json")
            level, text = findings[-1]
            assert level == "fail" and "Probá con sudo" in text and "grupo bluetooth" in text
            with pytest.raises(TransportError, match="Probá con sudo"):
                await forget_all_devices("hci0", dbus.address, tmp_path / "peers.json")
        finally:
            bus.disconnect()

    run(scenario)


def test_diagnose_without_a_system_bus(tmp_path, quiet_probes):
    async def scenario():
        findings, _ = await diagnose("hci0", "unix:path=/nonexistent/socket", tmp_path / "peers.json")
        assert findings[-1][0] == "fail" and "D-Bus" in findings[-1][1]

    run(scenario)


@pytest.mark.parametrize(
    "rfkill_text, level, fragment",
    [
        ("0: hci0: Bluetooth\n\tSoft blocked: yes\n\tHard blocked: no\n", "fail", "rfkill unblock bluetooth"),
        ("0: hci0: Bluetooth\n\tSoft blocked: no\n\tHard blocked: yes\n", "fail", "hardware"),
        ("0: hci0: Bluetooth\n\tSoft blocked: no\n\tHard blocked: no\n", "ok", "no está bloqueado"),
        ("", "warn", "ningún adaptador"),
    ],
)
def test_rfkill_findings(rfkill_text, level, fragment):
    assert buddy_bluez._rfkill_finding(None) is None
    got_level, got_text = buddy_bluez._rfkill_finding(rfkill_text)
    assert got_level == level and fragment in got_text


# --- resilience -------------------------------------------------------------------


def test_bluetoothd_restart_re_registers_everything(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            await s.connect()
            await s.fake.restart()
            await wait_for(lambda: s.fake.app and s.fake.adv and s.fake.agent and s.transport.ready, timeout=4)
            assert s.fake.agent["default"] is True
            assert s.link_downs >= 1 and not s.core.connected
            # and it still works afterwards
            c2 = await s.connect(mac="AA:BB:CC:DD:EE:01")
            await c2.send({"cmd": "status"})
            assert (await c2.next())["ack"] == "status"
        finally:
            await s.stop()

    run(scenario)


def test_advertisement_released_by_bluez_comes_back(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            await s.fake.drop_advertisement()
            assert s.fake.adv is None
            await wait_for(lambda: s.fake.adv is not None)
            assert s.fake.adv["scan_rsp_name"] == "Claude-3456"
        finally:
            await s.stop()

    run(scenario)


def test_a_disconnect_leaves_the_advertisement_alone(dbus, tmp_path):
    """The kernel resumes advertising by itself when a peripheral link drops. Registering again
    would only race with it."""

    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect()
            await c.send({"cmd": "status"})
            await c.next()
            before, adv = s.fake.adv_registrations, s.fake.adv
            c.disconnect()
            await wait_for(lambda: s.link_downs == 1)
            await asyncio.sleep(0.4)  # longer than every timer the transport has
            assert s.fake.adv_registrations == before and s.fake.adv is adv
        finally:
            await s.stop()

    run(scenario)


@pytest.fixture
def signals_only(monkeypatch):
    """The supervisor sleeps for the whole test, so only BlueZ's own signals can notice changes."""
    monkeypatch.setattr(buddy_bluez, "SUPERVISE_NOT_READY_S", 60.0)
    monkeypatch.setattr(buddy_bluez, "SUPERVISE_READY_S", 60.0)


def test_a_released_advertisement_means_the_gatt_database_is_gone_too(dbus, tmp_path, signals_only):
    """BlueZ only releases an advertisement while tearing an adapter down, and the GATT database
    goes with it. Registering just the advertisement again would leave a visible device with no
    service."""

    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            await s.fake.drop_advertisement(with_gatt=True)
            assert s.fake.app is None and s.fake.adv is None
            await wait_for(lambda: s.fake.app and s.fake.adv and s.transport.ready, timeout=3)
            c = await s.connect()
            await c.send({"cmd": "status"})
            assert (await c.next())["ack"] == "status"
        finally:
            await s.stop()

    run(scenario)


def test_the_adapter_vanishing_quietly_is_noticed_from_the_signal_alone(dbus, tmp_path, signals_only):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            s.fake.adv = s.fake.app = None  # no Release call, no device to remove: only the signal is left
            await s.fake.remove_adapter()
            await wait_for(lambda: not s.transport.ready, timeout=2)
            assert "hci0" in s.transport.not_ready_reason
            s.fake.add_adapter()
            await wait_for(lambda: s.transport.ready and s.fake.app and s.fake.adv, timeout=3)
        finally:
            await s.stop()

    run(scenario)


def test_adapter_that_disappears_and_comes_back_is_set_up_from_scratch(dbus, tmp_path, signals_only):
    """Pulling a USB dongle (or a controller reset): the Mac is dropped, the buddy says so, and
    when the adapter returns everything is registered again on a fresh controller."""

    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            c = await s.connect()
            await c.send({"cmd": "status"})
            await c.next()
            await s.fake.remove_adapter()
            await wait_for(lambda: not s.transport.ready and s.link_downs >= 1)
            assert "hci0" in s.transport.not_ready_reason
            assert s.fake.app is None and s.fake.adv is None
            s.fake.add_adapter()  # off, no alias, nothing registered
            await wait_for(lambda: s.transport.ready and s.fake.app and s.fake.adv, timeout=4)
            f = s.fake
            assert f.powered and f.pairable and f.alias == "Claude-3456"
            assert f.app["rx_path"] == RX_PATH and f.app["tx_path"] == TX_PATH
            assert f.agent["default"] is True
            assert f.adv["scan_rsp_name"] == "Claude-3456"
            # the whole path works again: pair, subscribe, talk
            c2 = await s.connect(mac="AA:BB:CC:DD:EE:01")
            await c2.send({"cmd": "status"})
            assert (await c2.next())["ack"] == "status"
        finally:
            await s.stop()

    run(scenario)


def test_the_alias_survives_an_adapter_reset_and_is_still_restored_at_the_end(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        s.fake.alias = "my-pi"  # the person had named the Pi themselves
        await s.start()
        assert s.fake.alias == "Claude-3456"
        await s.fake.remove_adapter()
        await wait_for(lambda: not s.transport.ready)
        s.fake.add_adapter()
        await wait_for(lambda: s.transport.ready and s.fake.alias == "Claude-3456", timeout=4)
        await s.stop()
        assert s.fake.alias == "my-pi"  # not "Claude-3456" and not the empty one the reset left

    run(scenario)


def test_silently_lost_advertisement_is_noticed_by_the_supervisor(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start()
        try:
            s.fake.adv = None
            s.fake.adv_interface.changed()
            await wait_for(lambda: s.fake.adv is not None, timeout=3)
        finally:
            await s.stop()

    run(scenario)


def test_waits_for_bluetoothd_instead_of_dying(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path)
        await s.start(own_name=False)
        try:
            assert not s.transport.ready and "bluetoothd" in s.transport.not_ready_reason
            await s.fake.claim_name()
            await wait_for(lambda: s.transport.ready, timeout=4)
            assert s.fake.app and s.fake.adv
        finally:
            await s.stop()

    run(scenario)


def test_missing_adapter_is_reported_and_retried(dbus, tmp_path):
    async def scenario():
        s = Stack(dbus, tmp_path, adapter="hci1")  # the fake only has hci0
        await s.start()
        try:
            assert not s.transport.ready
            assert "hci1" in s.transport.not_ready_reason
        finally:
            await s.stop()

    run(scenario)
