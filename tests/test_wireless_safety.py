"""Write interruption and identity checks, using a fixture instead of hardware."""
import asyncio
import plistlib

import pytest

from homescreen.device import FileSource
from homescreen.model import LayoutError
from homescreen.session import Session
from fake_phone import build


class Phone(FileSource):
    identity = "A"
    writes = 0
    fail = False
    unreadable = False
    landed = True

    async def info(self):
        return {"UniqueDeviceID": self.identity, "DeviceName": "Pretend phone"}

    async def read(self):
        if self.unreadable:
            raise OSError("Wi-Fi disconnected")
        return await super().read()

    async def write(self, *args):
        self.writes += 1
        if self.landed:
            await super().write(*args)
        if self.fail:
            self.unreadable = True
            raise OSError("response lost")


def setup(tmp_path):
    path = tmp_path / "phone.plist"
    build(path, as_dict=True)
    source = Phone(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move(lay.pages[2][1].id, "library"))
    return session, source


@pytest.mark.parametrize("identity", ["B", None])
@pytest.mark.parametrize("action", ["apply", "restore"])
def test_wrong_or_unknown_identity_never_writes(tmp_path, identity, action):
    session, source = setup(tmp_path)
    state, library = asyncio.run(source.read())
    backup = session._backup(state, library, "test")
    source.identity = identity
    with pytest.raises(LayoutError, match="identity|different device"):
        asyncio.run(session.apply(session.require().token()) if action == "apply" else session.restore(backup.name))
    assert source.writes == 0


def test_interrupted_write_is_persistent_and_requires_explicit_read(tmp_path):
    session, source = setup(tmp_path)
    source.fail = True
    token = session.require().token()
    result = asyncio.run(session.apply(token))
    assert result["ok"] is False and result["outcome"] == "unknown"
    assert result["needs_read"] and result["backup"] in session.backups()
    assert source.writes == 1
    source.unreadable = source.fail = False
    restarted = Session(source, session.dir)
    with pytest.raises(LayoutError, match="read|Read"):
        asyncio.run(restarted.apply(restarted.require().token()))
    with pytest.raises(LayoutError, match="read|Read"):
        asyncio.run(restarted.restore(result["backup"]))
    assert source.writes == 1
    asyncio.run(restarted.read())
    assert not restarted.needs_read
    assert source.writes == 1  # reading/reconnecting never reapplies


def test_successful_send_but_unreadable_verification_is_unknown(tmp_path):
    session, source = setup(tmp_path)
    original_write = source.write
    async def write(*args):
        await original_write(*args)
        source.unreadable = True
    source.write = write
    result = asyncio.run(session.apply(session.require().token()))
    assert result["outcome"] == "unknown" and session.needs_read
    assert source.writes == 1 and result["backup"] in session.backups()


def test_backup_records_device_and_rejects_copied_backup(tmp_path):
    session, source = setup(tmp_path)
    state, library = asyncio.run(source.read())
    first = session._backup(state, library, "test")
    second = session._backup(state, library, "test")
    assert first != second
    blob = plistlib.loads(first.read_bytes())
    assert blob["device"] == "A"
    blob["device"] = "B"
    first.write_bytes(plistlib.dumps(blob))
    with pytest.raises(LayoutError, match="different device"):
        asyncio.run(session.restore(first.name))
    assert source.writes == 0


def test_concurrent_read_cannot_clear_pending_write(tmp_path):
    session, source = setup(tmp_path)
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        original = source.write
        async def delayed(*args):
            entered.set()
            await release.wait()
            await original(*args)
        source.write = delayed
        task = asyncio.create_task(session.apply(session.require().token()))
        await entered.wait()
        other = Session(source, session.dir)
        try:
            with pytest.raises(LayoutError, match="busy|progress"):
                await other.read()
        finally:
            release.set()
            await task
        assert source.writes == 1
    asyncio.run(check())


def test_cancellation_during_write_requires_read_after_restart(tmp_path):
    session, source = setup(tmp_path)
    async def cancelled(*args):
        source.writes += 1
        raise asyncio.CancelledError
    source.write = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.apply(session.require().token()))
    restarted = Session(source, session.dir)
    assert restarted.needs_read and source.writes == 1


def test_draft_identity_pins_unselected_auto_source_after_restart(tmp_path):
    from homescreen.device import PhoneSource
    session, _ = setup(tmp_path)
    phone = PhoneSource(transport="auto")
    restarted = Session(phone, session.dir)
    assert restarted.device_id == "A" and phone.udid == "A"


def test_remote_info_with_missing_optional_values_can_be_saved(tmp_path):
    path = tmp_path / "phone.plist"
    build(path, as_dict=True)
    source = Phone(path)
    async def info():
        return {"UniqueDeviceID": "A", "DeviceName": None, "ProductType": None, "ProductVersion": "17.4"}
    source.info = info
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    assert Session(source, session.dir).info == {"ProductVersion": "17.4"}
