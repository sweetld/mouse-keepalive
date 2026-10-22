# Mouse keep-alive

Moves the mouse pointer a few pixels now and then while you're away, so screensavers, screen locks, VPN sessions and remote-desktop/VM sessions don't time out. It leaves you alone while you're working.

It's a single Python file, `keepalive.py`, that runs on Windows, macOS and Linux (X11 desktops). It needs Python 3.8 or later and nothing else.

**Download:** get the latest `mouse-keepalive-<version>.zip` from [Releases](https://github.com/sweetld/mouse-keepalive/releases/latest) and unzip it anywhere, then follow the steps for your OS below.

## How it behaves

- It reads the operating system's "time since last keyboard or mouse input" counter about once a second.
- It nudges the pointer only after that counter reaches a random target between `min_wait` and `max_wait` seconds. Any key press or mouse movement from you resets the counter, so it can't fire while you're typing or using the mouse.
- After each nudge it picks a new random target. Because the target is random, it often fires well before `max_wait`, but it never waits longer.
- A nudge moves the pointer 1–3 pixels, then about 0.1s later puts it back exactly where it was. If you touch anything in that moment, it doesn't move the pointer back. It never clicks, types or changes which window has focus.
- Outside `active_hours`/`active_days`, or while you've locked the screen, it does nothing, so the machine can sleep as usual.
- During active hours it also asks the OS to keep the display and system awake (`keep_awake`).

## Settings (`keepalive.json`)

| Setting | Default | Meaning |
|---|---|---|
| `max_wait` | `240` | Longest idle time in seconds before a nudge. Keep it below your shortest timeout (screensaver, VPN, RDP). |
| `min_wait` | `30` | Shortest idle time before a nudge. |
| `active_hours` | `"08:00-18:00"` | Only runs during these hours. Overnight ranges like `"22:00-06:00"` work. Use `"always"` for all day. |
| `active_days` | `"mon-fri"` | Days to run, e.g. `"mon-fri"`, `"mon,wed,fri"` or `"all"`. |
| `keep_awake` | `true` | Also stop the display or system from sleeping during active hours. |
| `return_pointer` | `true` | Put the pointer back after each nudge. |
| `pause_when_locked` | `true` | Do nothing while you've locked the screen. |
| `nudge_pixels` | `3` | Largest distance of a nudge, in pixels. |

Restart it after editing the file (`stop`, then `start`). Command-line options override the file for one run. See `python keepalive.py --help`; for example: `python keepalive.py start --max-wait 120 --hours always`.

Logs are written to `%LOCALAPPDATA%\MouseKeepAlive\keepalive.log` on Windows, `~/Library/Application Support/MouseKeepAlive/keepalive.log` on macOS and `~/.local/state/MouseKeepAlive/keepalive.log` on Linux.

## Windows

1. If Python isn't installed, install it from python.org or the Microsoft Store, or run `winget install Python.Python.3.12`.
2. Open a terminal in this folder and run:

```
python keepalive.py install     # start at every login, and start now (no window)
python keepalive.py status
python keepalive.py stop
python keepalive.py start
python keepalive.py uninstall   # remove from login and stop
```

It runs silently under `pythonw.exe`. Login autostart uses your user's `Run` registry key, so admin rights aren't needed.

## macOS

1. Run `python3 --version`. If macOS offers to install the developer tools, accept.
2. In Terminal, in this folder, run `python3 keepalive.py install`.
3. **Grant Accessibility permission.** macOS ignores synthetic mouse moves until you do. The first run should show a prompt. If it doesn't, open System Settings → Privacy & Security → Accessibility and enable **Python** (and **Terminal** if you start it from Terminal). `python3 keepalive.py status` shows the Python path to add with the **+** button. Then run `python3 keepalive.py stop` and `python3 keepalive.py start`.
4. Check the log for "Nudged pointer". If you see an Accessibility warning there instead, the permission isn't in place yet.

Login autostart is a LaunchAgent at `~/Library/LaunchAgents/com.mousekeepalive.plist`.

## Linux

Works in X11 sessions with any desktop (GNOME, KDE Plasma, Cinnamon, MATE, Xfce and others). It uses the X libraries every X11 desktop already has (libX11, libXss, libXtst), so there's nothing to install.

1. Check you're on X11: `echo $XDG_SESSION_TYPE` should print `x11`. If it prints `wayland`, see below.
2. In a terminal in your desktop session, in this folder, run:

```
python3 keepalive.py install     # start at every login, and start now
python3 keepalive.py status
python3 keepalive.py stop
python3 keepalive.py start
python3 keepalive.py uninstall   # remove from login and stop
```

Login autostart is `~/.config/autostart/mouse-keepalive.desktop`, so it also shows up in your desktop's Startup Applications settings. Screen-lock detection asks the GNOME, Cinnamon, MATE or freedesktop screensaver service over D-Bus (via `gdbus`). If your desktop has none of those, it's treated as never locked. `keep_awake` uses `gnome-session-inhibit` on GNOME and `systemd-inhibit` elsewhere.

**Wayland isn't supported yet.** Wayland doesn't let ordinary apps read the idle time or move the pointer, so the script refuses to start there. Choose an X11 session at the login screen instead (on GNOME, click the gear icon and pick "GNOME on Xorg").

## Remote desktops / VMs

For a remote session (RDP, Citrix, VMware Horizon, etc.) to see the nudge, its window has to be the one in front when the nudge happens. That's usually the case when you leave a session open full-screen. If the remote session sits in a background window, only the local machine is kept alive.

## Licence

MIT. See [LICENSE](LICENSE).
