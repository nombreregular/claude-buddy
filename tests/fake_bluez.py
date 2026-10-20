"""A small fake of the parts of BlueZ that buddy_bluez talks to.

It runs on a private dbus-daemon and mimics what the real bluetoothd does at the
D-Bus level: it calls GetManagedObjects on the registered GATT application and
validates it, reads the advertisement properties and lays them out the way BlueZ
and the kernel do (31 byte advertisement, 31 byte scan response), holds the agent
registration, and plays the central (the Mac) by calling WriteValue / StartNotify
and collecting PropertiesChanged(Value) notifications.

It is strict on purpose: wrong D-Bus types, unknown GATT flags, notifications
longer than MTU-3 and unauthenticated access are all errors, so the tests catch
mistakes the daemon could only otherwise find on real hardware. It is still only
a model: it cannot prove the real BlueZ behaves the same.

The BlueZ/kernel behaviors it copies were read from the source of BlueZ 5.66 and 5.82
(advertising.c, gatt-database.c, adapter.c) and the kernel (mgmt.c, eir.c):
- LocalName: BlueZ puts the complete name in the scan response (generate_scan_rsp).
- Includes local-name: the kernel appends the adapter name and keeps only 10 characters.
- LocalName together with Includes local-name is refused ("Local name already included").
- DiscoverableTimeout, even 0, arms a timer on 5.66 that strips the Flags field.
- WriteValue options for a GATT server are device, mtu, link and, when set, offset.
- Removing an adapter releases the advertisements and destroys the GATT database.
- After the central sends its identity key, the Device1 object keeps its path and only
  Address changes.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from dbus_fast import DBusError, Message, MessageType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

VALID_GATT_FLAGS = {
    "broadcast", "read", "write-without-response", "write", "notify", "indicate",
    "authenticated-signed-writes", "extended-properties", "reliable-write", "writable-auxiliaries",
    "encrypt-read", "encrypt-write", "encrypt-notify", "encrypt-indicate",
    "encrypt-authenticated-read", "encrypt-authenticated-write", "encrypt-authenticated-notify",
    "encrypt-authenticated-indicate", "secure-read", "secure-write", "secure-notify", "secure-indicate",
    "authorize",
}
AGENT_CAPABILITIES = {"", "DisplayOnly", "DisplayYesNo", "KeyboardOnly", "NoInputNoOutput", "KeyboardDisplay"}
ADV_LIMIT = 31
KERNEL_NAME_CHARS = 10  # HCI_MAX_SHORT_NAME_LENGTH: what Includes local-name can carry


class DBusDaemon:
    """A throwaway private bus."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="bz")
        conf = os.path.join(self.dir, "bus.conf")
        with open(conf, "w") as fh:
            fh.write(
                '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" '
                '"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">'
                f"<busconfig><type>session</type><listen>unix:path={self.dir}/sock</listen>"
                "<auth>EXTERNAL</auth><policy context='default'><allow send_destination='*' eavesdrop='true'/>"
                "<allow eavesdrop='true'/><allow own='*'/></policy></busconfig>"
            )
        self.proc = subprocess.Popen(
            ["dbus-daemon", f"--config-file={conf}", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE, text=True,
        )
        assert self.proc.stdout is not None
        self.address = self.proc.stdout.readline().strip()
        assert self.address.startswith("unix:"), self.address

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


# --- fake BlueZ objects ------------------------------------------------------------


class AgentManager1(ServiceInterface):
    def __init__(self, fake: "FakeBlueZ") -> None:
        super().__init__("org.bluez.AgentManager1")
        self.fake = fake

    @method()
    def RegisterAgent(self, agent: "o", capability: "s"):  # noqa: F821
        if capability not in AGENT_CAPABILITIES:
            raise DBusError("org.bluez.Error.InvalidArguments", f"bad capability {capability!r}")
        if self.fake.agent is not None:
            raise DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
        self.fake.agent = {"sender": self.fake.last_sender, "path": agent, "capability": capability, "default": False}

    @method()
    def UnregisterAgent(self, agent: "o"):  # noqa: F821
        if self.fake.agent is None or self.fake.agent["path"] != agent:
            raise DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")
        self.fake.agent = None

    @method()
    def RequestDefaultAgent(self, agent: "o"):  # noqa: F821
        if self.fake.agent is None or self.fake.agent["path"] != agent:
            raise DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")
        self.fake.agent["default"] = True


class Adapter1(ServiceInterface):
    def __init__(self, fake: "FakeBlueZ") -> None:
        super().__init__("org.bluez.Adapter1")
        self.fake = fake

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":  # noqa: F821
        return self.fake.mac

    @dbus_property(access=PropertyAccess.READ)
    def Name(self) -> "s":  # noqa: F821
        return self.fake.system_name

    @dbus_property()
    def Alias(self) -> "s":  # noqa: F821
        return self.fake.alias or self.fake.system_name

    @Alias.setter
    def Alias(self, value: "s"):  # noqa: F821
        self.fake.alias = value
        self.fake.calls.append(("Set", "Alias", value))
        self.emit_properties_changed({"Alias": value or self.fake.system_name})

    @dbus_property(access=PropertyAccess.READ)
    def Roles(self) -> "as":  # noqa: F821
        return list(self.fake.roles)

    @dbus_property()
    def Powered(self) -> "b":  # noqa: F821
        return self.fake.powered

    @Powered.setter
    def Powered(self, value: "b"):  # noqa: F821
        self.fake.powered = value
        self.fake.calls.append(("Set", "Powered", value))

    @dbus_property()
    def Pairable(self) -> "b":  # noqa: F821
        return self.fake.pairable

    @Pairable.setter
    def Pairable(self, value: "b"):  # noqa: F821
        self.fake.pairable = value
        self.fake.calls.append(("Set", "Pairable", value))

    @method()
    def RemoveDevice(self, device: "o"):  # noqa: F821
        central = self.fake.device_paths.get(device)
        if central is None:
            raise DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")
        self.fake.removed.append(device)
        central.removed_by_bluez()


class GattManager1(ServiceInterface):
    def __init__(self, fake: "FakeBlueZ") -> None:
        super().__init__("org.bluez.GattManager1")
        self.fake = fake

    @method()
    async def RegisterApplication(self, application: "o", options: "a{sv}"):  # noqa: F821
        await self.fake.register_application(application)

    @method()
    def UnregisterApplication(self, application: "o"):  # noqa: F821
        if self.fake.app is None or self.fake.app["root"] != application:
            raise DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")
        self.fake.app = None


class LEAdvertisingManager1(ServiceInterface):
    def __init__(self, fake: "FakeBlueZ") -> None:
        super().__init__("org.bluez.LEAdvertisingManager1")
        self.fake = fake

    @dbus_property(access=PropertyAccess.READ)
    def ActiveInstances(self) -> "y":  # noqa: F821
        return 1 if self.fake.adv else 0

    @dbus_property(access=PropertyAccess.READ)
    def SupportedInstances(self) -> "y":  # noqa: F821
        return 5

    @dbus_property(access=PropertyAccess.READ)
    def SupportedIncludes(self) -> "as":  # noqa: F821
        return list(self.fake.supported_includes)

    @dbus_property(access=PropertyAccess.READ)
    def SupportedFeatures(self) -> "as":  # noqa: F821
        return []

    def changed(self) -> None:
        self.emit_properties_changed({"ActiveInstances": 1 if self.fake.adv else 0})

    @method()
    async def RegisterAdvertisement(self, advertisement: "o", options: "a{sv}"):  # noqa: F821
        await self.fake.register_advertisement(advertisement)
        self.changed()

    @method()
    def UnregisterAdvertisement(self, advertisement: "o"):  # noqa: F821
        if self.fake.adv is None or self.fake.adv["path"] != advertisement:
            raise DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")
        self.fake.adv = None
        self.changed()


class Device1(ServiceInterface):
    def __init__(self, central: "FakeCentral") -> None:
        super().__init__("org.bluez.Device1")
        self.central = central
        self.paired = False
        self.connected = True

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":  # noqa: F821
        return self.central.mac

    @dbus_property(access=PropertyAccess.READ)
    def Alias(self) -> "s":  # noqa: F821
        return self.central.alias

    @dbus_property(access=PropertyAccess.READ)
    def Paired(self) -> "b":  # noqa: F821
        return self.paired

    @dbus_property(access=PropertyAccess.READ)
    def Connected(self) -> "b":  # noqa: F821
        return self.connected

    def set_paired(self, value: bool) -> None:
        self.paired = value
        self.emit_properties_changed({"Paired": value})

    def set_connected(self, value: bool) -> None:
        self.connected = value
        self.emit_properties_changed({"Connected": value})

    def set_address(self, value: str) -> None:
        self.central.mac = value
        self.emit_properties_changed({"Address": value})


class ClassicDevice1(Device1):
    """A BR/EDR device (keyboard, headset): BlueZ gives those a Class property."""

    @dbus_property(access=PropertyAccess.READ)
    def Class(self) -> "u":  # noqa: F821
        return 0x000540


# --- the fake daemon ---------------------------------------------------------------


class FakeBlueZ:
    def __init__(
        self,
        address: str,
        *,
        adapter: str = "hci0",
        mac: str = "DC:A6:32:12:34:56",
        supported_includes: Optional[List[str]] = None,
        system_name: str = "raspberrypi",
        roles: Optional[List[str]] = None,
    ) -> None:
        self.address = address
        self.adapter_path = f"/org/bluez/{adapter}"
        self.mac = mac
        self.system_name = system_name
        self.roles = ["central", "peripheral"] if roles is None else roles
        self.alias = ""
        self.powered = False
        self.pairable = False
        self.supported_includes = ["tx-power", "appearance", "local-name"] if supported_includes is None else supported_includes
        self.bus: Optional[MessageBus] = None
        self.last_sender: Optional[str] = None
        self.client: Optional[str] = None
        self.app: Optional[Dict[str, Any]] = None
        self.adv: Optional[Dict[str, Any]] = None
        self.agent: Optional[Dict[str, Any]] = None
        self.calls: List[Any] = []
        self.removed: List[str] = []
        self.device_paths: Dict[str, "FakeCentral"] = {}
        self.centrals: List["FakeCentral"] = []
        self.adv_interface: Optional[LEAdvertisingManager1] = None
        self.adv_registrations = 0
        self._adapter_ifaces: List[ServiceInterface] = []

    async def start(self, own_name: bool = True) -> None:
        self.bus = await MessageBus(bus_address=self.address).connect()
        self.bus.add_message_handler(self._spy)
        self.bus.export("/org/bluez", AgentManager1(self))
        self._export_adapter()
        if own_name:
            await self.bus.request_name("org.bluez")

    def _export_adapter(self) -> None:
        assert self.bus is not None
        self.adv_interface = LEAdvertisingManager1(self)
        self._adapter_ifaces = [Adapter1(self), GattManager1(self), self.adv_interface]
        for iface in self._adapter_ifaces:
            self.bus.export(self.adapter_path, iface)

    async def remove_adapter(self) -> None:
        """hci0 disappears (dongle pulled, controller reset). BlueZ calls Release on the
        advertisements and throws the GATT database away, drops every device, and finally the
        adapter objects go. bluetoothd itself keeps running, and so does the agent registration."""
        assert self.bus is not None
        adv, self.adv, self.app = self.adv, None, None
        for central in list(self.centrals):
            if central.device is not None:
                central.removed_by_bluez()
        if adv is not None and self.client is not None:
            await self.call(self.client, adv["path"], "org.bluez.LEAdvertisement1", "Release")
        for iface in self._adapter_ifaces:
            self.bus.unexport(self.adapter_path, iface)
        self._adapter_ifaces = []
        self.adv_interface = None

    def add_adapter(self) -> None:
        """The controller is back, factory fresh: off, not pairable, no alias, nothing registered."""
        self.alias = ""
        self.powered = False
        self.pairable = False
        self.app = self.adv = None
        self._export_adapter()

    async def claim_name(self) -> None:
        assert self.bus is not None
        await self.bus.request_name("org.bluez")

    async def restart(self) -> None:
        """What bluetoothd crashing and coming back looks like to a client."""
        assert self.bus is not None
        await self.bus.release_name("org.bluez")
        self.app = self.adv = self.agent = None
        for central in self.centrals:
            central.hard_disconnect()
        await asyncio.sleep(0.05)
        await self.bus.request_name("org.bluez")

    def stop(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()

    # -- spying on traffic (to learn the client's unique name and catch notifications) --

    def _spy(self, msg: Message) -> None:
        if msg.message_type == MessageType.METHOD_CALL:
            self.last_sender = msg.sender
            return None
        if msg.message_type == MessageType.SIGNAL and msg.member == "PropertiesChanged" and self.app:
            if msg.path == self.app["tx_path"] and msg.sender == self.client:
                iface, changed, _ = msg.body
                if iface == "org.bluez.GattCharacteristic1" and "Value" in changed:
                    for central in self.centrals:
                        central.on_notification(bytes(changed["Value"].value))
        return None

    async def call(self, dest: str, path: str, iface: str, member: str, sig: str = "", body: Optional[list] = None) -> Message:
        assert self.bus is not None
        reply = await asyncio.wait_for(
            self.bus.call(Message(destination=dest, path=path, interface=iface, member=member,
                                  signature=sig, body=body or [])), 5)
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name or "org.freedesktop.DBus.Error.Failed", str(reply.body[0]) if reply.body else "")
        return reply

    # -- RegisterApplication ----------------------------------------------------------

    async def register_application(self, root: str) -> None:
        if self.app is not None:
            raise DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
        client = self.last_sender
        assert client is not None
        reply = await self.call(client, root, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        if reply.signature != "a{oa{sa{sv}}}":
            raise DBusError("org.bluez.Error.InvalidArguments", f"bad GetManagedObjects signature {reply.signature}")
        objects = reply.body[0]
        services: Dict[str, str] = {}
        chars: Dict[str, Dict[str, Any]] = {}

        def prop(props: Dict[str, Variant], name: str, sig: str) -> Any:
            if name not in props:
                raise DBusError("org.bluez.Error.InvalidArguments", f"missing property {name}")
            if props[name].signature != sig:
                raise DBusError("org.bluez.Error.InvalidArguments", f"{name} must be {sig}, got {props[name].signature}")
            return props[name].value

        for path, ifaces in objects.items():
            if not path.startswith(root + "/"):
                raise DBusError("org.bluez.Error.InvalidArguments", f"{path} is outside the application root")
            if "org.bluez.GattService1" in ifaces:
                p = ifaces["org.bluez.GattService1"]
                services[path] = prop(p, "UUID", "s")
                prop(p, "Primary", "b")
        for path, ifaces in objects.items():
            if "org.bluez.GattCharacteristic1" in ifaces:
                p = ifaces["org.bluez.GattCharacteristic1"]
                service = prop(p, "Service", "o")
                if service not in services:
                    raise DBusError("org.bluez.Error.InvalidArguments", f"{path}: unknown service {service}")
                flags = prop(p, "Flags", "as")
                bad = [f for f in flags if f not in VALID_GATT_FLAGS]
                if bad:
                    raise DBusError("org.bluez.Error.InvalidArguments", f"{path}: invalid flags {bad}")
                chars[path] = {"uuid": prop(p, "UUID", "s"), "flags": flags, "service": service}
        by_uuid = {c["uuid"]: (path, c) for path, c in chars.items()}
        rx = by_uuid.get("6e400002-b5a3-f393-e0a9-e50e24dcca9e")
        tx = by_uuid.get("6e400003-b5a3-f393-e0a9-e50e24dcca9e")
        self.client = client
        self.app = {
            "root": root,
            "services": services,
            "chars": chars,
            "rx_path": rx[0] if rx else None,
            "tx_path": tx[0] if tx else None,
            "rx_flags": rx[1]["flags"] if rx else None,
            "tx_flags": tx[1]["flags"] if tx else None,
        }
        # BlueZ watches the application's objects for property changes.
        await self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
                        [f"type='signal',sender='{client}',interface='org.freedesktop.DBus.Properties',"
                         "member='PropertiesChanged'"])

    # -- RegisterAdvertisement --------------------------------------------------------

    async def register_advertisement(self, path: str) -> None:
        if self.adv is not None:
            raise DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
        client = self.last_sender
        assert client is not None
        reply = await self.call(client, path, "org.freedesktop.DBus.Properties", "GetAll", "s", ["org.bluez.LEAdvertisement1"])
        props = {k: v.value for k, v in reply.body[0].items()}
        if props.get("Type") not in ("broadcast", "peripheral"):
            raise DBusError("org.bluez.Error.InvalidArguments", "bad Type")
        includes = props.get("Includes", [])
        for inc in includes:
            if inc not in self.supported_includes:
                raise DBusError("org.bluez.Error.NotSupported", f"include {inc!r} not supported")
        if "LocalName" in props and "local-name" in includes:
            raise DBusError("org.bluez.Error.InvalidArguments", "Local name already included")
        # Advertisement: Flags (the kernel adds them when the instance is discoverable) + the UUIDs.
        used = 3 if props.get("Discoverable") else 0
        uuids = props.get("ServiceUUIDs", [])
        n128 = len([u for u in uuids if len(u) > 8])
        n16 = len(uuids) - n128
        if n128:
            used += 2 + 16 * n128
        if n16:
            used += 2 + 2 * n16
        if used > ADV_LIMIT:
            raise DBusError("org.bluez.Error.InvalidLength", "advertising data too long")
        # BlueZ 5.66 arms a discoverable timer for any DiscoverableTimeout, even 0, and when it
        # fires the kernel drops the Flags field: a central that filters on them stops seeing us.
        flags_present = bool(props.get("Discoverable")) and "DiscoverableTimeout" not in props
        # Scan response: where the name goes.
        scan_rsp_name: Optional[str] = None
        scan_rsp_complete: Optional[bool] = None
        if "LocalName" in props:
            # generate_scan_rsp: complete when it fits (29 characters), shortened otherwise
            name = props["LocalName"]
            scan_rsp_name = name[: ADV_LIMIT - 2]
            scan_rsp_complete = scan_rsp_name == name
        elif "local-name" in includes:
            # eir_append_local_name: the adapter name, complete only up to 10 characters
            name = self.alias or self.system_name
            scan_rsp_name = name[:KERNEL_NAME_CHARS]
            scan_rsp_complete = len(name) <= KERNEL_NAME_CHARS
        self.adv = {
            "path": path, "props": props, "adv_bytes": used, "flags_present": flags_present,
            "scan_rsp_name": scan_rsp_name, "scan_rsp_complete": scan_rsp_complete,
        }
        self.adv_registrations += 1

    async def drop_advertisement(self, with_gatt: bool = False) -> None:
        """The kernel/BlueZ removed our instance (timeout, power cycle): call Release.
        With `with_gatt` BlueZ also threw the GATT database away, as it does when it tears the
        adapter down."""
        if with_gatt:
            self.app = None
        if self.adv is None:
            return
        path, self.adv = self.adv["path"], None
        assert self.adv_interface is not None
        self.adv_interface.changed()
        await self.call(self.client, path, "org.bluez.LEAdvertisement1", "Release")  # type: ignore[arg-type]

    def add_classic_device(self, mac: str = "11:22:33:44:55:88", alias: str = "Pi mouse", connected: bool = True) -> "FakeCentral":
        """A BR/EDR device (mouse, headset) the Pi has nothing to do with the buddy about."""
        central = FakeCentral(self, mac=mac, alias=alias)
        central.attach(connected=connected, paired=True, classic=True)
        return central

    def add_bonded_device(self, mac: str = "11:22:33:44:55:66", alias: str = "Old iPad") -> "FakeCentral":
        """A device that is bonded but not connected."""
        central = FakeCentral(self, mac=mac, alias=alias)
        central.attach(connected=False, paired=True)
        return central


class FakeCentral:
    """Plays the Mac."""

    def __init__(self, fake: FakeBlueZ, mac: str = "AA:BB:CC:DD:EE:FF", alias: str = "Ana's MacBook Pro", mtu: int = 185) -> None:
        self.fake = fake
        self.mac = mac
        self.alias = alias
        self.mtu = mtu
        self.path = f"{fake.adapter_path}/dev_{mac.replace(':', '_')}"
        self.device: Optional[Device1] = None
        self.subscribed = False
        self._buf = bytearray()
        self.lines: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self.notifications: List[bytes] = []
        self.violations: List[str] = []
        fake.centrals.append(self)

    # -- device lifecycle --------------------------------------------------------------

    def attach(self, connected: bool = True, paired: bool = False, classic: bool = False) -> None:
        assert self.fake.bus is not None
        self.device = ClassicDevice1(self) if classic else Device1(self)
        self.device.connected = connected
        self.device.paired = paired
        self.fake.bus.export(self.path, self.device)  # dbus-fast emits InterfacesAdded
        self.fake.device_paths[self.path] = self

    async def connect(self) -> None:
        self.attach(connected=True, paired=False)
        await asyncio.sleep(0.05)

    def disconnect(self, stop_notify: bool = True) -> None:
        assert self.device is not None
        self.subscribed = False
        self.device.set_connected(False)

    def hard_disconnect(self) -> None:
        self.subscribed = False
        if self.device is not None:
            self.device.connected = False

    def resolve_identity(self, identity_mac: str) -> None:
        """The central handed over its identity key. BlueZ keeps the Device1 object, whose path
        still has the private address it first connected with, and swaps the Address property."""
        assert self.device is not None
        self.device.set_address(identity_mac)

    def removed_by_bluez(self) -> None:
        if self.device is not None and self.device.connected:
            self.disconnect()
        if self.device is not None and self.fake.bus is not None:
            self.fake.bus.unexport(self.path, self.device)
        self.fake.device_paths.pop(self.path, None)
        self.device = None

    # -- pairing -------------------------------------------------------------------------

    async def pair(self, passkey: int = 4242, cancel: bool = False) -> None:
        """LE Secure Connections passkey entry, from BlueZ's point of view."""
        agent = self.fake.agent
        if agent is None:
            raise DBusError("org.bluez.Error.AuthenticationFailed", "no agent registered")
        assert agent["capability"] == "DisplayOnly", agent
        await self.fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", "DisplayPasskey", "ouq",
                             [self.path, passkey, 0])
        for entered in (1, 3, 6):  # the user typing on the Mac
            await self.fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", "DisplayPasskey", "ouq",
                                 [self.path, passkey, entered])
        if cancel:
            await self.fake.call(agent["sender"], agent["path"], "org.bluez.Agent1", "Cancel")
            return
        assert self.device is not None
        self.device.set_paired(True)
        await asyncio.sleep(0.05)

    # -- GATT access ---------------------------------------------------------------------

    def _need_auth(self, flags: Optional[List[str]], kind: str) -> None:
        needs = any(f.startswith(("encrypt-authenticated", "secure")) for f in (flags or []))
        if needs and not (self.device and self.device.paired):
            raise DBusError("org.bluez.Error.NotPermitted", f"Insufficient Authentication ({kind})")

    async def write(self, data: bytes) -> None:
        app = self.fake.app
        assert app and app["rx_path"], "no RX characteristic registered"
        self._need_auth(app["rx_flags"], "write")
        assert len(data) <= self.mtu - 3, "central tried to write more than MTU-3"
        options = {  # append_options() in BlueZ's gatt-database.c
            "device": Variant("o", self.path),
            "link": Variant("s", "LE"),
            "mtu": Variant("q", self.mtu),
        }
        await self.fake.call(self.fake.client, app["rx_path"], "org.bluez.GattCharacteristic1", "WriteValue",
                             "aya{sv}", [data, options])  # type: ignore[arg-type]

    async def send(self, obj: Dict[str, Any]) -> None:
        """Write one JSON line the way the desktop does: split at the MTU."""
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        size = self.mtu - 3
        for i in range(0, len(line), size):
            await self.write(line[i : i + size])

    async def start_notify(self) -> None:
        app = self.fake.app
        assert app and app["tx_path"], "no TX characteristic registered"
        self._need_auth(app["tx_flags"], "notify")
        await self.fake.call(self.fake.client, app["tx_path"], "org.bluez.GattCharacteristic1", "StartNotify")  # type: ignore[arg-type]
        self.subscribed = True

    async def stop_notify(self) -> None:
        app = self.fake.app
        assert app and app["tx_path"]
        await self.fake.call(self.fake.client, app["tx_path"], "org.bluez.GattCharacteristic1", "StopNotify")  # type: ignore[arg-type]
        self.subscribed = False

    def on_notification(self, value: bytes) -> None:
        if not self.subscribed:
            return
        if len(value) > self.mtu - 3:
            self.violations.append(f"notification of {len(value)} bytes exceeds MTU-3 = {self.mtu - 3}")
        self.notifications.append(value)
        self._buf += value
        while b"\n" in self._buf:
            line, _, rest = bytes(self._buf).partition(b"\n")
            self._buf = bytearray(rest)
            self.lines.put_nowait(json.loads(line.decode("utf-8")))

    async def next(self, timeout: float = 2.0) -> Dict[str, Any]:
        return await asyncio.wait_for(self.lines.get(), timeout)

    async def expect_silence(self, seconds: float = 0.3) -> None:
        await asyncio.sleep(seconds)
        assert self.lines.empty(), f"unexpected message: {self.lines.get_nowait()}"
