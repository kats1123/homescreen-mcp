"""Local stdio MCP server. Every edit tool changes the draft only; apply/restore write the device."""

from __future__ import annotations

import os
from typing import Any, Callable

from mcp.server.mcpserver import Image, MCPServer

from . import organize, preview, usage, config
from .transport import discover_devices
from .config import DATA, session
from .model import Layout, LayoutError

ALLOW_APPLY = os.environ.get("HOMESCREEN_ALLOW_APPLY") == "1"

INSTRUCTIONS = """Arrange an iPhone/iPad Home Screen over USB or paired Wi-Fi. Use list_devices and
connect_device to select transport and UDID; pair_wifi needs explicit user approval.
Workflow: read_phone -> show_layout -> edit the
draft (arrange / move / create_folder / sort_* / file_into_folders ...) -> diff -> render_preview
-> apply only when the user explicitly asks. Every edit is one undo step (undo / redo).
Item ids: apps use their bundle id; folders and widgets use folder:N / widget:N.
Targets: 'dock', 'library', 'bank' (a holding tray; empty it before applying), 'page:N'
(1-based; N = last+1 makes a new page), 'folder:<id>[:N]'.
To take an app off the Home Screen, move it to 'library' (App Library only). Leaving it out
does NOT work: iOS puts it back. Pages hold 24 slots (6 rows x 4 columns); widgets take
small=2x2, medium=4x2, large=4x4. place_at puts an item in an exact spot, leaving gaps; any move
drops that. iOS doesn't report gaps made on the phone over USB, so those close on apply. Folders hold 9 per page. Leave page 1 and the dock alone unless asked: they are
muscle memory. To sort by color, call icon_colors first; get_icons only for the few you
can't judge."""

mcp = MCPServer("homescreen", instructions=INSTRUCTIONS)


def _edit(fn: Callable[[Layout], Any]) -> dict[str, Any]:
    """Run an edit as one undoable step; on error nothing changes."""
    s = session()
    try:
        out = s.edit(fn)
    except LayoutError as e:
        return {"ok": False, "error": str(e)}
    res: dict[str, Any] = {"ok": True, "problems": s.require().validate()}
    if isinstance(out, dict):
        res.update(out)
    elif out is not None:
        res["result"] = out
    return res


@mcp.tool()
async def list_devices() -> list[dict[str, Any]]:
    """Discover trusted USB and previously paired Wi-Fi devices and their methods."""
    return await discover_devices()


@mcp.tool()
async def connect_device(transport: str = "usb", udid: str | None = None) -> dict[str, Any]:
    """Select USB, Wi-Fi or Auto and connect. Select a UDID when several devices exist.
    Does not read, replace or apply a draft. Auto never switches physical devices."""
    config.configure_connection(transport, udid)
    return await config.reconnect()


@mcp.tool()
async def reconnect_device() -> dict[str, Any]:
    """Rediscover the same device. After an interrupted write, call read_phone to inspect;
    reconnect never writes or clears the pending-write guard."""
    return await config.reconnect()


@mcp.tool()
async def pair_wifi() -> dict[str, Any]:
    """Enable wireless connections and save a RemotePairing record over trusted USB.
    Only call when the user explicitly requests Wi-Fi pairing. Does not change the layout."""
    return await config.pair_wifi()


@mcp.tool()
async def read_phone() -> dict[str, Any]:
    """Read the Home Screen from the connected iPhone and start a fresh draft from it.
    Discards any unapplied draft."""
    return await session().read()


@mcp.tool()
def show_layout() -> dict[str, Any]:
    """The current draft: dock, every page and the App Library-only apps, as {id, name} items
    (folders carry their contents, widgets their size)."""
    return session().require().summary()


@mcp.tool()
def find_apps(query: str) -> list[dict[str, Any]]:
    """Find items by name or bundle id substring."""
    lay = session().require()
    pos = lay.positions()
    return [{**n.describe(), "position": pos.get(n.id, "not placed")} for n in lay.find(query)]


@mcp.tool()
async def icon_colors(ids: list[str] | None = None) -> dict[str, Any]:
    """Dominant icon color per app (family, background, hex, hue 0-360). Omit ids for every app."""
    return await session().icon_colors(ids)


@mcp.tool()
async def get_icons(ids: list[str]) -> list[str | Image]:
    """Icon images for up to 24 apps, in the order given."""
    s = session()
    lay = s.require()
    out: list[str | Image] = []
    for i in ids[:24]:
        node = lay.get(i)
        png = await s.icon(node.bundle_id) if node.bundle_id else None
        out.append(f"{node.name} [{i}]")
        if png:
            out.append(Image(data=png, format="png"))
    return out


@mcp.tool()
def arrange(pages: list[list[str]], dock: list[str] | None = None,
            library: list[str] | None = None) -> dict[str, Any]:
    """Set the whole top-level layout at once. pages = list of pages, each a list of ids in
    order (row by row). library = apps kept only in the App Library. Every top-level item and
    library app must appear exactly once. dock / library omitted = unchanged."""
    return _edit(lambda lay: lay.arrange(pages, dock, library))


@mcp.tool()
def move(item_id: str, target: str, index: int | None = None, before: str | None = None) -> dict[str, Any]:
    """Move one item to target ('dock', 'library', 'page:N', 'folder:<id>[:N]'), at index
    (0-based, default end) or just before the item with id `before`.
    'library' = App Library only, off the Home Screen."""
    return _edit(lambda lay: lay.move(item_id, target, index, before))


@mcp.tool()
def create_folder(name: str, item_ids: list[str], target: str, index: int | None = None) -> dict[str, Any]:
    """Make a folder at target/index holding item_ids (in order)."""
    return _edit(lambda lay: {"folder_id": lay.create_folder(name, item_ids, target, index)})


@mcp.tool()
def rename_folder(folder_id: str, name: str) -> dict[str, Any]:
    """Rename a folder."""
    return _edit(lambda lay: lay.rename_folder(folder_id, name))


@mcp.tool()
def dissolve_folder(folder_id: str) -> dict[str, Any]:
    """Remove a folder, putting its apps where it was."""
    return _edit(lambda lay: lay.dissolve_folder(folder_id))


@mcp.tool()
def file_into_folders(groups: dict[str, list[str]], keep_first_page: bool = True) -> dict[str, Any]:
    """File loose apps into folders: {folder name: [app ids]}. Apps join an existing folder
    with that name, or a new folder (2+ apps). Apps on page 1 are left alone by default."""
    return _edit(lambda lay: organize.into_folders(lay, groups, keep_first_page))


@mcp.tool()
async def sort_by_color(keep_first_page: bool = True) -> dict[str, Any]:
    """Rainbow-sort loose apps (red -> purple, then white/gray/black). Folders and widgets
    go first. Page 1 is left alone by default."""
    colors = await session().icon_colors()
    return _edit(lambda lay: organize.by_color(lay, colors, keep_first_page))


@mcp.tool()
def sort_alphabetically(keep_first_page: bool = True, folders_first: bool = True) -> dict[str, Any]:
    """A-Z sort of folders and loose apps. Page 1 is left alone by default."""
    return _edit(lambda lay: organize.alphabetical(lay, keep_first_page, folders_first))


@mcp.tool()
def sort_folder_contents() -> dict[str, Any]:
    """A-Z inside every folder."""
    return _edit(lambda lay: organize.sort_folder_contents(lay))


@mcp.tool()
def app_store_categories(ids: list[str] | None = None) -> dict[str, str]:
    """App Store genre per app (Apple apps are 'Apple'). This sends the bundle ids to Apple's
    public lookup API, so only call it when the user is fine with that. Cached locally."""
    lay = session().require()
    ids = ids or [n.id for n in lay.nodes.values() if n.kind == "app"]
    return organize.fetch_categories(ids, DATA / "categories.json")


@mcp.tool()
def place_at(item_id: str, page: int, row: int, column: int) -> dict[str, Any]:
    """Put an item in an exact spot (row 1-6, column 1-4) on a page, leaving empty spaces
    around it, like dragging it to an empty spot on the iPhone. Other items flow around it."""
    return _edit(lambda lay: lay.pin(item_id, page, (row - 1) * 4 + (column - 1)))


@mcp.tool()
def close_gaps(item_ids: list[str]) -> dict[str, Any]:
    """Let placed items flow with the rest again (removes their gaps)."""
    return _edit(lambda lay: [lay.unpin(i) for i in item_ids])


@mcp.tool()
def swap(a: str, b: str) -> dict[str, Any]:
    """Trade the places of two items."""
    return _edit(lambda lay: lay.swap(a, b))


@mcp.tool()
async def screen_time(days: int = 30, source: str = "device") -> dict[str, Any]:
    """Which apps get used, the top apps with where they are now, and estimated page swipes for
    the device vs the draft. source='device': the device's own memory reports (no setup; apps iOS
    keeps running for calls or in memory every day count less, listed under 'background');
    source='mac': Screen Time synced to this Mac (Share Across Devices), last `days` days."""
    s = session()
    u = await s.load_device_usage() if source == "device" else s.load_usage(days)
    if not u.get("ok"):
        return u
    lay = s.require()
    return {"source": source, "days": u["days"], "background": u.get("background", []),
            "top": usage.top_apps(lay, u["opens"], 40),
            "swipes_on_phone": usage.swipes(lay.original(), u["opens"]),
            "swipes_in_draft": usage.swipes(lay, u["opens"])}


@mcp.tool()
async def optimize_by_usage(keep_first_page: bool = False) -> dict[str, Any]:
    """Most-used apps and folders onto the earliest pages, most-used apps onto each folder's
    first page (usage from screen_time, else the device's memory reports, else Screen Time).
    Widgets keep their pages; dock and App Library untouched."""
    s = session()
    if s.usage is None:
        try:
            await s.load_device_usage()
        except Exception:
            s.usage = None
        if not (s.usage or {}).get("ok"):
            s.load_usage()
    if not s.usage.get("ok"):
        return {"ok": False, "error": s.usage.get("error")}
    opens = s.usage["opens"]
    return _edit(lambda lay: usage.optimize(lay, opens, keep_first_page))


@mcp.tool()
def page_op(op: str, page: int, to: int | None = None) -> dict[str, Any]:
    """Page operations: op='insert' (new empty page at `page`), 'move' (page -> `to`),
    'delete' (its apps go to the App Library, like iOS)."""
    def run(lay: Layout) -> None:
        if op == "insert":
            lay.insert_page(page)
        elif op == "move":
            if to is None:
                raise LayoutError("move needs `to`")
            lay.move_page(page, to)
        elif op == "delete":
            lay.delete_page(page)
        else:
            raise LayoutError(f"unknown op {op!r}")
    return _edit(run)


@mcp.tool()
def remove_empty() -> dict[str, Any]:
    """Drop empty pages and empty folders from the draft."""
    return _edit(lambda lay: lay.drop_empty())


@mcp.tool()
def undo() -> dict[str, Any]:
    """Undo the last draft edit."""
    s = session()
    return {"ok": s.undo(), **s.history()}


@mcp.tool()
def redo() -> dict[str, Any]:
    """Redo the last undone draft edit."""
    s = session()
    return {"ok": s.redo(), **s.history()}


@mcp.tool()
def reset_draft() -> dict[str, Any]:
    """Throw away draft edits and go back to the layout as read from the phone (undoable)."""
    session().reset()
    return {"ok": True}


@mcp.tool()
def diff() -> dict[str, Any]:
    """What applying the draft would change, any problems, and the apply_token."""
    return session().require().diff()


@mcp.tool()
async def render_preview() -> dict[str, Any]:
    """Write an HTML preview (draft vs phone, moved icons outlined) and return its path."""
    path = await preview.render(session(), DATA / "preview.html")
    return {"path": str(path)}


@mcp.tool()
async def apply(apply_token: str) -> dict[str, Any]:
    """Write the draft to the iPhone. Only call when the user explicitly asked to apply this
    draft. Needs the apply_token from the latest diff. Backs up first and verifies after."""
    if not ALLOW_APPLY:
        return {"ok": False, "error": "apply from the assistant is off. The user can apply in the app "
                f"(`homescreen ui`), run `homescreen apply {apply_token}`, or set HOMESCREEN_ALLOW_APPLY=1."}
    try:
        return await session().apply(apply_token)
    except LayoutError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def list_backups() -> list[str]:
    """Layouts saved before each apply/restore, oldest first."""
    return session().backups()


@mcp.tool()
async def restore(backup: str) -> dict[str, Any]:
    """Write a backup to the iPhone. Only when the user explicitly asked. Same gate as apply."""
    if not ALLOW_APPLY:
        return {"ok": False, "error": f"restore from the assistant is off. The user can run `homescreen restore {backup}`."}
    try:
        return await session().restore(backup)
    except LayoutError as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
