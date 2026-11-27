# Claude Zero

Show your live **Claude.ai usage** on a **Flipper Zero** screen.

The Flipper displays your current **session** and **weekly** usage as bars, with
the reset times, fed over USB by a small program on your PC.

```
  CLAUDE ZERO            *     <- burst mark, top-right
  Session 20%      1:10a       <- session reset (only shown when > 0%)
  [####------------------]
  Week 29%
  [######----------------]
  resets Fri 9a                <- weekly reset
```

## How it works

The Flipper can't make HTTPS calls, so this uses a PC-side helper, the same
pattern as the popular PC-monitor apps:

```
claude.ai (your login)  ->  PC backend (Python)  ->  USB serial  ->  Flipper app
   reads usage endpoint      formats the numbers      230400 baud     draws the screen
```

The backend reuses **your own** logged-in browser session to read the usage
numbers, formats them into a tiny line, and streams it to a custom Flipper app
over the USB serial (CDC) link.

## ⚠️ Honest caveats — read these

- **Unofficial.** Claude.ai has no public API for subscription usage. This reads
  the same internal endpoint the Settings → Usage page uses, by reusing your
  browser's session cookie. It can break any time Anthropic changes that page.
- **Personal use, your account only.** Don't point it at anyone else's session.
- **Your session cookie is sensitive.** The backend reads it from your browser
  at runtime (via `browser-cookie3`) and sends it only to claude.ai. Nothing is
  written to disk. Still, treat this like your password and keep it local.
- Official **Flipper firmware** only has been tested (fw 1.5.x / API 88.x). Custom
  firmware may work but rebuild against its SDK.

## Requirements

- A Flipper Zero on official firmware, connected by USB.
- Python 3.9+ on the PC.
- [`ufbt`](https://pypi.org/project/ufbt/) to build the Flipper app (`pip install ufbt`).
- You must be **logged into claude.ai** in a supported browser
  (Firefox / Chrome / Edge / Brave).

## 1. Build & install the Flipper app

```
cd app
pip install ufbt
ufbt            # builds dist/claude_usage.fap
```

Copy `app/dist/claude_usage.fap` to the Flipper's SD card under `/ext/apps/USB/`
using qFlipper's file manager, then run it on the device:
**Apps → USB → Claude Zero**. You'll see "Connect PC backend".

(Or, with the Flipper plugged in and qFlipper closed: `ufbt launch` builds,
uploads, and starts it in one step.)

## 2. Run the PC backend

```
cd pc
pip install -r ../requirements.txt
python claude_usage_backend.py          # streams to the Flipper
```

Useful flags:
- `--print`  parse and print once, don't touch the Flipper (quick sanity check).
- `--probe`  dump the raw usage JSON (handy if the fields ever change).
- `--tray`   run hidden in the system tray (no console window); right-click the
  tray icon → **Quit** to stop.
- `--browser firefox|chrome|edge|brave`  force which browser's cookies to use.
- `--port COM5`  set the serial port manually (otherwise auto-detected).

**Port rule:** only one program can hold the Flipper's USB port at a time. Close
**qFlipper** (and any browser tab on `lab.flipper.net`) before running the backend.

## 3. (Windows, optional) One-click desktop button

From `pc/`, double-click **`install_desktop_button.bat`** once. It puts a
**Claude Zero** shortcut on your desktop (with the icon) that launches the feed
straight into the system tray — no console window.

## Troubleshooting

- **`Access is denied` on the COM port** — qFlipper, a browser serial tab, or
  another backend instance is holding it. Close them and retry.
- **Screen stays on "Connect PC backend"** — the backend isn't running or grabbed
  the wrong port. Run `python claude_usage_backend.py --print` to confirm it reads.
- **The new app version isn't showing after a reflash** — the running app holds
  the USB port, so the upload was a no-op. Press **Back** on the Flipper to exit
  the app first, then `ufbt launch` again.
- **Wrong numbers / blank reset** — run `--probe` and compare the JSON field names
  to `parse_usage()` in the backend.

## Repo layout

```
app/                     Flipper app (C, built with ufbt)
  application.fam
  claude_usage_app.c
pc/                      PC-side backend + Windows helpers
  claude_usage_backend.py
  claude_zero.ico
  run_claude_usage.bat         launch the tray feed
  install_desktop_button.bat   create the desktop shortcut
  create_shortcut.ps1
requirements.txt
```

## License

MIT — see [LICENSE](LICENSE).

---

*Not affiliated with or endorsed by Anthropic or Flipper Devices. "Claude" and
"Flipper Zero" are trademarks of their respective owners.*
