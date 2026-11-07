"""Shared setup for the CLI, the MCP server and the app: where data lives, which device to use."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .device import FileSource, PhoneSource, SimSource, Source
from .session import Session

DATA = Path(os.environ.get("HOMESCREEN_DATA") or Path.home() / "Library/Application Support/homescreen-mcp")


def booted_sim() -> str:
    devs = json.loads(subprocess.run(["xcrun", "simctl", "list", "devices", "booted", "-j"],
                                     capture_output=True, text=True, check=True).stdout)["devices"]
    booted = [d["udid"] for items in devs.values() for d in items if d["state"] == "Booted"]
    if len(booted) != 1:
        raise RuntimeError(f"HOMESCREEN_SIM=booted needs exactly one booted simulator, found {len(booted)}")
    return booted[0]


def make_source() -> Source:
    """HOMESCREEN_FIXTURE (a saved layout) > HOMESCREEN_SIM (booted|udid) > selected iPhone/iPad transport."""
    fixture, sim = os.environ.get("HOMESCREEN_FIXTURE"), os.environ.get("HOMESCREEN_SIM")
    if fixture:
        return FileSource(fixture)
    if sim:
        return SimSource(booted_sim() if sim == "booted" else sim)
    return PhoneSource(os.environ.get("HOMESCREEN_UDID"), transport=os.environ.get("HOMESCREEN_TRANSPORT", "usb"))


_session: Session | None = None

# The app (and `homescreen ui`) can switch between connected devices, each with its own draft.
# The CLI and MCP server keep one session for the device the environment picks.
_switching = False
_mode = os.environ.get("HOMESCREEN_TRANSPORT", "usb")
_sessions: dict[str, Session] = {}
_current: str | None = None
LAST_DEVICE = DATA / "last-device.txt"


def enable_device_switching() -> None:
    global _switching, _current, _mode
    _mode = os.environ.get("HOMESCREEN_TRANSPORT", "usb")
    if os.environ.get("HOMESCREEN_FIXTURE") or os.environ.get("HOMESCREEN_SIM") or os.environ.get("HOMESCREEN_UDID"):
        return  # a pinned source: nothing to switch between
    _switching = True
    _adopt_shared_draft()
    if LAST_DEVICE.exists():
        _current = LAST_DEVICE.read_text().strip() or None


def _adopt_shared_draft() -> None:
    """Carry the single shared draft (CLI / older versions) over as its device's own draft."""
    import plistlib
    import shutil
    shared = DATA / "draft.plist"
    try:
        dev = plistlib.loads(shared.read_bytes()).get("device") if shared.exists() else None
    except Exception:
        return
    if dev and dev != "unselected":
        target = DATA / "drafts" / f"{''.join(c if c.isalnum() or c in '-._' else '_' for c in dev)}.plist"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shared, target)


def switching() -> bool:
    return _switching


def current_device() -> str | None:
    return _current if _switching else None


def select_device(udid: str) -> Session:
    global _current
    if session().busy:
        raise ValueError("A device operation is in progress; wait before switching devices")
    _current = udid
    DATA.mkdir(parents=True, exist_ok=True)
    LAST_DEVICE.write_text(udid)
    return session()


def known_devices() -> dict[str, dict]:
    """Devices with a saved draft: {udid: {"name", "version", "model"}}."""
    import plistlib
    out = {}
    for f in sorted((DATA / "drafts").glob("*.plist")) if (DATA / "drafts").exists() else []:
        try:
            blob = plistlib.loads(f.read_bytes())
        except Exception:
            continue
        info = blob.get("info", {})
        if blob.get("device"):
            out[blob["device"]] = {"name": info.get("DeviceName"), "version": info.get("ProductVersion"),
                                   "model": info.get("ProductType")}
    return out


def session() -> Session:
    global _session
    if _switching:
        key = _current or ""
        if key not in _sessions:
            _sessions[key] = Session(PhoneSource(_current, transport=_mode), DATA, device=_current or "unselected")
        return _sessions[key]
    if _session is None:
        _session = Session(make_source(), DATA)
    return _session


def connection_status() -> dict:
    s = session()
    if isinstance(s.source, PhoneSource):
        return {**s.source.connection.status(), "needs_read": s.needs_read}
    return {"mode": "fixture" if isinstance(s.source, FileSource) else "simulator",
            "state": "ready", "transport": None, "needs_read": s.needs_read}


def configure_connection(mode: str, udid: str | None = None) -> Session:
    """Select a transport/device without reading, discarding or applying a draft."""
    global _session, _mode
    from .transport import MODES
    if mode not in MODES:
        raise ValueError(f"connection must be one of {', '.join(MODES)}")
    s = session()
    if s.busy:
        raise ValueError("A device operation is in progress; wait before changing connection")
    if not isinstance(s.source, PhoneSource):
        raise ValueError("Connection controls apply only to a physical iPhone or iPad")
    if udid and udid != s.source.udid:
        if _switching:
            s = select_device(udid)
        else:
            _session = s = Session(PhoneSource(udid, transport=mode), DATA, device=udid)
    _mode = mode
    s.source.connection.mode = mode
    s.source.connection.state = "disconnected"
    s.source.connection.error = None
    return s


async def reconnect() -> dict:
    """Probe the selected connection. Never reads/replaces a draft or clears recovery."""
    s = session()
    if not isinstance(s.source, PhoneSource):
        raise ValueError("Reconnect applies only to a physical iPhone or iPad")
    # Opening always rediscovers the same UDID and closes the operation's sockets.
    async with s.source.connection.open():
        pass
    return connection_status()


async def pair_wifi() -> dict:
    s = session()
    if not isinstance(s.source, PhoneSource):
        raise ValueError("Wi-Fi pairing requires a trusted USB iPhone or iPad")
    return await s.source.connection.pair_wifi()
