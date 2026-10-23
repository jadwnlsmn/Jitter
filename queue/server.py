"""Jitter: Queue: Claude's side. Turn the user's stimuli into workers that watch for them, read what the workers
noticed, and manage them. The supervisor (supervisor.py) runs the workers and wakes Claude to react.
"""
import json
import subprocess
import sys
import time

from mcp.server.fastmcp import FastMCP

from notify import notify
from queue_core import HERE, SETTINGS, STATE, WORKERS, ago, check_name, db, settings

mcp = FastMCP("jitter-queue", instructions="""\
Jitter: Queue lets you act without the user. The user gives a stimulus ("tell me when X replies", "when grades
post, summarize them", "if the site goes down, ..."). You write a WORKER that watches for it, and the supervisor
runs it every interval. When it notices something, the note is saved and, if the worker has a reaction, Claude is
woken headless to carry the reaction out.

Writing a worker (add_worker):
- A Python file that defines check(state) and returns a note string, a list of notes, or None when there's nothing
  new. It runs in a fresh process every interval; `state` is a dict that persists between checks (JSON values
  only). Use it to remember what you've seen so you report only what's new. The first check should record a
  baseline and return None. helpers.new_items(state, key, items) does exactly that. A quiet first check is that
  working, not a broken worker — don't "fix" it.
- Don't write one from scratch: copy templates/worker.py and fill its three blanks. Every stimulus is three
  choices from the palette in helpers.py -- a SOURCE (fetch, window_text, read_file, dir_list, ping), an EXTRACT
  (match, extract, json_path, count), and a CONDITION (new_items, changed, matched, crossed, every, at_time).
  Compose those for this app and stimulus; reach past the palette to any Python only when none of the blocks fit.
  Workers run with the user's rights.
- Workers only OBSERVE. Never click, type, send, post, or change anything in a worker. Acting is the reaction's job.
- Notes should be short and self-contained ("Sam in Study Group: see you at 5").
- The reaction is what a woken Claude will do with each note, unattended, with only the Jitter Screen and Queue
  tools. Read it back to the user before saving, since it runs without them. An empty reaction means notes only.
- Always test_worker() after adding, and fix it until a check runs cleanly.
Pick intervals that fit: chats 15-60s, sites minutes, slow things hours.

MANAGER MODE (start_mission): the user gives a goal-level to-do list and a time/wake budget, and you run it on your
own, like a manager. First confirm with the user: the goal, run_hours, budget_wakes, and the standing permissions
(what you may do unattended, and what must always be handed back). Then start_mission. The supervisor wakes you to
plan the goal into tasks (add_task, then plan_done), then wakes you to work them one at a time, urgent/deadline
first. Hand anything only the user should do (payments, submitting graded work, posting publicly) back as a
needs_you task or a raise_checkin; never do those unattended. finish_task / block_task when a task ends. Optional
tasks and budget overruns become check-ins the user answers. Watch a mission with mission(id).""")


@mcp.tool()
def add_worker(name: str, stimulus: str, code: str, interval_seconds: float = 60, reaction: str = "",
               wake: bool = True) -> str:
    """Create or replace a worker. name: lowercase-dashes ('messenger-study-group'). stimulus: the user's request in their
    words. code: Python defining check(state). reaction: what a woken Claude does with each note (empty: notes only).
    wake: False to only collect notes. interval_seconds: 5 or more."""
    import types
    check_name(name)
    compiled = compile(code, f"{name}.py", "exec")  # a syntax error fails here, not at 3am
    if not any(isinstance(c, types.CodeType) and c.co_name == "check" for c in compiled.co_consts):
        raise ValueError("the code must define check(state) as a function (def check(state): ...)")
    (WORKERS / f"{name}.py").write_text(code, encoding="utf-8")
    con = db()
    con.execute("insert or replace into workers(name, stimulus, reaction, interval_s, enabled, wake, created, next_run)"
                " values(?,?,?,?,1,?,?,0)", (name, stimulus, reaction, max(5.0, interval_seconds), int(wake), time.time()))
    return f"worker {name} saved; it runs within seconds once the supervisor is up. Call test_worker('{name}') now."


@mcp.tool()
def test_worker(name: str) -> str:
    """Run one check now and show what it returned (or its error). Updates its state, like a real check."""
    p = subprocess.run([sys.executable, str(HERE / "run_worker.py"), check_name(name)], capture_output=True,
                       text=True, encoding="utf-8", timeout=120, cwd=HERE,
                       stdin=subprocess.DEVNULL)  # not the MCP pipe: a child holding it can hang the call
    if p.returncode:
        return f"FAILED:\n{(p.stderr or p.stdout).strip()[-3000:]}"
    return f"ran cleanly. notes: {p.stdout.strip().splitlines()[-1] if p.stdout.strip() else '[]'}"


@mcp.tool()
def workers() -> str:
    """Every worker with its stimulus, interval, status and last error, and whether the supervisor is running."""
    con = db()
    beat = con.execute("select value from meta where key='heartbeat'").fetchone()
    alive = beat and time.time() - float(beat[0]) < 15
    lines = [f"supervisor: {'running' if alive else 'NOT running'}"
             + (" (paused)" if settings()["paused"] else "")]
    for w in con.execute("select * from workers order by name"):
        lines.append(f"- {w['name']} [{'on' if w['enabled'] else 'off'}, every {w['interval_s']:g}s, "
                     f"{'wakes Claude' if w['wake'] and w['reaction'] else 'notes only'}] {w['stimulus']}\n"
                     f"  last run {ago(w['last_run'])}, {w['runs']} runs, {w['fails']} failures"
                     + (f"\n  last error: {w['last_error'][-300:]}" if w["last_error"] else ""))
    return "\n".join(lines) if len(lines) > 1 else lines[0] + "\nno workers yet"


@mcp.tool()
def notes(worker: str = "", unread_only: bool = True, mark_read: bool = True, limit: int = 50) -> str:
    """What the workers noticed, newest last. Marks them read unless mark_read=False."""
    con = db()
    q, args = "select * from notes where 1=1", []
    if worker:
        q, args = q + " and worker=?", [check_name(worker)]
    if unread_only:
        q += " and read=0"
    rows = con.execute(q + " order by id desc limit ?", (*args, max(1, min(limit, 500)))).fetchall()[::-1]
    if mark_read and rows:
        con.execute(f"update notes set read=1 where id in ({','.join('?' * len(rows))})", [r["id"] for r in rows])
    woke = {0: "", 1: " (woke Claude)", -1: " (wake skipped: hourly limit)", -2: " (held for quiet hours)"}
    return "\n".join(f"[{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}] {r['worker']}: {r['text']}"
                     + woke.get(r["woke"], "") for r in rows) or "no notes"


@mcp.tool()
def wakes(limit: int = 10) -> str:
    """The latest times a worker woke Claude, and what that Claude said it did."""
    rows = db().execute("select * from wakes order by id desc limit ?", (max(1, min(limit, 100)),)).fetchall()
    return "\n\n".join(f"[{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}] {r['worker']} "
                       f"(exit {r['exit_code'] if r['exit_code'] is not None else 'still running'}):\n"
                       f"{(r['output'] or '')[-800:]}" for r in rows) or "no wakes yet"


@mcp.tool()
def set_worker(name: str, enabled: bool | None = None, interval_seconds: float | None = None,
               reaction: str | None = None, wake: bool | None = None) -> str:
    """Change a worker: turn it on/off, change its interval, reaction, or whether it wakes Claude."""
    con = db()
    if not con.execute("select 1 from workers where name=?", (check_name(name),)).fetchone():
        raise LookupError(f"no worker {name!r}")
    for col, val in (("enabled", enabled), ("interval_s", interval_seconds and max(5.0, interval_seconds)),
                     ("reaction", reaction), ("wake", wake)):
        if val is not None:
            con.execute(f"update workers set {col}=? where name=?", (int(val) if isinstance(val, bool) else val, name))
    return f"updated {name}"


@mcp.tool()
def remove_worker(name: str) -> str:
    """Delete a worker, its code and its memory. Its notes stay."""
    db().execute("delete from workers where name=?", (check_name(name),))
    for f in (WORKERS / f"{name}.py", STATE / f"{name}.json"):
        f.unlink(missing_ok=True)
    return f"removed {name}"


@mcp.tool()
def pause(paused: bool = True) -> str:
    """Pause (or resume) every worker and wake at once."""
    cfg = {**(json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}), "paused": paused}
    SETTINGS.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return "paused all workers" if paused else "resumed all workers"


# --- manager mode: missions, tasks, check-ins ---


@mcp.tool()
def start_mission(goal: str, run_hours: float = 3.0, budget_wakes: int = 40, permissions: str = "") -> str:
    """Give Claude a goal-level to-do list to work on its own over a time/wake budget. Claude plans it into tasks,
    does the urgent and deadline-bound ones first, hands you anything only you should do, and asks before optional
    work. Before calling this, confirm with the user: the goal, how long to run (run_hours), the wake budget, and
    `permissions` (what Claude may do unattended, e.g. 'reply in my class Discord and Messenger; draft but never submit
    schoolwork; never send money or post publicly'). The supervisor plans within seconds once it's up."""
    con = db()
    mid = con.execute("insert into missions(goal, created, run_until, budget_wakes, permissions, status) "
                      "values(?,?,?,?,?, 'planning')",
                      (goal.strip(), time.time(), time.time() + max(0.1, run_hours) * 3600,
                       max(1, budget_wakes), permissions.strip())).lastrowid
    return f"mission {mid} created and will be planned shortly. Watch it with mission({mid})."


@mcp.tool()
def add_task(mission_id: int, title: str, detail: str = "", urgency: int = 2, deadline: str = "",
             needs_you: bool = False, optional: bool = False, depends_on: str = "") -> str:
    """Add one task to a mission (used by the planner Claude while planning). urgency 1-4 (4 = urgent/deadline).
    deadline: ISO time or empty. needs_you: only the user can do it (handed to them, not done). depends_on: comma
    task ids that must finish first."""
    import datetime
    dl = None
    if deadline.strip():
        try:
            dl = datetime.datetime.fromisoformat(deadline.strip()).timestamp()
        except ValueError:
            dl = None
    tid = db().execute("insert into tasks(mission_id, title, detail, urgency, deadline, needs_you, optional, "
                       "depends_on, created) values(?,?,?,?,?,?,?,?,?)",
                       (mission_id, title.strip(), detail.strip(), max(1, min(int(urgency), 4)), dl,
                        int(needs_you), int(optional), depends_on.strip(), time.time())).lastrowid
    return f"task {tid} added to mission {mission_id}"


@mcp.tool()
def plan_done(mission_id: int) -> str:
    """Call when every task for a mission has been added. Moves the mission from planning to running."""
    db().execute("update missions set status='running' where id=? and status='planning'", (mission_id,))
    return f"mission {mission_id} planned; working it now"


@mcp.tool()
def finish_task(task_id: int, result: str = "done") -> str:
    """Mark a task done with a one-line result (used by the worker Claude when it finishes a task)."""
    con = db()
    if not con.execute("select 1 from tasks where id=?", (task_id,)).fetchone():
        raise LookupError(f"no task {task_id}")
    con.execute("update tasks set status='done', result=?, done_at=? where id=?", (result.strip(), time.time(), task_id))
    return f"task {task_id} done"


@mcp.tool()
def block_task(task_id: int, why: str) -> str:
    """Mark a task blocked with the reason (the worker Claude couldn't finish it)."""
    con = db()
    if not con.execute("select 1 from tasks where id=?", (task_id,)).fetchone():
        raise LookupError(f"no task {task_id}")
    con.execute("update tasks set status='blocked', result=? where id=?", (why.strip(), task_id))
    return f"task {task_id} blocked"


@mcp.tool()
def raise_checkin(mission_id: int, question: str, kind: str = "answer", options: str = "", task_id: int = 0) -> str:
    """Ask the user something and stop: a decision, an approval, or something only they can do. kind: 'approve' or
    'answer'. options: comma-separated choices, if any. The mission waits until they answer in the panel."""
    opts = json.dumps([o.strip() for o in options.split(",") if o.strip()])
    con = db()
    cid = con.execute("insert into checkins(mission_id, task_id, kind, question, options, ts) values(?,?,?,?,?,?)",
                      (mission_id, task_id or None, kind.strip(), question.strip(), opts, time.time())).lastrowid
    if task_id:  # park the task so reconcile won't record it 'done'; the answer resumes it (and reaches this task)
        con.execute("update tasks set status='waiting' where id=? and status='doing'", (task_id,))
    notify(f"Mission #{mission_id} needs you", question.strip()[:200])
    return f"check-in {cid} raised; the mission waits for the user"


@mcp.tool()
def tell_user(message: str, title: str = "Jitter") -> str:
    """Send the user a Windows notification. Use when you've done something they asked to be told about, or finished
    a mission, and no session is open. For a decision or approval you need, use raise_checkin instead."""
    notify(title.strip() or "Jitter", message.strip())
    return "notified the user"


@mcp.tool()
def mission(mission_id: int = 0) -> str:
    """A mission's goal, budget, status and tasks (or, with no id, all missions). Also shows open check-ins."""
    con = db()
    if not mission_id:
        rows = con.execute("select id, goal, status, wakes_used, budget_wakes from missions order by id desc").fetchall()
        return "\n".join(f"#{r['id']} [{r['status']}] {r['wakes_used']}/{r['budget_wakes']} wakes: {r['goal'][:70]}"
                         for r in rows) or "no missions yet"
    m = con.execute("select * from missions where id=?", (mission_id,)).fetchone()
    if not m:
        raise LookupError(f"no mission {mission_id}")
    out = [f"Mission #{m['id']} [{m['status']}] - {m['goal']}",
           f"  budget: {m['wakes_used']}/{m['budget_wakes']} wakes, runs until {time.strftime('%m-%d %H:%M', time.localtime(m['run_until']))}",
           f"  permissions: {m['permissions'] or '(none set)'}"]
    if m["summary"]:
        out.append(f"  summary: {m['summary']}")
    mark = {"todo": "·", "doing": "▸", "waiting": "❓", "done": "✓", "blocked": "✗", "skipped": "–"}
    for t in con.execute("select * from tasks where mission_id=? order by id", (mission_id,)):
        flags = "".join(f for f, on in (("!", t["urgency"] >= 4), ("@you", t["needs_you"]), ("?opt", t["optional"])) if on)
        out.append(f"  {mark.get(t['status'],'·')} [{t['id']}] {t['title']} {flags}"
                   + (f" — {t['result']}" if t["result"] else ""))
    opens = con.execute("select * from checkins where mission_id=? and answer is null", (mission_id,)).fetchall()
    for c in opens:
        out.append(f"  ❓ check-in {c['id']}: {c['question']}")
    return "\n".join(out)


@mcp.tool()
def checkins(answered: bool = False) -> str:
    """Open check-ins waiting for the user (or, with answered=True, ones already answered). Answer with answer_checkin."""
    q = "select * from checkins where answer is " + ("not null" if answered else "null") + " order by id desc"
    rows = db().execute(q).fetchall()
    return "\n".join(f"[{c['id']}] mission {c['mission_id']} ({c['kind']}): {c['question']}"
                     + (f"  options: {', '.join(json.loads(c['options']))}" if json.loads(c["options"]) else "")
                     + (f"  -> {c['answer']}" if c["answer"] else "") for c in rows) or "no check-ins"


@mcp.tool()
def answer_checkin(checkin_id: int, answer: str) -> str:
    """Answer a check-in (an approval or a decision). The mission resumes on the next tick."""
    con = db()
    c = con.execute("select * from checkins where id=?", (checkin_id,)).fetchone()
    if not c:
        raise LookupError(f"no check-in {checkin_id}")
    con.execute("update checkins set answer=?, answered_ts=? where id=?", (answer.strip(), time.time(), checkin_id))
    con.execute("update missions set status='running' where id=? and status='waiting'", (c["mission_id"],))
    return f"answered check-in {checkin_id}; mission {c['mission_id']} will resume"


@mcp.tool()
def extend_mission(mission_id: int, add_hours: float = 1.0, add_wakes: int = 20) -> str:
    """Give a mission more time and wake budget, and resume it if it had stopped at its limit."""
    db().execute("update missions set run_until=run_until+?, budget_wakes=budget_wakes+?, "
                 "status=case when status in ('done','cancelled') then status else 'running' end where id=?",
                 (max(0, add_hours) * 3600, max(0, add_wakes), mission_id))
    return f"mission {mission_id} extended by {add_hours}h / {add_wakes} wakes"


@mcp.tool()
def cancel_mission(mission_id: int) -> str:
    """Stop a mission. Tasks already done stay recorded; nothing more is worked."""
    db().execute("update missions set status='cancelled' where id=?", (mission_id,))
    return f"mission {mission_id} cancelled"


if __name__ == "__main__":
    mcp.run()
