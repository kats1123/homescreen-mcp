"""Device selection and short-lived USB / RemotePairing connections.

No reads or reconnects write a layout. Wi-Fi never pairs implicitly. See
THIRD_PARTY_NOTICES.md for the userspace provider adapter's source attribution.
"""
from __future__ import annotations

import asyncio
import contextlib
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

MODES = ("usb", "wifi", "auto")
TIMEOUT = 30.0
DISCOVERY_TIMEOUT = 5.0
_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class ConnectionError(RuntimeError):
    pass


def _lock() -> asyncio.Lock:
    # CLI/tests may use more than one event loop in a process.
    loop = asyncio.get_running_loop()
    return _locks.setdefault(loop, asyncio.Lock())


async def _close(close: Callable[[], Awaitable[Any]]) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(close(), 5)


def device_values(provider: Any) -> dict[str, Any]:
    values = dict(provider.all_values)
    props = (getattr(provider, "peer_info", None) or {}).get("Properties", {})
    for key, fallback in (("UniqueDeviceID", "UniqueDeviceID"), ("ProductType", "ProductType"),
                          ("ProductVersion", "OSVersion"), ("DeviceName", "DeviceName")):
        if not values.get(key):
            values[key] = props.get(fallback)
    # Identity comes from the connected, authenticated provider, never the requested UDID.
    values["UniqueDeviceID"] = getattr(provider, "udid", None) or values.get("UniqueDeviceID")
    return values


@dataclass
class Link:
    provider: Any
    close: Callable[[], Awaitable[Any]]
    transport: str


class PymobiledeviceBackend:
    async def usb_devices(self) -> list[str]:
        from pymobiledevice3 import usbmux
        return sorted({d.serial for d in await usbmux.list_devices() if d.connection_type == "USB"})

    async def _wifi_services(self, udid: str | None = None):
        from pymobiledevice3.remote import tunnel_service
        acquired = []
        original = tunnel_service.create_core_device_tunnel_service_using_remotepairing

        async def owned_service(remote_identifier, hostname, port, autopair=False):
            # Own the socket before connect: the library's collector otherwise
            # loses partial/acquired connections when its browse is cancelled.
            service = tunnel_service.RemotePairingTunnelService(remote_identifier, hostname, port)
            acquired.append(service)
            try:
                await service.connect(autopair=False)  # discovery NEVER starts pairing
            except BaseException:
                await _close(service.close)
                raise
            return service

        # All callers hold our operation lock; scope this library adapter to the browse.
        tunnel_service.create_core_device_tunnel_service_using_remotepairing = owned_service
        try:
            result = await tunnel_service.get_remote_pairing_tunnel_services(
                bonjour_timeout=DISCOVERY_TIMEOUT, udid=udid)
        except BaseException:
            for service in acquired:
                await _close(service.close)
            raise
        finally:
            tunnel_service.create_core_device_tunnel_service_using_remotepairing = original
        return result

    async def wifi_devices(self) -> list[str]:
        services = await self._wifi_services()
        try:
            return sorted({s.remote_identifier for s in services})
        finally:
            for service in services:
                await _close(service.close)

    async def usb(self, udid: str) -> Link:
        from pymobiledevice3.lockdown import create_using_usbmux
        provider = await create_using_usbmux(serial=udid, connection_type="USB", autopair=True, pair_timeout=15)
        return Link(provider, provider.close, "usb")

    async def describe(self, udid: str, methods: list[str]) -> dict[str, Any]:
        if "usb" not in methods:
            return {}  # Wi-Fi names come from the device's saved draft after its first read
        from pymobiledevice3.lockdown import create_using_usbmux
        provider = None
        try:
            async with asyncio.timeout(3):
                provider = await create_using_usbmux(serial=udid, connection_type="USB", autopair=False)
                values = device_values(provider)
                if values.get("UniqueDeviceID") != udid:
                    return {}
                return {"name": values.get("DeviceName"), "model": values.get("ProductType"),
                        "version": values.get("ProductVersion")}
        except Exception:
            return {}  # discovery must still list an untrusted/locked device, without pairing
        finally:
            if provider is not None:
                await _close(provider.close)

    async def wifi(self, udid: str) -> Link:
        from pymobiledevice3.remote import userspace_tunnel
        services = await self._wifi_services(udid)
        if not services:
            raise ConnectionError("Paired device did not answer over Wi-Fi. Unlock it on the same network; "
                                  "allow Local Network access, or pair again over USB.")
        service = services[0]
        for extra in services[1:]:
            await _close(extra.close)
        if service.remote_identifier != udid:
            await _close(service.close)
            raise ConnectionError("Wi-Fi pairing identity does not match the selected device")

        # UserspaceRsdTunnel otherwise prefers USB even in explicit Wi-Fi mode.
        # Supply the authenticated RemotePairing provider only during aopen, under
        # the process-wide operation lock; restore the library hook on every exit.
        async def provider(serial, autopair, remotepairing_fallback=True):
            return service, None

        tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
        original = userspace_tunnel._create_no_root_tunnel_provider
        userspace_tunnel._create_no_root_tunnel_provider = provider
        try:
            rsd = await tunnel.aopen()
        except BaseException:
            await _close(tunnel.aclose)
            await _close(service.close)
            raise
        finally:
            userspace_tunnel._create_no_root_tunnel_provider = original
        return Link(rsd, tunnel.aclose, "wifi")

    async def pair(self, provider: Any) -> None:
        from pymobiledevice3.exceptions import RemotePairingCompletedError
        from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService
        if int(str(device_values(provider).get("ProductVersion") or "0").split(".")[0]) < 17:
            raise ConnectionError("Wi-Fi requires iOS or iPadOS 17 or later")
        await provider.set_enable_wifi_connections(True)
        service = await RemotePairingLockdownService.create(provider)
        try:
            try:
                await service.connect(autopair=True)
            except RemotePairingCompletedError:
                pass  # the library saved the record before the phone closed the channel
        finally:
            await _close(service.close)


async def discover_devices(backend=None) -> list[dict[str, Any]]:
    backend = backend or PymobiledeviceBackend()
    async with _lock():
        try:
            async with asyncio.timeout(TIMEOUT):
                usb = await backend.usb_devices()
                wifi = await backend.wifi_devices()
                devices = []
                for udid in sorted(set(usb) | set(wifi)):
                    methods = [m for m, ids in (("usb", usb), ("wifi", wifi)) if udid in ids]
                    detail = await backend.describe(udid, methods) if hasattr(backend, "describe") else {}
                    devices.append({"udid": udid, "methods": methods, **detail})
        except TimeoutError as e:
            raise ConnectionError("Device discovery timed out; check USB and Local Network access") from e
    return devices


class PhoneConnection:
    def __init__(self, udid: str | None = None, mode: str = "usb", *, backend=None, timeout=TIMEOUT):
        if mode not in MODES:
            raise ValueError(f"connection must be one of {', '.join(MODES)}")
        self.udid, self.mode = udid, mode
        self.backend = backend or PymobiledeviceBackend()
        self.timeout = timeout
        self.transport: str | None = None
        self.state = "disconnected"
        self.error: str | None = None

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "udid": self.udid, "transport": self.transport,
                "state": self.state, "error": self.error}

    async def _select(self, mode: str) -> tuple[str, str]:
        usb = await self.backend.usb_devices() if mode != "wifi" else []
        if self.udid:
            if self.udid in usb:
                return self.udid, "usb"
            if mode == "usb":
                raise ConnectionError("Selected device is not connected over USB. Unlock and trust this Mac first.")
            return self.udid, "wifi"
        ids = usb or (await self.backend.wifi_devices() if mode != "usb" else [])
        if len(ids) != 1:
            raise ConnectionError("Select a device by UDID; expected one available device, "
                                  f"found {len(ids)}. For Wi-Fi, pair over trusted USB first.")
        return ids[0], "usb" if usb else "wifi"

    @asynccontextmanager
    async def open(self, *, mode: str | None = None):
        # Serialize the whole service operation, not just tunnel creation. USB calls
        # also wait so no other connection observes the temporary provider adapter.
        async with _lock():
            link = None
            self.state, self.error = "connecting", None
            try:
                async with asyncio.timeout(self.timeout):
                    udid, kind = await self._select(mode or self.mode)
                    link = await (self.backend.usb(udid) if kind == "usb" else self.backend.wifi(udid))
                    actual = device_values(link.provider).get("UniqueDeviceID")
                    if not actual or actual != udid:
                        raise ConnectionError("Connected device identity does not match the selected device")
                    self.udid, self.transport = actual, kind
                    self.state = "connected"
                    yield link.provider
                self.state = "ready"  # last operation succeeded; socket has been released
            except asyncio.CancelledError:
                self.state = "disconnected"
                raise
            except Exception as e:
                self.state = "disconnected"
                self.error = ("Connection or operation timed out; reconnect and read before writing."
                              if isinstance(e, TimeoutError) else str(e))
                raise ConnectionError(self.error) from e
            finally:
                if link is not None:
                    await _close(link.close)

    async def pair_wifi(self) -> dict[str, Any]:
        async with self.open(mode="usb") as provider:
            await self.backend.pair(provider)
        return {"ok": True, "udid": self.udid, "message": "Wi-Fi paired. Unplug USB, select Wi-Fi and reconnect."}
