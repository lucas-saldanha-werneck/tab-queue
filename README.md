# tab-queue

[![CI](https://github.com/lucas-saldanha-werneck/tab-queue/actions/workflows/test.yml/badge.svg)](https://github.com/lucas-saldanha-werneck/tab-queue/actions/workflows/test.yml)
[![License](https://img.shields.io/github/license/lucas-saldanha-werneck/tab-queue)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-blue)](CHANGELOG.md)

One Chrome window on your Mac becomes a queue you can see from your phone or another computer. You watch, read or
reply, then tap **Done**, and the tab closes on the Mac. A link you send from the phone opens on the Mac in the
background, at the end of the queue window.

It is a Claude Code plugin (a skill that installs and checks it), but the tool itself is a small command, a robot and
a web page, and works without Claude.

```
Chrome on the Mac          robot (every 30 s)            server                 phone / other computer
queue window  ───────────► reads the tabs ─────────────► keeps the list ◄─────── secret link
              ◄─────────── closes / opens tabs ◄──────── Done, Undo, new link ─┘
```

## What you get

- Every tab of the queue window is in the list. A tab you close by hand on the Mac leaves the list.
- Filters: All, Videos (YouTube, Vimeo), Lavish (local `lavish-axi` review pages), Pages.
- **Open** opens the page on the phone (YouTube links open the YouTube app). Links can be rewritten first, for example
  a local address to its public one.
- **Done** hides the item at once and closes the tab on the Mac after the Undo time (10 s by default). **Undo** within
  that time keeps the tab; Undo later opens the link again on the Mac.
- A field to send a link: it opens on the Mac in the background. `page/?add=<url>` pre-fills it (handy for an iOS
  Shortcut in the Share sheet).
- Tabs frozen by suspender extensions (`park.html?...&url=`) are shown with their real address.
- New lavish pages that open in another window move to the queue window by themselves (configurable regex list).
- `tab-queue send-front` moves the front Chrome tab to the queue window (put it on a keyboard shortcut).

## Requirements

macOS with Google Chrome, `/usr/bin/python3` (Xcode Command Line Tools) and, for a server elsewhere, Docker there.

## Install

As a Claude Code plugin:

```bash
claude plugin marketplace add lucas-saldanha-werneck/tab-queue
claude plugin install tab-queue@tab-queue
```

Then ask Claude to "install the tab queue", or run the command yourself (`bin/tab-queue` in the plugin folder, or a
clone of this repo).

**Server on this Mac** (simplest; the page works while the Mac is awake and reachable from the phone):

```bash
bin/tab-queue install --lang en --link-bin        # --listen 0.0.0.0 to reach it from other devices on your network
tab-queue link                                    # prints http://127.0.0.1:8765/<secret>/
```

**Server elsewhere** (the list stays readable while the Mac sleeps; Done waits for the Mac to wake up):

```bash
# on the server, in a folder with server/app.py, page.html, Dockerfile and docker-compose.example.yml:
echo "TQ_TOKEN=$(openssl rand -hex 24)" > .env && chmod 600 .env
cp docker-compose.example.yml docker-compose.yml && docker compose up -d --build
# put HTTPS in front (Traefik/Caddy/nginx). Copy the token into a file on the Mac, then:
bin/tab-queue install --server https://queue.example.com --token-file ~/tq-token --link-bin
rm ~/tq-token   # the token is now in ~/.config/tab-queue/config.json (mode 600)
tab-queue link
```

The first robot run may ask to let `osascript` control **Google Chrome** and **System Events**. Click Allow.

## Commands

| Command | What it does |
|---|---|
| `tab-queue install [options]` | Writes the config, copies the app to `~/.local/share/tab-queue/app`, loads the LaunchAgents. Run it again after an update; it keeps the token. |
| `tab-queue status` | Installed version, LaunchAgents, server health, last robot sync, last log lines. Exit 1 when something is wrong. |
| `tab-queue link` | Prints the page link (also saved to `~/.config/tab-queue/link.txt`, mode 600). |
| `tab-queue rotate-key` | New secret link; the old one answers 404 at once. |
| `tab-queue send-front` | Moves the front Chrome tab to the queue window. |
| `tab-queue once` | Runs the robot once, verbose. |
| `tab-queue serve` | Runs the local server in the foreground. |
| `tab-queue uninstall` | Removes the LaunchAgents (keeps config and data). |

Install options: `--window <id>` (default `largest`: the Chrome window with most tabs, remembered after; if its id
changes after a Chrome restart, the robot finds the window that holds the same tabs), `--interval 30`,
`--open-method applescript|open-g`, `--route REGEX` (repeatable; `--route ""` turns routing off), `--lang en|pt`,
`--rewrite FROM=TO`, `--port`, `--listen`, `--public-url`.

## Configuration

`~/.config/tab-queue/config.json` (robot): `server`, `public_url`, `token`, `window`, `interval`, `open_method`,
`route_to_queue`, `chrome_app`.

Server settings come from environment variables or a JSON file named by `TQ_CONFIG`
(see [server/server.example.json](server/server.example.json)):

| Setting | Env | Default |
|---|---|---|
| robot token (required, 16+ chars) | `TQ_TOKEN` | none |
| data folder | `TQ_DATA` | `server/data` |
| address, port | `TQ_HOST`, `TQ_PORT` | `127.0.0.1`, `8765` |
| page language (`en`, `pt`) | `TQ_LANG` / `lang` | `en` |
| Undo time in seconds | `TQ_UNDO_SECONDS` / `undo_seconds` | `10` |
| link rewrites `[{"from","to"}]` | `TQ_REWRITES` / `rewrites` | none |
| what counts as lavish (regex list) | `TQ_LAVISH_MATCH` / `lavish_match` | `127.0.0.1:4387/session/` |
| order: `newest` or `window` | `order` | `newest` |
| YouTube channel names (youtube.com/oembed) | `TQ_OEMBED=0` turns off / `oembed` | on |

The secret key is created on first start and kept in `<data>/key` (mode 600). `TQ_KEY` sets the first one.

## How it opens a tab without taking the screen

Measured on macOS 26 with Chrome:

- `open -g -a "Google Chrome" URL` still brings Chrome to the front. Kept only as `--open-method open-g`.
- AppleScript `make new tab` alone brings Chrome to the front too.
- Default (`applescript`): **one** osascript process remembers the front app, makes the tab at the end of the queue
  window and activates the front app again. Chrome was in front for 0.0 to 0.6 s in 5 of 5 runs. Doing the last step
  in a separate process left Chrome in front for 3.5 to 8.8 s. So Chrome may flash in front for under a second.
- Chrome's AppleScript `move tab` loses the page (the moved tab becomes an empty New Tab). So "move to the queue
  window" opens the same link in the queue window, checks it is there, and only then closes the original tab.
- A Chrome extension calling `chrome.tabs.create({active: false})` would open tabs with no flash at all. It is not
  built; it would need a one-time install of an unpacked extension.

The robot logs the front app before and after each open (`front app before=… after=… (kept)`).

## Safety

- The robot never opens a Chrome window and never touches incognito windows.
- It closes a tab only when the tab id **and** its real address still match what the server asked for. After a Chrome
  restart ids change; the server finds tabs by address and sends the new ids on the next run.
- It does nothing while Chrome is closed or started less than 90 s ago (session restore).
- Anyone with the page link can see your tab titles and close tabs. Use HTTPS, and `tab-queue rotate-key` if it leaks.
- Pages that need a login (LinkedIn, claude.ai…) open on the phone only if you are logged in there.

See [SECURITY.md](SECURITY.md) for what the tool reads, stores and sends.

## Tests

```bash
node --test tests/core.test.js            # robot logic
/usr/bin/python3 -m unittest discover -s tests -v   # server, CLI, and the real robot against a fake Chrome (macOS)
```

The robot tests run the real `robot.js` through `osascript` with `TQ_FAKE_CHROME` pointing at a JSON file that stands
in for Chrome, against a real local server. Nothing in the test suite touches your Chrome.

## License

MIT
