"""Draft, backups and the guarded apply.

Rules:
- Edits only ever touch the draft. The phone is written by apply() and restore() alone.
- apply() needs the token from the latest diff, refuses if the draft has problems or the
  phone changed since it was read, backs up first, verifies after, and never retries.
"""

from __future__ import annotations

import copy
import fcntl
import functools
import os
import datetime as dt
import json
import uuid
import plistlib
from pathlib import Path
from typing import Any, Callable

from . import colors
from .device import Source
from .model import Grid, Layout, LayoutError, Node, structure

KEEP_READS = 10
MAX_UNDO = 200


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _pack(state: list[Any], library: list[Any] | None, pins: list[dict[str, int]] | None = None) -> bytes:
    blob: dict[str, Any] = {"state": state}
    if library is not None:
        blob["library"] = library
    if pins:
        blob["pins"] = pins
    return plistlib.dumps(blob, fmt=plistlib.FMT_BINARY)


def _unpack(data: bytes) -> tuple[list[Any], list[Any] | None]:
    blob = plistlib.loads(data)
    if isinstance(blob, list):  # backups from before the App Library was tracked
        return blob, None
    return blob["state"], blob.get("library")


def _unpack_pins(data: bytes) -> list[dict[str, int]] | None:
    blob = plistlib.loads(data)
    return blob.get("pins") if isinstance(blob, dict) else None


# ---- gaps (pinned cells) --------------------------------------------------------------
# In the dict format a pinned item is named by its identifier: bundle id for apps, the icon's
# uniqueIdentifier for widgets and folders.

def pin_key(node: Node) -> str | None:
    if node.kind in ("app", "webclip"):
        return node.bundle_id or node.id
    if node.kind == "widget":
        return node.raw.get("uniqueIdentifier") or node.raw.get("userSelectedElementIdentifier")
    if node.kind == "folder":
        return node.raw.get("uniqueIdentifier")
    return None


def memory_key(node: Node) -> str | None:
    """How a gap is remembered across phone reads. Phone folders come back without ids, so
    folders are remembered by name."""
    return f"folder:{node.name}" if node.kind == "folder" else pin_key(node)


def pins_by_page(lay: Layout) -> list[dict[str, int]]:
    """Per page, {identifier: cell} for the dict format. Pinned folders without an id get one."""
    out = []
    for page in lay.pages:
        d = {}
        for n in page:
            if n.id not in lay.pins:
                continue
            if n.kind == "folder" and not n.raw.get("uniqueIdentifier"):
                n.raw["uniqueIdentifier"] = str(uuid.uuid4()).upper()
            key = pin_key(n)
            if key:
                d[key] = lay.pins[n.id]
        out.append(d)
    return out


def node_pins(lay: Layout, keyed: dict[str, int], by_memory: bool = False) -> dict[str, int]:
    """{identifier or memory key: cell} -> {node id: cell}, keeping only pins that fit their page."""
    wanted: dict[str, int] = {}
    for page in lay.pages:
        for n in page:
            key = memory_key(n) if by_memory else pin_key(n)
            if key and key in keyed:
                wanted[n.id] = int(keyed[key])
    lay.pins = wanted
    for page in lay.pages:  # a page whose remembered gaps no longer fit loses them
        if any(n.id in wanted for n in page) and lay.place(page)[1]:
            for n in page:
                wanted.pop(n.id, None)
    return wanted


def placed_structure(state: list[Any], library: list[Any] | None, keyed_pins: dict[str, int] | None,
                     grid: Grid | None = None) -> str:
    """Like model.structure, but each page lists its items in grid order (row by row) after
    gaps are applied. iOS reorders a page's list by position when it has gaps, so list order
    alone would report a correct write as a mismatch."""
    lay = Layout(state, library=library, grid=grid)
    if keyed_pins:
        node_pins(lay, keyed_pins)
    pages = []
    for page in lay.pages:
        pos, _ = lay.place(page)
        order = sorted(page, key=lambda n: (pos[n.id][0], pos[n.id][1]) if n.id in pos else (99, 0))
        pages.append([[n.id, pos.get(n.id, (0, 0))[:2]] if n.kind != "folder" else
                      [n.name, [[c.id for c in fp] for fp in n.children]] for n in order])
    return json.dumps({"dock": [n.id for n in lay.dock], "pages": pages,
                       "library": None if library is None else sorted(n.id for n in lay.library or [])},
                      default=list)


def _widgets(state: list[Any]) -> list[dict[str, Any]]:
    """Every top-level widget dict in a list-format state (widgets can't be in folders)."""
    return [x for page in state for x in page
            if isinstance(x, dict) and (x.get("gridSize") or x.get("elementType") == "widget")]


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in s)


def _device_operation(fn):
    @functools.wraps(fn)
    async def run(self, *args, **kwargs):
        # Share one advisory lock across app, CLI and MCP processes using this data
        # directory. In particular, a concurrent read must not clear a live marker.
        with (self.dir / "device-operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LayoutError("A device operation is in progress; the session is busy. Try again when it finishes.")
            self.busy = True
            try:
                return await fn(self, *args, **kwargs)
            finally:
                self.busy = False
                fcntl.flock(lock, fcntl.LOCK_UN)
    return run


class Session:
    def __init__(self, source: Source, data_dir: str | Path, device: str | None = None):
        """device: keep this session's draft per device (the app switches between devices);
        None keeps the single shared draft the CLI and MCP server use."""
        self.busy = False
        self.source = source
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._draft_key = device
        self.layout: Layout | None = None
        self.device_id: str | None = device
        self.info: dict[str, Any] = {}
        self.grid = Grid()
        self.usage: dict[str, Any] | None = None  # Screen Time opens, loaded on request
        self._undo: list[tuple[Any, ...]] = []
        self._redo: list[tuple[Any, ...]] = []
        self._load_draft()
        # A persisted CLI/MCP draft pins Auto reconnection too.
        if hasattr(self.source, "udid") and not self.source.udid and self.device_id not in (None, "unselected"):
            self.source.udid = self.device_id

    # ---- persistence ------------------------------------------------------------

    @property
    def draft_path(self) -> Path:
        if self._draft_key:
            (self.dir / "drafts").mkdir(exist_ok=True)
            return self.dir / "drafts" / f"{_safe(self._draft_key)}.plist"
        return self.dir / "draft.plist"

    def _dev_dir(self, kind: str) -> Path:
        p = self.dir / kind / _safe(self.device_id or "unknown")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _save_draft(self) -> None:
        assert self.layout is not None
        blob = {"device": self.device_id, "info": self.info, "source": self.layout.source, "state": self.layout.to_state(),
                "source_library": self.layout.source_library, "library": self.layout.to_library(),
                "bank": self.layout.to_bank(), "pins": self.layout.pins, "source_pins": self.layout.source_pins,
                "grid": self.grid.to_dict()}
        blob = {k: v for k, v in blob.items() if v is not None}  # plists have no null
        self.draft_path.write_bytes(plistlib.dumps(blob, fmt=plistlib.FMT_BINARY))

    def _load_draft(self) -> None:
        if self.draft_path.exists():
            blob = plistlib.loads(self.draft_path.read_bytes())
            self.device_id = blob["device"]
            self.info = blob.get("info", {})
            self.grid = Grid(**blob["grid"]) if "grid" in blob else Grid()
            self.layout = Layout(blob["state"], source=blob["source"], library=blob.get("library"),
                                 source_library=blob.get("source_library"), bank=blob.get("bank"),
                                 pins=blob.get("pins"), source_pins=blob.get("source_pins"), grid=self.grid)

    def require(self) -> Layout:
        if self.layout is None:
            raise LayoutError("no layout loaded yet; call read first")
        return self.layout

    def edited(self) -> None:
        self._save_draft()

    # ---- edits with undo ----------------------------------------------------------

    def _snap(self) -> tuple[Any, ...]:
        lay = self.require()
        return (copy.deepcopy(lay.to_state()), copy.deepcopy(lay.to_library()), copy.deepcopy(lay.to_bank()),
                dict(lay.pins))

    def _load_snap(self, snap: tuple[Any, ...]) -> None:
        lay = self.require()
        self.layout = Layout(snap[0], lay.page_slots, lay.dock_slots, source=lay.source,
                             library=snap[1], source_library=lay.source_library, bank=snap[2],
                             pins=snap[3], source_pins=lay.source_pins, grid=lay.grid)

    def edit(self, fn: Callable[[Layout], Any]) -> Any:
        """Run fn on the draft as one undoable step. If it raises, the draft is left untouched."""
        before = self._snap()
        try:
            out = fn(self.require())
        except Exception:
            self._load_snap(before)
            raise
        self._undo.append(before)
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self._save_draft()
        return out

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snap())
        self._load_snap(self._undo.pop())
        self._save_draft()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snap())
        self._load_snap(self._redo.pop())
        self._save_draft()
        return True

    def load_usage(self, days: int = 30) -> dict[str, Any]:
        from . import usage
        lay = self.require()
        known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        self.usage = usage.load(known, days)
        return self.usage

    async def load_device_usage(self) -> dict[str, Any]:
        """Usage from the device's own memory reports (no Screen Time sync needed)."""
        from . import usage
        lay = self.require()
        if not hasattr(self.source, "memory_reports"):
            self.usage = {"ok": False, "error": "This source can't report usage."}
            return self.usage
        files = await self.source.memory_reports(self._dev_dir("usage"))
        apps = await self.source.installed_apps()
        known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        self.usage = usage.from_memory_reports(files, apps, known)
        return self.usage

    def history(self) -> dict[str, int]:
        return {"undo": len(self._undo), "redo": len(self._redo)}

    # ---- read -------------------------------------------------------------------

    # ---- widget element ids -------------------------------------------------------------
    # A widget's link to its configuration is its element id (userSelectedElementIdentifier over
    # USB). iOS can drop it, so every id ever seen is remembered per widget (by displayIdentifier)
    # and put back on write.

    @property
    def _widget_ids_path(self) -> Path:
        return self._dev_dir("widgets") / "element-ids.json"

    def _remember_widget_ids(self, state: list[Any]) -> None:
        import json
        known = json.loads(self._widget_ids_path.read_text()) if self._widget_ids_path.exists() else {}
        for w in _widgets(state):
            if w.get("displayIdentifier") and w.get("userSelectedElementIdentifier"):
                known[w["displayIdentifier"]] = w["userSelectedElementIdentifier"]
        self._widget_ids_path.write_text(json.dumps(known, indent=1, sort_keys=True))

    def _restore_widget_ids(self, state: list[Any]) -> list[Any]:
        import json
        if not self._widget_ids_path.exists():
            return state
        known = json.loads(self._widget_ids_path.read_text())
        state = copy.deepcopy(state)
        for w in _widgets(state):
            if not w.get("userSelectedElementIdentifier") and known.get(w.get("displayIdentifier")):
                w["userSelectedElementIdentifier"] = known[w["displayIdentifier"]]
        return state

    # ---- web clip memory -----------------------------------------------------------------
    # Web clips kept off the Home Screen aren't installed apps, so the installed-app list can't
    # put them in the App Library, and USB never reports them. A write that leaves one out makes
    # iOS put it in the first free cell on a page. So every web clip ever seen is remembered per
    # phone, and one that isn't on the Home Screen is sent in the App Library.

    @property
    def _webclips_path(self) -> Path:
        return self._dev_dir("webclips") / "seen.json"

    def _known_webclips(self) -> dict[str, dict[str, str]]:
        return json.loads(self._webclips_path.read_text()) if self._webclips_path.exists() else {}

    def _remember_webclips(self, state: list[Any], library: list[Any] | None) -> None:
        known = self._known_webclips()
        before = len(known)
        for n in Layout(state, library=library).nodes.values():
            if n.kind == "webclip":
                known[n.id] = {k: n.raw[k] for k in ("displayIdentifier", "displayName", "webClipURL")
                               if isinstance(n.raw.get(k), str)}
        if len(known) != before:
            self._webclips_path.write_text(json.dumps(known, indent=1, sort_keys=True))

    def _with_webclips(self, state: list[Any], library: list[Any] | None) -> list[Any] | None:
        """library plus every remembered web clip that isn't on the Home Screen."""
        if library is None:
            return None
        here = {n.id for n in Layout(state, library=library).nodes.values()}
        return library + [dict(w) for i, w in sorted(self._known_webclips().items()) if i not in here]

    async def _read_source(self) -> tuple[list[Any], list[Any] | None]:
        state, library = await self.source.read()
        self._remember_widget_ids(state)
        self._remember_webclips(state, library)
        return state, self._with_webclips(state, library)

    # ---- gap memory ----------------------------------------------------------------------
    # The phone never reports gaps over USB, and a write without them closes every gap. So the
    # gaps set in Homescreen are remembered per phone and sent again on every write.

    @property
    def _gaps_path(self) -> Path:
        return self._dev_dir("gaps") / "pins.json"

    def _remember_gaps(self, lay: Layout) -> None:
        mem = {}
        for page in lay.pages:
            for n in page:
                key = memory_key(n)
                if key and n.id in lay.pins:
                    mem[key] = lay.pins[n.id]
        self._gaps_path.write_text(json.dumps(mem, indent=1, sort_keys=True))

    def _layout_from(self, state: list[Any], library: list[Any] | None) -> Layout:
        """A Layout with gaps: from the source when it can report them, else from memory."""
        lay = Layout(state, library=library, grid=self.grid)
        reported = getattr(self.source, "pins", None)
        if reported is not None:
            node_pins(lay, reported)
        elif self._gaps_path.exists():
            node_pins(lay, json.loads(self._gaps_path.read_text()), by_memory=True)
        return Layout(state, library=library, pins=lay.pins, grid=self.grid)

    @property
    def gaps_readable(self) -> bool:
        return getattr(self.source, "pins", None) is not None

    @_device_operation
    async def read(self) -> dict[str, Any]:
        info = await self.source.info()
        actual = info.get("UniqueDeviceID")
        if not actual:
            raise LayoutError("Cannot verify device identity; no layout was read.")
        if self._draft_key not in (None, "unselected", actual):
            raise LayoutError("The selected draft belongs to a different device; no layout was read.")
        self.device_id = actual
        self.info = {k: v for k, v in info.items() if k in ("DeviceName", "ProductType", "ProductVersion") and v is not None}
        self.grid = Grid.from_metrics(info.get("icon_metrics"))
        state, library = await self._read_source()
        reads = self._dev_dir("reads")
        (reads / f"{_stamp()}.plist").write_bytes(_pack(state, library))
        for old in sorted(reads.glob("*.plist"))[:-KEEP_READS]:
            old.unlink()
        self.layout = self._layout_from(state, library)
        self._undo.clear()
        self._redo.clear()
        self._save_draft()
        self._pending_path.unlink(missing_ok=True)
        return {"device": {k: v for k, v in info.items() if k != "UniqueDeviceID"},
                **self.layout.summary()["counts"]}

    def reset(self) -> None:
        self.edit(lambda lay: None)  # records an undo step for the reset
        self.layout = self.require().original()
        self._save_draft()

    # ---- icons ------------------------------------------------------------------

    async def icon(self, bundle_id: str) -> bytes | None:
        cache = self._dev_dir("icons") / f"{_safe(bundle_id)}.png"
        if cache.exists():
            return cache.read_bytes()
        png = await self.source.icon_png(bundle_id)
        if png:
            cache.write_bytes(png)
        return png

    async def icon_colors(self, ids: list[str] | None = None) -> dict[str, Any]:
        lay = self.require()
        ids = ids or [i for i in lay.leaf_ids() if lay.nodes[i].bundle_id]
        out: dict[str, Any] = {}
        for i in ids:
            node = lay.get(i)
            png = await self.icon(node.bundle_id) if node.bundle_id else None
            out[i] = {"name": node.name, **(colors.dominant(png) if png else {"family": "unknown"})}
        return out

    # ---- write ------------------------------------------------------------------

    def _backup(self, state: list[Any], library: list[Any] | None, label: str,
                pins: list[dict[str, int]] | None = None) -> Path:
        p = self._dev_dir("backups") / f"{_stamp()}-{uuid.uuid4().hex[:8]}-{label}.plist"
        blob = plistlib.loads(_pack(state, library, pins))
        blob["device"] = self.device_id
        with p.open("xb") as backup:
            backup.write(plistlib.dumps(blob, fmt=plistlib.FMT_BINARY))
            backup.flush()
            os.fsync(backup.fileno())
        return p

    async def _write_verified(self, target: list[Any], target_lib: list[Any] | None,
                              label: str, pins: list[dict[str, int]] | None = None) -> dict[str, Any]:
        self._require_resolved_write()
        await self._verify_identity()
        current, current_lib = await self._read_source()
        backup = self._backup(current, current_lib, label,
                              pins_by_page(self._layout_from(current, current_lib)))
        target = self._restore_widget_ids(target)
        if target_lib is not None:
            target_lib = self._with_webclips(target, target_lib)
        lib_known = target_lib is not None
        # Persist BEFORE dispatch, so cancellation/process death also requires a read.
        # Exclusive creation prevents a second process from dispatching while pending.
        try:
            with self._pending_path.open("x") as marker:
                json.dump({"backup": backup.name, "device": self.device_id}, marker)
                marker.flush()
                os.fsync(marker.fileno())
        except FileExistsError:
            raise LayoutError("A write is pending or unverified. Read the device before another write.")
        write_error = None
        try:
            await self.source.write(target, target_lib, pins)
        except Exception as e:
            write_error = str(e)
        # A lost response cannot tell us whether the write landed. Read once on a
        # fresh connection to the same phone; NEVER resend or automatically restore.
        try:
            await self._verify_identity()
            after, after_lib = await self._read_source()
        except Exception as e:
            return {"ok": False, "outcome": "unknown", "backup": backup.name, "needs_read": True,
                    "error": f"Write outcome is unknown; verification failed ({e}). "
                             "Reconnect, then Read device and inspect it before applying or restoring. "
                             "Nothing was retried.", "write_error": write_error}
        want_pins = {k: v for page in pins or [] for k, v in page.items()}
        # the phone can't report gaps; there the written gaps are taken as given and the order checked
        got_pins = getattr(self.source, "pins", None)
        got_pins = want_pins if got_pins is None else got_pins
        ok = (placed_structure(after, after_lib if lib_known else None, got_pins, self.grid)
              == placed_structure(target, target_lib, want_pins, self.grid))
        res: dict[str, Any] = {"ok": ok, "backup": backup.name,
                               "outcome": "verified" if ok else "mismatch", "needs_read": False}
        if write_error:
            res["error"] = f"Write call failed ({write_error}); re-read shows the new layout {'DID land' if ok else 'did NOT land'}. Nothing was retried."
        if after_lib is not None:
            res["app_library_only"] = len(after_lib)
        if not ok:
            want = Layout(target, library=target_lib, grid=self.grid)
            got = Layout(after, library=after_lib if lib_known else None, grid=self.grid)
            wp, gp = want.positions(), got.positions()
            res["mismatches"] = {
                "added_by_ios": sorted(set(gp) - set(wp)),
                "missing": sorted(set(wp) - set(gp)),
                "moved": {k: f"wanted {wp[k]}, got {gp[k]}" for k in wp
                          if k in gp and wp[k] != gp[k] and not k.startswith(("folder:", "widget:"))},
                "pages": f"wanted {len(target) - 1}, got {len(after) - 1}",
            }
            surfaced = [got.nodes[i].name for i in res["mismatches"]["added_by_ios"]
                        if got.nodes[i].kind == "webclip"]
            if surfaced:
                # remembered now, so the next write keeps them in the App Library
                res["hidden_webclips"] = surfaced
                res["hint"] = ("iOS put web clips it had never reported into free spots. They're "
                               "remembered now: move them to the App Library and apply again.")
        if ok:
            # remember the gaps that were just written (the phone won't report them back)
            written = Layout(after, library=after_lib, grid=self.grid)
            node_pins(written, {k: v for page in pins or [] for k, v in page.items()})
            self._remember_gaps(written)
        self.layout = self._layout_from(after, after_lib)
        self._undo.clear()
        self._redo.clear()
        self._save_draft()
        self._pending_path.unlink(missing_ok=True)
        return res

    @property
    def _pending_path(self) -> Path:
        return self._dev_dir("pending-writes") / "write.json"

    @property
    def needs_read(self) -> bool:
        return self._pending_path.exists()

    def _require_resolved_write(self) -> None:
        if self.needs_read:
            raise LayoutError("A previous write is pending or unverified. Read the device before another write.")

    async def _verify_identity(self) -> None:
        actual = (await self.source.info()).get("UniqueDeviceID")
        if not actual or not self.device_id or self.device_id == "unselected":
            raise LayoutError("Cannot verify device identity. Read the intended device first; nothing was written.")
        if actual != self.device_id:
            raise LayoutError("This layout or backup belongs to a different device; nothing was written.")

    @_device_operation
    async def apply(self, token: str) -> dict[str, Any]:
        self._require_resolved_write()
        await self._verify_identity()
        lay = self.require()
        if token != lay.token():
            raise LayoutError("token doesn't match the current draft; run diff and use its apply_token")
        problems = lay.validate()
        if problems:
            raise LayoutError(f"draft has problems, not applying: {problems}")
        now, now_lib = await self._read_source()
        if structure(now, now_lib) != structure(lay.source, lay.source_library):
            raise LayoutError("the phone's layout changed since it was read (app installed or moved?). "
                              "Read again and redo the edits.")
        return await self._write_verified(lay.to_state(), lay.to_library(), "before-apply", pins_by_page(lay))

    def backups(self) -> list[str]:
        return sorted(p.name for p in self._dev_dir("backups").glob("*.plist"))

    @_device_operation
    async def restore(self, name: str) -> dict[str, Any]:
        p = self._dev_dir("backups") / name
        if not p.exists() or p.parent != self._dev_dir("backups"):
            raise LayoutError(f"no backup named {name!r}")
        data = p.read_bytes()
        blob = plistlib.loads(data)
        if isinstance(blob, dict) and blob.get("device") and blob["device"] != self.device_id:
            raise LayoutError("This backup belongs to a different device; nothing was written.")
        return await self._write_verified(*_unpack(data), "before-restore", _unpack_pins(data))
