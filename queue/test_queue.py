import os
import tempfile

os.environ["JITTER_QUEUE_HOME"] = tempfile.mkdtemp(prefix="jq-test-")  # never touch the user's real queue

import pytest  # noqa: E402

import server  # noqa: E402
import supervisor  # noqa: E402
from queue_core import DEFAULTS, STATE, db  # noqa: E402

REAL_CLAUDE = supervisor._claude  # the fixture swaps it out; one test needs the real one

COUNTER = '''
from helpers import new_items
def check(state):
    state["n"] = state.get("n", 0) + 1
    return new_items(state, "seen", [f"item {i}" for i in range(state["n"])])
'''


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    con = db()
    for t in ("workers", "notes", "wakes", "meta", "missions", "tasks", "checkins"):
        con.execute(f"delete from {t}")
    for f in STATE.glob("*.json"):  # worker memory from earlier tests
        f.unlink()
    woken = []
    monkeypatch.setattr(supervisor, "_claude", lambda wake_id, prompt, cfg: woken.append(prompt))
    monkeypatch.setattr(supervisor, "_background", lambda fn, *args: fn(*args))  # run wakes inline in tests
    # run worker checks inline, not on the thread pool, so a test can assert right after tick()
    monkeypatch.setattr(supervisor._pool, "submit", lambda fn, *a: fn(*a))
    return woken


def test_add_worker_checks_name_and_code():
    with pytest.raises(ValueError):
        server.add_worker("Bad Name", "x", COUNTER)
    with pytest.raises(SyntaxError):
        server.add_worker("ok", "x", "def check(state) return 1")
    with pytest.raises(ValueError):
        server.add_worker("ok", "x", "x = 1")
    assert "saved" in server.add_worker("counter", "count things", COUNTER)


def test_first_check_is_a_quiet_baseline_then_only_new_items():
    server.add_worker("counter", "count things", COUNTER)
    assert server.test_worker("counter").endswith("notes: []")
    assert '"item 1"' in server.test_worker("counter")


def test_supervisor_records_notes_and_wakes_with_an_hourly_limit(fresh, monkeypatch):
    monkeypatch.setattr(supervisor, "settings", lambda: {**DEFAULTS, "max_wakes_per_hour": 1})
    server.add_worker("counter", "tell me about new items", COUNTER, interval_seconds=5, reaction="say hi")
    con = db()
    for _ in range(3):  # baseline, then two checks that each find a new item
        con.execute("update workers set next_run=0")
        supervisor.tick(con)
    rows = con.execute("select text, woke from notes order by id").fetchall()
    assert [r["text"] for r in rows] == ["item 1", "item 2"]
    assert [r["woke"] for r in rows] == [1, -1]  # the second wake hit the hourly limit
    assert len(fresh) == 1 and "item 1" in fresh[0] and "say hi" in fresh[0]
    assert "notes" in server.notes() or "item 1" in server.notes(unread_only=False)


def test_broken_worker_backs_off_and_reports_its_error():
    server.add_worker("broken", "x", "def check(state):\n    raise RuntimeError('site moved')", interval_seconds=5)
    con = db()
    supervisor.tick(con)
    w = con.execute("select * from workers where name='broken'").fetchone()
    assert w["fails"] == 1 and "site moved" in w["last_error"] and w["next_run"] - w["last_run"] >= 60
    assert "site moved" in server.workers()


def test_pause_stops_everything():
    server.add_worker("counter", "x", COUNTER, interval_seconds=5)
    server.pause(True)
    try:
        supervisor.tick(db())
        assert db().execute("select runs from workers").fetchone()[0] == 0
    finally:
        server.pause(False)


def test_note_text_reaches_claude_only_through_stdin(monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen["args"], seen["input"] = args, kw.get("input")
        return type("P", (), {"returncode": 0, "stdout": "done", "stderr": ""})()

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)
    monkeypatch.setattr(supervisor, "db", lambda: type("C", (), {"execute": lambda *a: None, "close": lambda s: None})())
    evil = r'x" & del /q C:\*  && echo pwned'
    REAL_CLAUDE(1, f"<<<NOTE\n{evil}\nNOTE>>>", DEFAULTS)
    assert evil not in " ".join(seen["args"]) and evil in seen["input"]
    assert seen["args"][1:4] == ["-p", "--permission-mode", "dontAsk"]


def test_workers_cannot_import_action_modules():
    server.add_worker("acts", "tries to click", "import pyautogui\ndef check(state):\n    return 'x'")
    out = server.test_worker("acts")
    assert "FAILED" in out and "observe only" in out


def test_parallel_checks_do_not_restart_an_in_flight_worker(monkeypatch):
    server.add_worker("counter", "x", COUNTER, interval_seconds=5)
    seen = []
    monkeypatch.setattr(supervisor, "run_check", lambda w: seen.append(w["name"]))
    supervisor._running.add("counter")  # pretend its check is already running
    supervisor.tick(db())
    assert seen == []  # not started again
    supervisor._running.discard("counter")


# --- manager mode ---
import manager  # noqa: E402


def _mk_task(**kw):
    base = dict(id=1, mission_id=1, title="t", detail="", urgency=2, deadline=None, needs_you=0,
                optional=0, depends_on="", status="todo", result=None)
    base.update(kw)
    return base


def test_next_task_orders_urgent_and_deadline_first_holds_needs_you_and_optional():
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes) values(1,'g',0,9e12,40)")
    rows = [
        ("plain", 2, None, 0, 0), ("urgent", 4, None, 0, 0), ("soon", 2, 1000.0, 0, 0),
        ("mine", 3, None, 1, 0), ("maybe", 3, None, 0, 1),
    ]
    for title, urg, dl, you, opt in rows:
        con.execute("insert into tasks(mission_id, title, urgency, deadline, needs_you, optional, created) "
                    "values(1,?,?,?,?,?,0)", (title, urg, dl, you, opt))
    picked = []
    while (t := manager.next_task(con, 1)):
        picked.append(t["title"])
        con.execute("update tasks set status='done' where id=?", (t["id"],))
    assert picked == ["soon", "urgent", "plain"]  # deadline, then urgency; needs_you and optional held back
    assert manager.next_task(con, 1, allow_optional=True)["title"] == "maybe"  # optional only when asked


def test_deps_block_until_done():
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes) values(1,'g',0,9e12,40)")
    con.execute("insert into tasks(id, mission_id, title, created) values(10,1,'first',0)")
    con.execute("insert into tasks(id, mission_id, title, depends_on, created) values(11,1,'second','10',0)")
    assert manager.next_task(con, 1)["id"] == 10
    con.execute("update tasks set status='done' where id=10")
    assert manager.next_task(con, 1)["id"] == 11


def test_budget_and_quiet_hours():
    assert manager.budget_left({"budget_wakes": 5, "wakes_used": 3, "run_until": 9e12})
    assert not manager.budget_left({"budget_wakes": 5, "wakes_used": 5, "run_until": 9e12})
    assert not manager.budget_left({"budget_wakes": 0, "wakes_used": 0, "run_until": 1.0})
    assert manager.in_quiet_hours({"quiet_hours": [0, 24]})  # all day
    assert not manager.in_quiet_hours({"quiet_hours": []})


def test_mission_plans_then_works_and_finalizes(monkeypatch):
    import supervisor
    import server as srv
    con = db()
    srv.start_mission("do the thing", run_hours=3, budget_wakes=40, permissions="reply in chat")

    def fake_claude(wake_id, prompt, cfg):
        if "Break this goal into concrete tasks" in prompt:
            srv.add_task(1, "urgent one", "do it", urgency=4)
            srv.add_task(1, "ask the user", "needs them", needs_you=True)
            srv.plan_done(1)
        else:  # working the one auto-pickable task (the urgent one, id 1)
            srv.finish_task(1, "did it")

    monkeypatch.setattr(supervisor, "_claude", fake_claude)
    monkeypatch.setattr(supervisor, "_background", lambda fn, *a: fn(*a))
    for _ in range(5):
        supervisor.manage(con)
    m = con.execute("select * from missions where id=1").fetchone()
    assert m["status"] == "waiting"  # the needs_you task is left for the user
    done = con.execute("select result from tasks where status='done'").fetchall()
    assert len(done) == 1 and done[0]["result"] == "did it"
    assert m["wakes_used"] >= 2  # one plan wake + one work wake


def test_needs_you_task_raises_checkin_then_approve_or_skip():
    import supervisor
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes, status) values(1,'g',0,9e12,40,'running')")
    con.execute("insert into tasks(id, mission_id, title, detail, needs_you, created) values(5,1,'post','do it',1,0)")
    supervisor._ask_about_needs_you(con, con.execute("select * from missions where id=1").fetchone())
    c = con.execute("select * from checkins where task_id=5").fetchone()
    assert c and c["answer"] is None
    # asking again doesn't duplicate
    supervisor._ask_about_needs_you(con, con.execute("select * from missions where id=1").fetchone())
    assert con.execute("select count(*) from checkins where task_id=5").fetchone()[0] == 1
    # approve -> needs_you cleared so it becomes workable
    con.execute("update checkins set answer='approve' where id=?", (c["id"],))
    supervisor._apply_answers(con, con.execute("select * from missions where id=1").fetchone())
    assert con.execute("select needs_you from tasks where id=5").fetchone()[0] == 0
    # a second task, skipped
    con.execute("insert into tasks(id, mission_id, title, needs_you, created) values(6,1,'post2',1,0)")
    supervisor._ask_about_needs_you(con, con.execute("select * from missions where id=1").fetchone())
    c2 = con.execute("select id from checkins where task_id=6").fetchone()[0]
    con.execute("update checkins set answer='skip' where id=?", (c2,))
    supervisor._apply_answers(con, con.execute("select * from missions where id=1").fetchone())
    assert con.execute("select status from tasks where id=6").fetchone()[0] == 'skipped'


def test_budget_checkin_extends_or_stops_and_does_not_duplicate():
    import supervisor
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes, wakes_used, status) "
                "values(1,'g',0,1.0,2,2,'running')")
    con.execute("insert into tasks(id, mission_id, title, created) values(5,1,'a',0)")
    m = con.execute("select * from missions where id=1").fetchone()
    supervisor._ask_extend(con, m)
    supervisor._ask_extend(con, con.execute("select * from missions where id=1").fetchone())
    assert con.execute("select count(*) from checkins where mission_id=1 and kind='budget'").fetchone()[0] == 1
    cid = con.execute("select id from checkins where kind='budget'").fetchone()[0]
    con.execute("update checkins set answer='1 hour' where id=?", (cid,))
    supervisor._apply_answers(con, con.execute("select * from missions where id=1").fetchone())
    m2 = con.execute("select budget_wakes, status from missions where id=1").fetchone()
    assert m2["budget_wakes"] == 22 and m2["status"] == "running"  # +20 wakes, resumed


def test_budget_checkin_stop_cancels():
    import supervisor
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes, wakes_used, status) "
                "values(1,'g',0,1.0,2,2,'running')")
    con.execute("insert into checkins(id, mission_id, kind, question, answer, ts) values(9,1,'budget','?','stop',0)")
    supervisor._apply_answers(con, con.execute("select * from missions where id=1").fetchone())
    assert con.execute("select status from missions where id=1").fetchone()[0] == "cancelled"


def test_reconcile_blocks_an_unconfirmed_task_and_never_fakes_done():
    import supervisor
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes) values(1,'g',0,9e12,40)")
    # exit 0 but Claude never called a tool: this must NOT be reported as done (the bug the old test enshrined).
    con.execute("insert into tasks(id, mission_id, title, status, created) values(5,1,'a','doing',0)")
    con.execute("insert into wakes(id, worker, ts, exit_code, output) values(30,'mission:1/task5',0,0,'talked about it')")
    supervisor._reconcile_task(30, 5)
    t = con.execute("select status, result from tasks where id=5").fetchone()
    assert t["status"] == "blocked" and "without calling" in t["result"]
    # a task Claude actually closed (finish_task set it done) is left exactly as-is
    con.execute("insert into tasks(id, mission_id, title, status, result, created) values(7,1,'c','done','real',0)")
    con.execute("insert into wakes(id, worker, ts, exit_code, output) values(32,'mission:1/task7',0,0,'x')")
    supervisor._reconcile_task(32, 7)
    assert con.execute("select status, result from tasks where id=7").fetchone()["result"] == "real"


def test_raise_checkin_parks_task_so_reconcile_cannot_close_it():
    import server as srv
    import supervisor
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes, status) values(1,'g',0,9e12,40,'running')")
    con.execute("insert into tasks(id, mission_id, title, status, created) values(5,1,'decide','doing',0)")
    srv.raise_checkin(1, "blue or red?", kind="answer", task_id=5)
    assert con.execute("select status from tasks where id=5").fetchone()[0] == "waiting"  # parked, not doing
    supervisor._reconcile_task(99, 5)  # a wake ending now must leave a parked task alone
    assert con.execute("select status from tasks where id=5").fetchone()[0] == "waiting"


def test_free_form_answer_resumes_task_and_carries_the_text_into_its_detail():
    import supervisor
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes, status) values(1,'g',0,9e12,40,'waiting')")
    con.execute("insert into tasks(id, mission_id, title, detail, status, created) values(5,1,'pick','base','waiting',0)")
    con.execute("insert into checkins(id, mission_id, task_id, kind, question, answer, ts) "
                "values(9,1,5,'answer','blue or red?','use the blue one',0)")
    supervisor._apply_answers(con, con.execute("select * from missions where id=1").fetchone())
    t = con.execute("select status, detail from tasks where id=5").fetchone()
    assert t["status"] == "todo" and "use the blue one" in t["detail"]  # answer reaches the next wake


def test_natural_language_approval_is_not_silently_dropped():
    import supervisor
    con = db()
    con.execute("insert into missions(id, goal, created, run_until, budget_wakes, status) values(1,'g',0,9e12,40,'waiting')")
    con.execute("insert into tasks(id, mission_id, title, needs_you, status, created) values(5,1,'post',1,'todo',0)")
    con.execute("insert into checkins(id, mission_id, task_id, kind, question, answer, ts) "
                "values(9,1,5,'approve','approve or skip?','sure, go for it',0)")
    supervisor._apply_answers(con, con.execute("select * from missions where id=1").fetchone())
    assert con.execute("select needs_you from tasks where id=5").fetchone()[0] == 0  # 'sure, go for it' = approve


def test_quiet_hours_defers_a_worker_wake_then_delivers_it(fresh, monkeypatch):
    con = db()
    server.add_worker("counter", "x", COUNTER, interval_seconds=5, reaction="say hi")
    monkeypatch.setattr(supervisor, "settings", lambda: {**DEFAULTS, "quiet_hours": [0, 24]})  # quiet all day
    for _ in range(2):  # baseline, then one new item during quiet hours
        con.execute("update workers set next_run=0")
        supervisor.tick(con)
    assert fresh == [] and con.execute("select woke from notes").fetchone()[0] == -2  # held, not dropped
    monkeypatch.setattr(supervisor, "settings", lambda: dict(DEFAULTS))  # quiet hours end
    supervisor._wake_deferred(con)
    assert len(fresh) == 1 and "item 1" in fresh[0]
    assert con.execute("select woke from notes").fetchone()[0] == 1  # delivered


def test_a_burst_in_one_check_is_one_wake_not_one_per_note(fresh):
    con = db()
    server.add_worker("grades", "when grades post, summarize", "def check(state):\n"
                      "    if state.get('seen'): return None\n    state['seen']=True\n"
                      "    return ['g1','g2','g3','g4','g5']", interval_seconds=5, reaction="summarize")
    con.execute("update workers set next_run=0")
    supervisor.tick(con)
    assert con.execute("select count(*) from notes").fetchone()[0] == 5  # all recorded
    assert len(fresh) == 1 and all(f"g{i}" in fresh[0] for i in range(1, 6))  # one wake carrying all five
