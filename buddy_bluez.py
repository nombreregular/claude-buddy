"""BlueZ transport for the buddy: GATT server (Nordic UART), LE advertising and a
DisplayOnly pairing agent, all spoken over D-Bus with dbus-fast.

It never touches the protocol itself. Bytes from the RX characteristic go to
`on_data`, and `send()` turns a dict into a line and notifies it on TX, chunked to
the negotiated MTU.

Built from the BlueZ D-Bus API docs (gatt-api, advertising-api, agent-api) and the
official ESP32 firmware. It has not been run against a real adapter yet: see the
README for what to check on the Pi.
"""
import asyncio
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from dbus_fast import BusType, DBusError, Message, MessageType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

from buddy_core import NUS_RX_UUID, NUS_SERVICE_UUID, NUS_TX_UUID, encode_line

log = logging.getLogger("buddy.bluez")

BLUEZ = "org.bluez"
BLUEZ_ROOT = "/org/bluez"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
IFACE_OBJMGR = "org.freedesktop.DBus.ObjectManager"
IFACE_ADAPTER = "org.bluez.Adapter1"
IFACE_DEVICE = "org.bluez.Device1"
IFACE_GATT_MGR = "org.bluez.GattManager1"
IFACE_ADV_MGR = "org.bluez.LEAdvertisingManager1"
IFACE_AGENT_MGR = "org.bluez.AgentManager1"

APP_ROOT = "/com/claudebuddy/gatt"  # only GATT objects live under this path
SERVICE_PATH = APP_ROOT + "/service0"
RX_PATH = SERVICE_PATH + "/char0"
TX_PATH = SERVICE_PATH + "/char1"
ADV_PATH = "/com/claudebuddy/adv0"
AGENT_PATH = "/com/claudebuddy/agent"

DEFAULT_MTU = 23
MAX_NOTIFY_BYTES = 180  # same cap as the reference firmware
TX_GAP_S = 0.004  # the reference firmware yields 4 ms between notifications
CALL_TIMEOUT_S = 10.0
UNPAIR_GRACE_S = 0.5  # let the ack leave before the connected device is dropped
READVERTISE_DELAY_S = 1.0
RESETUP_DELAY_S = 1.0  # after bluetoothd (re)appears, give it time to enumerate adapters
SUPERVISE_NOT_READY_S = 5.0
SUPERVISE_READY_S = 15.0
QUIET_PEER_TIMEOUT_S = 20.0  # a buddy desktop connected this long without subscribing or writing is dropped
QUIET_PEER_MAX_KICKS = 3  # per silent spell; a subscription, a write or leaving on its own starts a new one

MATCH_RULES = (
    "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
    "member='PropertiesChanged',path_namespace='/org/bluez'",
    "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesAdded'",
    "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesRemoved'",
    "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
    "member='NameOwnerChanged',arg0='org.bluez'",
)


class TransportError(Exception):
    """Something the person can act on. The message is shown as is, so it is in Spanish."""


def rejected(text: str = "Rejected") -> DBusError:
    return DBusError("org.bluez.Error.Rejected", text)


ERR_NO_BLUEZ = ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner")
ERR_DENIED = "org.freedesktop.DBus.Error.AccessDenied"


# --- plain D-Bus helpers (shared by the transport, `check` and `forget-all`) ------------


async def _open_bus(bus_address: Optional[str]) -> MessageBus:
    bus = MessageBus(bus_address=bus_address) if bus_address else MessageBus(bus_type=BusType.SYSTEM)
    try:
        return await bus.connect()
    except Exception as exc:
        raise TransportError(f"no puedo conectar al bus D-Bus del sistema: {exc}") from exc


async def _bus_call(
    bus: MessageBus,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: Optional[list] = None,
    destination: str = BLUEZ,
) -> Message:
    msg = Message(
        destination=destination, path=path, interface=interface, member=member,
        signature=signature, body=body or [],
    )
    reply = await asyncio.wait_for(bus.call(msg), CALL_TIMEOUT_S)
    if reply.message_type == MessageType.ERROR:
        raise DBusError(reply.error_name or "org.freedesktop.DBus.Error.Failed",
                        str(reply.body[0]) if reply.body else "", reply)
    return reply


async def _get_managed_objects(bus: MessageBus) -> Dict[str, Dict[str, Dict[str, Variant]]]:
    """Everything BlueZ knows, with the failures a person can fix turned into TransportError."""
    try:
        reply = await _bus_call(bus, "/", IFACE_OBJMGR, "GetManagedObjects")
    except DBusError as exc:
        if exc.type in ERR_NO_BLUEZ:
            raise TransportError("bluetoothd no está corriendo (sudo systemctl enable --now bluetooth)") from exc
        if exc.type == ERR_DENIED:
            raise TransportError(
                "D-Bus no me deja hablar con BlueZ. Probá con sudo (sudo .venv/bin/python buddy.py ...). "
                "En sistemas con la política vieja de D-Bus ayuda estar en el grupo bluetooth "
                "(sudo usermod -aG bluetooth $USER y volver a entrar)"
            ) from exc
        raise
    return reply.body[0]


async def _get_all(bus: MessageBus, path: str, interface: str) -> Dict[str, Any]:
    reply = await _bus_call(bus, path, IFACE_PROPS, "GetAll", "s", [interface])
    return {k: v.value for k, v in reply.body[0].items()}


class PeerBook:
    """Addresses of the devices that talked to the buddy over BLE.

    The Pi is a normal computer: it may also have a Bluetooth keyboard or headphones paired.
    Forgetting (the desktop's Forget button, the `u` key, `forget-all`) must only touch the
    desktops that paired through the buddy, never those."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else None
        self.addresses: Set[str] = set()
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = []
            if isinstance(data, list):
                self.addresses = {a.upper() for a in data if isinstance(a, str)}

    def __contains__(self, address: object) -> bool:
        return isinstance(address, str) and address.upper() in self.addresses

    def add(self, address: str) -> None:
        address = address.upper()
        if address and address not in self.addresses:
            self.addresses.add(address)
            self._save()

    def discard(self, address: str) -> None:
        if address.upper() in self.addresses:
            self.addresses.discard(address.upper())
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(sorted(self.addresses)))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)


# --- D-Bus objects we export --------------------------------------------------------


class GattService(ServiceInterface):
    def __init__(self, uuid: str) -> None:
        super().__init__("org.bluez.GattService1")
        self._uuid = uuid

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":  # noqa: F821
        return True


class RxCharacteristic(ServiceInterface):
    """Desktop -> device. Write only."""

    def __init__(self, uuid: str, flags: List[str], on_write: Callable[[bytes, Dict[str, Any]], None]) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self._uuid = uuid
        self._flags = flags
        self._on_write = on_write

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":  # noqa: F821
        return SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: F821
        return list(self._flags)

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):  # noqa: F821
        self._on_write(bytes(value), {k: v.value for k, v in options.items()})


class TxCharacteristic(ServiceInterface):
    """Device -> desktop. Notify only; a notification is a PropertiesChanged on Value."""

    def __init__(self, uuid: str, flags: List[str], on_notify_state: Callable[[bool], None]) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self._uuid = uuid
        self._flags = flags
        self._on_notify_state = on_notify_state
        self._notifying = False
        self._value = b""

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":  # noqa: F821
        return SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: F821
        return list(self._flags)

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":  # noqa: F821
        return self._notifying

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":  # noqa: F821
        return self._value

    @method()
    def StartNotify(self):
        self._set_notifying(True)

    @method()
    def StopNotify(self):
        self._set_notifying(False)

    def _set_notifying(self, state: bool) -> None:
        if state != self._notifying:
            self._notifying = state
            self.emit_properties_changed({"Notifying": state})
        self._on_notify_state(state)

    def reset(self) -> None:
        self._notifying = False

    def push(self, chunk: bytes) -> None:
        log.debug("TX notify %d bytes", len(chunk))
        self._value = chunk
        self.emit_properties_changed({"Value": chunk})


class _AdvBase(ServiceInterface):
    """LEAdvertisement1 with the properties both modes share.

    No DiscoverableTimeout on purpose: BlueZ 5.66 arms a timer even for 0 and then strips the
    Flags field about a second later (advertising.c, parse_discoverable_timeout)."""

    def __init__(self, uuid: str, on_release: Callable[[], None]) -> None:
        super().__init__("org.bluez.LEAdvertisement1")
        self._uuid = uuid
        self._on_release = on_release

    @method()
    def Release(self):
        self._on_release()

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":  # noqa: F821
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":  # noqa: F821
        return [self._uuid]

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":  # noqa: F821
        return True


class AdvName(_AdvBase):
    """The default. With LocalName set, BlueZ itself puts the complete name in the scan
    response (advertising.c, generate_scan_rsp) and keeps flags plus the 128-bit UUID in the
    advertisement: the same layout as the ESP32 reference."""

    def __init__(self, uuid: str, name: str, on_release: Callable[[], None]) -> None:
        super().__init__(uuid, on_release)
        self._name = name

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":  # noqa: F821
        return self._name


class AdvKernelName(_AdvBase):
    """Fallback: Includes=local-name asks the kernel to append the adapter name. The kernel only
    keeps 10 characters of it (eir_append_local_name), so "Claude-3456" goes out as "Claude-345"."""

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":  # noqa: F821
        return ["local-name"]


class PasskeyAgent(ServiceInterface):
    """org.bluez.Agent1 with DisplayOnly capability: BlueZ picks the passkey, we show it,
    the user types it on the desktop. Everything else is refused."""

    def __init__(
        self,
        on_display: Callable[[str, str, int], None],
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__("org.bluez.Agent1")
        self._on_display = on_display
        self._on_cancel = on_cancel

    @method()
    def Release(self):
        log.info("pairing agent released by BlueZ")
        self._on_cancel()

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        log.warning("pairing refused for %s: it asked for a legacy PIN code", device)
        raise rejected("PIN codes are not supported")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
        log.info("legacy PIN requested for %s (ignored)", device)

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        log.warning("pairing refused for %s: it expects us to type a passkey, but we can only show one", device)
        raise rejected("this device only displays passkeys")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        self._on_display(device, "%06d" % passkey, entered)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        log.warning("pairing refused for %s: it asked for numeric comparison, which needs a screen with a button", device)
        raise rejected("numeric comparison is not supported")

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        log.warning("pairing refused for %s: it chose 'just works', which protects against nobody", device)
        raise rejected("unauthenticated pairing refused")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        log.warning("service authorization refused for %s (%s)", device, uuid)
        raise rejected("service authorization refused")

    @method()
    def Cancel(self):
        self._on_cancel()


# --- the transport ------------------------------------------------------------------


class BlueZTransport:
    def __init__(
        self,
        on_data: Callable[[bytes], None],
        on_link_down: Callable[[], None],
        *,
        adapter: str = "hci0",
        bus_address: Optional[str] = None,
        secure: bool = True,
        adv_mode: str = "name",
        peers_path: Optional[Path] = None,
        on_passkey: Optional[Callable[[Optional[str], int], None]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        if adv_mode not in ("name", "kernel"):
            raise ValueError("adv_mode must be name or kernel")
        self._on_data = on_data
        self._on_link_down = on_link_down
        self._adapter = adapter
        self._bus_address = bus_address
        self.secure = secure
        self._adv_mode = adv_mode
        self._on_passkey = on_passkey
        self._on_event = on_event
        self._peers = PeerBook(peers_path)
        self._peer_paths: Set[str] = set()  # device objects that talked to the buddy during this run
        self._bus: Optional[MessageBus] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: Set["asyncio.Future[Any]"] = set()
        self._setup_lock: Optional[asyncio.Lock] = None
        self._adv_lock: Optional[asyncio.Lock] = None
        self._txq: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._rx: Optional[RxCharacteristic] = None
        self._tx: Optional[TxCharacteristic] = None
        self._adv: Optional[_AdvBase] = None
        self._agent: Optional[PasskeyAgent] = None
        self._exported = False
        self.ready = False
        self.not_ready_reason = "starting"
        self.adapter_path = f"{BLUEZ_ROOT}/{adapter}"
        self.address = ""
        self.device_name = "Claude"
        self.effective_adv_mode = ""
        self._prev_alias: Optional[str] = None
        self._alias_was_default = False
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._active_device: Optional[str] = None
        self._mtu = DEFAULT_MTU
        self._tx_subscribed = False
        self._quiet_timers: Dict[str, "asyncio.Future[Any]"] = {}  # device path -> pending kick
        self._quiet_kicks: Dict[str, int] = {}  # device path -> disconnects we forced this silent spell
        self._kicked: Set[str] = set()  # device paths whose next disconnect is our own doing
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._setup_lock = asyncio.Lock()
        self._adv_lock = asyncio.Lock()
        self._bus = await _open_bus(self._bus_address)
        self._bus.add_message_handler(self._on_bus_message)
        for rule in MATCH_RULES:
            await self._call("/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [rule],
                             destination="org.freedesktop.DBus")
        self._export_objects()
        self._spawn(self._tx_pump())
        self._spawn(self._supervisor())
        await self._try_setup()

    async def stop(self) -> None:
        self._stopping = True
        if self._bus is None:
            return
        if self.ready:
            for member, iface, path in (
                ("UnregisterAdvertisement", IFACE_ADV_MGR, ADV_PATH),
                ("UnregisterApplication", IFACE_GATT_MGR, APP_ROOT),
            ):
                await self._best_effort(self.adapter_path, iface, member, "o", [path])
            if self.secure:
                await self._best_effort(BLUEZ_ROOT, IFACE_AGENT_MGR, "UnregisterAgent", "o", [AGENT_PATH])
            await self._restore_alias()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._bus.disconnect()
        self.ready = False

    def _spawn(self, coro: Any) -> "asyncio.Future[Any]":
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _event(self, event: str, **data: Any) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event, data)
            except Exception:
                log.exception("event listener failed")

    # -- low level D-Bus helpers ---------------------------------------------------

    async def _call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Optional[list] = None,
        destination: str = BLUEZ,
    ) -> Message:
        assert self._bus is not None
        return await _bus_call(self._bus, path, interface, member, signature, body, destination)

    async def _best_effort(self, *args: Any, **kwargs: Any) -> None:
        try:
            await self._call(*args, **kwargs)
        except (DBusError, asyncio.TimeoutError, OSError) as exc:
            log.debug("ignored failure in %s: %s", args[2] if len(args) > 2 else args, exc)

    async def _get_props(self, path: str, interface: str) -> Dict[str, Any]:
        assert self._bus is not None
        return await _get_all(self._bus, path, interface)

    async def _set_prop(self, path: str, interface: str, name: str, signature: str, value: Any) -> None:
        await self._call(path, IFACE_PROPS, "Set", "ssv", [interface, name, Variant(signature, value)])

    # -- setup ---------------------------------------------------------------------

    def _export_objects(self) -> None:
        assert self._bus is not None
        if self._exported:
            return
        rx_flags = ["write", "write-without-response"]
        tx_flags = ["notify"]
        if self.secure:
            rx_flags.append("encrypt-authenticated-write")
            tx_flags.append("encrypt-authenticated-notify")
        self._rx = RxCharacteristic(NUS_RX_UUID, rx_flags, self._handle_write)
        self._tx = TxCharacteristic(NUS_TX_UUID, tx_flags, self._handle_notify_state)
        self._bus.export(SERVICE_PATH, GattService(NUS_SERVICE_UUID))
        self._bus.export(RX_PATH, self._rx)
        self._bus.export(TX_PATH, self._tx)
        if self.secure:
            self._agent = PasskeyAgent(self._handle_passkey, self._handle_passkey_cancel)
            self._bus.export(AGENT_PATH, self._agent)
        self._exported = True

    async def _try_setup(self) -> bool:
        assert self._setup_lock is not None
        async with self._setup_lock:
            if self.ready or self._stopping:
                return self.ready
            try:
                await self._setup_bluez()
            except TransportError as exc:
                self._not_ready(str(exc))
            except DBusError as exc:
                self._not_ready(f"BlueZ rechazó un pedido: {exc.type}: {exc.text}")
            except (asyncio.TimeoutError, OSError) as exc:
                self._not_ready(f"BlueZ no contestó ({exc.__class__.__name__})")
            return self.ready

    def _not_ready(self, reason: str) -> None:
        if reason != self.not_ready_reason:
            log.warning("Bluetooth not ready: %s", reason)
        self.ready = False
        self.not_ready_reason = reason
        self._event("not_ready", reason=reason)

    async def _setup_bluez(self) -> None:
        assert self._bus is not None
        objects = await _get_managed_objects(self._bus)
        ifaces = objects.get(self.adapter_path, {})
        if IFACE_ADAPTER not in ifaces:
            raise TransportError(
                f"no encuentro el adaptador {self._adapter} (¿rfkill unblock bluetooth? ¿el dongle está enchufado?)"
            )
        for needed in (IFACE_GATT_MGR, IFACE_ADV_MGR):
            if needed not in ifaces:
                raise TransportError(
                    f"el adaptador {self._adapter} no ofrece {needed}: no puede actuar como periférico BLE"
                )
        props = {k: v.value for k, v in ifaces[IFACE_ADAPTER].items()}
        self.address = str(props.get("Address", ""))
        suffix = self.address.replace(":", "")[-4:].upper() if self.address else ""
        self.device_name = f"Claude-{suffix}" if suffix else "Claude"

        self._devices = {
            p: {k: v.value for k, v in i[IFACE_DEVICE].items()}
            for p, i in objects.items()
            if IFACE_DEVICE in i and p.startswith(self.adapter_path + "/")
        }

        if not props.get("Powered"):
            await self._set_prop(self.adapter_path, IFACE_ADAPTER, "Powered", "b", True)
        await self._best_effort_prop("Pairable", True)
        await self._apply_alias(props)

        self.effective_adv_mode = self._adv_mode
        if self._adv_mode == "kernel":
            adv_props = await self._get_props(self.adapter_path, IFACE_ADV_MGR)
            if "local-name" not in list(adv_props.get("SupportedIncludes", [])):
                log.warning("this BlueZ/kernel does not list local-name in SupportedIncludes; trying anyway")

        await self._register_app_and_agent()
        await self._register_advertisement()
        self.ready = True
        self.not_ready_reason = ""
        log.info("Bluetooth ready: %s (%s), advertising mode %s, %s", self.device_name, self.address,
                 self.effective_adv_mode, "secure" if self.secure else "INSECURE (no pairing)")
        self._event("ready", name=self.device_name, address=self.address, adv_mode=self.effective_adv_mode,
                    secure=self.secure)
        for path, props in self._devices.items():
            if props.get("Connected"):  # the Mac kept its link across our restart: it has to open the channel
                self._watch_quiet_peer(path)

    async def _best_effort_prop(self, name: str, value: Any) -> None:
        try:
            await self._set_prop(self.adapter_path, IFACE_ADAPTER, name, "b", value)
        except (DBusError, asyncio.TimeoutError) as exc:
            log.warning("could not set adapter %s: %s", name, exc)

    async def _apply_alias(self, props: Dict[str, Any]) -> None:
        alias = str(props.get("Alias", ""))
        if self._prev_alias is None:
            self._prev_alias = alias
            self._alias_was_default = alias == str(props.get("Name", ""))
        if alias != self.device_name:
            await self._set_prop(self.adapter_path, IFACE_ADAPTER, "Alias", "s", self.device_name)

    async def _restore_alias(self) -> None:
        if self._prev_alias is None:
            return
        value = "" if self._alias_was_default else self._prev_alias
        await self._best_effort(
            self.adapter_path, IFACE_PROPS, "Set", "ssv", [IFACE_ADAPTER, "Alias", Variant("s", value)]
        )

    async def _register_app_and_agent(self) -> None:
        try:
            await self._call(self.adapter_path, IFACE_GATT_MGR, "RegisterApplication", "oa{sv}", [APP_ROOT, {}])
        except DBusError as exc:
            if exc.type != "org.bluez.Error.AlreadyExists":
                raise
        if self.secure:
            try:
                await self._call(BLUEZ_ROOT, IFACE_AGENT_MGR, "RegisterAgent", "os", [AGENT_PATH, "DisplayOnly"])
            except DBusError as exc:
                if exc.type != "org.bluez.Error.AlreadyExists":
                    raise
            try:
                await self._call(BLUEZ_ROOT, IFACE_AGENT_MGR, "RequestDefaultAgent", "o", [AGENT_PATH])
            except (DBusError, asyncio.TimeoutError) as exc:
                log.warning("could not become the default pairing agent (%s): another agent, like the "
                            "desktop Bluetooth applet, may ask for the code instead", exc)

    async def _register_advertisement(self) -> None:
        assert self._bus is not None and self._adv_lock is not None
        async with self._adv_lock:  # a release, a disconnect and the supervisor may all ask at once
            if self._adv is not None:
                await self._best_effort(self.adapter_path, IFACE_ADV_MGR, "UnregisterAdvertisement", "o", [ADV_PATH])
                self._bus.unexport(ADV_PATH)
            if self.effective_adv_mode == "kernel":
                self._adv = AdvKernelName(NUS_SERVICE_UUID, self._adv_released)
            else:
                self._adv = AdvName(NUS_SERVICE_UUID, self.device_name, self._adv_released)
            self._bus.export(ADV_PATH, self._adv)
            try:
                await self._call(self.adapter_path, IFACE_ADV_MGR, "RegisterAdvertisement", "oa{sv}", [ADV_PATH, {}])
            except DBusError as exc:
                if exc.type != "org.bluez.Error.AlreadyExists":
                    raise
        self._event("advertising", name=self.device_name, mode=self.effective_adv_mode)

    def _adv_released(self) -> None:
        # BlueZ only releases an advertisement when it tears the adapter down, and it destroys the
        # GATT database in the same breath. So redo everything, not just the advertisement.
        log.info("BlueZ released the advertisement; setting everything up again")
        if self.ready and not self._stopping:
            self._spawn(self._resetup(READVERTISE_DELAY_S))

    async def _resetup(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping:
            return
        self.ready = False
        await self._try_setup()

    async def _supervisor(self) -> None:
        """Keep a 24/7 daemon healthy: retry setup while BlueZ is missing, and set
        everything up again if the advertising instance disappeared."""
        while True:
            await asyncio.sleep(SUPERVISE_NOT_READY_S if not self.ready else SUPERVISE_READY_S)
            if self._stopping:
                return
            if not self.ready:
                await self._try_setup()
                continue
            try:
                props = await self._get_props(self.adapter_path, IFACE_ADV_MGR)
            except (DBusError, asyncio.TimeoutError, OSError):
                self.ready = False
                self._not_ready("BlueZ dejó de contestar")
                continue
            if props.get("ActiveInstances", 1) == 0:
                log.warning("no active advertising instance; setting everything up again")
                await self._resetup(0.0)

    # -- incoming signals ----------------------------------------------------------

    def _on_bus_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return None
        try:
            self._on_signal(msg)
        except Exception:
            log.exception("error handling a D-Bus signal")
        return None

    def _on_signal(self, msg: Message) -> None:
        iface, member = msg.interface, msg.member
        if iface == IFACE_PROPS and member == "PropertiesChanged":
            name, changed, _invalidated = msg.body
            if name == IFACE_DEVICE:
                self._device_changed(msg.path or "", changed)
        elif iface == IFACE_OBJMGR and member == "InterfacesAdded":
            path, interfaces = msg.body
            if IFACE_DEVICE in interfaces:
                self._device_changed(path, interfaces[IFACE_DEVICE])
            elif path == self.adapter_path and IFACE_ADAPTER in interfaces and not self._stopping:
                log.info("adapter %s is back; setting up again", self._adapter)
                self._spawn(self._resetup(RESETUP_DELAY_S))
        elif iface == IFACE_OBJMGR and member == "InterfacesRemoved":
            path, interfaces = msg.body
            if IFACE_DEVICE in interfaces:
                self._device_gone(path)
            elif path == self.adapter_path and IFACE_ADAPTER in interfaces:
                log.warning("adapter %s went away", self._adapter)
                self._not_ready(f"el adaptador {self._adapter} desapareció (¿se desenchufó?)")
                self._link_gone()
                self._devices.clear()
        elif iface == "org.freedesktop.DBus" and member == "NameOwnerChanged":
            name, _old, new = msg.body
            if name == BLUEZ:
                self._bluez_owner_changed(bool(new))

    def _bluez_owner_changed(self, present: bool) -> None:
        if not present:
            log.warning("bluetoothd went away")
            self._not_ready("bluetoothd se detuvo")
            self._link_gone()
            self._devices.clear()
        elif not self._stopping:
            log.info("bluetoothd is back; setting up again")
            self._spawn(self._resetup(RESETUP_DELAY_S))

    def _device_changed(self, path: str, changed: Dict[str, Variant]) -> None:
        if not path.startswith(self.adapter_path + "/"):
            return
        known = self._devices.setdefault(path, {})
        values = {k: v.value for k, v in changed.items()}
        old_address = known.get("Address")
        known.update(values)
        log.debug("device %s changed: %s", path, values)
        label = known.get("Alias") or known.get("Name") or path
        if "Address" in values and path in self._peer_paths:
            # Once the central hands over its identity key, BlueZ keeps the object but swaps the
            # private address it first connected with for the real one.
            if old_address and old_address != values["Address"]:
                self._peers.discard(str(old_address))
            self._peers.add(str(values["Address"]))
        if "Connected" in values:
            classic = "Class" in known  # headsets, keyboards, phones: nothing to do with a BLE buddy
            if values["Connected"]:
                log.info("device connected: %s", label)
                self._kicked.discard(path)
                if not classic:
                    self._event("device_connected", device=path, label=label)
                    self._watch_quiet_peer(path)
            else:
                log.info("device disconnected: %s", label)
                self._unwatch_quiet_peer(path)
                if path in self._kicked:
                    self._kicked.discard(path)  # our doing: the count of this silent spell stands
                else:
                    self._quiet_kicks.pop(path, None)  # it left on its own: next time starts fresh
                if not classic:
                    self._event("device_disconnected", device=path, label=label)
                # An unrelated device going away (a mouse that sleeps) must not end our link or
                # cancel a passkey on screen. Before the first write we cannot tell who is ours,
                # so only act when nobody is connected any more.
                if path == self._active_device or (self._active_device is None and not self._any_connected()):
                    self._link_gone()
        if values.get("Paired") and path in self._peer_paths:
            self._handle_passkey_cancel()
            self._event("paired", device=path, label=label)
            if known.get("Connected"):
                self._watch_quiet_peer(path)  # the code was typed; now it should subscribe

    def _any_connected(self) -> bool:
        return any(d.get("Connected") for d in self._devices.values())

    def _device_gone(self, path: str) -> None:
        self._devices.pop(path, None)
        self._peer_paths.discard(path)
        self._unwatch_quiet_peer(path)
        self._quiet_kicks.pop(path, None)
        self._kicked.discard(path)
        if path == self._active_device:
            self._link_gone()

    def _remember(self, path: str) -> None:
        """This device is talking to the buddy. Remember it, so forgetting never touches
        anything else the Pi has paired."""
        self._peer_paths.add(path)
        address = self._devices.get(path, {}).get("Address")
        if address:
            self._peers.add(str(address))

    def _link_gone(self) -> None:
        self._unwatch_all_quiet_peers()
        was_active = self._active_device is not None or self._tx_subscribed
        self._active_device = None
        self._tx_subscribed = False
        self._mtu = DEFAULT_MTU
        while not self._txq.empty():
            self._txq.get_nowait()
        if self._tx is not None:
            self._tx.reset()
        self._handle_passkey_cancel()
        if was_active:
            self._on_link_down()

    # -- GATT callbacks ------------------------------------------------------------

    def _handle_write(self, data: bytes, options: Dict[str, Any]) -> None:
        device = options.get("device")
        log.debug("RX %d bytes from %s (mtu %s)", len(data), device, options.get("mtu"))
        if device:
            self._active_device = str(device)
            self._remember(self._active_device)
            self._quiet_peer_spoke(self._active_device)
        mtu = options.get("mtu")
        if isinstance(mtu, int) and mtu >= DEFAULT_MTU:
            self._mtu = mtu
        self._on_data(data)

    def _handle_notify_state(self, state: bool) -> None:
        self._tx_subscribed = state
        log.info("desktop %s notifications", "enabled" if state else "disabled")
        if state:
            self._quiet_peer_spoke(None)
        else:
            while not self._txq.empty():
                self._txq.get_nowait()
        self._event("notify", enabled=state)

    def _handle_passkey(self, device: str, passkey: str, entered: int) -> None:
        log.info("pairing: passkey shown for %s", device)
        self._remember(device)
        self._unwatch_quiet_peer(device)  # the person is typing the code; the clock restarts once paired
        if self._on_passkey is not None:
            self._on_passkey(passkey, entered)

    def _handle_passkey_cancel(self) -> None:
        if self._on_passkey is not None:
            self._on_passkey(None, 0)

    # -- a desktop that is connected but never opens the channel ----------------------

    def _is_buddy_peer(self, path: str) -> bool:
        """A device that paired or wrote through the buddy (the desktop), never the Pi's own mouse."""
        props = self._devices.get(path, {})
        if "Class" in props:
            return False
        address = props.get("Address")
        return path in self._peer_paths or bool(address and address in self._peers)

    def _watch_quiet_peer(self, path: str) -> None:
        """Seen with the real Mac (17 Oct 2026): after the daemon or the Pi restarts, the Mac keeps or
        regains its Bluetooth link but the Hardware Buddy bridge never subscribes to TX again, so the
        buddy sleeps with the Mac "connected" for good. What fixes it by hand is `bluetoothctl
        disconnect`: the Mac reconnects within 10-20 s and opens the channel. So a known desktop gets
        QUIET_PEER_TIMEOUT_S to subscribe or write; then we disconnect it, a few times at most."""
        if self._stopping or not self._is_buddy_peer(path):
            return
        self._unwatch_quiet_peer(path)
        self._quiet_timers[path] = self._spawn(self._kick_if_still_quiet(path))

    def _unwatch_quiet_peer(self, path: str) -> None:
        timer = self._quiet_timers.pop(path, None)
        if timer is not None:
            timer.cancel()

    def _unwatch_all_quiet_peers(self) -> None:
        for path in list(self._quiet_timers):
            self._unwatch_quiet_peer(path)

    def _quiet_peer_spoke(self, path: Optional[str]) -> None:
        """The desktop wrote (we know which one) or subscribed (BlueZ does not say which, so all)."""
        if path is None:
            self._unwatch_all_quiet_peers()
            self._quiet_kicks.clear()
        else:
            self._unwatch_quiet_peer(path)
            self._quiet_kicks.pop(path, None)

    async def _kick_if_still_quiet(self, path: str) -> None:
        await asyncio.sleep(QUIET_PEER_TIMEOUT_S)
        if self._quiet_timers.get(path) is asyncio.current_task():
            del self._quiet_timers[path]
        props = self._devices.get(path, {})
        if self._stopping or not self.ready or not props.get("Connected"):
            return
        if self._tx_subscribed or path == self._active_device:
            return  # it did open the channel after all
        label = props.get("Alias") or props.get("Name") or path
        kicks = self._quiet_kicks.get(path, 0)
        if kicks >= QUIET_PEER_MAX_KICKS:
            log.warning("%s sigue conectado y callado después de %d desconexiones; no insisto más "
                        "(Connect en Open Hardware Buddy)", label, kicks)
            return
        self._quiet_kicks[path] = kicks + 1
        log.info("%s está conectado pero callado (%g s sin suscribirse a TX ni escribir); lo desconecto "
                 "para que vuelva a enganchar (%d de %d)", label, QUIET_PEER_TIMEOUT_S, kicks + 1, QUIET_PEER_MAX_KICKS)
        self._kicked.add(path)
        try:
            await self._call(path, IFACE_DEVICE, "Disconnect")
        except (DBusError, asyncio.TimeoutError, OSError) as exc:
            self._kicked.discard(path)
            log.warning("no pude desconectar a %s: %s", label, exc)

    # -- sending -------------------------------------------------------------------

    @property
    def notify_chunk(self) -> int:
        size = self._mtu - 3 if self._mtu > 3 else 20
        return max(20, min(size, MAX_NOTIFY_BYTES))

    def send(self, obj: Dict[str, Any]) -> None:
        if not self._tx_subscribed:
            log.debug("dropping %s: the desktop is not subscribed to TX", obj.get("ack") or obj.get("cmd"))
            return
        self._txq.put_nowait(encode_line(obj))

    async def _tx_pump(self) -> None:
        while True:
            line = await self._txq.get()
            size = self.notify_chunk
            for i in range(0, len(line), size):
                if not self._tx_subscribed or self._tx is None:
                    break
                self._tx.push(line[i : i + size])
                await asyncio.sleep(TX_GAP_S)

    async def drain(self, timeout: float = 2.0) -> None:
        """Wait until everything queued has been handed to BlueZ (tests, shutdown)."""
        deadline = (self._loop.time() if self._loop else 0) + timeout
        while not self._txq.empty() and self._loop and self._loop.time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(TX_GAP_S * 2)

    # -- security ------------------------------------------------------------------

    def is_secure(self) -> bool:
        """True when we require authenticated encryption and the connected device is paired."""
        if not self.secure or self._active_device is None:
            return False
        return bool(self._devices.get(self._active_device, {}).get("Paired"))

    async def unpair(self) -> None:
        """Forget the devices that paired through the buddy (the desktop's Forget button).
        The one currently connected goes last, after the ack had time to leave."""
        connected, others = self._peer_device_paths()
        for path in others:
            await self._remove_device(path)
        if connected and self._loop is not None:
            self._loop.call_later(UNPAIR_GRACE_S, lambda: self._spawn(self._remove_all(connected)))

    async def forget_all(self) -> int:
        """Immediately forget the devices that paired through the buddy. Used by the keyboard."""
        connected, others = self._peer_device_paths()
        await self._remove_all(others + connected)
        return len(others) + len(connected)

    def _peer_device_paths(self) -> "tuple[List[str], List[str]]":
        connected, others = [], []
        for path, props in self._devices.items():
            address = props.get("Address")
            if path == self._active_device or path in self._peer_paths or (address and address in self._peers):
                (connected if props.get("Connected") else others).append(path)
        return connected, others

    async def _remove_all(self, paths: List[str]) -> None:
        for path in paths:
            await self._remove_device(path)

    async def _remove_device(self, path: str) -> None:
        address = self._devices.get(path, {}).get("Address")
        try:
            await self._call(self.adapter_path, IFACE_ADAPTER, "RemoveDevice", "o", [path])
        except DBusError as exc:
            if exc.type != "org.bluez.Error.DoesNotExist":
                raise
        self._devices.pop(path, None)
        self._peer_paths.discard(path)
        if address:
            self._peers.discard(str(address))
        log.info("forgot %s", path)


# --- command line helpers that work without starting the daemon ---------------------------


def _pkg_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "?"


def _probe_rfkill() -> Optional[str]:
    exe = shutil.which("rfkill")
    if exe is None:
        return None
    try:
        return subprocess.run([exe, "list", "bluetooth"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_bluetoothd() -> Optional[str]:
    for exe in (shutil.which("bluetoothd"), "/usr/libexec/bluetooth/bluetoothd", "/usr/lib/bluetooth/bluetoothd"):
        if exe and os.access(exe, os.X_OK):
            try:
                out = subprocess.run([exe, "-v"], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if out:
                return out
    return None


def _rfkill_finding(text: Optional[str]) -> Optional[Tuple[str, str]]:
    if text is None:
        return None
    if "Hard blocked: yes" in text:
        return "fail", "el Bluetooth está bloqueado por hardware (¿interruptor del equipo?)"
    if "Soft blocked: yes" in text:
        return "fail", "el Bluetooth está bloqueado por software: sudo rfkill unblock bluetooth"
    if "Soft blocked: no" in text:
        return "ok", "rfkill: el Bluetooth no está bloqueado"
    return "warn", "rfkill no lista ningún adaptador Bluetooth"


async def diagnose(
    adapter: str = "hci0", bus_address: Optional[str] = None, peers_path: Optional[Path] = None
) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """Read-only health check behind `buddy.py check`.

    Returns ([(level, text), ...], facts) where level is "ok", "warn" or "fail".
    """
    findings: List[Tuple[str, str]] = []
    facts: Dict[str, Any] = {"active_instances": 0}

    def add(level: str, text: str) -> None:
        findings.append((level, text))

    add("ok", f"Python {platform.python_version()}, dbus-fast {_pkg_version('dbus-fast')}")
    rfkill = _rfkill_finding(_probe_rfkill())
    if rfkill is not None:
        add(*rfkill)
    version = _probe_bluetoothd()
    if version:
        add("ok", f"bluetoothd {version}")
    else:
        add("warn", "no encuentro el programa bluetoothd (sudo apt install bluez)")

    try:
        bus = await _open_bus(bus_address)
    except TransportError as exc:
        add("fail", str(exc))
        return findings, facts
    try:
        try:
            objects = await _get_managed_objects(bus)
        except TransportError as exc:
            add("fail", str(exc))
            return findings, facts
        except (DBusError, asyncio.TimeoutError, OSError) as exc:
            add("fail", f"BlueZ no responde bien: {exc}")
            return findings, facts
        add("ok", "D-Bus: bluetoothd responde")

        path = f"{BLUEZ_ROOT}/{adapter}"
        ifaces = objects.get(path, {})
        if IFACE_ADAPTER not in ifaces:
            seen = sorted(p.rsplit("/", 1)[-1] for p, i in objects.items() if IFACE_ADAPTER in i)
            add("fail", f"no existe el adaptador {adapter} (adaptadores que veo: {', '.join(seen) or 'ninguno'}); "
                        "¿rfkill unblock bluetooth? ¿el dongle está enchufado?")
            return findings, facts
        props = {k: v.value for k, v in ifaces[IFACE_ADAPTER].items()}
        add("ok", f"adaptador {adapter}: {props.get('Address', '?')}, nombre {props.get('Alias') or props.get('Name') or '?'}")
        if props.get("Powered") is False:
            add("warn", "el adaptador está apagado; el daemon lo enciende solo")
        roles = props.get("Roles")
        if isinstance(roles, list) and "peripheral" not in roles:
            add("fail", f"el adaptador no puede actuar como periférico (roles: {', '.join(map(str, roles))})")
        missing = [label for label, iface in (("servidor GATT", IFACE_GATT_MGR), ("anuncios BLE", IFACE_ADV_MGR))
                   if iface not in ifaces]
        if missing:
            add("fail", f"BlueZ no ofrece {' ni '.join(missing)} en {adapter}")
            return findings, facts
        add("ok", "el adaptador ofrece servidor GATT y anuncios BLE")

        try:
            adv = await _get_all(bus, path, IFACE_ADV_MGR)
        except (DBusError, asyncio.TimeoutError, OSError) as exc:
            add("warn", f"no pude leer las capacidades de anuncio: {exc}")
            adv = {}
        active, total = int(adv.get("ActiveInstances", 0)), int(adv.get("SupportedInstances", 0))
        facts["active_instances"] = active
        if active:
            add("warn", f"ya hay {active} anuncio(s) BLE activo(s) de {total}: si buddy.py está corriendo, cerralo antes de repetir check")
        else:
            add("ok", f"anuncios BLE libres: {total}")

        peers = PeerBook(peers_path)
        devices = [
            (p, {k: v.value for k, v in i[IFACE_DEVICE].items()})
            for p, i in objects.items()
            if IFACE_DEVICE in i and p.startswith(path + "/")
        ]
        if not devices:
            add("ok", "BlueZ no recuerda ningún dispositivo")
        else:
            mine = 0
            for _path, d in devices:
                address = str(d.get("Address") or "")
                label = d.get("Alias") or d.get("Name") or address
                is_mine = address in peers
                mine += is_mine
                add("ok", f"  - {label}: {'emparejado' if d.get('Paired') else 'sin emparejar'}, "
                          f"{'conectado' if d.get('Connected') else 'desconectado'}"
                          f"{', del buddy' if is_mine else ''}")
            if mine:
                add("warn", f"{mine} dispositivo(s) del buddy recordado(s): si el Mac no logra conectar, "
                            "python3 buddy.py forget-all y también borrá el Pi en el Mac")
        return findings, facts
    finally:
        bus.disconnect()


async def forget_all_devices(
    adapter: str = "hci0", bus_address: Optional[str] = None, peers_path: Optional[Path] = None
) -> Tuple[int, int]:
    """Forget every device that paired through the buddy, without starting the daemon.

    Returns (forgotten, left_alone); the second number counts other bonds BlueZ keeps (keyboards...).
    """
    peers = PeerBook(peers_path)
    bus = await _open_bus(bus_address)
    try:
        objects = await _get_managed_objects(bus)
        path = f"{BLUEZ_ROOT}/{adapter}"
        if IFACE_ADAPTER not in objects.get(path, {}):
            raise TransportError(f"no encuentro el adaptador {adapter}")
        mine: List[Tuple[str, str]] = []
        others = 0
        for p, i in objects.items():
            if IFACE_DEVICE not in i or not p.startswith(path + "/"):
                continue
            address_var = i[IFACE_DEVICE].get("Address")
            address = str(address_var.value) if address_var is not None else ""
            if address and address in peers:
                mine.append((p, address))
            else:
                others += 1
        for p, address in mine:
            try:
                await _bus_call(bus, path, IFACE_ADAPTER, "RemoveDevice", "o", [p])
            except DBusError as exc:
                if exc.type != "org.bluez.Error.DoesNotExist":
                    raise
            peers.discard(address)
        return len(mine), others
    finally:
        bus.disconnect()
