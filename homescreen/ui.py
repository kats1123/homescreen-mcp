"""The Homescreen app: a local web UI over the same draft, apply and backup engine.

Listens on 127.0.0.1 only. Every POST needs the per-launch token embedded in the page, and the
Host header must be local, so other sites open in the browser can't drive it.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import organize, usage
from . import config
from .config import DATA, session
from .model import Layout, LayoutError, Node, widget_apps

WEB = Path(__file__).parent / "web"
TOKEN = secrets.token_urlsafe(24)
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_device_lock = asyncio.Lock()  # one read/apply/restore at a time
_icon_gate = asyncio.Semaphore(4)


# ---- view -----------------------------------------------------------------------------

def _item(n: Node, lay: Layout) -> dict[str, Any]:
    d: dict[str, Any] = {"id": n.id, "name": n.name, "kind": n.kind}
    if n.bundle_id:
        d["icon"] = n.bundle_id
    if n.kind == "widget":
        apps = [b for b in widget_apps(n.raw) if b]
        d["name"] = lay.widget_label(n)
        d["stack"] = len(apps) > 1
        if apps:
            d["icon"] = apps[0]
        d["size"] = str(n.raw.get("gridSize", "small")).lower()
        d["slots"] = n.slots
    if n.kind == "folder":
        d["pages"] = [[_item(c, lay) for c in p] for p in n.children]
    return d


def _page(page: list[Node], lay: Layout) -> list[dict[str, Any]]:
    pos, _ = lay.place(page)
    out = []
    for n in page:
        d = _item(n, lay)
        if n.id in pos:
            d["pos"] = list(pos[n.id])
        if n.id in lay.pins:
            d["pinned"] = True
        out.append(d)
    return out


def _usage_view(lay: Layout) -> dict[str, Any] | None:
    u = session().usage
    if u is None:
        return None
    if not u.get("ok"):
        return {"ok": False, "error": u.get("error")}
    opens = u["opens"]
    return {"ok": True, "days": u["days"], "source": u.get("source", "mac"), "reports": u.get("reports"),
            "background": [lay.nodes[b].name for b in u.get("background", []) if b in lay.nodes],
            "original": usage.swipes(lay.original(), opens),
            "now": usage.swipes(lay, opens), "top": usage.top_apps(lay, opens, 12)}


def view() -> dict[str, Any]:
    s = session()
    if s.layout is None:
        return {"loaded": False, "source": s.source.name, "device_id": config.current_device(),
                "connection": config.connection_status()}
    lay = s.layout
    d = lay.diff()
    moved = {k.rsplit("[", 1)[-1].rstrip("]") for k in d["moves"]}
    return {
        "loaded": True,
        "connection": config.connection_status(),
        "source": s.source.name,
        "device_id": s.device_id,
        "device": s.info,
        "dock": [_item(n, lay) for n in lay.dock],
        "pages": [_page(p, lay) for p in lay.pages],
        "library": None if lay.library is None else [_item(n, lay) for n in lay.library],
        "bank": [_item(n, lay) for n in lay.bank],
        "usage": _usage_view(lay),
        "moved": sorted(moved),
        "diff": {k: d[k] for k in ("items_moved", "to_app_library", "pages_before", "pages_after",
                                   "problems", "apply_token", "gaps_changed")},
        "moves": d["moves"],
        "history": s.history(),
        "gaps": {"readable": s.gaps_readable, "count": len(lay.pins), "changed": d["gaps_changed"]},
        "slots": {"page": lay.page_slots, "dock": lay.dock_slots},
        "grid": lay.grid.to_dict(),
    }


# ---- operations -------------------------------------------------------------------------

def _ops() -> dict[str, Callable[..., Callable[[Layout], Any]]]:
    return {
        "move": lambda id, target, index=None, before=None: lambda lay: lay.move(id, target, index, before),
        "move_many": lambda ids, target, before=None: lambda lay: [lay.move(i, target, None, before) for i in ids],
        "swap": lambda a, b: lambda lay: lay.swap(a, b),
        "pin": lambda id, page, cell: lambda lay: lay.pin(id, page, cell),
        "unpin": lambda ids: lambda lay: [lay.unpin(i) for i in ids],
        "create_folder": lambda name, ids, target, index=None: lambda lay: lay.create_folder(name, ids, target, index),
        "rename_folder": lambda id, name: lambda lay: lay.rename_folder(id, name),
        "dissolve_folder": lambda id: lambda lay: lay.dissolve_folder(id),
        "insert_page": lambda page: lambda lay: lay.insert_page(page),
        "move_page": lambda page, to: lambda lay: lay.move_page(page, to),
        "delete_page": lambda page: lambda lay: lay.delete_page(page),
        "remove_empty": lambda: lambda lay: lay.drop_empty(),
        "sort_alpha": lambda keep_first_page=True: lambda lay: organize.alphabetical(lay, keep_first_page),
        "sort_folders": lambda: lambda lay: organize.sort_folder_contents(lay),
        "file_into_folders": lambda groups, keep_first_page=True:
            lambda lay: organize.into_folders(lay, groups, keep_first_page),
    }


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _guard(request: Request) -> Response | None:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in LOCAL_HOSTS:
        return JSONResponse({"error": "bad host"}, status_code=403)
    if request.method == "POST" and request.headers.get("x-token") != TOKEN:
        return JSONResponse({"error": "bad token"}, status_code=403)
    return None


def _err(e: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": str(e), "view": view()}, status_code=status)


async def index(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    stamp = str(int(max(f.stat().st_mtime for f in WEB.iterdir())))  # fresh assets after an update
    html = ((WEB / "index.html").read_text().replace("__TOKEN__", TOKEN)
            .replace("/static/app.css", f"/static/app.css?v={stamp}").replace("/static/app.js", f"/static/app.js?v={stamp}"))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def get_state(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    return JSONResponse(view())


async def post_op(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    name, args = body.get("op"), body.get("args") or {}
    s = session()
    try:
        if name == "undo":
            s.undo()
        elif name == "redo":
            s.redo()
        elif name == "reset":
            s.reset()
        elif name == "sort_color":
            colors = await s.icon_colors()
            s.edit(lambda lay: organize.by_color(lay, colors, args.get("keep_first_page", True)))
        elif name == "load_usage":
            if args.get("source") == "device":
                async with _device_lock:
                    await s.load_device_usage()
            else:
                await asyncio.to_thread(s.load_usage, int(args.get("days", 30)))
        elif name == "optimize_usage":
            if s.usage is None:  # the device's own reports first, then Screen Time on this Mac
                try:
                    async with _device_lock:
                        await s.load_device_usage()
                except Exception:
                    s.usage = None
                if not (s.usage or {}).get("ok"):
                    await asyncio.to_thread(s.load_usage)
            if not s.usage.get("ok"):
                raise LayoutError(s.usage.get("error", "no usage data"))
            opens = s.usage["opens"]
            s.edit(lambda lay: usage.optimize(lay, opens, args.get("keep_first_page", False)))
        elif name == "group_categories":
            lay = s.require()
            ids = [n.id for p in lay.pages for n in p if n.kind == "app"]
            cats = await asyncio.to_thread(organize.fetch_categories, ids, DATA / "categories.json")
            keep = args.get("keep_first_page", True)
            s.edit(lambda lay: organize.into_folders(lay, organize.category_groups(lay, cats, keep), keep))
        else:
            factory = _ops().get(name)
            if factory is None:
                raise LayoutError(f"unknown operation {name!r}")
            s.edit(factory(**args))
    except (LayoutError, TypeError, ValueError, KeyError) as e:
        return _err(e)
    except OSError as e:
        return _err(e, 502)
    return JSONResponse(view())


async def post_read(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    async with _device_lock:
        try:
            await session().read()
        except Exception as e:
            return _err(RuntimeError(f"couldn't read the device: {e}"), 502)
    return JSONResponse(view())


async def post_apply(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    async with _device_lock:
        try:
            result = await session().apply(str(body.get("token", "")))
        except LayoutError as e:
            return _err(e)
        except Exception as e:
            return _err(RuntimeError(f"apply failed: {e}"), 502)
    return JSONResponse({"result": result, "view": view()})


async def _connected() -> dict[str, dict[str, Any]]:
    from .transport import discover_devices
    return {d["udid"]: {**{k: d[k] for k in ("name", "model", "version") if d.get(k)},
                         "methods": d["methods"],
                         "connection": " / ".join("Wi-Fi" if m == "wifi" else "USB" for m in d["methods"])}
            for d in await discover_devices()}


async def get_devices(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    if not config.switching():
        return JSONResponse({"switching": False, "current": None, "devices": []})
    try:
        connected = await _connected()
    except Exception as e:
        return _err(RuntimeError(f"Device discovery failed: {e}"), 502)
    known = config.known_devices()
    devices = [{"udid": u, **known.get(u, {}), **c, "connected": True, "draft": u in known} for u, c in connected.items()]
    devices += [{"udid": u, **k, "connected": False, "draft": True} for u, k in known.items() if u not in connected]
    return JSONResponse({"switching": True, "current": config.current_device(), "devices": devices})


async def post_device(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    udid = str((await _json(request)).get("udid", ""))
    if not udid:
        return _err(LayoutError("no device given"))
    async with _device_lock:
        try:
            config.select_device(udid)
        except ValueError as e:
            return _err(e)
    return JSONResponse(view())


async def post_connection(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    async with _device_lock:
        try:
            config.configure_connection(str(body.get("mode", "usb")))
        except ValueError as e:
            return _err(e)
    return JSONResponse(view())


async def post_reconnect(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    async with _device_lock:
        try:
            await config.reconnect()
        except Exception as e:
            return _err(e, 502)
    return JSONResponse(view())


async def post_pair_wifi(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    async with _device_lock:
        try:
            result = await config.pair_wifi()
        except Exception as e:
            return _err(e, 502)
    return JSONResponse({"result": result, "view": view()})


async def get_backups(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    s = session()
    return JSONResponse({"backups": list(reversed(s.backups())) if s.device_id else []})


async def post_restore(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    async with _device_lock:
        try:
            result = await session().restore(str(body.get("name", "")))
        except LayoutError as e:
            return _err(e)
        except Exception as e:
            return _err(RuntimeError(f"restore failed: {e}"), 502)
    return JSONResponse({"result": result, "view": view()})


async def get_icon(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    bundle = request.query_params.get("id", "")
    async with _icon_gate:
        try:
            png = await session().icon(bundle)
        except Exception:
            png = None
    if not png:
        return Response(status_code=404)
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/state", get_state),
    Route("/api/op", post_op, methods=["POST"]),
    Route("/api/read", post_read, methods=["POST"]),
    Route("/api/apply", post_apply, methods=["POST"]),
    Route("/api/devices", get_devices),
    Route("/api/device", post_device, methods=["POST"]),
    Route("/api/connection", post_connection, methods=["POST"]),
    Route("/api/reconnect", post_reconnect, methods=["POST"]),
    Route("/api/pair-wifi", post_pair_wifi, methods=["POST"]),
    Route("/api/backups", get_backups),
    Route("/api/restore", post_restore, methods=["POST"]),
    Route("/icon", get_icon),
    Mount("/static", StaticFiles(directory=WEB), name="static"),
])


def _exit_with_parent(pid: int) -> None:
    """Inside Homescreen.app: stop when the app is gone, even if it crashed."""
    import os
    import time

    def watch() -> None:
        while True:
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass
    threading.Thread(target=watch, daemon=True).start()


def main(port: int = 8765, open_browser: bool = True) -> None:
    import os
    import uvicorn
    config.enable_device_switching()
    if os.environ.get("HOMESCREEN_PARENT_PID", "").isdigit():
        _exit_with_parent(int(os.environ["HOMESCREEN_PARENT_PID"]))
    url = f"http://127.0.0.1:{port}/"
    print(f"Homescreen is running at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
