"""Terminal commands. The human-side path for applying and restoring."""

from __future__ import annotations

import argparse
import asyncio
import json
import plistlib
import sys

from .model import LayoutError
from .config import DATA, session
from . import config
from .transport import ConnectionError, MODES


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _confirm(msg: str) -> bool:
    return input(f"{msg} Type yes to continue: ").strip().lower() == "yes"


async def run(args: argparse.Namespace) -> int:
    if args.cmd == "devices":
        from .transport import discover_devices
        _print(await discover_devices())
        return 0
    s = session()
    if args.cmd in ("connect", "reconnect"):
        _print(await config.reconnect())
    elif args.cmd == "pair-wifi":
        if not _confirm("This enables wireless connections and saves pairing credentials using trusted USB."):
            return 1
        _print(await config.pair_wifi())
    elif args.cmd == "read":
        _print(await s.read())
    elif args.cmd == "show":
        _print(s.require().summary())
    elif args.cmd == "diff":
        _print(s.require().diff())
    elif args.cmd == "preview":
        from . import preview
        print(await preview.render(s, DATA / "preview.html"))
    elif args.cmd == "apply":
        d = s.require().diff()
        print(f"{d['items_moved']} items moved, pages {d['pages_before']} -> {d['pages_after']}.")
        if not _confirm("This writes the draft to the phone (a backup is saved first)."):
            return 1
        _print(await s.apply(args.token))
    elif args.cmd == "backups":
        print("\n".join(s.backups()) or "no backups yet")
    elif args.cmd == "restore":
        if not _confirm(f"This writes backup {args.name} to the phone."):
            return 1
        _print(await s.restore(args.name))
    elif args.cmd == "probe":
        # Dump the raw icon state in every format version, to learn what this iOS exposes
        # (e.g. whether App Library-only apps are marked somewhere).
        out = DATA / "probe"
        out.mkdir(parents=True, exist_ok=True)
        info = await s.source.info()
        (out / "info.json").write_text(json.dumps(info, indent=2, default=str))
        if not hasattr(s.source, "read_raw"):
            print("probe only applies to a USB phone")
            return 1
        for fv in (None, "1", "2", "3", "4", "5"):
            try:
                raw = await s.source.read_raw(fv)
                (out / f"state-fv{fv}.plist").write_bytes(plistlib.dumps(raw, fmt=plistlib.FMT_XML))
                kind = (f"DICT with keys {sorted(raw)}" if isinstance(raw, dict)
                        else f"list of {len(raw)}")
                lib = "  <-- has App Library ('ignored')" if isinstance(raw, dict) and "ignored" in raw else ""
                print(f"formatVersion {fv}: {kind}{lib}")
            except Exception as e:
                print(f"formatVersion {fv}: failed ({e})")
        print("If a version shows the App Library, use it: HOMESCREEN_FORMAT=<version>")
        print(f"saved to {out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="homescreen", description="iPhone and iPad Home Screen layouts over USB or Wi-Fi")
    p.add_argument("--transport", choices=MODES, help="USB (default), Wi-Fi, or USB-first Auto")
    p.add_argument("--udid", help="select the physical device (never changes automatically)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="discover USB and paired Wi-Fi devices")
    sub.add_parser("pair-wifi", help="enable Wi-Fi and pair once over trusted USB (asks first)")
    sub.add_parser("connect", help="check the selected connection without changing the draft")
    sub.add_parser("reconnect", help="rediscover the same device without writing or replacing the draft")
    sub.add_parser("read", help="read the phone and start a fresh draft")
    sub.add_parser("show", help="print the draft")
    sub.add_parser("diff", help="what applying would change, plus the apply token")
    sub.add_parser("preview", help="write an HTML preview")
    a = sub.add_parser("apply", help="write the draft to the phone")
    a.add_argument("token")
    sub.add_parser("backups", help="list backups")
    r = sub.add_parser("restore", help="write a backup to the phone")
    r.add_argument("name")
    sub.add_parser("probe", help="dump raw icon state in every format version (read-only)")
    u = sub.add_parser("ui", help="open the Homescreen app in your browser")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    if args.transport:
        import os
        os.environ["HOMESCREEN_TRANSPORT"] = args.transport
    if args.udid:
        import os
        os.environ["HOMESCREEN_UDID"] = args.udid
    if args.cmd == "ui":
        from . import ui
        ui.main(args.port, not args.no_browser)
        return
    try:
        sys.exit(asyncio.run(run(args)))
    except (LayoutError, ConnectionError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
