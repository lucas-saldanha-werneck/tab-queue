# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-17
- `key_in_link` setting (`TQ_KEY_IN_LINK=0`): the page also opens at `/` with no key, for a server that already sits
  behind a login such as Cloudflare Access. The keyed link keeps working; `tab-queue link` prints the plain address.

## [0.1.0] - 2026-11-17
- First version: JXA robot (LaunchAgent every 30 s), Python standard-library server (local LaunchAgent or Docker),
  dark phone page (All / Videos / Lavish / Pages, Open, Done with Undo, send a link), `tab-queue` command
  (install, status, link, rotate-key, send-front, once, serve, uninstall) and a Claude Code skill.
- Opens tabs with one osascript process that gives the screen back to the front app (Chrome may flash for under 1 s).
- Closes a tab only when its id and real address both match; suspended tabs (`park.html?url=`) are resolved.
