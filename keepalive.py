#!/usr/bin/env python3
"""
keepalive.py - nudge the mouse pointer now and then while you're away, so the
screensaver / screen lock, VPN and remote-desktop sessions don't time out.

* Only acts once the machine has seen no keyboard or mouse input at all for a
  random interval between min_wait and max_wait seconds, so it never moves the
  pointer while you are typing or using the mouse.
* Each nudge is a few pixels, and the pointer is put back where it was. It never
  clicks, types or changes window focus.
* Windows and macOS. Python 3.8+, standard library only.

Usage:
    python keepalive.py start       start in the background (no window)
    python keepalive.py stop        stop the background copy
    python keepalive.py status      show whether it's running and the settings in use
    python keepalive.py install     start automatically at login (and start now)
    python keepalive.py uninstall   remove the login item and stop it
    python keepalive.py run         run in the foreground with a log (Ctrl+C to quit)

Settings live in keepalive.json next to this script. Command-line options
(see --help) override them for that run.
"""

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import random
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

APP = "MouseKeepAlive"
SCRIPT = Path(__file__).resolve()
CONFIG_FILE = SCRIPT.with_name("keepalive.json")
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
POLL_SECONDS = 1.0
OFF_HOURS_POLL_SECONDS = 30.0
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAC_AGENT_LABEL = "com.mousekeepalive"

log = logging.getLogger(APP)


# ----------------------------------------------------------------------------- config

@dataclass
class Config:
    max_wait: float = 240.0
    min_wait: float = 30.0
    active_hours: Optional[Tuple[dt.time, dt.time]] = None  # None = all day
    active_days: Optional[Set[int]] = None                  # None = every day (0 = Monday)
    keep_awake: bool = True
    return_pointer: bool = True
    pause_when_locked: bool = True
    nudge_pixels: int = 3

    def describe(self) -> str:
        hours = "always" if self.active_hours is None else "{:%H:%M}-{:%H:%M}".format(*self.active_hours)
        days = "every day" if self.active_days is None else ",".join(DAY_NAMES[d] for d in sorted(self.active_days))
        onoff = lambda b: "on" if b else "off"
        return (f"max_wait={self.max_wait:g}s min_wait={self.min_wait:g}s active_hours={hours} "
                f"active_days={days} keep_awake={onoff(self.keep_awake)} "
                f"return_pointer={onoff(self.return_pointer)} pause_when_locked={onoff(self.pause_when_locked)} "
                f"nudge_pixels={self.nudge_pixels}")


def parse_hours(spec) -> Optional[Tuple[dt.time, dt.time]]:
    """'08:00-18:00' -> (08:00, 18:00). Overnight ranges like '22:00-06:00' work too."""
    if spec is None or str(spec).strip().lower() in ("", "always", "all", "*"):
        return None
    try:
        start_s, end_s = str(spec).split("-")
        start = dt.datetime.strptime(start_s.strip(), "%H:%M").time()
        end = dt.datetime.strptime(end_s.strip(), "%H:%M").time()
    except ValueError:
        raise ValueError(f"active_hours should look like 08:00-18:00, got {spec!r}")
    if start == end:
        raise ValueError("active_hours start and end are the same; use 'always' for all day")
    return start, end


def parse_days(spec) -> Optional[Set[int]]:
    """'mon-fri', 'mon,wed,fri', ['mon', 'tue'] or 'all'."""
    if spec is None:
        return None
    parts = spec if isinstance(spec, list) else str(spec).split(",")
    if len(parts) == 1 and str(parts[0]).strip().lower() in ("", "all", "every", "daily", "*"):
        return None

    def index(name: str) -> int:
        name = name.strip().lower()[:3]
        if name not in DAY_NAMES:
            raise ValueError(f"unknown day {name!r} in active_days (use mon, tue, ... sun)")
        return DAY_NAMES.index(name)

    days = set()
    for part in parts:
        part = str(part)
        if "-" in part:
            first, last = (index(p) for p in part.split("-", 1))
            d = first
            days.add(d)
            while d != last:
                d = (d + 1) % 7
                days.add(d)
        else:
            days.add(index(part))
    return days


def load_config(args) -> Config:
    raw = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            raw = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    known = set(Config.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown setting(s) in {CONFIG_FILE.name}: {', '.join(sorted(unknown))}")

    cli = {
        "max_wait": args.max_wait,
        "min_wait": args.min_wait,
        "active_hours": args.hours,
        "active_days": args.days,
        "keep_awake": args.keep_awake,
        "return_pointer": args.return_pointer,
        "pause_when_locked": args.pause_when_locked,
    }
    raw.update({k: v for k, v in cli.items() if v is not None})

    cfg = Config()
    if "max_wait" in raw:
        cfg.max_wait = float(raw["max_wait"])
    if "min_wait" in raw:
        cfg.min_wait = float(raw["min_wait"])
    if "active_hours" in raw:
        cfg.active_hours = parse_hours(raw["active_hours"])
    if "active_days" in raw:
        cfg.active_days = parse_days(raw["active_days"])
    for flag in ("keep_awake", "return_pointer", "pause_when_locked"):
        if flag in raw:
            setattr(cfg, flag, bool(raw[flag]))
    if "nudge_pixels" in raw:
        cfg.nudge_pixels = int(raw["nudge_pixels"])

    if cfg.max_wait < 10:
        raise ValueError("max_wait must be at least 10 seconds")
    if cfg.min_wait < 5:
        raise ValueError("min_wait must be at least 5 seconds")
    if cfg.min_wait > cfg.max_wait:
        cfg.min_wait = cfg.max_wait / 2
    if not 1 <= cfg.nudge_pixels <= 50:
        raise ValueError("nudge_pixels must be between 1 and 50")
    return cfg


def is_active_period(cfg: Config, now: dt.datetime) -> bool:
    if cfg.active_days is not None and now.weekday() not in cfg.active_days:
        return False
    if cfg.active_hours is None:
        return True
    start, end = cfg.active_hours
    t = now.time()
    if start < end:
        return start <= t < end
    return t >= start or t < end  # overnight range


# ----------------------------------------------------------------------------- platforms

class Platform:
    """What the main loop needs from the OS. Positions are in screen pixels."""

    def idle_seconds(self) -> float:
        """Seconds since the last keyboard/mouse input of any kind."""
        raise NotImplementedError

    def cursor_pos(self) -> Optional[Tuple[float, float]]:
        raise NotImplementedError

    def move_to(self, x: float, y: float) -> bool:
        """Move the pointer as real input (resets the OS idle timer)."""
        raise NotImplementedError

    def restore(self, x: float, y: float) -> None:
        self.move_to(x, y)

    def is_locked(self) -> bool:
        return False

    def set_keep_awake(self, on: bool) -> None:
        pass

    def startup_checks(self) -> None:
        pass


class WindowsPlatform(Platform):
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ct = ctypes
        user32 = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            user32.SetProcessDPIAware()  # work in real pixels on scaled displays
        except Exception:
            pass

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]  # largest member, so the size matches the real INPUT

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

        self.LASTINPUTINFO, self.MOUSEINPUT, self.INPUTUNION, self.INPUT = LASTINPUTINFO, MOUSEINPUT, INPUTUNION, INPUT
        self.POINT = wintypes.POINT

        user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        user32.GetLastInputInfo.restype = wintypes.BOOL
        kernel32.GetTickCount.restype = wintypes.DWORD
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
        user32.SwitchDesktop.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL
        kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        kernel32.SetThreadExecutionState.restype = wintypes.DWORD

    def idle_seconds(self) -> float:
        lii = self.LASTINPUTINFO(cbSize=self.ct.sizeof(self.LASTINPUTINFO))
        if not self.user32.GetLastInputInfo(self.ct.byref(lii)):
            return 0.0  # unknown: treat as "you're active" so we stay out of the way
        return ((self.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0

    def cursor_pos(self):
        pt = self.POINT()
        if not self.user32.GetCursorPos(self.ct.byref(pt)):
            return None  # e.g. on the secure desktop
        return pt.x, pt.y

    def move_to(self, x, y) -> bool:
        # Absolute SendInput over the whole virtual desktop: counts as real input (resets
        # the idle timer and is forwarded by RDP/Citrix clients) and isn't affected by
        # pointer acceleration.
        left, top = self.user32.GetSystemMetrics(76), self.user32.GetSystemMetrics(77)
        width, height = self.user32.GetSystemMetrics(78), self.user32.GetSystemMetrics(79)
        nx = int(round((x - left) * 65535 / max(width - 1, 1)))
        ny = int(round((y - top) * 65535 / max(height - 1, 1)))
        flags = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
        mi = self.MOUSEINPUT(nx, ny, 0, flags, 0, 0)
        inp = self.INPUT(type=0, u=self.INPUTUNION(mi=mi))
        return self.user32.SendInput(1, self.ct.byref(inp), self.ct.sizeof(self.INPUT)) == 1

    def restore(self, x, y) -> None:
        self.move_to(x, y)
        self.user32.SetCursorPos(int(x), int(y))  # exact pixel, in case of rounding

    def is_locked(self) -> bool:
        desk = self.user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not desk:
            return True
        try:
            return not self.user32.SwitchDesktop(desk)
        finally:
            self.user32.CloseDesktop(desk)

    def set_keep_awake(self, on: bool) -> None:
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
        self.kernel32.SetThreadExecutionState(flags)


class MacPlatform(Platform):
    HID_SYSTEM_STATE = 1      # kCGEventSourceStateHIDSystemState
    ANY_INPUT = 0xFFFFFFFF    # kCGAnyInputEventType
    MOUSE_MOVED = 5           # kCGEventMouseMoved
    HID_EVENT_TAP = 0         # kCGHIDEventTap
    UTF8 = 0x08000100         # kCFStringEncodingUTF8

    def __init__(self):
        import ctypes
        from ctypes import c_bool, c_double, c_int32, c_long, c_uint32, c_void_p, c_char_p

        self.ct = ctypes
        cg = self.cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        self.CGPoint = CGPoint
        cg.CGEventSourceSecondsSinceLastEventType.argtypes = [c_int32, c_uint32]
        cg.CGEventSourceSecondsSinceLastEventType.restype = c_double
        cg.CGEventSourceCreate.argtypes = [c_int32]
        cg.CGEventSourceCreate.restype = c_void_p
        cg.CGEventCreate.argtypes = [c_void_p]
        cg.CGEventCreate.restype = c_void_p
        cg.CGEventGetLocation.argtypes = [c_void_p]
        cg.CGEventGetLocation.restype = CGPoint
        cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
        cg.CGEventCreateMouseEvent.restype = c_void_p
        cg.CGEventPost.argtypes = [c_uint32, c_void_p]
        cg.CGEventPost.restype = None
        cg.CGSessionCopyCurrentDictionary.argtypes = []
        cg.CGSessionCopyCurrentDictionary.restype = c_void_p
        cg.AXIsProcessTrusted.argtypes = []
        cg.AXIsProcessTrusted.restype = c_bool
        cg.AXIsProcessTrustedWithOptions.argtypes = [c_void_p]
        cg.AXIsProcessTrustedWithOptions.restype = c_bool
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
        cf.CFDictionaryGetValue.restype = c_void_p
        cf.CFBooleanGetValue.argtypes = [c_void_p]
        cf.CFBooleanGetValue.restype = c_bool
        cf.CFDictionaryCreate.argtypes = [c_void_p, c_void_p, c_void_p, c_long, c_void_p, c_void_p]
        cf.CFDictionaryCreate.restype = c_void_p

        self.source = cg.CGEventSourceCreate(self.HID_SYSTEM_STATE)
        self.locked_key = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", self.UTF8)
        self.caffeinate = None

    def idle_seconds(self) -> float:
        return self.cg.CGEventSourceSecondsSinceLastEventType(self.HID_SYSTEM_STATE, self.ANY_INPUT)

    def cursor_pos(self):
        event = self.cg.CGEventCreate(None)
        if not event:
            return None
        try:
            p = self.cg.CGEventGetLocation(event)
            return p.x, p.y
        finally:
            self.cf.CFRelease(event)

    def move_to(self, x, y) -> bool:
        event = self.cg.CGEventCreateMouseEvent(self.source, self.MOUSE_MOVED, self.CGPoint(x, y), 0)
        if not event:
            return False
        try:
            self.cg.CGEventPost(self.HID_EVENT_TAP, event)
            return True
        finally:
            self.cf.CFRelease(event)

    def is_locked(self) -> bool:
        session = self.cg.CGSessionCopyCurrentDictionary()
        if not session:
            return False
        try:
            value = self.cf.CFDictionaryGetValue(session, self.locked_key)
            return bool(value) and self.cf.CFBooleanGetValue(value)
        finally:
            self.cf.CFRelease(session)

    def set_keep_awake(self, on: bool) -> None:
        if on and self.caffeinate is None:
            # -d display, -i idle sleep; -w makes it exit with us even if we're killed
            self.caffeinate = subprocess.Popen(
                ["caffeinate", "-d", "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not on and self.caffeinate is not None:
            self.caffeinate.terminate()
            self.caffeinate = None

    def accessibility_trusted(self, prompt: bool = False) -> bool:
        if not prompt:
            return self.cg.AXIsProcessTrusted()
        try:
            ct, cf = self.ct, self.cf
            key = ct.c_void_p.in_dll(self.cg, "kAXTrustedCheckOptionPrompt")
            true = ct.c_void_p.in_dll(cf, "kCFBooleanTrue")
            keys = (ct.c_void_p * 1)(key.value)
            values = (ct.c_void_p * 1)(true.value)
            key_cb = ct.addressof(ct.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
            value_cb = ct.addressof(ct.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
            options = cf.CFDictionaryCreate(None, keys, values, 1, key_cb, value_cb)
            try:
                return self.cg.AXIsProcessTrustedWithOptions(options)
            finally:
                cf.CFRelease(options)
        except Exception:
            return self.cg.AXIsProcessTrusted()

    def startup_checks(self) -> None:
        if not self.accessibility_trusted(prompt=True):
            log.warning(
                "macOS hasn't given this Python Accessibility permission, so pointer nudges will be "
                "ignored. Allow it in System Settings > Privacy & Security > Accessibility (Python: %s), "
                "then restart with: python3 %s stop && python3 %s start",
                os.path.realpath(sys.executable), SCRIPT, SCRIPT)


def get_platform() -> Platform:
    if IS_WIN:
        return WindowsPlatform()
    if IS_MAC:
        return MacPlatform()
    raise SystemExit("Sorry, only Windows and macOS are supported.")


# ----------------------------------------------------------------------------- core

def nudge(plat: Platform, cfg: Config, sleep=time.sleep, clock=time.monotonic) -> bool:
    start = plat.cursor_pos()
    if start is None:
        return False
    n = cfg.nudge_pixels
    dx = random.choice((-1, 1)) * random.randint(1, n)
    dy = random.choice((-1, 1)) * random.randint(1, n)
    moved_at = clock()
    if not plat.move_to(start[0] + dx, start[1] + dy):
        return False
    sleep(random.uniform(0.05, 0.25))
    # Only put the pointer back if nobody has touched anything since our move;
    # otherwise we'd be fighting the user.
    if cfg.return_pointer and plat.idle_seconds() + 0.1 >= clock() - moved_at:
        plat.restore(*start)
    return True


def run_loop(cfg: Config, plat: Platform, sleep=time.sleep, clock=time.monotonic,
             now=dt.datetime.now, should_stop: Callable[[], bool] = lambda: False) -> None:
    def pick_wait() -> float:
        return random.uniform(cfg.min_wait, cfg.max_wait)

    target = pick_wait()
    last_nudge = clock()
    state = None
    try:
        while not should_stop():
            if not is_active_period(cfg, now()):
                new_state = "outside active hours"
            elif cfg.pause_when_locked and plat.is_locked():
                new_state = "screen locked"
            else:
                new_state = "watching"

            if new_state != state:
                log.info("Now %s.", new_state)
                if cfg.keep_awake:
                    plat.set_keep_awake(new_state == "watching")
                last_nudge, target = clock(), pick_wait()
                state = new_state

            if state != "watching":
                sleep(OFF_HOURS_POLL_SECONDS if state == "outside active hours" else POLL_SECONDS)
                continue

            # Our own nudges reset the OS idle timer too, so this is "time since anyone,
            # you or us, last did anything". Any real input restarts the countdown.
            idle = min(plat.idle_seconds(), clock() - last_nudge)
            if idle >= target:
                if nudge(plat, cfg, sleep=sleep, clock=clock):
                    log.info("Nudged pointer after %.0fs with no input.", idle)
                else:
                    log.debug("Nudge skipped (pointer not available).")
                last_nudge, target = clock(), pick_wait()
            sleep(POLL_SECONDS)
    finally:
        if cfg.keep_awake:
            plat.set_keep_awake(False)


# ----------------------------------------------------------------------------- process management

def state_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    path = base / APP
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return state_dir() / "keepalive.log"


class InstanceLock:
    """Held for the life of the running copy; the OS drops it if the process dies."""

    def __init__(self):
        self.path = state_dir() / "keepalive.lock"
        self.fh = None

    def acquire(self) -> bool:
        fh = open(self.path, "a+b")
        try:
            if IS_WIN:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if self.fh is None:
            return
        if IS_WIN:
            import msvcrt
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.fh.close()
        self.fh = None


def pid_file() -> Path:
    return state_dir() / "keepalive.pid"


def running_pid() -> Optional[int]:
    """PID of the running copy (0 if running but unknown), or None if not running."""
    lock = InstanceLock()
    if lock.acquire():
        lock.release()
        return None
    try:
        return int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return 0


def setup_logging(verbose: bool, console: bool) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(log_path(), maxBytes=512_000, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console and sys.stderr is not None:  # pythonw has no stderr
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def python_for_background() -> str:
    if IS_WIN:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


def cmd_run(cfg: Config, verbose: bool) -> int:
    setup_logging(verbose, console=True)
    lock = InstanceLock()
    if not lock.acquire():
        log.info("Already running (pid %s); nothing to do.", running_pid() or "?")
        return 0
    pid_file().write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        plat = get_platform()
        log.info("Started (pid %d). %s", os.getpid(), cfg.describe())
        plat.startup_checks()
        run_loop(cfg, plat)
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped.")
    except Exception:
        log.exception("Crashed.")
        return 1
    finally:
        try:
            pid_file().unlink()
        except OSError:
            pass
        lock.release()
    return 0


def cmd_start(passthrough: List[str]) -> int:
    pid = running_pid()
    if pid is not None:
        print(f"Already running (pid {pid or '?'}).")
        return 0
    cmd = [python_for_background(), str(SCRIPT), "run", *passthrough]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    for _ in range(50):
        time.sleep(0.1)
        pid = running_pid()
        if pid is not None:
            print(f"Started in the background (pid {pid or '?'}). Log: {log_path()}")
            return 0
    print(f"It didn't start; see {log_path()}")
    return 1


def cmd_stop() -> int:
    pid = running_pid()
    if pid is None:
        print("Not running.")
        return 0
    if not pid:
        print("It's running but its PID file is missing; stop it from Task Manager / Activity Monitor.")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"Couldn't stop pid {pid}: {e}")
        return 1
    for _ in range(50):
        if running_pid() is None:
            print("Stopped.")
            return 0
        time.sleep(0.1)
    print(f"Asked pid {pid} to stop but it's still running.")
    return 1


WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def mac_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_AGENT_LABEL}.plist"


def autostart_installed() -> bool:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as key:
                winreg.QueryValueEx(key, APP)
            return True
        except OSError:
            return False
    return mac_agent_path().exists()


def cmd_install() -> int:
    if IS_WIN:
        import winreg
        command = f'"{python_for_background()}" "{SCRIPT}" run'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, command)
        print("Will start automatically when you log in.")
        return cmd_start([])
    if IS_MAC:
        import plistlib
        path = mac_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump({
                "Label": MAC_AGENT_LABEL,
                "ProgramArguments": [sys.executable, str(SCRIPT), "run"],
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            }, fh)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{MAC_AGENT_LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True)
        print(f"Will start automatically when you log in ({path}).")
        time.sleep(1.5)
        if running_pid() is None:
            return cmd_start([])
        print(f"Running. Log: {log_path()}")
        return 0
    raise SystemExit("Sorry, only Windows and macOS are supported.")


def cmd_uninstall() -> int:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, APP)
        except FileNotFoundError:
            pass
    elif IS_MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{MAC_AGENT_LABEL}"], capture_output=True)
        try:
            mac_agent_path().unlink()
        except FileNotFoundError:
            pass
    print("Removed from login items.")
    return cmd_stop()


def cmd_status(cfg: Config) -> int:
    pid = running_pid()
    print("Running:   " + ("no" if pid is None else f"yes (pid {pid or '?'})"))
    print("Autostart: " + ("installed" if autostart_installed() else "not installed"))
    print("Settings:  " + cfg.describe())
    print(f"Config:    {CONFIG_FILE}" + ("" if CONFIG_FILE.exists() else " (not found, using defaults)"))
    print(f"Log:       {log_path()}")
    if IS_MAC:
        trusted = MacPlatform().accessibility_trusted()
        print("Accessibility (this Python, as launched from here): " + ("granted" if trusted else "NOT granted"))
        print(f"Python:    {os.path.realpath(sys.executable)}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        description="Nudge the mouse occasionally while you're idle to keep the screen, VPN and remote sessions alive.")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "start", "stop", "status", "install", "uninstall"])
    p.add_argument("--max-wait", type=float, help="longest idle time (s) before a nudge (default 240)")
    p.add_argument("--min-wait", type=float, help="shortest idle time (s) before a nudge (default 30)")
    p.add_argument("--hours", help="only active between these times, e.g. 08:00-18:00, or 'always'")
    p.add_argument("--days", help="only active on these days, e.g. mon-fri, or 'all'")
    p.add_argument("--no-keep-awake", dest="keep_awake", action="store_const", const=False,
                   help="don't also ask the OS to keep the display/system awake")
    p.add_argument("--no-return", dest="return_pointer", action="store_const", const=False,
                   help="leave the pointer where the nudge put it")
    p.add_argument("--no-pause-when-locked", dest="pause_when_locked", action="store_const", const=False,
                   help="keep going even when you've locked the screen")
    p.add_argument("-v", "--verbose", action="store_true", help="more detail in the log")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Config problem: {e}", file=sys.stderr)
        return 2

    if args.command == "run":
        return cmd_run(cfg, args.verbose)
    if args.command == "start":
        passthrough = list(argv)
        passthrough.remove("start")
        return cmd_start(passthrough)
    if args.command == "stop":
        return cmd_stop()
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "install":
        return cmd_install()
    return cmd_uninstall()


if __name__ == "__main__":
    sys.exit(main())
