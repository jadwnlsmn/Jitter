"""Jitter: Windows screen-control MCP server. Screenshots plus mouse/keyboard,
UI Automation element clicks, window focus, and a compact command language
with saved routes.

Forked from winuse-lite (itself sifted from ryanportfolio/winuse-mcp, MIT).
Input is refused when the target window is a terminal or Claude itself.
"""
import base64
import asyncio
import ctypes
import functools
import io
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # match capture and click pixels

import pyautogui
import pygetwindow as gw
import uiautomation as uia
from uiautomation.uiautomation import _AutomationClient
from mcp.server.fastmcp import FastMCP, Image

pyautogui.FAILSAFE = True  # park mouse at (0, 0) to abort
pyautogui.PAUSE = 0.05
MAX_EDGE = 1372
VERSION = "1.0.0"
# not editable: typing into these would bypass Claude's own permission prompts
WHERE_BLOCKED = "the off-limits list is in the Jitter panel: Access > Off-limits windows"
ALWAYS_BLOCKED = ("powershell", "command prompt", "cmd.exe", "terminal", "git bash", "mingw", "claude")
HERE = Path(__file__).parent
ROUTES, ADDRESSES, SETTINGS = HERE / "routes", HERE / "addresses", HERE / "settings.json"
PACKAGES = HERE / "packages.json"  # login packages, encrypted for this Windows user (see deliver)
# hold_seconds: safety release when the client never calls done()
DEFAULTS = {"blocked_titles": [], "hold_seconds": 120, "border_px": 16, "opacity": 0.6, "countdown_seconds": 3,
            "handoff_minutes": 10}
LIMITS = {"hold_seconds": (10, 3600), "border_px": (1, 60), "opacity": (0.1, 1.0), "countdown_seconds": (0, 10),
          "handoff_minutes": (1, 60)}
_cfg = {"mtime": None, "cfg": DEFAULTS}


def settings() -> dict:
    """Settings from settings.json, reloaded whenever the file changes, so panel edits apply live."""
    try:
        m = SETTINGS.stat().st_mtime
    except OSError:
        return DEFAULTS
    if m != _cfg["mtime"]:
        try:
            _cfg["cfg"] = {**DEFAULTS, **json.loads(SETTINGS.read_text(encoding="utf-8"))}
        except ValueError:
            pass  # half-written file: keep the last good settings
        _cfg["mtime"] = m
    return _cfg["cfg"]


# --- Claude Code's own permissions for Jitter, switched on and off from the panel ---

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
# (label, where, rule): "allow" = permissions.allow (no prompt), "auto" = autoMode.allow (safety-check context)
CLAUDE_PERMISSIONS = [
    ("Use Jitter without asking each time", "allow", "mcp__jitter"),
    ("Read and summarize what's in my apps (chats, pages, forms) through Jitter", "auto",
     "Jitter screen access: the user installed the Jitter MCP server so Claude can see and operate their desktop. "
     "Reading on-screen app content (chats, pages, forms) through Jitter tools and summarizing it for the user "
     "is expected."),
    ("Build and test Jitter, including code that reads my app windows and Windows usage stats", "auto",
     f"Jitter development: building, testing and running code in {HERE}, including code that reads the "
     "user's own app windows and Windows UserAssist usage stats for Jitter's Immerse, is expected."),
]


def load_claude() -> dict:
    return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) if CLAUDE_SETTINGS.exists() else {}


def rules(cfg: dict, where: str) -> list:
    return cfg.get("permissions", {}).get("allow", []) if where == "allow" else cfg.get("autoMode", {}).get("allow", [])


def set_permission(where: str, rule: str, on: bool) -> None:
    """Add or remove one rule in ~/.claude/settings.json, leaving everything else in the file as it was."""
    cfg = load_claude()  # re-read: Claude Code may have changed the file since the panel opened
    backup = CLAUDE_SETTINGS.with_name("settings.json.jitter-backup")
    if CLAUDE_SETTINGS.exists() and not backup.exists():
        backup.write_bytes(CLAUDE_SETTINGS.read_bytes())
    if where == "allow":
        lst = cfg.setdefault("permissions", {}).setdefault("allow", [])
    else:  # "$defaults" keeps Claude's built-in safety rules in force alongside ours
        lst = cfg.setdefault("autoMode", {}).setdefault("allow", ["$defaults"])
    if on and rule not in lst:
        lst.append(rule)
    if not on and rule in lst:
        lst.remove(rule)
    if where == "auto" and lst == ["$defaults"]:
        del cfg["autoMode"]["allow"]
        if not cfg["autoMode"]:
            del cfg["autoMode"]
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CLAUDE_SETTINGS)


def save_settings(new: dict) -> dict:
    cfg = {**DEFAULTS, **new}
    for k, (lo, hi) in LIMITS.items():
        cfg[k] = max(lo, min(float(cfg[k]), hi))
    cfg["blocked_titles"] = sorted({t.strip().lower() for t in cfg["blocked_titles"] if t.strip()})
    SETTINGS.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg

mcp = FastMCP("jitter", instructions="""\
Jitter operates the Windows desktop like a person, and it is meant for autonomous work.

Finish the task. The ONLY reason to stop is a permission restriction: Jitter refusing a window (PermissionError),
the user pressing a key (they took the screen back; ask before acting again), or Claude Code denying a tool. Never
work around those. Everything else is a detour, not an end: a renamed window or tab, a moved or renamed button, a
changed layout, a failed route, a dialog in the way, slow loading. Look (read() or screenshot), adapt, try another
way, and keep going. Rotate through what you have: element names (E), coordinates (C), the keyboard (K: tab,
enter, escape, ctrl+f, alt+letter, arrows), search boxes, URLs (G), scrolling, other windows and tabs (F also
launches apps that aren't open). If something you need is truly missing, say what tool would close the gap.

- A run that hits a non-permission failure returns a DETOUR: what failed, the window's text with @x,y (a picture only
  if the text is unreliable), and the remaining commands. Fix the failed step your own way, then run the rest.
- Speed matters. Once you know an app, do the whole task in ONE run() call ending in S instead of a call and a
  screenshot per step. Prefer E(name) to C(x,y): names survive layout changes.
- Text first, images last: screenshots cost far more than text. read() or X returns every piece of text and
  every clickable item in the window with its @x,y, from accessibility plus OCR; it returns a picture instead
  only when the text is unusable. Click by E(name) or C(x,y) from that list. Take a screenshot only to see what
  text can't carry: images, colors, video, icons without text, or a layout question. To find something by
  sight in a long list or grid (gifs, images, unnamed items), use scan(): one call scrolls the area and returns
  every view as one numbered sheet.
- When asked to send something without a destination, send it to the chat or conversation that is open.
- Sign-ins, passwords, 2FA codes, CAPTCHAs and payments are the user's to do: open that page, then call
  human("Human login required") (or U(...) in a run). Jitter hands over the screen and can't see it until the
  user clicks done. Never type, read or ask for credentials. This is not a stop: continue the task afterwards.
  If packages() lists a login package for that site, use deliver(name) (or L(name) in a run) instead: the user
  approves it in a box and Jitter fills the form without you seeing the login. Then submit the form.
- When asked to write or create something in an app, start a new document or tab (ctrl+n, ctrl+t) instead of
  typing into whatever is open there. Never edit the user's existing documents unless asked to.
- Before a task, call routes(app): run a saved route with R(app/task, args...). Its address book lists the app's
  clickable names. After a task likely to repeat, or after a route needed a detour, save_route() it so it holds
  up next time, using $1, $2 for the parts that change.
- Call done() when the task is finished, so the overlay goes away and the user gets the screen back.
- If the user asks for Jitter settings or to run Immerse, call panel(): Immerse is the user's button to press.""")
# --- journal: every tool call, how long it took and how it ended, so a task can be reviewed afterwards ---

JOURNAL = HERE / "journal.jsonl"
_depth = [0]  # calls made inside run() are nested under it


def _clip(v, n: int = 120) -> str:
    s = v if isinstance(v, str) else repr(v)
    return s if len(s) <= n else s[:n] + "..."


def _outcome(result) -> tuple[str, str]:
    text = result if isinstance(result, str) else result[0] if isinstance(result, list) and result and isinstance(result[0], str) else ""
    if "STOPPED by a permission" in text:
        return "permission", text.splitlines()[-3] if text.count("\n") > 2 else text
    if "DETOUR" in text:
        failed = next((line for line in text.splitlines() if ": FAILED, " in line), "")
        return "detour", failed
    return "ok", text.splitlines()[0] if text else ""


def _journal(fn):
    @functools.wraps(fn)
    def logged(*args, **kwargs):
        start, outcome, note = time.monotonic(), "ok", ""
        _depth[0] += 1
        try:
            result = fn(*args, **kwargs)
            outcome, note = _outcome(result)
            return result
        except Exception as e:
            outcome = "permission" if isinstance(e, (PermissionError, UserStop)) else "error"
            note = f"{e.__class__.__name__}: {e}"
            raise
        finally:
            _depth[0] -= 1
            entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": fn.__name__, "depth": _depth[0],
                     "args": _clip(", ".join([*map(_clip, args), *(f"{k}={_clip(v)}" for k, v in kwargs.items())])),
                     "ms": round((time.monotonic() - start) * 1000), "outcome": outcome, "note": _clip(note, 200)}
            try:
                if JOURNAL.exists() and JOURNAL.stat().st_size > 5_000_000:
                    JOURNAL.replace(JOURNAL.with_suffix(".old.jsonl"))
                with JOURNAL.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass  # the journal must never break the action it records
    return logged


def tool(fn):
    """Register an MCP tool whose every call is journaled."""
    return mcp.tool()(_journal(fn))


user32 = ctypes.windll.user32
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]


# --- session feedback: Jitter holds the screen from its first action until done() (or HOLD seconds idle).
# A real keypress while it holds the screen hands it back: the next action is refused. ---

_abort = threading.Event()
# countdown: seconds left before a fresh takeover acts; paused: toggled by clicking the corner badge
# human: the reason text while the user does a step only they should do (sign-in, password, 2FA), else None
# ask: {"lines", "answer"} while a login package waits for the user's approval in the middle of the screen
_busy = {"n": 0, "last": 0.0, "released": False, "countdown": 0.0, "paused": False, "badge_off_until": 0.0,
         "human": None, "ask": None}


class UserStop(Exception):
    pass


def _holding() -> bool:
    return (_busy["n"] > 0 or _busy["countdown"] > 0 or _busy["paused"] or _busy["human"] is not None
            or time.monotonic() - _busy["last"] < settings()["hold_seconds"])


def _countdown() -> None:
    """Warn before taking the screen: count down on the corner badge. Pausing freezes it; a keypress cancels."""
    left, last = float(settings()["countdown_seconds"]), time.monotonic()
    _busy["countdown"] = left
    try:
        while left > 0:
            _check_abort()
            now = time.monotonic()
            if not _busy["paused"]:
                left -= now - last
            last = now
            _busy["countdown"] = max(left, 0.001)
            time.sleep(0.05)
    finally:
        _busy["countdown"] = 0.0


def _wait_if_paused() -> None:
    while _busy["paused"] or _busy["human"] is not None:  # acting waits out a pause and a human step
        _check_abort()
        time.sleep(0.1)


def _check_abort() -> None:
    """Refuse one action after a keypress, then release the screen."""
    if _abort.is_set():
        _abort.clear()
        _busy["last"], _busy["released"] = 0.0, _busy["n"] > 0
        raise UserStop("user pressed a key and took the screen back; stop and ask them before acting again")


class _session:
    """Wrap every acting tool: counts down on a fresh takeover, waits while paused, refuses after a keypress."""

    def __enter__(self):
        _check_abort()
        if _busy["n"] == 0 and not _holding():
            _countdown()
        _wait_if_paused()
        _busy["n"] += 1

    def __exit__(self, *_):
        _busy["n"] -= 1
        if _busy["n"] == 0:
            _busy["last"] = 0.0 if _busy["released"] else time.monotonic()
            _busy["released"] = False


ORANGE = "#ffba69"


def _badge_rect() -> tuple[int, int, int, int]:
    """The corner badge, in physical pixels: a large square at the top right."""
    s = user32.GetDpiForSystem() / 96
    size, margin, w = round(128 * s), round(20 * s), pyautogui.size()[0]
    return w - margin - size, margin, w - margin, margin + size


def _clear_badge(nx: int, ny: int) -> None:
    """If Jitter is about to click under the badge, get the badge out of the way first."""
    left, top, right, bottom = _badge_rect()
    if left <= nx < right and top <= ny < bottom:
        _busy["badge_off_until"] = time.monotonic() + 1.5
        time.sleep(0.25)  # the overlay thread hides it on its next tick


def _overlay() -> None:
    import tkinter as tk

    user_window = user32.GetForegroundWindow()
    root = tk.Tk()
    root.withdraw()  # never shown the normal way: a new process's first window would take focus from the user
    root.overrideredirect(True)
    root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
    root.configure(bg="magenta", highlightthickness=16, highlightbackground=ORANGE)
    root.attributes("-transparentcolor", "magenta", "-alpha", 0.0)  # topmost is set below, without activating
    root.update_idletasks()
    hwnd = int(root.wm_frame(), 16)
    # layered | click-through | no taskbar button | never activated; hidden from our own screenshots
    user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x80000 | 0x20 | 0x80 | 0x08000000)
    user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE (it's invisible until Jitter holds the screen: alpha 0)

    # the corner badge: countdown, then "working"; clicking it pauses and resumes
    left, top, right, bottom = _badge_rect()
    size, s = right - left, (right - left) / 128
    badge = tk.Toplevel(root)
    badge.withdraw()  # same as the root: shown only without activation, below
    badge.overrideredirect(True)
    badge.geometry(f"{size}x{size}+{left}+{top}")
    badge.attributes("-alpha", 0.94)
    canvas = tk.Canvas(badge, width=size, height=size, bg="#0e0e10", highlightthickness=round(3 * s),
                       highlightbackground=ORANGE, cursor="hand2")
    canvas.pack()
    badge.update_idletasks()
    bh = int(badge.wm_frame(), 16)
    # click without taking focus from the app Claude is working in | no taskbar button | hidden from screenshots
    user32.SetWindowLongW(bh, -20, user32.GetWindowLongW(bh, -20) | 0x08000000 | 0x80)
    user32.SetWindowDisplayAffinity(bh, 0x11)
    badge.withdraw()
    badge_shown = [False]

    def show_badge(on: bool) -> None:
        # Tk's deiconify activates the window and would steal focus from the app Claude works in: show it raw
        if on and not badge_shown[0]:
            user32.ShowWindow(bh, 4)  # SW_SHOWNOACTIVATE
            user32.SetWindowPos(bh, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x13)  # TOPMOST; NOSIZE|NOMOVE|NOACTIVATE
        elif not on and badge_shown[0]:
            user32.ShowWindow(bh, 0)  # SW_HIDE
        badge_shown[0] = on

    focus_before = [None]

    def remember_focus(_):
        focus_before[0] = user32.GetForegroundWindow()

    def toggle(_):
        if _busy["human"] is not None:
            _busy["human"] = None  # the user says the human step is done
        else:
            _busy["paused"] = not _busy["paused"]
        if focus_before[0] and user32.GetForegroundWindow() != focus_before[0]:
            user32.SetForegroundWindow(focus_before[0])  # in case the click took focus anyway
        draw()

    canvas.bind("<Enter>", remember_focus)
    canvas.bind("<Button-1>", toggle)

    # the approval box for a login package, in the middle of the screen; only the user's own click answers it
    bw, bht = round(440 * s), round(196 * s)
    sw, sh = pyautogui.size()
    box_rect = ((sw - bw) // 2, (sh - bht) // 2, (sw + bw) // 2, (sh + bht) // 2)
    box = tk.Toplevel(root)
    box.withdraw()
    box.overrideredirect(True)
    box.geometry(f"{bw}x{bht}+{box_rect[0]}+{box_rect[1]}")
    bc = tk.Canvas(box, width=bw, height=bht, bg="#0e0e10", highlightthickness=round(3 * s),
                   highlightbackground=ORANGE, cursor="hand2")
    bc.pack()
    box.update_idletasks()
    xh = int(box.wm_frame(), 16)
    user32.SetWindowLongW(xh, -20, user32.GetWindowLongW(xh, -20) | 0x08000000 | 0x80)  # like the badge
    user32.SetWindowDisplayAffinity(xh, 0x11)
    box.withdraw()

    def draw_box(lines):
        bc.delete("all")
        font = lambda px, weight="normal": ("Segoe UI Variable Display", -round(px * s), weight)  # noqa: E731
        bc.create_text(bw / 2, 38 * s, text=lines[0], fill="#ececf1", font=font(18, "bold"))
        bc.create_text(bw / 2, 70 * s, text="Jitter fills in this login on", fill="#8b8b96", font=font(12))
        bc.create_text(bw / 2, 92 * s, text=lines[1][:56], fill=ORANGE, font=font(13, "bold"))
        bc.create_text(bw / 2, 114 * s, text="Claude never sees it", fill="#8b8b96", font=font(11))
        for tag, x, label, fill, ink in (("no", 120, "Deny", "#26262c", "#ececf1"),
                                         ("yes", 320, "Approve", ORANGE, "#1a1000")):
            bc.create_rectangle((x - 84) * s, 138 * s, (x + 84) * s, 176 * s, fill=fill, width=0, tags=tag)
            bc.create_text(x * s, 157 * s, text=label, fill=ink, font=font(13, "bold"), tags=tag)

    def answer(yes: bool):
        ask = _busy["ask"]
        if ask is not None and _human_clicked(box_rect):  # Jitter's own (injected) clicks can't approve
            ask["answer"] = yes
        if focus_before[0] and user32.GetForegroundWindow() != focus_before[0]:
            user32.SetForegroundWindow(focus_before[0])  # the form being filled keeps focus

    bc.bind("<Enter>", remember_focus)
    bc.tag_bind("yes", "<Button-1>", lambda _: answer(True))
    bc.tag_bind("no", "<Button-1>", lambda _: answer(False))

    def text(y, label, px, color="#ececf1", weight="normal"):
        canvas.create_text(size / 2, y * s, text=label, fill=color,
                           font=("Segoe UI Variable Display", -round(px * s), weight))

    def draw():
        canvas.delete("all")
        if _busy["human"] is not None:
            canvas.create_oval(50 * s, 18 * s, 78 * s, 46 * s, outline=ORANGE, width=round(4 * s))  # a person
            canvas.create_arc(38 * s, 48 * s, 90 * s, 92 * s, start=0, extent=180, style="arc", outline=ORANGE,
                              width=round(4 * s))
            words = _busy["human"].split()
            line1 = " ".join(words[:2]) if len(words) > 2 else _busy["human"]
            text(84, line1, 13, weight="bold")
            if len(words) > 2:
                text(100, " ".join(words[2:]), 13, weight="bold")
            text(117, "click when done", 10, "#8b8b96")
        elif _busy["ask"] is not None or _busy["paused"]:
            waiting = _busy["ask"] is not None
            for x in (52, 68):
                canvas.create_rectangle(x * s, 30 * s, (x + 9) * s, 62 * s, fill=ORANGE, width=0)
            text(84, "Waiting for you" if waiting else "Paused", 13 if waiting else 15, weight="bold")
            text(106, "approve or deny" if waiting else "click to resume", 11, "#8b8b96")
        elif _busy["countdown"] > 0:
            text(50, str(int(_busy["countdown"]) + 1), 54, ORANGE, "bold")
            text(90, "Claude takes over", 12)
            text(108, "click to pause", 10, "#8b8b96")
        else:
            pts = [(30, 36), (44, 64), (58, 37), (72, 64), (86, 37), (100, 64)]  # the Jitter zigzag
            canvas.create_line(*[v * s for p in pts for v in p], fill=ORANGE, width=round(6 * s),
                               capstyle="round", joinstyle="round")
            text(88, "Claude is working", 12, weight="bold")
            text(108, "click to pause", 10, "#8b8b96")

    def tick(shown=[None], asked=[None]):
        if _busy["ask"] is not asked[0]:  # a new request (each is its own dict), or none
            asked[0] = _busy["ask"]
            if asked[0] is not None:
                draw_box(asked[0]["lines"])
                user32.ShowWindow(xh, 4)  # SW_SHOWNOACTIVATE
                user32.SetWindowPos(xh, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x13)
            else:
                user32.ShowWindow(xh, 0)
        c = settings()
        on = _holding()
        border = on and _busy["human"] is None  # the user's turn during a human step: no border
        state = (border, c["opacity"], c["border_px"])
        if state != shown[0]:
            root.configure(highlightthickness=int(c["border_px"]))
            root.attributes("-alpha", c["opacity"] if border else 0.0)
            user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x13)  # topmost, no activation
            shown[0] = state
        visible = on and time.monotonic() >= _busy["badge_off_until"]
        show_badge(visible)
        if visible:
            draw()
        root.after(100, tick)

    tick()
    root.update()
    if user32.GetForegroundWindow() in (hwnd, bh) and user_window:
        user32.SetForegroundWindow(user_window)  # Tk took focus while starting up: give it straight back
    root.mainloop()


class _KBD(ctypes.Structure):
    _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


class _MSLL(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


_real_click = [0.0, 0, 0]  # time and physical x, y of the user's last real (not injected) left click


def _human_clicked(rect) -> bool:
    """Did the user's own mouse just click inside rect? Jitter's clicks are injected and never count."""
    t, x, y = _real_click
    return time.monotonic() - t < 1.0 and rect[0] <= x < rect[2] and rect[1] <= y < rect[3]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HANDLE


def _on_user_key() -> None:
    if not _holding() or _busy["human"] is not None:  # during a human step, typing is the user doing that step
        return
    # between calls, typing into Claude or a terminal is talking to Claude, not taking the screen
    if _busy["n"] == 0 and _blocked(user32.GetForegroundWindow()):
        return
    _abort.set()
    if _busy["n"] == 0:
        _busy["last"] = 0.0  # hide the overlay now, as confirmation


def _keyhook() -> None:
    @_HOOKPROC
    def proc(n, w, l):
        # 0x100/0x104 = WM_(SYS)KEYDOWN; flag 0x10 = injected, i.e. our own keystrokes
        if n == 0 and w in (0x100, 0x104) and not ctypes.cast(l, ctypes.POINTER(_KBD)).contents.flags & 0x10:
            _on_user_key()
        return user32.CallNextHookEx(None, n, w, l)

    @_HOOKPROC
    def mouse(n, w, l):
        # 0x201 = WM_LBUTTONDOWN; flag 1 = injected. Only real clicks can approve a login package
        if n == 0 and w == 0x201:
            m = ctypes.cast(l, ctypes.POINTER(_MSLL)).contents
            if not m.flags & 1:
                _real_click[:] = [time.monotonic(), m.pt.x, m.pt.y]
        return user32.CallNextHookEx(None, n, w, l)

    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE  # the default (32-bit int) cuts the handle: no hook at all
    module = kernel32.GetModuleHandleW(None)
    hooks = (user32.SetWindowsHookExW(13, proc, module, 0),  # WH_KEYBOARD_LL
             user32.SetWindowsHookExW(14, mouse, module, 0))  # WH_MOUSE_LL
    _busy["hooks"] = all(hooks)  # visible to tests and status: the keypress stop and real-click approval depend on it
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass


# --- coordinates, guards, capture ---

def _scale() -> float:
    return min(1.0, MAX_EDGE / max(pyautogui.size()))


def _native(x: int, y: int) -> tuple[int, int]:
    w, h = pyautogui.size()
    s = _scale()
    # clamp 1px in: (0, 0) is the failsafe point
    return max(1, min(round(x / s), w - 1)), max(1, min(round(y / s), h - 1))


def _title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(user32.GetAncestor(hwnd, 2), buf, 256)  # 2 = GA_ROOT
    return buf.value


def _title_blocked(text: str) -> bool:
    t = text.lower()
    # whole-word match, so a name like "Claudeswap" is not caught by the "claude" rule
    return any(re.search(rf"(?<!\w){re.escape(b)}(?!\w)", t) for b in (*ALWAYS_BLOCKED, *settings()["blocked_titles"]))


def _blocked(hwnd) -> bool:
    # a browser window is never a terminal or the Claude app, and its title is only the active tab, so the whole
    # window (and its other tabs) must not be judged by it. Only non-browser windows are checked by title.
    try:
        if _exe(hwnd) in BROWSERS:
            return False
    except Exception:
        pass
    return _title_blocked(_title(hwnd))


def _check(hwnd) -> None:
    if _blocked(hwnd):
        t = _title(hwnd)
        raise PermissionError(f"input blocked: window '{t}' is off limits ({WHERE_BLOCKED})")


def _guard_point(nx: int, ny: int) -> None:
    _check(user32.WindowFromPoint(wintypes.POINT(nx, ny)))


def _target(nx: int, ny: int) -> None:
    """Before acting at a point: move the badge if it's in the way, then check the window there is allowed."""
    _clear_badge(nx, ny)
    _guard_point(nx, ny)


def _guard_focus() -> None:
    _check(user32.GetForegroundWindow())


def _jpeg(img, quality: int = 80) -> Image:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)  # JPEG: far smaller and faster than PNG
    return Image(data=buf.getvalue(), format="jpeg")


def _no_peeking() -> None:
    """Nothing on screen is captured or read while the user does a human step (they may be typing a password)."""
    if _busy["human"] is not None:
        raise PermissionError(f"the user is doing a human step ({_busy['human']}); Jitter won't look at the screen "
                              "until they click the badge to say they're done")


def _frame():
    _no_peeking()
    img = pyautogui.screenshot()
    s = _scale()
    return img.resize((round(img.width * s), round(img.height * s))) if s < 1.0 else img


# --- seeing ---

@tool
def screenshot() -> Image:
    """Screenshot of the primary monitor, downscaled. All coordinates use this image's pixels.
    Costly: try read() first (text, clickable items, OCR); screenshot for images, layout, or when read() fails."""
    return _jpeg(_frame())


@tool
def zoom(x1: int, y1: int, x2: int, y2: int) -> Image:
    """Capture region (x1, y1)-(x2, y2) in screenshot coordinates at full resolution, to read small text."""
    if x2 <= x1 or y2 <= y1:
        raise ValueError("need x2 > x1 and y2 > y1")
    _no_peeking()
    (a, b), (c, d) = _native(x1, y1), _native(x2, y2)
    return _jpeg(pyautogui.screenshot(region=(a, b, max(1, c - a), max(1, d - b))), 85)


def _scroll_position(nx: int, ny: int):
    """How far the scrollable container under a point is scrolled (0-100), or None if it doesn't say."""
    try:
        c = uia.ControlFromPoint(nx, ny)
        for _ in range(25):  # walk up to the nearest container that scrolls vertically
            if c is None:
                return None
            p = c.GetPattern(10004)  # ScrollPattern
            if p and p.VerticallyScrollable:
                return p.VerticalScrollPercent
            c = c.GetParentControl()
    except Exception:
        pass
    return None


def _stuck(before, again, after) -> bool:
    """Did a scroll fail to move the view? `before` and `again` are two captures taken just before scrolling: pixels
    that differ between them animate on their own (gifs, videos), so only the still pixels are judged."""
    g = lambda im: im.convert("L").resize((64, 40)).tobytes()  # noqa: E731
    p, p2, q = g(before), g(again), g(after)
    still = [i for i in range(len(p)) if abs(p[i] - p2[i]) <= 12]
    if len(still) < len(p) // 10:
        return False  # nearly everything animates, so it can't tell: keep going (steps bounds it)
    # a real scroll changes most still pixels; slow animation that the two looks missed changes only some
    return sum(abs(p2[i] - q[i]) > 12 for i in still) < 0.25 * len(still)


def _same_view(a, b) -> bool:
    from PIL import ImageChops

    thumb = lambda im: im.resize((64, 40)).convert("L")  # noqa: E731
    return sum(ImageChops.difference(thumb(a), thumb(b)).histogram()[25:]) < 20


def _contact_sheet(frames: list, max_width: int = 1800):
    """Views side by side in a grid, each numbered from 1, so one image shows a whole scrolled list."""
    from PIL import Image as PILImage, ImageDraw, ImageFont

    cols = min(len(frames), 3)
    rows = -(-len(frames) // cols)
    pad = 12
    w = min(frames[0].width, (max_width - pad * (cols + 1)) // cols)
    h = round(frames[0].height * w / frames[0].width)
    sheet = PILImage.new("RGB", (cols * w + pad * (cols + 1), rows * h + pad * (rows + 1)), "#0e0e10")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("segoeuib.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    for i, f in enumerate(frames):
        x, y = pad + (i % cols) * (w + pad), pad + (i // cols) * (h + pad)
        sheet.paste(f.convert("RGB").resize((w, h)), (x, y))
        draw.rectangle([x, y, x + 46, y + 40], fill=ORANGE)
        draw.text((x + 23, y + 20), str(i + 1), fill="black", font=font, anchor="mm")
    return sheet


@tool
def scan(x1: int, y1: int, x2: int, y2: int, direction: str = "down", steps: int = 6, amount: int = 3) -> list:
    """Look through a scrolling area in ONE call: capture region (x1, y1)-(x2, y2), scroll it, repeat, and get every
    view back as one numbered contact sheet. For finding something by sight in a long list or grid (images, gifs,
    thumbnails, unnamed items). steps: 1-12 views. Stops early at the end of the list. The result says how to scroll
    back to any view.
    """
    if x2 <= x1 or y2 <= y1 or direction not in ("up", "down"):
        raise ValueError("need x2 > x1, y2 > y1, direction up/down")
    steps = max(1, min(steps, 12))
    (a, b), (c, d) = _native(x1, y1), _native(x2, y2)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    _no_peeking()
    grab = lambda: pyautogui.screenshot(region=(a, b, max(1, c - a), max(1, d - b)))  # noqa: E731
    with _session():
        frames, end = [grab()], False
        for _ in range(steps - 1):
            before = _scroll_position((a + c) // 2, (b + d) // 2)
            time.sleep(0.3)
            again = grab()  # a second look before scrolling shows which pixels animate on their own
            scroll(direction, amount, cx, cy)
            time.sleep(0.6)  # let lazy-loaded items render
            after, view = _scroll_position((a + c) // 2, (b + d) // 2), grab()
            # the container's own scroll position is the truth; pixels only when it won't say
            stuck = (abs(after - before) < 0.01 if before is not None and after is not None
                     else _stuck(frames[-1], again, view))
            if stuck:
                end = True
                break
            frames.append(view)
    n, back = len(frames), "up" if direction == "down" else "down"
    note = (f"{n} views, numbered 1-{n}; the area now shows view {n}{' (the end of the list)' if end else ''}. "
            f"To return to view k, scroll {back} ({n} - k) x {amount} at ({cx}, {cy}): "
            f"view 1 is D({back} {amount * (n - 1)},{cx},{cy}).")
    return [note, _jpeg(_contact_sheet(frames), 80)]


@tool
def record(seconds: float = 3.0, frames: int = 4) -> list:
    """Evenly spaced screenshots over `seconds` (0.5-10), 2-6 frames, in one call. For animations and loading."""
    seconds, frames = max(0.5, min(seconds, 10.0)), max(2, min(frames, 6))
    out = []
    for i in range(frames):
        if i:
            time.sleep(seconds / (frames - 1))
        out.append(_jpeg(_frame(), 70))
    return out


@tool
def wait_for_change(timeout: float = 10.0, sensitivity: int = 50) -> list:
    """Block until the screen visibly changes (or timeout, max 20s), then return a screenshot.

    Use after an action that triggers loading instead of polling with screenshots.
    sensitivity: changed-pixel count on a 64x40 thumbnail needed to count as a change.
    """
    from PIL import ImageChops

    thumb = lambda: _frame().resize((64, 40)).convert("L")  # noqa: E731
    base, end = thumb(), time.monotonic() + max(0.5, min(timeout, 20.0))
    while time.monotonic() < end:
        time.sleep(0.25)
        diff = ImageChops.difference(base, thumb()).histogram()
        if sum(diff[25:]) >= sensitivity:
            return ["screen changed", _jpeg(_frame())]
    return ["timed out, no change", _jpeg(_frame())]


# --- acting ---

@tool
def click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Click at (x, y). button: left, right, middle. clicks: 1-3."""
    if button not in ("left", "right", "middle") or not 1 <= clicks <= 3:
        raise ValueError("button must be left/right/middle, clicks 1-3")
    nx, ny = _native(x, y)
    with _session():
        _target(nx, ny)
        pyautogui.click(nx, ny, clicks=clicks, button=button)
    return f"{button} click x{clicks} at ({x}, {y})"


@tool
def move(x: int, y: int) -> str:
    """Move the mouse to (x, y) without clicking."""
    with _session():
        pyautogui.moveTo(*_native(x, y))
    return f"moved to ({x}, {y})"


@tool
def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Left-button drag from (x1, y1) to (x2, y2)."""
    with _session():
        _drag_native(*_native(x1, y1), *_native(x2, y2))
    return f"dragged ({x1}, {y1}) -> ({x2}, {y2})"


def _drag_native(sx: int, sy: int, ex: int, ey: int) -> None:
    _target(sx, sy)
    _target(ex, ey)
    pyautogui.moveTo(sx, sy)
    try:
        pyautogui.dragTo(ex, ey, duration=0.4, button="left")
    finally:
        pyautogui.FAILSAFE = False  # a failsafe abort must not leave the button held
        pyautogui.mouseUp(button="left")
        pyautogui.FAILSAFE = True


def _point(target: str) -> tuple[int, int]:
    """'x,y' in screenshot coordinates, or an element's name: the native screen point to act on."""
    m = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", target)
    return _native(int(m[1]), int(m[2])) if m else _pick(target)[1]


def hover(target: str) -> str:
    """Rest the mouse on a name or x,y, for things that only appear on hover (menus, captions, tooltips)."""
    with _session():
        pyautogui.moveTo(*_point(target))
        time.sleep(0.4)  # let hover effects appear
    return f"hovering over {target!r}"


def right_click(target: str) -> str:
    with _session():
        nx, ny = _point(target)
        _target(nx, ny)
        pyautogui.click(nx, ny, button="right")
    return f"right-clicked {target!r}"


def drag_to(source: str, destination: str) -> str:
    with _session():
        _drag_native(*_point(source), *_point(destination))
    return f"dragged {source!r} onto {destination!r}"


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 8)]  # sized as MOUSEINPUT


def _type_unicode(text: str) -> None:
    """Type any characters as Unicode key events (no clipboard), one UTF-16 unit at a time."""
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        u = int.from_bytes(units[i:i + 2], "little")
        events = (_INPUT * 2)(_INPUT(1, _KEYBDINPUT(0, u, 4, 0, 0)), _INPUT(1, _KEYBDINPUT(0, u, 4 | 2, 0, 0)))
        user32.SendInput(2, events, ctypes.sizeof(_INPUT))  # 4 = KEYEVENTF_UNICODE, 2 = KEYUP
        time.sleep(0.01)


@tool
def type(text: str) -> str:
    """Type text at the current focus. Any language, emoji and symbols included."""
    _guard_focus()
    with _session():
        before = _focused_value()
        for part in re.split(r"([^\x00-\x7f]+)", text):  # ASCII through pyautogui (handles \n), the rest as Unicode
            if part.isascii():
                pyautogui.write(part, interval=0.01)
            else:
                _type_unicode(part)
        time.sleep(0.15)
        after = _focused_value()
    if text.strip() and before is not None and before == after:
        raise RuntimeError(f"typed {len(text)} characters but the focused field didn't change: it may be read-only, "
                           "or focus is somewhere else")
    return f"typed {len(text)} characters"


def _focused_value():
    """The focused field's text, when it reports one (not for password fields), to check that typing landed."""
    try:
        c = uia.GetFocusedControl()
        if c is None or c.IsPassword:
            return None
        p = c.GetPattern(10002)  # ValuePattern
        return p.Value if p else None
    except Exception:
        return None


@tool
def key(keys: str, repeat: int = 1) -> str:
    """Press a key or chord ('enter', 'ctrl+s', 'alt+tab') 1-50 times. No Windows key."""
    parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
    bad = [p for p in parts if p not in pyautogui.KEYBOARD_KEYS or p in ("win", "winleft", "winright")]
    if not parts or bad or not 1 <= repeat <= 50:
        raise ValueError(f"invalid or disallowed key(s) {bad}, or repeat outside 1-50")
    _guard_focus()
    with _session():
        for _ in range(repeat):
            pyautogui.hotkey(*parts)
    return f"pressed {keys} x{repeat}"


@tool
def scroll(direction: str, amount: int = 3, x: int | None = None, y: int | None = None) -> str:
    """Scroll up/down 1-100 wheel notches, optionally at (x, y)."""
    if direction not in ("up", "down") or not 1 <= amount <= 100:
        raise ValueError("direction up/down, amount 1-100")
    with _session():
        if x is not None and y is not None:
            nx, ny = _native(x, y)
            _target(nx, ny)
            pyautogui.moveTo(nx, ny)
        # pyautogui on Windows sends raw wheel units; one notch is WHEEL_DELTA = 120
        pyautogui.scroll(120 * (amount if direction == "up" else -amount))
    return f"scrolled {direction} {amount}"


@tool
def wait(seconds: float = 1.0) -> str:
    """Wait 0.1-10 seconds."""
    seconds = max(0.1, min(seconds, 10.0))
    time.sleep(seconds)
    return f"waited {seconds:.1f}s"


# --- windows and UI Automation ---

def _visible_windows():
    return [w for w in gw.getAllWindows() if w.title.strip()]


@tool
def windows() -> str:
    """List titles of visible top-level windows."""
    return "\n".join(w.title for w in _visible_windows())


BROWSERS = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi")


def _tab_strip(hwnd) -> list:
    """A browser window's tabs (UIA TabItems above the page, so not tabs inside the page)."""
    ua = _AutomationClient.instance().IUIAutomation
    root = ua.ElementFromHandle(hwnd)
    doc = root.FindFirst(7, ua.CreatePropertyCondition(30003, 50030))  # Document
    doc_top = doc.CurrentBoundingRectangle.top if doc else 10**6
    arr = root.FindAll(7, ua.CreatePropertyCondition(30003, 50019))  # TabItem
    return [t for t in (uia.Control.CreateControlFromElement(arr.GetElement(i)) for i in range(arr.Length))
            if t.BoundingRectangle.bottom <= doc_top]


def _norm(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def _words_in(want: str, have: str) -> bool:
    """Every word of `want` appears in `have`, ignoring case, punctuation and order ('chat 4242' ~ '4242³ | Chat')."""
    words = _norm(have)
    return bool(_norm(want)) and all(any(w in h for h in words) for w in _norm(want))


def _windows_and_tabs():
    """(label, hwnd, tab) for every window, then every browser tab (a browser titles itself by its active tab)."""
    wins = _visible_windows()
    for w in wins:
        yield w.title, w._hWnd, None
    for w in wins:
        if _exe(w._hWnd) in BROWSERS:
            for t in _tab_strip(w._hWnd):
                yield t.Name, w._hWnd, t


def _bring(hwnd, tab, label: str) -> str:
    _check(hwnd)
    with _session():
        _raise(hwnd)
        if tab:
            tab.GetSelectionItemPattern().Select()
            time.sleep(0.3)
    return f"focused {'tab ' if tab else ''}{label!r}"


_apps: list = []


def _start_apps() -> list:
    """Installed apps from the Start menu, desktop and Store alike: [{'Name', 'AppID'}]. Cached per process."""
    if not _apps:
        ps = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             encoding="utf-8", timeout=30, creationflags=0x08000000).stdout  # no console window
        _apps.extend(json.loads(out or "[]"))
    return _apps


def _launch(name: str) -> str | None:
    """Start the installed app best matching `name` and return its window once it appears."""
    apps = _start_apps()
    pick = next((a for a in apps if a["Name"].lower() == name.lower()), None) \
        or next((a for a in apps if name.lower() in a["Name"].lower()), None) \
        or next((a for a in apps if _words_in(name, a["Name"])), None)
    if not pick:
        return None
    if _title_blocked(pick["Name"]):  # whole-word, so "Claudeswap" is not caught by the "claude" rule
        raise PermissionError(f"launch blocked: {pick['Name']!r} is off limits ({WHERE_BLOCKED})")
    before = {w._hWnd for w in _visible_windows()}
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{pick['AppID']}"])
    end = time.monotonic() + 20
    while time.monotonic() < end:
        time.sleep(0.5)
        new = [w for w in _visible_windows() if w._hWnd not in before and not _blocked(w._hWnd)]
        if new:
            time.sleep(1.0)  # let it finish drawing
            return _bring(new[0]._hWnd, None, f"{new[0].title} (launched {pick['Name']})")
    return None


@tool
def focus(title: str) -> str:
    """Bring a window to the front, trying in turn until one works:

    a window title containing `title`; a browser tab whose title contains it (switched to); a window or tab with
    the same words in any order; a window of the program named `title` ('chrome', 'notepad'); and finally launching
    the installed app of that name. 'a|b|c' gives alternatives, each tried at every rung.
    """
    alts = [a.strip() for a in title.split("|") if a.strip()]
    off_limits = []  # matches Jitter may not touch: skipped, and only an error if nothing else matches
    for test in (lambda alt, label: alt.lower() in label.lower(), _words_in):
        for alt in alts:
            for label, hwnd, tab in _windows_and_tabs():
                if test(alt, label):
                    if _blocked(hwnd):
                        off_limits.append(label)
                        continue
                    return _bring(hwnd, tab, label)
    for alt in alts:
        for w in _visible_windows():
            if _exe(w._hWnd) == alt.lower().removesuffix(".exe") and not _blocked(w._hWnd):
                return _bring(w._hWnd, None, w.title)
    for alt in alts:
        launched = _launch(alt)
        if launched:
            return launched
    if off_limits:
        raise PermissionError(f"only off-limits windows match {title!r}: {off_limits[0]!r} ({WHERE_BLOCKED})")
    raise LookupError(f"no window, browser tab, program or installed app matches {title!r}")


def _alt_tap(hwnd) -> None:
    # an injected Alt tap lifts Windows' foreground lock for background processes
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def _borrow_input(hwnd) -> None:
    # share input state with the current foreground thread, which is allowed to hand focus over
    fg = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(me, fg, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(me, fg, False)


def _minimize_restore(hwnd) -> None:
    # restoring a minimized window activates it, foreground lock or not
    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE


def _raise(hwnd) -> None:
    """Bring a window to the front, escalating when Windows' foreground lock refuses the gentler ways."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    for way in (_alt_tap, _borrow_input, _minimize_restore):
        way(hwnd)
        time.sleep(0.25)
        if user32.GetForegroundWindow() == hwnd:
            return
    raise RuntimeError(f"could not bring {_title(hwnd)!r} to the front")


def _innermost(found: list) -> list:
    """Drop matches that contain another match: the window title or a list item wrapping a same-named link."""
    def box(c):
        try:
            r = c.BoundingRectangle
            return r.left, r.top, r.right, r.bottom
        except Exception:  # an element that errors on being asked (stale, virtualized) is skipped, not fatal
            return 0, 0, 0, 0

    rects = [box(c) for c, _ in found]
    # hidden elements (a collapsed page, display:none) can report "on screen" with an empty box at (0, 0)
    found, rects = [m for m, r in zip(found, rects) if r[2] > r[0] and r[3] > r[1]], [r for r in rects if r[2] > r[0] and r[3] > r[1]]

    def inside(j, i):  # rect j within rect i; identical rects keep the deeper (later) one
        a, b = rects[j], rects[i]
        return b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3] and (a != b or j > i)

    return [(c, rects[i]) for i, (c, _) in enumerate(found)
            if not any(inside(j, i) for j in range(len(found)) if j != i)]


def _find_all(name: str, flags: int, onscreen: bool = True) -> list:
    """Native UIA search of the foreground window: one cross-process call, not one per node (Chrome has ~20k)."""
    ua = _AutomationClient.instance().IUIAutomation
    cond = ua.CreatePropertyConditionEx(30005, name, flags)  # Name; flags 1 = ignore case, 3 = also substring
    if onscreen:
        cond = ua.CreateAndCondition(cond, ua.CreatePropertyCondition(30022, False))  # not IsOffscreen
    arr = ua.ElementFromHandle(user32.GetForegroundWindow()).FindAll(7, cond)  # 7 = TreeScope_Subtree
    return [(uia.Control.CreateControlFromElement(arr.GetElement(i)), None) for i in range(arr.Length)]


def _scroll_into_view(name: str) -> bool:
    """Scroll the first matching element that is out of view (long lists, long pages) into view."""
    for c, _ in _find_all(name, 3, onscreen=False):
        try:
            c.GetScrollItemPattern().ScrollIntoView()
            time.sleep(0.4)
            return True
        except Exception:  # not scrollable that way; try the next match
            continue
    return False


def _word_matches(name: str) -> list:
    """Visible elements whose name has every word of `name`, in any order: survives renames like 'Send' -> 'Send now'."""
    ua = _AutomationClient.instance().IUIAutomation
    cache = ua.CreateCacheRequest()
    cache.AddProperty(30005)
    cond = ua.CreateAndCondition(ua.CreateNotCondition(ua.CreatePropertyCondition(30005, "")),
                                 ua.CreatePropertyCondition(30022, False))
    arr = ua.ElementFromHandle(user32.GetForegroundWindow()).FindAllBuildCache(7, cond, cache)
    return [(uia.Control.CreateControlFromElement(e), None)
            for e in (arr.GetElement(i) for i in range(arr.Length)) if _words_in(name, e.CachedName)]


def _matches(name: str, timeout: float = 3.0) -> list:
    """Elements in the foreground window for `name`, loosening step by step until something fits:

    exact name, then partial name, then scrolling an out-of-view match into view, then every word in any order.
    Returns [(control, (left, top, right, bottom))]. Retries until `timeout`, so it doubles as a wait for loading.
    """
    start = time.monotonic()
    end, scrolled = start + timeout, False
    while True:
        try:
            found = _innermost(_find_all(name, 1)) or _innermost(_find_all(name, 3))
            if not found and not scrolled:
                scrolled = _scroll_into_view(name)
                if scrolled:
                    continue
            found = found or _innermost(_word_matches(name))
        except Exception:  # UI Automation hiccups (COMError) while a page is changing: treat as not yet, retry
            found = []
        now = time.monotonic()
        # out of patience, unless the page is visibly still loading (then up to 20s): slow sites are not failures
        if found or (now > end and not (now < start + 20 and _page_loading())):
            return found
        time.sleep(0.3)


def _page_loading() -> bool:
    """Is the front window a browser still loading its page? (Its reload button turns into a stop button.)"""
    try:
        ua = _AutomationClient.instance().IUIAutomation
        stop = ua.CreateOrCondition(ua.CreatePropertyConditionEx(30005, "Stop loading", 3),
                                    ua.CreatePropertyConditionEx(30005, "Stop (Esc)", 3))
        cond = ua.CreateAndCondition(ua.CreatePropertyCondition(30003, 50000), stop)
        return bool(ua.ElementFromHandle(user32.GetForegroundWindow()).FindFirst(7, cond))  # empty = NULL pointer
    except Exception:
        return False


def _settle(limit: float = 1.2) -> None:
    """After a click or key: wait until the window stops changing, in structure and on screen (a dialog sliding
    in, a menu opening, a page reacting), so the next step sees the result, not the moment before it."""
    def fingerprint():
        try:
            ua = _AutomationClient.instance().IUIAutomation
            kids = ua.ElementFromHandle(user32.GetForegroundWindow()).FindAll(2, ua.CreateTrueCondition())
            focused = ua.GetFocusedElement()  # focus moves when a dialog closes or a menu opens
            return (user32.GetForegroundWindow(), kids.Length, _doc_id(),
                    (focused.CurrentName, focused.CurrentControlType) if focused else None)
        except Exception:
            return None

    end, prev, look = time.monotonic() + limit, fingerprint(), _frame()
    time.sleep(0.3)  # reactions take a moment to start
    while time.monotonic() < end:
        cur, view = fingerprint(), _frame()
        if cur == prev and _same_view(look, view):  # animations count as change until they finish (or time's up)
            return
        prev, look = cur, view
        time.sleep(0.12)


def _doc_id():
    """Identity of the page in the front window: every navigation makes a new document element."""
    try:
        ua = _AutomationClient.instance().IUIAutomation
        doc = ua.ElementFromHandle(user32.GetForegroundWindow()).FindFirst(7, ua.CreatePropertyCondition(30003, 50030))
        return tuple(doc.GetRuntimeId()) if doc else None
    except Exception:
        return None


def _wait_loaded(before, limit: float = 20.0) -> bool:
    """After a navigation: wait for the new page's document to appear and stop loading. False if it never did."""
    end = time.monotonic() + limit
    time.sleep(0.3)
    while time.monotonic() < end and (_doc_id() in (before, None) or _page_loading()):
        time.sleep(0.25)
    time.sleep(0.3)  # the accessibility tree fills in just after the document appears
    return _doc_id() not in (before, None)


def _focus_address_bar() -> bool:
    """Put keyboard focus in the browser's address bar: the text field above the page. False if there isn't one."""
    try:
        ua = _AutomationClient.instance().IUIAutomation
        root = ua.ElementFromHandle(user32.GetForegroundWindow())
        doc = root.FindFirst(7, ua.CreatePropertyCondition(30003, 50030))
        top = doc.CurrentBoundingRectangle.top if doc else 10**6
        edits = root.FindAll(7, ua.CreatePropertyCondition(30003, 50004))
        for i in range(edits.Length):
            e = edits.GetElement(i)
            if e.CurrentBoundingRectangle.bottom <= top:
                e.SetFocus()
                time.sleep(0.15)
                return True
    except Exception:
        pass
    return False


TAGS = {50000: "button", 50002: "checkbox", 50003: "combobox", 50004: "field", 50005: "link",
        50011: "menuitem", 50013: "radio", 50016: "field", 50019: "tab"}  # 50016: spinner (number input)


INPUT_TAGS = {"checkbox", "radio", "combobox", "field"}
INPUT_TYPES = (50002, 50013, 50003, 50004, 50016)  # checkbox, radio, combobox, edit, spinner


def _inputs_condition(ua):
    cond = ua.CreatePropertyCondition(30003, INPUT_TYPES[0])
    for t in INPUT_TYPES[1:]:
        cond = ua.CreateOrCondition(cond, ua.CreatePropertyCondition(30003, t))
    return cond


def _state(e, ctype):
    """A control's state from cached properties: ticked or not, chosen or not, or the field's value."""
    try:
        if ctype == 50002:
            return {0: "☐", 1: "☑", 2: "◪"}.get(e.GetCachedPropertyValue(30086), "")
        if ctype == 50013:
            return "◉" if e.GetCachedPropertyValue(30079) else "○"
        if ctype in (50003, 50004, 50016):
            return "(hidden)" if e.GetCachedPropertyValue(30019) else e.GetCachedPropertyValue(30045)
    except Exception:
        pass
    return None


def _window_for(title: str):
    """The window showing `title` (window title or browser tab), found without focusing or clicking anything."""
    fg = user32.GetForegroundWindow()
    alts = [a.strip() for a in title.split("|") if a.strip()]
    for test in (lambda alt, label: alt.lower() in label.lower(), _words_in):
        for alt in alts:
            for label, hwnd, tab in _windows_and_tabs():
                if not test(alt, label):
                    continue
                if _blocked(hwnd):
                    raise PermissionError(f"window {label!r} is off limits ({WHERE_BLOCKED})")
                if tab and not tab.GetSelectionItemPattern().IsSelected:
                    if hwnd == fg:  # never switch tabs in the window the user is using
                        raise LookupError(f"tab {label!r} is in the window you're using but not showing; "
                                          f"F({alt}) switches to it")
                    tab.GetSelectionItemPattern().Select()  # browsers only expose the visible tab of a window
                    time.sleep(0.8)
                return hwnd
    raise LookupError(f"no window or browser tab matches {title!r}")


_gdi = ctypes.windll.gdi32
_P = ctypes.c_void_p
user32.GetWindowDC.restype = _gdi.CreateCompatibleDC.restype = _gdi.CreateCompatibleBitmap.restype = _P
user32.GetWindowDC.argtypes = [_P]
user32.PrintWindow.argtypes = [_P, _P, ctypes.c_uint]
user32.ReleaseDC.argtypes = [_P, _P]
_gdi.CreateCompatibleDC.argtypes = _gdi.DeleteObject.argtypes = _gdi.DeleteDC.argtypes = [_P]
_gdi.CreateCompatibleBitmap.argtypes = [_P, ctypes.c_int, ctypes.c_int]
_gdi.SelectObject.argtypes = [_P, _P]
_gdi.GetDIBits.argtypes = [_P, _P, ctypes.c_uint, ctypes.c_uint, _P, _P, ctypes.c_uint]


def _grab_window(hwnd):
    """A window's pixels even while other windows cover it (PrintWindow, full render): (image, left, top) native."""
    from PIL import Image as PILImage
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = user32.GetWindowDC(hwnd)
    mdc, bmp = _gdi.CreateCompatibleDC(hdc), _gdi.CreateCompatibleBitmap(hdc, w, h)
    try:
        _gdi.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT: works for GPU-drawn windows like Chrome
        info = (ctypes.c_int32 * 10)(40, w, -h, 1 | 32 << 16)  # BITMAPINFOHEADER: top-down, 32 bits per pixel
        buf = ctypes.create_string_buffer(w * h * 4)
        _gdi.GetDIBits(mdc, bmp, 0, h, buf, info, 0)
        return PILImage.frombuffer("RGB", (w, h), buf, "raw", "BGRX"), r.left, r.top
    finally:
        _gdi.DeleteObject(bmp)
        _gdi.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)


async def _recognize(img):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    writer = DataWriter()
    writer.write_bytes(img.convert("RGBA").tobytes("raw", "BGRA"))
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)
    return await OcrEngine.try_create_from_user_profile_languages().recognize_async(bmp)


def _ocr(hwnd) -> tuple[list[tuple[str, int, int]], object]:
    """Text in a window by Windows' built-in OCR (no AI, tens of ms): ([(text, x, y)], capture), x,y each item's
    center in Jitter's coordinates. Words on one line split at a wide gap, so two buttons side by side stay two."""
    _no_peeking()
    _check(hwnd)
    img, left, top = _grab_window(hwnd)
    with ThreadPoolExecutor(1) as pool:  # the server's own event loop may be running in this thread
        result = pool.submit(asyncio.run, _recognize(img)).result()
    s, items = _scale(), []
    for line in result.lines:
        chunks = []
        for word in line.words:
            r = word.bounding_rect
            last = chunks[-1][-1][1] if chunks else None
            if last is None or r.x - (last.x + last.width) > 1.5 * max(r.height, last.height):
                chunks.append([])
            chunks[-1].append((word.text, r))
        for chunk in chunks:
            a, b = chunk[0][1], chunk[-1][1]
            cx, cy = left + (a.x + b.x + b.width) / 2, top + a.y + max(a.height, b.height) / 2
            items.append((" ".join(t for t, _ in chunk), round(cx * s), round(cy * s)))
    return items, img


def _messy(found: list) -> bool:
    """OCR output too poor to work from: nothing found, or mostly fragments and symbols."""
    def junk(t):
        letters = sum(c.isalnum() for c in t)
        return letters < 3 or letters < 0.6 * len(t.replace(" ", ""))
    return not found or sum(map(junk, (t for t, _, _ in found))) > 0.4 * len(found)


def _ocr_find(name: str, items: list) -> tuple[str, int, int]:
    """The one OCR'd item `name` means: exact text, then whole word, then substring. LookupError otherwise."""
    low, word = name.lower(), re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.I)
    asked = set(m.lower() for m in OPPOSITES.findall(name))
    items = [i for i in items if set(m.lower() for m in OPPOSITES.findall(i[0])) <= asked]  # never the opposite
    for test in (lambda t: t.lower() == low, word.search, lambda t: low in t.lower()):
        hits = [i for i in items if test(i[0])]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise LookupError(f"{len(hits)} pieces of text match {name!r}: "
                              + "; ".join(f"{t[:30]!r} at ({x}, {y})" for t, x, y in hits[:6]))
    raise LookupError(f"no element or text {name!r} in the foreground window")


@tool
def read(window: str = "", offscreen: bool = True, max_chars: int = 15000, ocr: bool = True):
    """Everything in a window as text with coordinates, in one fast call. Use it before any screenshot.

    Each line is a piece of text or a clickable item with its center, `@x,y` (screenshot coordinates: C(x,y) clicks
    it). Lines without @x,y are scrolled out of view. Accessibility gives exact names and types; OCR then adds any
    text it missed (apps that draw their own UI: games, canvas, remote desktops), marked (OCR). If OCR comes out
    messy, a picture of the window comes back instead of the text (never both). ocr=False skips OCR for speed.

    window: empty for the window in front, or a window title / browser tab to read IN THE BACKGROUND, without taking
    focus or the screen (good for checking on something while the user works). 'a|b' gives alternatives.
    In a browser, only the page is read, not tabs and toolbars. Clickable items are tagged ([button], [link], ...)
    and their names work with E(name); fields show their current value. offscreen=True includes content scrolled
    out of view, so a long page is read without scrolling (apps that lazy-load still need a scroll).
    """
    text, picture = _read(window, offscreen, max_chars, ocr)
    return [text, picture] if picture else text


def _box(e):
    """An element's on-screen box in screenshot coordinates, or None when it's scrolled away or has no size."""
    try:
        r = e.CachedBoundingRectangle
        if e.CachedIsOffscreen or r.right <= r.left or r.bottom <= r.top:
            return None
    except Exception:
        return None
    s = _scale()
    return round(r.left * s), round(r.top * s), round(r.right * s), round(r.bottom * s)


def _read(window: str = "", offscreen: bool = True, max_chars: int = 15000, ocr: bool = True):
    """read() without the MCP wrapper: (text, picture or None)."""
    _no_peeking()
    hwnd = _window_for(window) if window else user32.GetForegroundWindow()
    ua = _AutomationClient.instance().IUIAutomation
    win = ua.ElementFromHandle(hwnd)
    named = ua.CreateNotCondition(ua.CreatePropertyCondition(30005, ""))
    doc = win.FindFirst(7, ua.CreateAndCondition(ua.CreatePropertyCondition(30003, 50030), named))  # Document
    cond = ua.CreateOrCondition(named, _inputs_condition(ua))  # unnamed checkboxes, radios, dropdowns, fields count
    if not offscreen:
        cond = ua.CreateAndCondition(cond, ua.CreatePropertyCondition(30022, False))
    cache = ua.CreateCacheRequest()
    # Name, ControlType, Value, toggled, selected, password, bounding box, offscreen
    for p in (30005, 30003, 30045, 30086, 30079, 30019, 30001, 30022):
        cache.AddProperty(p)
    arr = (doc or win).FindAllBuildCache(7, cond, cache)
    rows, prev = [], ""
    for i in range(arr.Length):
        e = arr.GetElement(i)
        name, ctype = e.CachedName.strip(), e.CachedControlType
        tag = TAGS.get(ctype)
        if not tag and (not name or name in prev):  # a container's name already includes its text children
            continue
        rows.append([tag, name, _state(e, ctype), _box(e)])
        prev = name or prev
    used = set()
    for i, row in enumerate(rows):
        if row[0] in INPUT_TAGS and not row[1]:
            # an unnamed input takes its label from the text beside it: after a checkbox or radio, before a field
            order = (i + 1, i - 1) if row[0] in ("checkbox", "radio") else (i - 1, i + 1)
            for j in order:
                if 0 <= j < len(rows) and not rows[j][0] and 0 < len(rows[j][1]) <= 80 and j not in used:
                    row[1] = rows[j][1]
                    used.add(j)
                    break
    lines = [f"window: {_title(hwnd)}"]
    for i, (tag, name, state, box) in enumerate(rows):
        if i in used:
            continue
        if not tag and i + 1 < len(rows) and rows[i + 1][1] and rows[i + 1][1] in name:  # wrapper repeating next
            continue
        mark = f" {state}" if tag in ("checkbox", "radio") and state else ""
        line = f"[{tag}{mark}] {name}" if tag else name
        line += f" = {state!r}" if tag in ("field", "combobox") and state is not None else ""
        lines.append(line + (f"  @{(box[0] + box[2]) // 2},{(box[1] + box[3]) // 2}" if box else ""))
    if any(row[2] == "(hidden)" for row in rows):  # a password field: the user's step, not Claude's
        lines.append('\n(This page asks for a password. Hand it to the user with human("Human login required"); '
                     "never type or read credentials yourself.)")
    picture = None
    if ocr and user32.IsIconic(hwnd):
        lines.append("\n(The window is minimized, so OCR can't see it: F(its name) brings it up.)")
    elif ocr:
        try:
            found, img = _ocr(hwnd)
        except PermissionError:  # an off-limits window in front: accessibility text only
            found, img = [], None
        # skip what accessibility already listed: text inside a listed item's box, or already named
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        small = 0.15 * (r.right - r.left) * (r.bottom - r.top) * _scale() ** 2
        boxes = [b for *_, b in rows if b and (b[2] - b[0]) * (b[3] - b[1]) < small]
        names = "\n".join(row[1].lower() for row in rows)
        extra = [(t, x, y) for t, x, y in found
                 if not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)
                 and re.sub(r"^\W*\w?\W+", "", t).lower() not in names]
        if img is not None and len(rows) < 8 and _messy(found):
            # the text is unusable: send the picture INSTEAD of it, never both
            s = _scale()
            picture = _jpeg(img.resize((max(1, round(img.width * s)), max(1, round(img.height * s)))))
            head = f"window: {_title(hwnd)}\n(Text unreadable here, so this is a picture instead.)"
            if any(row[2] == "(hidden)" for row in rows):  # keep the sign-in rule even without the text
                head += '\n(This page asks for a password. Hand it to the user with human("Human login required").)'
            return head, picture
        lines += [f"(OCR) {t}  @{x},{y}" for t, x, y in extra]
    text = "\n".join(lines)
    text = text if len(text) <= max_chars else text[:max_chars] + f"\n... truncated ({len(text)} chars)"
    return text, picture


# button, checkbox, combobox, edit, hyperlink, list item, menu item, radio, tab, tree item, data item
CLICKABLE = {50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013, 50016, 50019, 50024, 50029}


OPPOSITES = re.compile(r"\b(remove|delete|leave|block|report|discard|clear|erase|uninstall|disable|unfollow|unfriend|"
                       r"unfavorite|unpin|unsend|unsubscribe|log ?out|sign ?out)\b", re.I)


def _not_opposite(found: list, name: str) -> list:
    """A loose match must not do the opposite of what was asked: 'Favorites' never means 'Remove from Favorites'."""
    asked = set(m.lower() for m in OPPOSITES.findall(name))
    return [m for m in found if set(w.lower() for w in OPPOSITES.findall(m[0].Name)) <= asked]


def _narrow(found: list, name: str) -> list:
    """Break a tie the way a person would: `name` as a whole word ('GIF', not inside 'gift'), on something clickable.

    Each test is tried in turn and only used when it leaves exactly one candidate; otherwise all stay.
    """
    word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.I)
    tests = (lambda c: word.search(c.Name) and c.ControlType in CLICKABLE,
             lambda c: word.search(c.Name),
             lambda c: c.ControlType in CLICKABLE)
    for test in tests:
        hits = [m for m in found if test(m[0])]
        if len(hits) == 1:
            return hits
    return found


@tool
def click_element(name: str) -> str:
    """Click the element named `name` in the foreground window, via UI Automation.

    Exact name first, else substring. Ties are broken like a person would: the one that takes keyboard focus (a field
    over its placeholder), then the name as a whole word on something clickable ('GIF' picks the 'Open GIF picker'
    button over a 'Send a gift' button and a 'Send GIF' label). If still tied, lists them with coordinates.
    """
    with _session():  # look the element up after any countdown, so its position is current when clicked
        for attempt in range(3):
            try:
                c, (nx, ny) = _pick(name)
                break
            except LookupError as e:
                if not str(e).startswith("no element named"):
                    raise
                # nothing by that name in the accessibility tree (a game, canvas or custom-drawn app): try OCR
                text, x, y = _ocr_find(name, _ocr(user32.GetForegroundWindow())[0])
                nx, ny = _native(x, y)
                _target(nx, ny)
                pyautogui.click(nx, ny)
                return f"clicked text {text!r} at ({x}, {y}), found by OCR"
            except Exception:  # UI Automation hiccup (COMError) while the page changes: look again
                if attempt == 2:
                    raise
                time.sleep(0.4)
        what = f"{c.ControlTypeName} {c.Name!r}"  # read now: after the click the element may be gone
        _target(nx, ny)
        pyautogui.click(nx, ny)
    return f"clicked {what}"


def _pick(name: str):
    """The one element `name` means, and its center: (control, (x, y)). LookupError if none or still ambiguous.

    'name#2' means the 2nd match in reading order (top to bottom, left to right), for repeated names.
    """
    nth = re.fullmatch(r"(.+?)\s*#(\d+)", name)
    if nth:
        found = sorted(_innermost(_find_all(nth[1], 1)) or _matches(nth[1]), key=lambda m: (m[1][1] // 10, m[1][0]))
        if not 1 <= int(nth[2]) <= len(found):
            raise LookupError(f"there are {len(found)} matches for {nth[1]!r}, not {nth[2]}")
        c, r = found[int(nth[2]) - 1]
        return c, ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
    found = _matches(name)
    word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.I)
    if found and not any(m[0].ControlType in CLICKABLE for m in found):
        # the name only fits something you can't click (a label, a message): a button with that word is likelier
        buttons = [m for m in _not_opposite(_innermost(_find_all(name, 3)), name)
                   if m[0].ControlType in CLICKABLE and word.search(m[0].Name)]
        found = buttons or found
    if found and any(m[0].Name.lower() != name.lower() for m in found):
        loose = found
        found = _not_opposite(found, name)
        if not found:
            raise LookupError(f"only matches that would do the opposite of {name!r}: "
                              + "; ".join(repr(m[0].Name[:40]) for m in loose[:4]))
    if not found:
        raise LookupError(f"no element named {name!r} in the foreground window")
    s = _scale()
    center = lambda r: ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)  # noqa: E731
    if len(found) > 1 and all(c.Name.lower() == name.lower() for c, _ in found):
        # same exact name on a field and its placeholder/label: the one that takes focus is the target
        focusable = [m for m in found if m[0].IsKeyboardFocusable]
        found = focusable if len(focusable) == 1 else found
    found = _narrow(_in_page(found), name)
    if len(found) > 1:
        listed = "; ".join(f"{c.ControlTypeName} {c.Name[:40]!r} at ({round(x * s)}, {round(y * s)})"
                           for c, r in found[:6] for x, y in [center(r)])
        raise LookupError(f"{len(found)} elements match {name!r}, not guessing: {listed}")
    c, r = found[0]
    if c.ControlType not in CLICKABLE:
        # label text for an unnamed checkbox, radio, dropdown or field: the control beside it is the target
        near = _control_near(r)
        if near:
            c, r = near
    return c, center(r)


def _in_page(found: list) -> list:
    """In a browser, matches inside the page beat the browser's own buttons ('Close' means the page's, not a tab's)."""
    if len(found) < 2 or _exe(user32.GetForegroundWindow()) not in BROWSERS:
        return found
    ua = _AutomationClient.instance().IUIAutomation
    doc = ua.ElementFromHandle(user32.GetForegroundWindow()).FindFirst(7, ua.CreatePropertyCondition(30003, 50030))
    if not doc:
        return found
    d = doc.CurrentBoundingRectangle
    inside = [m for m in found if d.left <= (m[1][0] + m[1][2]) / 2 <= d.right and d.top <= (m[1][1] + m[1][3]) / 2 <= d.bottom]
    return inside or found


def _control_near(r):
    """The unnamed input closest to label text at rect r, within reach: (control, rect), or None."""
    ua = _AutomationClient.instance().IUIAutomation
    cond = ua.CreateAndCondition(ua.CreateAndCondition(ua.CreatePropertyCondition(30005, ""), _inputs_condition(ua)),
                                 ua.CreatePropertyCondition(30022, False))
    arr = ua.ElementFromHandle(user32.GetForegroundWindow()).FindAll(7, cond)
    reach, best = 150 * user32.GetDpiForSystem() / 96, None
    for i in range(arr.Length):
        b = arr.GetElement(i).CurrentBoundingRectangle
        if b.right <= b.left or b.bottom <= b.top:
            continue
        gap = max(0, b.left - r[2], r[0] - b.right) + max(0, b.top - r[3], r[1] - b.bottom)
        if gap <= reach and (best is None or gap < best[0]):
            best = (gap, i, (b.left, b.top, b.right, b.bottom))
    return (uia.Control.CreateControlFromElement(arr.GetElement(best[1])), best[2]) if best else None


# --- Immerse: map the user's frequently used apps into address books (run by the user from the panel) ---

SKIP_APPS = ("ueme_", "microsoft.windows.", "microsoftwindows.", "microsoft.lockapp", "windows.immersivecontrolpanel")
SKIP_WINDOWS = ("program manager", "windows input experience")
kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.restype = wintypes.HANDLE
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def _usage(top: int = 15) -> list[tuple[float, str]]:
    """Apps ranked by focus time, from Windows' own UserAssist stats: [(hours, app)]."""
    import codecs
    import struct
    import winreg

    hours: dict[str, float] = {}
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
        for i in range(winreg.QueryInfoKey(k)[0]):
            try:
                c = winreg.OpenKey(k, winreg.EnumKey(k, i) + r"\Count")
            except OSError:
                continue
            with c:
                for j in range(winreg.QueryInfoKey(c)[1]):
                    raw, data, _ = winreg.EnumValue(c, j)
                    name = codecs.decode(raw, "rot13")
                    low = name.lower()
                    if not isinstance(data, bytes) or len(data) < 72 or low.startswith(SKIP_APPS) \
                            or _title_blocked(low):  # whole-word, so "Claudeswap" still shows up in Immerse
                        continue
                    exe = re.search(r"([^\\/.]+\.exe)$", name, re.I)  # paths -> FL64.exe; app ids stay as-is
                    app = exe.group(1) if exe else name
                    hours[app] = hours.get(app, 0.0) + struct.unpack_from("<I", data, 12)[0] / 3.6e6
    ranked = sorted(((round(h, 1), a) for a, h in hours.items() if h >= 0.05), reverse=True)
    return ranked[:top]


def _exe(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    buf, n = ctypes.create_unicode_buffer(260), wintypes.DWORD(260)
    kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    kernel32.CloseHandle(h)
    return Path(buf.value).stem.lower()


def _addresses() -> list[str]:
    """Clickable names in the foreground window: what E(name) can target."""
    seen: dict[str, None] = {}
    for line in _read(max_chars=10**7, ocr=False)[0].splitlines():
        if line.startswith("["):
            seen.setdefault(line[:100])
    return list(seen)[:300]


def _slug(s: str) -> str:
    return re.sub(r"[^\w-]+", "-", s.lower()).strip("-")[:60] or "unnamed"


def _save_addresses(key: str, header: str, lines: list[str]) -> None:
    ADDRESSES.mkdir(exist_ok=True)
    (ADDRESSES / f"{key}.txt").write_text(f"{header}\nmapped: {time.strftime('%Y-%m-%d %H:%M')}\n\n"
                                          + "\n".join(lines), encoding="utf-8")


def _chrome_url(hwnd) -> str:
    ua = _AutomationClient.instance().IUIAutomation
    bar = ua.ElementFromHandle(hwnd).FindFirst(7, ua.CreateAndCondition(
        ua.CreatePropertyCondition(30003, 50004), ua.CreatePropertyCondition(30005, "Address and search bar")))
    return bar.GetCurrentPropertyValue(30045) if bar else ""


def _map_chrome(hwnd, mapped: dict, budget: int, log) -> int:
    """Visit each open tab of a Chrome window (select, read, move on), then reselect the user's tab."""
    tabs = _tab_strip(hwnd)
    original = next((t for t in tabs if t.GetSelectionItemPattern().IsSelected), None)
    try:
        for t in tabs:
            if budget <= 0:
                break
            _check_abort()
            t.GetSelectionItemPattern().Select()
            time.sleep(1.5)  # let a discarded tab reload
            url = _chrome_url(hwnd)
            s = urlsplit(url if "://" in url else "https://" + url)
            if not s.hostname:
                continue
            # ponytail: one book per host + first path segment (facebook.com/messages vs the feed)
            key = _slug(s.hostname.removeprefix("www.") + "-" + s.path.strip("/").split("/")[0])
            if key in mapped:
                continue
            _save_addresses(key, f"site: {url}\ntab: {t.Name}", _addresses())
            mapped[key] = t.Name
            budget -= 1
            log(f"mapped site {key}")
    finally:
        if original:
            original.GetSelectionItemPattern().Select()
    return budget


def immerse(log=print, max_tabs: int = 12) -> str:
    """Map every open app window (and each open Chrome tab) into addresses/<app>.txt, plus a usage inventory.

    Takes the screen while it runs (a keypress stops it) and puts the windows back the way they were.
    """
    start, mapped, budget = user32.GetForegroundWindow(), {}, max_tabs
    usage = _usage()
    log("frequently used: " + ", ".join(f"{a} ({h}h)" for h, a in usage[:8]))
    try:
        with _session():
            for w in _visible_windows():
                hwnd = w._hWnd
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if _blocked(hwnd) or w.title.lower() in SKIP_WINDOWS or pid.value == ctypes.windll.kernel32.GetCurrentProcessId():
                    continue
                app = _exe(hwnd)
                if app in mapped and app != "chrome":
                    continue
                _check_abort()
                minimized = user32.IsIconic(hwnd)
                try:
                    _raise(hwnd)
                    if app == "chrome":
                        budget = _map_chrome(hwnd, mapped, budget, log)
                        mapped.setdefault("chrome", w.title)
                    else:
                        _save_addresses(_slug(app), f"app: {app}\nwindow: {w.title}", _addresses())
                        mapped[app] = w.title
                        log(f"mapped app {app}")
                except UserStop:
                    raise
                except Exception as e:  # one stubborn window shouldn't sink the whole pass
                    log(f"skipped {w.title!r}: {e}")
                finally:
                    if minimized:
                        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        result = f"mapped {len(mapped)} apps and sites"
    except UserStop:
        result = f"stopped by keypress after {len(mapped)} apps and sites"
    finally:
        try:
            _raise(start)
        except RuntimeError:
            pass
    inventory = ["Frequently used apps (hours in focus, from Windows):"]
    inventory += [f"  {h:>5}h  {a}" + ("  [mapped]" if any(k in a.lower() for k in mapped) else "") for h, a in usage]
    inventory += ["", "Address books (routes(app) shows one):"] + [f"  {_slug(k)}: {t}" for k, t in mapped.items()]
    _save_addresses("_inventory", "Jitter inventory", inventory)
    log(result)
    return result


# --- command language and routes ---

CMD = re.compile(r"\s*([A-Z])(?:\(((?:\\.|[^\\)])*)\))?\s*")
ROUTE_NAME = re.compile(r"[\w-]+/[\w-]+")
NEEDS_ARG = set("FGECTKWVRDZHMPUL")
MAX_DEPTH = 5


def parse(commands: str) -> list[tuple[str, str]]:
    """'F(Chrome)T(hi\\))S' -> [('F', 'Chrome'), ('T', 'hi\\)'), ('S', '')]. Values stay escaped."""
    out, pos = [], 0
    while pos < len(commands):
        m = CMD.match(commands, pos)
        if not m or m.end() == pos:
            raise ValueError(f"bad command at position {pos}: {commands[pos:pos + 20]!r}")
        code, val = m.group(1), m.group(2)
        if code not in NEEDS_ARG | {"S", "X"} or (code != "X" and (val is None) == (code in NEEDS_ARG)):
            raise ValueError(f"bad command {m.group(0).strip()!r}")
        out.append((code, val or ""))
        pos = m.end()
    return out


def _unescape(v: str) -> str:
    # only the command language's own characters are escapes, so Windows paths keep their backslashes
    return re.sub(r"\\([\\(),])", r"\1", v)


def _escape(v: str) -> str:
    return re.sub(r"([\\(),])", r"\\\1", v)


def _args(v: str) -> list[str]:
    return [_unescape(a).strip() for a in re.split(r"(?<!\\),", v)]


def _route_file(name: str) -> Path:
    if not ROUTE_NAME.fullmatch(name):
        raise ValueError(f"route name must look like 'app/task', got {name!r}")
    return ROUTES / name


def expand(name: str, args: list[str]) -> str:
    """Route body with $1, $2, ... replaced by escaped args."""
    f = _route_file(name)
    if not f.exists():
        raise LookupError(f"no route {name!r}")

    def sub(m):
        i = int(m.group(1))
        if i > len(args):
            raise ValueError(f"route {name!r} needs argument ${i}")
        return _escape(args[i - 1])

    return re.sub(r"\$(\d+)", sub, f.read_text(encoding="utf-8"))


def _step(code: str, val: str, depth: int, out: list) -> str:
    if code == "S":
        out.append(_jpeg(_frame()))
        return "screenshot"
    if code == "X":  # X reads the window in front; X(title) reads that window or tab in the background
        text, picture = _read(_unescape(val)) if val else _read()
        out += [text, picture] if picture else [text]
        return f"read {_unescape(val)!r}" if val else "read text"
    if code == "R":
        name, *args = _args(val)
        if depth >= MAX_DEPTH:
            raise RecursionError("routes nested too deep")
        return _run(expand(name, args), depth + 1, out)
    v = _unescape(val)
    if code == "F":
        return focus(v)
    if code == "G":
        before = _doc_id()
        if not _focus_address_bar():  # keyboard focus can be lost (after a dialog, say), so don't rely on ctrl+l
            key("ctrl+l")
        key("ctrl+a")
        type(v)
        key("enter")
        if not _wait_loaded(before):  # the next step sees the loaded page, without a guessed W()
            raise RuntimeError(f"went to {v}, but the page didn't change")
        return f"went to {v}"
    if code == "U":  # U(Human login required): hand the screen to the user for that step
        return human(v)
    if code == "L":  # L(package): fill the sign-in form with a login package, after the user approves
        return deliver(v)
    if code == "H":
        return hover(v)
    if code == "M":
        return right_click(v)
    if code == "P":  # P(from > to): names or x,y on either side
        src, _, dst = v.partition(">")
        return drag_to(src.strip(), dst.strip())
    if code == "E":
        return click_element(v)
    if code == "C":
        return click(*map(int, _args(val)))
    if code == "T":
        return type(v)
    if code == "K":
        return ", ".join(key(k) for k in v.split())  # K(tab tab enter)
    if code == "W":
        return wait(float(v))
    if code == "D":  # D(down 3) or D(up 5, x, y)
        how, *at = _args(val)
        direction, *amount = how.split()
        return scroll(direction.lower(), int(amount[0]) if amount else 3, *map(int, at))
    if code == "Z":
        out.append(zoom(*map(int, _args(val))))
        return "zoomed"
    if code == "V":
        if not _matches(v, timeout=10):  # verifying is waiting: be patient
            raise AssertionError(f"verify failed: {v!r} not found")
        return f"verified {v!r}"


PERMISSION = (PermissionError, UserStop)  # the only failures that end a task; everything else is a detour


def _unparse(steps: list[tuple[str, str]]) -> str:
    return "".join(f"{c}({v})" if v or c in NEEDS_ARG else c for c, v in steps)


def _run(commands: str, depth: int, out: list) -> str:
    log, steps = [], parse(commands)
    for i, (code, val) in enumerate(steps):
        label = f"{code}({val})" if val else code
        rest = _unparse(steps[i + 1:])
        try:
            _check_abort()  # W, V and S steps don't open a session of their own
            _wait_if_paused()
            log.append(f"{label}: {_step(code, val, depth, out)}")
            if code in "ECMKP":  # clicks, keys and drags change the screen: let it settle before the next step
                _settle()
        except _Stopped as e:  # inside a route: its remaining steps come first, then ours
            raise _Stopped("\n".join(log + [f"{label}:", str(e)]), e.remaining + rest, e.permission)
        except Exception as e:  # later steps depend on this one, so hand back instead of pressing on blindly
            permission = isinstance(e, PERMISSION)
            log.append(f"{label}: {'STOPPED' if permission else 'FAILED'}, {e.__class__.__name__}: {e}")
            raise _Stopped("\n".join(log), _unparse(steps[i:]), permission)
    return "\n".join(log) if depth == 0 else "route ok\n  " + "\n  ".join(log)


class _Stopped(Exception):
    def __init__(self, log: str, remaining: str, permission: bool):
        super().__init__(log)
        self.remaining, self.permission = remaining, permission


@tool
def run(commands: str) -> list:
    """Run a command string in one call. Each uppercase letter is a command, its value in parentheses:

    F(title) focus window | G(url) go to URL in focused browser | E(name) click element by name
    C(x,y) click | T(text) type | K(keys) press keys, space-separated, e.g. K(ctrl+a backspace)
    W(s) wait | V(text) verify text is on screen, else stop | S screenshot | X read the window's text, X(title) read
    another window or tab in the background (see read)
    D(down 3) or D(up 5,x,y) scroll | Z(x1,y1,x2,y2) zoom into a region
    H(target) hover | M(target) right-click | P(from > to) drag. A target is an element name or x,y
    U(reason) hand the screen to the user for a step only they should do (sign-in, password, 2FA), see human()
    L(package) fill the sign-in form with a login package once the user approves, see deliver()
    R(app/task, args...) run a saved route
    Escape ( ) , \\ inside values with a backslash.
    A failed step returns a DETOUR (what failed, the window's text with @x,y, the remaining steps): solve that
    step another way, then run the rest. Only a permission restriction is a real stop.
    Example: F(Personal)G(discord.com/channels/@me)E(spdf)V(spdf)T(hello)K(enter)S
    """
    out: list = []
    with _session():
        try:
            log = _run(commands, 0, out)
        except _Stopped as e:
            if e.permission:
                log = f"{e}\n\nSTOPPED by a permission restriction. Do not work around it; tell the user."
            else:
                try:
                    text, picture = _read(max_chars=6000)
                except Exception as err:
                    text, picture = f"(couldn't read the window: {err})", _jpeg(_frame())
                log = (f"{e}\n\nDETOUR, not a stop: the failed step needs another way. Look at the window below, "
                       f"solve that step (other name, C(x,y), keys, search, scroll, another window or tab), then "
                       f"continue with the remaining steps, and re-save the route if one was used.\n"
                       f"Remaining (from the failed step): {e.remaining}\n\n{text}")
                if picture:
                    out.append(picture)
    return [log, *out]


@mcp.tool()  # not journaled itself
def journal(last: int = 40) -> str:
    """What Jitter did recently: each call with its time, duration, outcome (ok / detour / error / permission) and a
    note, steps inside a run indented under it, plus totals. For reviewing how a task went and what to improve."""
    if not JOURNAL.exists():
        return "the journal is empty"
    lines = JOURNAL.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines[-max(1, min(last, 500)):]]
    out = [f"{e['t'][11:]}  {'  ' * e['depth']}{e['tool']}({e['args']})  {e['ms']}ms  {e['outcome'].upper()}"
           + (f"  {e['note']}" if e["note"] and e["outcome"] != "ok" else "") for e in entries]
    top = [e for e in entries if e["depth"] == 0]
    count = lambda o: sum(e["outcome"] == o for e in top)  # noqa: E731
    out.append(f"\n{len(top)} calls in {sum(e['ms'] for e in top) / 1000:.1f}s: {count('ok')} ok, "
               f"{count('detour')} detours, {count('error')} errors, {count('permission')} permission stops")
    return "\n".join(out)


ACCOUNT = re.compile(r"user|e-?mail|login|phone|account|name", re.I)


def _sign_in_fields() -> tuple:
    """The foreground sign-in form's account field (username/email) and password field; either may be None."""
    ua = _AutomationClient.instance().IUIAutomation
    root = ua.ElementFromHandle(user32.GetForegroundWindow())
    doc = root.FindFirst(7, ua.CreatePropertyCondition(30003, 50030))
    cond = ua.CreateAndCondition(ua.CreatePropertyCondition(30003, 50004), ua.CreatePropertyCondition(30022, False))
    arr = (doc or root).FindAll(7, cond)
    fields = [arr.GetElement(i) for i in range(arr.Length)]
    return (next((f for f in fields if not f.CurrentIsPassword and ACCOUNT.search(f.CurrentName or "")), None),
            next((f for f in fields if f.CurrentIsPassword), None))


def _focus_sign_in_field() -> None:
    """Put the cursor in a sign-in form's first field (username/email, else password). Only focuses; types nothing."""
    try:
        account, password = _sign_in_fields()
        if account or password:
            (account or password).SetFocus()
    except Exception:
        pass  # a convenience: the hand-off works without it


@tool
def human(reason: str = "Human login required", wait_minutes: float = 0.0) -> str:
    """Hand the screen to the user for a step only they should do: signing in, a password, a 2FA code, a CAPTCHA,
    a payment. Open that page first, then call this. The border goes away, the corner badge shows `reason`, the
    user's typing doesn't stop Jitter, and Jitter can't see or read the screen until the user clicks the badge to
    say they're done. Returns when they are, or after wait_minutes (0 = the panel's setting): then call again.
    Never type, read or ask for credentials yourself.
    """
    _focus_sign_in_field()  # cursor ready in the form, so the user (or their browser's saved logins) can go at once
    _busy["human"] = reason.strip()[:40] or "Human step"
    end = time.monotonic() + max(0.05, min(wait_minutes or settings()["handoff_minutes"], 60)) * 60
    while _busy["human"] is not None and time.monotonic() < end:
        time.sleep(0.3)
    if _busy["human"] is not None:
        return "still waiting: the user hasn't clicked the badge yet. Call human() again to keep waiting"
    _busy["last"] = time.monotonic()  # Jitter holds the screen again
    return "the user is done: look at the screen again (read or screenshot) and continue the task"


# --- login packages: logins the user saves in the panel. Claude sees a trusted package's name and site, never what's
# inside; Jitter fills it into a sign-in form only after the user approves that delivery with their own click. ---

class _BLOB(ctypes.Structure):
    _fields_ = [("n", wintypes.DWORD), ("p", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Encrypt or decrypt with Windows DPAPI: only this Windows user on this machine can decrypt."""
    # ponytail: DPAPI keeps the file unreadable elsewhere, not from code running as this user
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _BLOB()
    crypt = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not crypt(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):  # 1 = CRYPTPROTECT_UI_FORBIDDEN
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.p, out.n)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.p, ctypes.c_void_p))


def _load_packages() -> list:
    try:
        return json.loads(PACKAGES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_packages(pkgs: list) -> list:
    tmp = PACKAGES.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(pkgs, key=lambda p: p["name"].lower()), indent=2), encoding="utf-8")
    tmp.replace(PACKAGES)
    return package_list()


def package_list() -> list:
    """What the panel shows: name, site and the trust switch. Never the login itself."""
    return [{"name": p["name"], "site": p["site"], "trusted": p["trusted"]} for p in _load_packages()]


def _site(s: str) -> str:
    """'https://www.github.com/login' -> 'github.com'."""
    s = s.strip().lower()
    return (urlsplit(s if "//" in s else "//" + s).hostname or "").removeprefix("www.")


def add_package(name: str, site: str, username: str, password: str) -> list:
    """Save (or replace) a login package. It starts untrusted: the user switches trust on separately."""
    name, site = name.strip(), _site(site)
    if not name or not site or not password:
        raise ValueError("a login package needs a name, a site and a password")
    secret = base64.b64encode(_dpapi(json.dumps({"u": username.strip(), "p": password}).encode(), True)).decode()
    pkgs = [p for p in _load_packages() if p["name"].lower() != name.lower()]
    return _save_packages(pkgs + [{"name": name, "site": site, "trusted": False, "secret": secret}])


def trust_package(name: str, on: bool) -> list:
    pkgs = _load_packages()
    for p in pkgs:
        if p["name"] == name:
            p["trusted"] = bool(on)
    return _save_packages(pkgs)


def delete_package(name: str) -> list:
    return _save_packages([p for p in _load_packages() if p["name"] != name])


@tool
def packages() -> str:
    """Login packages the user made in the Jitter panel and trusts you with: name and site only. Use deliver()."""
    pkgs = [p for p in _load_packages() if p["trusted"]]
    return "\n".join(f"{p['name']}: {p['site']}" for p in pkgs) or "no trusted login packages: use human() for sign-ins"


def _page(hwnd) -> str:
    """The foreground browser tab's site ('github.com'), or '' when it isn't a browser or can't be read."""
    try:
        return _site(_chrome_url(hwnd)) if _exe(hwnd) in BROWSERS else ""
    except Exception:
        return ""


def _ask(lines: list) -> bool:
    """Show the approval box and wait for the user's click. A keypress or no answer in time means no."""
    _busy["ask"] = {"lines": lines, "answer": None}
    end = time.monotonic() + settings()["handoff_minutes"] * 60
    try:
        while _busy["ask"]["answer"] is None and time.monotonic() < end:
            _check_abort()
            time.sleep(0.1)
        return _busy["ask"]["answer"] is True
    finally:
        _busy["ask"] = None


_grant = {"key": None, "until": 0.0}  # one approval also covers the next page of a two-step sign-in, for 2 minutes


@tool
def deliver(package: str) -> str:
    """Fill the sign-in form in front with a login package from packages(). Open the sign-in page first. Jitter
    pauses and asks the user in a box in the middle of the screen; nothing is filled until they approve, and you
    never see the login. Then submit the form yourself (E(Sign in) or K(enter)). For a two-step sign-in, deliver
    again on the password page: the same approval covers it for 2 minutes.
    """
    pkg = next((p for p in _load_packages() if p["name"].lower() == package.strip().lower() and p["trusted"]), None)
    if pkg is None:
        raise PermissionError(f"no trusted login package {package!r}: the user hasn't made or trusted one, so use "
                              "human() for this sign-in")
    _guard_focus()
    with _session():
        hwnd = user32.GetForegroundWindow()
        host = _page(hwnd)
        if host and host != pkg["site"] and not host.endswith("." + pkg["site"]):
            raise PermissionError(f"this page is {host} but {pkg['name']!r} is for {pkg['site']}: not delivered")
        if not any(_sign_in_fields()):
            raise RuntimeError("no sign-in fields here: open the sign-in form, then deliver again")
        where = host or f"the window '{_title(hwnd)}'"
        if _grant["key"] != (pkg["name"], where) or time.monotonic() > _grant["until"]:
            if not _ask([f"Sign in with {pkg['name']}?", where]):
                raise PermissionError(f"the user didn't approve delivering {pkg['name']!r}")
            _grant.update(key=(pkg["name"], where), until=time.monotonic() + 120)
        if user32.GetForegroundWindow() != hwnd:
            raise RuntimeError("the window changed while waiting for approval: nothing was filled")
        login, filled = json.loads(_dpapi(base64.b64decode(pkg["secret"]), False)), []
        for field, value, label in zip(_sign_in_fields(), (login["u"], login["p"]), ("username", "password")):
            if field and value:
                field.SetFocus()
                pyautogui.hotkey("ctrl", "a")
                _type_unicode(value)  # not type(): the journal records type()'s text
                filled.append(label)
    return f"filled the {' and '.join(filled)} from {pkg['name']}: now submit the form (E(Sign in) or K(enter))"


@tool
def done() -> str:
    """Call when the task is finished: hides the overlay and gives the screen back to the user."""
    _busy["last"] = 0.0
    return "screen released"


@tool
def save_route(name: str, commands: str) -> str:
    """Save a command string as routes/<app>/<task> after completing a task, so R(app/task, ...) repeats it.

    Use $1, $2, ... for argument slots, e.g. save_route('discord/dm-user', 'F(Discord)E($1)T($2)K(enter)').
    Overwrites an existing route of the same name.
    """
    parse(commands)
    f = _route_file(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(commands, encoding="utf-8")
    return f"saved route {name}"


@tool
def routes(app: str = "") -> str:
    """Saved routes and address books. No app: every route plus the inventory of mapped apps.

    With app (e.g. 'messenger', 'facebook', 'discord'): its routes and its address book, the clickable names
    Immerse found, ready for E(name). Partial names match.
    """
    q = _slug(app) if app else ""
    files = sorted(p for p in ROUTES.glob("*/*") if p.is_file() and q in p.parent.name)
    out = [f"{p.parent.name}/{p.name}: {p.read_text(encoding='utf-8').strip()}" for p in files] or ["no routes yet"]
    books = sorted(ADDRESSES.glob(f"*{q}*.txt")) if q else [ADDRESSES / "_inventory.txt"]
    out += [f"\n--- {p.stem} ---\n{p.read_text(encoding='utf-8')}" for p in books if p.exists()]
    if not ADDRESSES.exists():
        out.append("\nno address books yet: the user can run Immerse from the Jitter panel (panel())")
    return "\n".join(out)


@tool
def panel() -> str:
    """Open the Jitter control panel for the user: settings, blocked windows, Immerse, routes and address books."""
    subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), str(HERE / "panel.py")])
    return "opened the Jitter panel"


if __name__ == "__main__":
    threading.Thread(target=_overlay, daemon=True).start()
    threading.Thread(target=_keyhook, daemon=True).start()
    mcp.run()
