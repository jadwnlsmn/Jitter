"""Jitter control panel: panel.html in a native window (pywebview on Edge WebView2), backed by server.py.

Run with the venv's pythonw (the Jitter Panel shortcut does).
"""
import ctypes
import re
import threading
from pathlib import Path

import webview

import server
import queue_bridge


def library() -> list:
    """Tree nodes {label, count?, path?, code?, dim?, children}: routes by drawer, address books by kind."""
    drawers: dict = {}
    for f in sorted(server.ROUTES.glob("*/*")):
        if f.is_file():
            drawers.setdefault(f.parent.name, []).append(
                {"label": f.name, "path": str(f), "children": [{"label": f.read_text(encoding="utf-8").strip(), "code": True}]})
    books = []
    for f in sorted(server.ADDRESSES.glob("*.txt")):
        head, kinds = [], {}
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\[(\w+)\] (.*)", line)
            if m:
                kinds.setdefault(m.group(1), []).append({"label": m.group(2), "code": True})
            elif line.strip():
                head.append({"label": line.strip(), "dim": True})
        books.append({"label": f.stem, "path": str(f), "children": head + [
            {"label": f"{k}s", "count": len(v), "children": v} for k, v in sorted(kinds.items())]})
    routes = [{"label": d, "count": len(v), "children": v} for d, v in sorted(drawers.items())]
    return [{"label": "Routes", "count": sum(len(v) for v in drawers.values()),
             "children": routes or [{"label": "Claude saves a route here after doing a task once", "dim": True}]},
            {"label": "Address books", "count": len(books),
             "children": books or [{"label": "Run Immerse to fill this", "dim": True}]}]


class Api:
    """Everything the page can call, as window.pywebview.api.<name>(...)."""

    def __init__(self):
        self._window = None
        self._log: list[str] = []
        self._running = False

    def state(self) -> dict:
        try:
            cfg, broken = server.load_claude(), False
        except ValueError:
            cfg, broken = {}, True
        return {"permissions": [{"label": l, "on": r in server.rules(cfg, w)} for l, w, r in server.CLAUDE_PERMISSIONS],
                "claude_settings_broken": broken, "settings": server.settings(),
                "always_blocked": list(server.ALWAYS_BLOCKED), "library": library(), "version": server.VERSION,
                "packages": server.package_list(), "queue": queue_bridge.state(),
                "components": {"screen": True, "queue": queue_bridge.installed(), "agentic": _agentic_installed()}}

    # --- Queue section (no-ops that report "not installed" if Queue isn't there) ---
    def queue_state(self) -> dict:
        return queue_bridge.state()

    def queue_answer_checkin(self, checkin_id: int, answer: str) -> dict:
        return queue_bridge.answer_checkin(checkin_id, answer)

    def queue_start_mission(self, goal: str, run_hours: float, budget_wakes: int, permissions: str) -> dict:
        return queue_bridge.start_mission(goal, run_hours, budget_wakes, permissions)

    def queue_mission_action(self, mission_id: int, action: str, add_hours: float = 1.0, add_wakes: int = 20) -> dict:
        return queue_bridge.mission_action(mission_id, action, add_hours, add_wakes)

    def queue_set_worker(self, name: str, enabled: bool) -> dict:
        return queue_bridge.set_worker(name, enabled)

    def queue_remove_worker(self, name: str) -> dict:
        return queue_bridge.remove_worker(name)

    def queue_save_settings(self, new: dict) -> dict:
        return queue_bridge.save_queue_settings(new)

    def set_permission(self, i: int, on: bool) -> bool:
        _, where, rule = server.CLAUDE_PERMISSIONS[i]
        server.set_permission(where, rule, on)
        return on

    def save_settings(self, new: dict) -> dict:
        return server.save_settings(new)

    def add_package(self, name: str, site: str, username: str, password: str) -> list:
        return server.add_package(name, site, username, password)

    def trust_package(self, name: str, on: bool) -> list:
        return server.trust_package(name, on)

    def delete_package(self, name: str) -> list:
        return server.delete_package(name)

    def immerse(self, max_tabs: int) -> None:
        if self._running:
            return
        self._running, self._log = True, ["Running. Press any key to stop."]

        def work():
            with server.uia.UIAutomationInitializerInThread():
                try:
                    server.immerse(self._log.append, int(max_tabs))
                except Exception as e:
                    self._log.append(f"Failed: {e}")
            self._running = False

        threading.Thread(target=work, daemon=True).start()

    def immerse_status(self) -> dict:
        return {"running": self._running, "log": self._log}

    def delete(self, path: str) -> list:
        p = Path(path).resolve()
        # only files Jitter owns: a route or an address book, never anything the page names elsewhere
        if p.parent.parent == server.ROUTES.resolve() or p.parent == server.ADDRESSES.resolve():
            p.unlink(missing_ok=True)
        return library()

    def minimize(self) -> None:
        self._window.minimize()

    def close(self) -> None:
        self._window.destroy()


def _agentic_installed() -> bool:
    return (Path(__file__).resolve().parent.parent / "Jitter-Agentic" / "server.py").exists()


ICON = server.HERE / "assets" / "jitter.ico"
TITLE = "Jitter"


def _decorate(window) -> None:
    """Rounded Windows 11 corners and the Jitter icon on the title and taskbar."""
    try:
        hwnd = window.native.Handle.ToInt64()
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)  # corners: round
        load = ctypes.windll.user32.LoadImageW
        load.restype = ctypes.c_void_p
        for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG; 1 = IMAGE_ICON, 0x10 = LR_LOADFROMFILE
            icon = load(None, str(ICON), 1, size, size, 0x10)
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, which, ctypes.c_void_p(icon))  # WM_SETICON
    except Exception:
        pass  # cosmetic only


def _already_open() -> bool:
    """One panel at a time: if one is open, bring it forward instead."""
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    _already_open.mutex = kernel32.CreateMutexW(None, False, "Local\\JitterPanel")  # held for the process's life
    if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return False
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return True


if __name__ == "__main__":
    if _already_open():
        raise SystemExit
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Jitter.Panel")  # own taskbar button, not Python's
    api = Api()
    api._window = webview.create_window(TITLE, str(server.HERE / "panel.html"), js_api=api, width=920,
                                        height=620, min_size=(760, 520), frameless=True, easy_drag=False,
                                        background_color="#0e0e10")
    api._window.events.shown += lambda: _decorate(api._window)
    threading.Thread(target=server._keyhook, daemon=True).start()  # a keypress stops Immerse
    threading.Thread(target=server._overlay, daemon=True).start()  # border, countdown and pause badge for Immerse
    webview.start()
