---
name: tab-queue
description: 'Set up and look after tab-queue: one Chrome window on the Mac becomes a queue the user sees on the phone or another computer; Done closes the tab on the Mac, links sent from the phone open on the Mac in the background. Use when the user says "tab-queue", "tab queue", "queue of tabs", "install the tab queue", "is the queue working?", "the queue stopped", "give me the queue link", "change the queue link/key", "the link leaked", "send this tab to the queue", or wants to read, watch or close Mac Chrome tabs from the phone.'
argument-hint: "[install|status|link|rotate-key|send-front|once|uninstall]"
version: "0.2.1"
metadata:
  author: Lucas Saldanha Werneck
  tags: [chrome, tabs, macos, phone]
---

# tab-queue

Everything goes through `${CLAUDE_PLUGIN_ROOT}/bin/tab-queue` (Python 3.9+, macOS). `tab-queue --help` lists every command.

## Pieces

- **Robot** (LaunchAgent `local.tab-queue.robot`, every 30 s): `/usr/bin/osascript -l JavaScript robot.js`. Reads the tabs of the queue window, sends them to the server, closes tabs marked Done (only when the tab id AND its address still match), opens links sent from the phone, reopens after Undo. Never opens a Chrome window.
- **Server** (Python standard library + SQLite): local LaunchAgent `local.tab-queue.server`, or Docker on a server the user runs (`server/Dockerfile`, `server/docker-compose.example.yml`).
- **Page**: `<public_url>/<secret key>/`. Filters All / Videos / Lavish / Pages, Open, Done with Undo, a field to send a link.

## Instructions

1. **Install.** Ask which server: on this Mac (default; the page only works while the Mac is awake and reachable) or a server they already run. Then:
   - local: `tab-queue install [--lang pt] [--listen 0.0.0.0] [--rewrite FROM=TO]`
   - remote: the robot token must be in a file (never paste it in the chat or on a command line): `tab-queue install --server https://host --token-file PATH [--public-url URL]`
   - optional: `--window <id>` (default: the Chrome window with most tabs, remembered after), `--route REGEX` (new tabs matching it in other windows move to the queue; default local lavish pages), `--link-bin`.
   The first run may show a macOS prompt to let `osascript` control Google Chrome and System Events. The user must click Allow (use the mac-remote skill if they are away).
2. **Link.** `tab-queue link` prints the page link and saves it to `~/.config/tab-queue/link.txt`. Give it to the user. It is a secret: anyone with it sees the tab titles.
3. **Check.** `tab-queue status` (exit 1 = something is wrong; it prints why). `tab-queue once` runs the robot now and prints its last log lines. Log with seconds: `~/.local/state/tab-queue/robot.log`.
4. **Leaked link.** `tab-queue rotate-key`: new link, the old one answers 404 at once.
5. **Mac shortcut.** `tab-queue send-front` moves the front Chrome tab to the queue window. To put it on a key: a Shortcuts.app shortcut with a "Run Shell Script" action calling it, then a keyboard shortcut in its settings.

## Facts that drive the design

- Chrome's AppleScript `move tab` loses the page (it leaves an empty New Tab), so a move is: open the same link in the queue window, check it is there, close the original.
- `open -g` still brings Chrome to the front. The default `applescript` method opens the tab and gives the screen back in the same osascript process (Chrome may flash in front for under a second).
- Done waits `undo_seconds` (default 10) before the robot may close the tab; Undo after that asks the robot to open the link again.
- After a Chrome restart tab ids change; the server finds tabs by address and sends fresh ids, so the id-and-address check still holds.

Do not edit the installed copy in `~/.local/share/tab-queue/app`; run `tab-queue install` again after a plugin update.
