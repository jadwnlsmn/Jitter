"""Optional bridge from the Jitter panel to Jitter: Queue, if it's installed beside this folder. Screen never
depends on Queue: if Queue isn't found, everything here reports 'not installed' and the panel hides its section.

Queue's core is stdlib-only (sqlite/json), so the panel reads and writes Queue's database directly without needing
Queue's venv or its MCP deps. Claude is still the one who writes workers and plans missions; this is just the UI.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

_core = None  # queue_core module, or None when Queue isn't installed


def _find_queue():
    global _core
    if _core is not None:
        return _core
    here = Path(__file__).resolve().parent.parent
    for base in (here / "queue", here / "Jitter-Queue"):  # the bundle's layout, or installed side by side
        qc = base / "queue_core.py"
        if qc.exists():
            try:
                sys.path.insert(0, str(base))
                spec = importlib.util.spec_from_file_location("queue_core", qc)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _core = mod
                return mod
            except Exception:
                return None
    return None


def installed() -> bool:
    return _find_queue() is not None


def _rows(con, q, *a):
    return [dict(r) for r in con.execute(q, a).fetchall()]


def state() -> dict:
    """Everything the panel's Queue section shows: supervisor health, missions, open check-ins, workers, settings."""
    qc = _find_queue()
    if qc is None:
        return {"installed": False}
    con = qc.db()
    beat = con.execute("select value from meta where key='heartbeat'").fetchone()
    alive = bool(beat) and time.time() - float(beat[0]) < 15
    missions = _rows(con, "select * from missions order by id desc limit 50")
    for m in missions:
        m["tasks"] = _rows(con, "select id,title,status,urgency,needs_you,optional,result from tasks "
                                "where mission_id=? order by id", m["id"])
    return {
        "installed": True,
        "supervisor": "running" if alive else "stopped",
        "paused": qc.settings().get("paused", False),
        "missions": missions,
        "checkins": _rows(con, "select * from checkins where answer is null order by id desc"),
        "workers": _rows(con, "select name,stimulus,enabled,wake,reaction,last_error,runs,fails,last_run "
                              "from workers order by name"),
        "notes": _rows(con, "select worker,ts,text,woke from notes order by id desc limit 40"),
        "settings": qc.settings(),
    }


def answer_checkin(checkin_id: int, answer: str) -> dict:
    qc = _find_queue()
    con = qc.db()
    c = con.execute("select mission_id from checkins where id=?", (checkin_id,)).fetchone()
    con.execute("update checkins set answer=?, answered_ts=? where id=?", (answer.strip(), time.time(), checkin_id))
    if c:
        con.execute("update missions set status='running' where id=? and status='waiting'", (c["mission_id"],))
    return state()


def start_mission(goal: str, run_hours: float, budget_wakes: int, permissions: str) -> dict:
    qc = _find_queue()
    con = qc.db()
    con.execute("insert into missions(goal, created, run_until, budget_wakes, permissions, status) "
                "values(?,?,?,?,?, 'planning')",
                (goal.strip(), time.time(), time.time() + max(0.1, run_hours) * 3600, max(1, int(budget_wakes)),
                 permissions.strip()))
    return state()


def mission_action(mission_id: int, action: str, add_hours: float = 1.0, add_wakes: int = 20) -> dict:
    qc = _find_queue()
    con = qc.db()
    if action == "cancel":
        con.execute("update missions set status='cancelled' where id=?", (mission_id,))
    elif action == "extend":
        con.execute("update missions set run_until=run_until+?, budget_wakes=budget_wakes+?, "
                    "status=case when status in ('done','cancelled') then status else 'running' end where id=?",
                    (max(0, add_hours) * 3600, max(0, int(add_wakes)), mission_id))
    return state()


def set_worker(name: str, enabled: bool) -> dict:
    qc = _find_queue()
    qc.db().execute("update workers set enabled=? where name=?", (int(enabled), name))
    return state()


def remove_worker(name: str) -> dict:
    qc = _find_queue()
    qc.db().execute("delete from workers where name=?", (name,))
    for f in (qc.WORKERS / f"{name}.py", qc.STATE / f"{name}.json"):
        f.unlink(missing_ok=True)
    return state()


def save_queue_settings(new: dict) -> dict:
    qc = _find_queue()
    cur = {}
    if qc.SETTINGS.exists():
        try:
            cur = json.loads(qc.SETTINGS.read_text(encoding="utf-8"))
        except ValueError:
            pass
    cur.update(new)
    qc.SETTINGS.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return state()
