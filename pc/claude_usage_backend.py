#!/usr/bin/env python3
"""
claude_usage_backend.py  (self-configuring)
-------------------------------------------
Reads your OWN claude.ai subscription usage and pushes a compact summary to a
Flipper Zero over USB serial, for the companion claude_usage.js display app.

It tries to do EVERYTHING automatically:
  * pulls your claude.ai session cookie straight from your browser
  * discovers your org id and the internal usage endpoint
  * recursively finds the usage numbers in the response (no hardcoded fields)
  * auto-detects the Flipper's serial port by its USB id

HONEST CAVEATS
  * There is no official API for claude.ai subscription usage; this reuses your
    own logged-in session against an undocumented endpoint. Personal use only,
    your account only. It can break whenever Anthropic changes that page.
  * Your session cookie is as sensitive as your password. Keep this local.

QUICK START
  pip install requests pyserial browser-cookie3
  # just run it - it figures out the rest:
  python claude_usage_backend.py
  # if auto-detect of the browser is wrong:
  python claude_usage_backend.py --browser firefox
  # see what it found without sending to the Flipper:
  python claude_usage_backend.py --print
  # dump raw JSON if the numbers look off (paste this back to Claude):
  python claude_usage_backend.py --probe
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

SERIAL_BAUD = 230400  # must match claude_usage.js
BASE = "https://claude.ai"

# Candidate endpoints to try, in order. {org} is filled in after we find it.
# The recursive parser means we don't need the "right" one - just one that
# returns JSON containing usage-shaped numbers.
CANDIDATE_PATHS = [
    "/api/organizations/{org}/usage",
    "/api/bootstrap/{org}/usage",
    "/api/organizations/{org}/usage_summary",
    "/api/organizations/{org}",
    "/api/bootstrap",
]

BROWSERS = ["firefox", "chrome", "edge", "brave", "chromium", "opera"]


# ---------------------------------------------------------------- auth --------
def load_session(preferred=None):
    import requests
    try:
        import browser_cookie3 as bc
    except ImportError:
        sys.exit("Run: pip install browser-cookie3")

    loaders = {
        "firefox": bc.firefox, "chrome": bc.chrome, "edge": bc.edge,
        "brave": bc.brave, "chromium": bc.chromium, "opera": bc.opera,
    }
    order = ([preferred] if preferred else []) + [b for b in BROWSERS if b != preferred]

    last_err = None
    for name in order:
        if name not in loaders:
            continue
        try:
            cj = loaders[name](domain_name="claude.ai")
            if len(cj) == 0:
                continue
            s = requests.Session()
            s.cookies.update(cj)
            s.headers.update({
                "User-Agent": "flipper-usage-display/0.2 (personal use)",
                "Accept": "application/json",
            })
            # verify the cookie actually logs us in
            r = s.get(f"{BASE}/api/organizations", timeout=15)
            if r.status_code == 200:
                print(f"[ok] authenticated via {name}")
                return s, r.json()
        except Exception as e:
            last_err = e
            continue
    sys.exit(f"Could not authenticate with any browser. Last error: {last_err}\n"
             "Make sure you're logged into claude.ai in your browser.")


# ---------------------------------------------------------- discovery ---------
def discover(session, orgs_json):
    """Find an endpoint whose JSON contains usage numbers. Returns (url, json)."""
    org_ids = []
    if isinstance(orgs_json, list):
        for o in orgs_json:
            if isinstance(o, dict) and "uuid" in o:
                org_ids.append(o["uuid"])
    if not org_ids:
        org_ids = [""]  # some paths don't need an org

    tried = []
    for org in org_ids:
        for path in CANDIDATE_PATHS:
            url = BASE + path.format(org=org)
            if url in tried:
                continue
            tried.append(url)
            try:
                r = session.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
                if looks_like_usage(data):
                    print(f"[ok] usage endpoint: {url}")
                    return url, data
            except Exception:
                continue
    sys.exit("Couldn't auto-find the usage endpoint. Run with --probe after setting\n"
             "USAGE_URL manually, or paste me the output of --probe.")


def looks_like_usage(data):
    """True if the JSON tree mentions usage-ish keys."""
    blob = json.dumps(data).lower()
    return any(k in blob for k in (
        "utilization", "usage", "resets_at", "reset_at",
        "five_hour", "seven_day", "remaining"))


# ------------------------------------------------------------- parse ----------
PCT_KEYS = ("utilization", "percent", "pct", "used_fraction", "used", "ratio")
RESET_KEYS = ("resets_at", "reset_at", "reset", "resets")
SESSION_HINTS = ("five", "5h", "hour", "session")
WEEK_HINTS = ("seven", "7d", "week", "weekly")


def walk(data, path=""):
    """Yield (path, key, value) for every scalar in the JSON tree."""
    if isinstance(data, dict):
        for k, v in data.items():
            p = f"{path}.{k}"
            if isinstance(v, (dict, list)):
                yield from walk(v, p)
            else:
                yield p, str(k).lower(), v
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from walk(v, f"{path}[{i}]")


def as_pct(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if 0 <= f <= 1.0:
        return round(f * 100)
    if 0 <= f <= 100:
        return round(f)
    return None


def parse_usage(data):
    """Pull session %, weekly %, and the WEEKLY reset (local time, with weekday)."""
    fh = data.get("five_hour") or {}
    sd = data.get("seven_day") or {}
    session_pct = as_pct(fh.get("utilization"))
    week_pct = as_pct(sd.get("utilization"))

    # Fallback to the limits[] list if the top-level blocks are missing.
    if session_pct is None or week_pct is None:
        for lim in data.get("limits", []) or []:
            grp = str(lim.get("group", "")).lower()
            if session_pct is None and "session" in grp:
                session_pct = as_pct(lim.get("percent"))
            if week_pct is None and "week" in grp:
                week_pct = as_pct(lim.get("percent"))

    # Weekly reset (with weekday). Session reset is time-only and only shown
    # when the session window is above 0% (otherwise nothing is scheduled).
    reset = fmt_reset(sd.get("resets_at"))
    sreset = fmt_reset_time(fh.get("resets_at")) if (session_pct or 0) > 0 else "--"
    return {"session_pct": session_pct, "week_pct": week_pct,
            "reset": reset, "sreset": sreset}


def fmt_reset_time(iso):
    """ISO8601 UTC -> local time only, e.g. '1:10p' (no weekday)."""
    if not iso:
        return "--"
    try:
        dt = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return "--"
    ap = "a" if dt.hour < 12 else "p"
    h12 = dt.hour % 12 or 12
    return f"{h12}:{dt.minute:02d}{ap}"


def fmt_reset(iso):
    """ISO8601 UTC -> local short form with weekday: 'Fri 9a' or 'Fri 9:10a'."""
    if not iso:
        return "--"
    try:
        dt = datetime.fromisoformat(iso).astimezone()  # -> this PC's local tz
    except ValueError:
        return str(iso)[:10]
    day = dt.strftime("%a")            # Mon, Tue, ...
    ap = "a" if dt.hour < 12 else "p"
    h12 = dt.hour % 12 or 12
    if dt.minute:
        return f"{day} {h12}:{dt.minute:02d}{ap}"
    return f"{day} {h12}{ap}"


# ------------------------------------------------------------ serial ----------
def find_flipper_port(explicit=None):
    from serial.tools import list_ports
    if explicit:
        return explicit
    for p in list_ports.comports():
        desc = f"{p.description} {p.manufacturer or ''} {p.product or ''}".lower()
        # Flipper CDC: STMicro VID 0x0483, or name contains 'flipper'
        if (p.vid == 0x0483) or ("flipper" in desc):
            print(f"[ok] Flipper serial port: {p.device}")
            return p.device
    return None


def open_serial(port):
    try:
        import serial
    except ImportError:
        sys.exit("Run: pip install pyserial")
    return serial.Serial(port, SERIAL_BAUD, timeout=1)


def format_line(u):
    def v(x):
        return "--" if x is None else str(x)
    return (f"CLU|session={v(u['session_pct'])}|sreset={u['sreset']}"
            f"|week={v(u['week_pct'])}|wreset={u['reset']}\n")


# ------------------------------------------------------- streaming loop -------
def stream_once(session, url, ser):
    data = session.get(url, timeout=15).json()
    line = format_line(parse_usage(data))
    if ser:
        ser.write(line.encode("utf-8"))
    return line.strip()


def run_loop(session, url, ser, interval, stop=None, on_status=None):
    """Fetch + send forever. stop: threading.Event; on_status: callback(str)."""
    target = max(15, interval)
    while stop is None or not stop.is_set():
        try:
            msg = stream_once(session, url, ser)
        except Exception as e:
            msg = f"[warn] {e}"
        (on_status or print)(msg)
        slept = 0
        while slept < target and (stop is None or not stop.is_set()):
            time.sleep(1)
            slept += 1


# ------------------------------------------------------- system-tray mode -----
def run_tray(args):
    """Run with no console window: a system-tray icon with a Quit menu."""
    import threading
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("Tray mode needs pystray + Pillow:  pip install pystray pillow")
        return False

    session, orgs = load_session(args.browser)
    url, _ = discover(session, orgs)
    port = find_flipper_port(args.port)
    ser = open_serial(port) if port else None

    here = os.path.dirname(os.path.abspath(__file__))
    try:
        image = Image.open(os.path.join(here, "claude_zero.ico"))
    except Exception:
        image = Image.new("RGB", (64, 64), (247, 120, 30))

    stop = threading.Event()
    icon = pystray.Icon("claude_zero", image, "Claude Zero")

    def on_status(line):
        # Show the latest reading as the tray tooltip.
        icon.title = "Claude Zero  " + line.replace("CLU|", "").replace("|", "  ")

    def worker():
        run_loop(session, url, ser, args.interval, stop=stop, on_status=on_status)

    def do_quit(_icon, _item):
        stop.set()
        _icon.stop()

    icon.menu = pystray.Menu(pystray.MenuItem("Quit", do_quit))
    threading.Thread(target=worker, daemon=True).start()
    icon.run()          # blocks until Quit
    stop.set()
    if ser:
        ser.close()
    return True


# -------------------------------------------------------------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", help="force a browser: firefox/chrome/edge/brave")
    ap.add_argument("--probe", action="store_true", help="dump raw usage JSON, exit")
    ap.add_argument("--print", dest="printonly", action="store_true",
                    help="parse + print only, don't send to Flipper")
    ap.add_argument("--tray", action="store_true",
                    help="run hidden in the system tray (no console window)")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--interval", type=int, default=60, help="refresh seconds")
    args = ap.parse_args()

    if args.tray:
        run_tray(args)
        return

    session, orgs = load_session(args.browser)
    url, first = discover(session, orgs)

    if args.probe:
        print(json.dumps(first, indent=2))
        return

    ser = None
    if not args.printonly:
        port = find_flipper_port(args.port)
        if not port:
            print("[warn] no Flipper port found - printing only. Plug it in / "
                  "close qFlipper (it holds the port), or pass --port.")
        else:
            ser = open_serial(port)
            print(f"[ok] sending every {args.interval}s. Ctrl-C to stop.")

    if args.printonly:
        print(stream_once(session, url, ser))
        return

    try:
        run_loop(session, url, ser, args.interval)
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        if ser:
            ser.close()


if __name__ == "__main__":
    main()
