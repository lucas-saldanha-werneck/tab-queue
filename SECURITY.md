# Security Policy

## Reporting a vulnerability

Please report it privately: open the repository's **Security** tab and choose **Report a vulnerability**
(https://github.com/lucas-saldanha-werneck/tab-queue/security/advisories/new). Please do not open a public issue for a
security problem.

## Supported versions

Only the latest release is supported.

## What tab-queue accesses

- **Chrome on your Mac**, through Apple's `osascript` (AppleScript/JXA): reads the address and title of the tabs of
  every normal window (to find the queue window and new pages to route), closes tabs in the queue window that you
  marked Done, opens links in the queue window. It may also close a tab in another window after opening the same link
  in the queue window (routing and `send-front`). It never opens a window and skips incognito windows.
- **System Events**: asks for the name of the front app, so it can give the screen back after opening a tab.
- **Network**: the robot sends the queue window's addresses and titles to the server you configure (`curl`, with the
  robot token in a temporary header file, never on the command line). The server fetches channel names of new
  YouTube videos from `youtube.com/oembed` (turn off with `TQ_OEMBED=0`). The page loads video thumbnails from
  `i.ytimg.com`.
- **Files**: config and token in `~/.config/tab-queue/` (mode 600), the app copy in `~/.local/share/tab-queue/app`,
  local server data (SQLite with addresses and titles, the secret key) in `~/.local/share/tab-queue/data`, logs in
  `~/.local/state/tab-queue/`. The local server LaunchAgent holds the token in its plist (mode 600).
- **The page link** is the only protection of the page: anyone with it can see your tab titles, close tabs and open
  links on your Mac. Serve it over HTTPS and rotate it with `tab-queue rotate-key` if it leaks.
