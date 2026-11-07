"""Transport policy tests: no physical device is contacted."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from homescreen import transport


class Backend:
    def __init__(self, usb=(), wifi=(), actual=None):
        self.usb_ids, self.wifi_ids = list(usb), list(wifi)
        self.actual = actual
        self.calls = []
        self.closed = []

    async def usb_devices(self):
        return self.usb_ids

    async def wifi_devices(self):
        return self.wifi_ids

    async def usb(self, udid):
        self.calls.append(("usb", udid))
        return self.link(udid, "usb")

    async def wifi(self, udid):
        self.calls.append(("wifi", udid))
        return self.link(udid, "wifi")

    def link(self, udid, kind):
        async def close():
            self.closed.append((kind, udid))
        return transport.Link(SimpleNamespace(all_values={"UniqueDeviceID": self.actual or udid},
                                              udid=self.actual or udid), close, kind)

    async def pair(self, provider):
        self.calls.append(("pair", provider.udid))


def test_auto_prefers_usb_then_reconnects_same_phone():
    async def check():
        backend = Backend(usb=["A"], wifi=["A", "B"])
        connection = transport.PhoneConnection(mode="auto", backend=backend)
        async with connection.open() as provider:
            assert provider.udid == "A"
        backend.usb_ids = ["B"]
        async with connection.open() as provider:
            assert provider.udid == "A"
        assert backend.calls == [("usb", "A"), ("wifi", "A")]
        assert backend.closed == backend.calls
        assert connection.status()["transport"] == "wifi"
    asyncio.run(check())


@pytest.mark.parametrize("mode", ["usb", "wifi", "auto"])
def test_ambiguous_device_selection_refused(mode):
    async def check():
        backend = Backend(usb=["A", "B"], wifi=["A", "B"])
        with pytest.raises(transport.ConnectionError, match="Select"):
            async with transport.PhoneConnection(mode=mode, backend=backend).open():
                pytest.fail("must not connect")
        assert backend.calls == []
    asyncio.run(check())


def test_wifi_mode_never_opens_usb_and_discovery_merges_transports():
    async def check():
        backend = Backend(usb=["A"], wifi=["A", "B"])
        devices = await transport.discover_devices(backend=backend)
        assert devices == [{"udid": "A", "methods": ["usb", "wifi"]},
                           {"udid": "B", "methods": ["wifi"]}]
        async with transport.PhoneConnection("A", "wifi", backend=backend).open():
            pass
        assert backend.calls == [("wifi", "A")]
    asyncio.run(check())


def test_identity_mismatch_closes_connection_before_service_use():
    async def check():
        backend = Backend(usb=["A"], actual="B")
        connection = transport.PhoneConnection("A", backend=backend)
        with pytest.raises(transport.ConnectionError, match="identity"):
            async with connection.open():
                pytest.fail("wrong phone")
        assert backend.closed == [("usb", "A")]
        assert connection.status()["state"] == "disconnected"
    asyncio.run(check())


def test_pairing_is_usb_only_and_does_not_change_selected_mode():
    async def check():
        backend = Backend(usb=["A"], wifi=["B"])
        connection = transport.PhoneConnection("A", "wifi", backend=backend)
        result = await connection.pair_wifi()
        assert result["udid"] == "A"
        assert backend.calls == [("usb", "A"), ("pair", "A")]
        assert connection.mode == "wifi"
    asyncio.run(check())


def test_timeout_and_cancellation_do_not_leave_live_connection():
    async def check():
        backend = Backend(usb=["A"])
        connection = transport.PhoneConnection("A", backend=backend, timeout=0.01)
        async def stalled(udid):
            await asyncio.sleep(5)
        backend.usb = stalled
        with pytest.raises(transport.ConnectionError, match="timed out"):
            async with connection.open():
                pass
        backend.usb = lambda udid: Backend.usb(backend, udid)
        with pytest.raises(asyncio.CancelledError):
            async with connection.open():
                raise asyncio.CancelledError
        assert backend.closed == [("usb", "A")]
    asyncio.run(check())


def test_auto_does_not_fallback_after_usb_authentication_failure():
    async def check():
        backend = Backend(usb=["A"], wifi=["A"])
        async def fail(udid):
            raise RuntimeError("trust denied")
        backend.usb = fail
        with pytest.raises(transport.ConnectionError, match="trust denied"):
            async with transport.PhoneConnection("A", "auto", backend=backend).open():
                pass
        assert backend.calls == []
    asyncio.run(check())


def test_phone_source_uses_same_payload_over_both_transports(monkeypatch):
    from homescreen.device import PhoneSource
    from fake_phone import app, widget
    state = [[app("dock", "Dock")], [widget("Weather", "small"), app("home", "Home")]]
    library = [app("hidden", "Hidden")]
    writes = []
    class Springboard:
        def __init__(self, lockdown):
            self.provider = lockdown
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def set_icon_state(self, payload):
            writes.append(payload)
        async def get_icon_state(self, format_version):
            return state
        async def get_homescreen_icon_metrics(self):
            return {}
        async def get_icon_pngdata(self, bundle):
            return b"icon"
    class Apps(Springboard):
        async def get_apps(self, **kwargs):
            assert kwargs["show_placeholders"]
            return {"hidden": {"CFBundleDisplayName": "Hidden"}, "home": {}, "dock": {}}
    monkeypatch.setattr("pymobiledevice3.services.springboard.SpringBoardServicesService", Springboard)
    monkeypatch.setattr("pymobiledevice3.services.installation_proxy.InstallationProxyService", Apps)
    async def check():
        for mode in ("usb", "wifi", "auto"):
            backend = Backend(usb=["A"], wifi=["A"])
            source = PhoneSource("A", transport=mode)
            source.connection.backend = backend
            assert (await source.info())["UniqueDeviceID"] == "A"
            got, lib = await source.read()
            assert got == state and [x["bundleIdentifier"] for x in lib] == ["hidden"]
            await source.write(got, lib, [{"home": 6}])
            assert await source.icon_png("home") == b"icon"
            assert len(backend.closed) == len(backend.calls)
        assert writes[0] == {k: v for k, v in writes[1].items() if k not in ("listUniqueIdentifiers", "listMetadata")} | {
            "listUniqueIdentifiers": writes[0]["listUniqueIdentifiers"], "listMetadata": writes[0]["listMetadata"]}
        for payload in writes:
            assert payload["ignored"] == ["hidden"]
            assert payload["iconLists"][0][0] == state[1][0]
            assert list(payload["listMetadata"].values())[0]["fixedLocations"] == {"home": 6}
    asyncio.run(check())


def test_library_backend_usb_filters_network_devices_and_passes_serial(monkeypatch):
    async def check():
        calls = []
        async def devices():
            return [SimpleNamespace(serial="A", connection_type="USB"),
                    SimpleNamespace(serial="B", connection_type="Network")]
        async def create(**kwargs):
            calls.append(kwargs)
            async def close():
                pass
            return SimpleNamespace(close=close)
        monkeypatch.setattr("pymobiledevice3.usbmux.list_devices", devices)
        monkeypatch.setattr("pymobiledevice3.lockdown.create_using_usbmux", create)
        backend = transport.PymobiledeviceBackend()
        assert await backend.usb_devices() == ["A"]
        await backend.usb("A")
        assert calls[0]["serial"] == "A" and calls[0]["connection_type"] == "USB"
    asyncio.run(check())


def test_library_wifi_adapter_forces_remote_provider_and_restores_hook(monkeypatch):
    from pymobiledevice3.remote import userspace_tunnel
    async def check():
        closed = []
        class Service:
            remote_identifier = "A"
            async def close(self):
                closed.append("service")
        service = Service()
        class Tunnel:
            def __init__(self, **kwargs):
                assert kwargs == {"serial": "A", "autopair": False}
            async def aopen(self):
                assert await userspace_tunnel._create_no_root_tunnel_provider("A", False) == (service, None)
                return SimpleNamespace(udid="A")
            async def aclose(self):
                closed.append("tunnel")
        monkeypatch.setattr(userspace_tunnel, "UserspaceRsdTunnel", Tunnel)
        original = userspace_tunnel._create_no_root_tunnel_provider
        backend = transport.PymobiledeviceBackend()
        async def services(udid=None):
            assert udid == "A"
            return [service]
        monkeypatch.setattr(backend, "_wifi_services", services)
        link = await backend.wifi("A")
        assert link.transport == "wifi" and userspace_tunnel._create_no_root_tunnel_provider is original
        await link.close()
        async def fail(self):
            raise OSError("network lost")
        monkeypatch.setattr(Tunnel, "aopen", fail)
        with pytest.raises(OSError, match="network lost"):
            await backend.wifi("A")
        assert userspace_tunnel._create_no_root_tunnel_provider is original
        assert closed == ["tunnel", "tunnel", "service"]
    asyncio.run(check())


def test_library_pairing_enables_wifi_and_closes_service(monkeypatch):
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService
    async def check():
        calls = []
        class Provider:
            udid = "A"
            all_values = {"ProductVersion": "17.4"}
            async def set_enable_wifi_connections(self, value):
                calls.append(("enable", value))
        class Service:
            async def connect(self, autopair):
                calls.append(("pair", autopair))
                raise RemotePairingCompletedError()
            async def close(self):
                calls.append("close")
        async def create(provider):
            return Service()
        monkeypatch.setattr(RemotePairingLockdownService, "create", create)
        await transport.PymobiledeviceBackend().pair(Provider())
        assert calls == [("enable", True), ("pair", True), "close"]
    asyncio.run(check())


def test_connection_operations_are_serialized():
    async def check():
        backend = Backend(wifi=["A", "B"])
        entered = asyncio.Event()
        async def first():
            async with transport.PhoneConnection("A", "wifi", backend=backend).open():
                entered.set()
                await asyncio.sleep(.01)
                assert backend.calls == [("wifi", "A")]
        async def second():
            await entered.wait()
            async with transport.PhoneConnection("B", "wifi", backend=backend).open():
                assert backend.closed == [("wifi", "A")]
        await asyncio.gather(first(), second())
        assert backend.closed == [("wifi", "A"), ("wifi", "B")]
    asyncio.run(check())


def test_usb_discovery_retains_names_without_pairing(monkeypatch):
    async def check():
        calls, closed = [], []
        async def create(**kwargs):
            calls.append(kwargs)
            async def close():
                closed.append(True)
            return SimpleNamespace(udid="A", all_values={"DeviceName": "Test iPad", "ProductType": "iPad14,5",
                                                       "UniqueDeviceID": "A"}, close=close)
        monkeypatch.setattr("pymobiledevice3.lockdown.create_using_usbmux", create)
        backend = transport.PymobiledeviceBackend()
        async def usb():
            return ["A"]
        async def wifi():
            return []
        monkeypatch.setattr(backend, "usb_devices", usb)
        monkeypatch.setattr(backend, "wifi_devices", wifi)
        result = await transport.discover_devices(backend)
        assert result[0]["name"] == "Test iPad"
        assert calls[0]["autopair"] is False and closed == [True]
    asyncio.run(check())


@pytest.mark.parametrize("during_connect", [False, True])
def test_cancelled_bonjour_discovery_closes_acquired_and_partial_services(monkeypatch, during_connect):
    from pathlib import Path
    from pymobiledevice3.remote import tunnel_service
    async def check():
        acquired, closed = [], []
        class Service:
            def __init__(self, identifier, hostname, port):
                self.remote_identifier = identifier
                acquired.append(self)
            async def connect(self, autopair=False):
                if during_connect:
                    await asyncio.sleep(5)
            async def close(self):
                closed.append(self)
        async def browse(timeout):
            yield SimpleNamespace(addresses=[SimpleNamespace(full_ip="192.0.2.1")], port=1234)
            await asyncio.sleep(5)
        monkeypatch.setattr(tunnel_service, "RemotePairingTunnelService", Service)
        monkeypatch.setattr(tunnel_service, "iter_remote_pair_records_by_identifier",
                            lambda: [("A", Path("/unused"), {"peer_alt_irk": b"test"})])
        monkeypatch.setattr(tunnel_service, "_match_remote_pair_record", lambda *args: "A")
        monkeypatch.setattr(tunnel_service, "iter_browse_remotepairing", browse)
        original = tunnel_service.create_core_device_tunnel_service_using_remotepairing
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(transport.PymobiledeviceBackend().wifi_devices(), .01)
        assert acquired and all(service in closed for service in acquired)
        assert tunnel_service.create_core_device_tunnel_service_using_remotepairing is original
    asyncio.run(check())


def test_dictionary_layout_preserves_folders_stacks_and_unknown_fields(monkeypatch):
    from homescreen.device import PhoneSource
    from fake_phone import folder, app, widget
    stack = widget("Stack", "small")
    stack["elements"].append({"containerBundleIdentifier": "com.apple.calendar", "kind": "Day"})
    stack["futureConfiguration"] = {"nested": [1, 2, 3]}
    raw = {"buttonBar": ["dock"], "iconLists": [[stack, folder("Folder", [app("nested", "Nested")])]],
           "ignored": ["library"], "futureArchiveKey": {"keep": "untouched"}}
    sent = []
    class Springboard:
        def __init__(self, lockdown):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get_icon_state(self, format_version):
            return raw
        async def set_icon_state(self, payload):
            sent.append(payload)
    monkeypatch.setattr("pymobiledevice3.services.springboard.SpringBoardServicesService", Springboard)
    async def check():
        for mode in ("usb", "wifi"):
            source = PhoneSource("A", transport=mode)
            source.connection.backend = Backend(usb=["A"], wifi=["A"])
            state, library = await source.read()
            await source.write(state, library)
        assert sent == [raw, raw]
    asyncio.run(check())
