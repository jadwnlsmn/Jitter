"""Jitter: Queue core: the store shared by the MCP server (Claude's side) and the supervisor (the always-on side)."""
import json
import os
import re
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).parent  # the code
HOME = Path(os.environ.get("JITTER_QUEUE_HOME", HERE))  # the user's workers and data (tests point this elsewhere)
DATA, WORKERS = HOME / "data", HOME / "workers"
STATE, DB, SETTINGS = DATA / "state", DATA / "queue.db", HOME / "settings.json"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
DEFAULTS = {
    "paused": False,  # stops all workers and wakes
    "wake_tools": ["mcp__jitter", "mcp__jitter-queue"],  # all a woken Claude may use: Screen and Queue
    "max_wakes_per_hour": 6,  # per worker, so a chatty source can't run up usage
    "wake_timeout_s": 900,
    "quiet_hours": [],          # e.g. [23, 7]: no screen wakes between 11pm and 7am (the user may be asleep/working)
    "always_ask": ["send money", "pay", "purchase", "delete", "submit graded", "post publicly", "sign up"],
}
SCHEMA = """
create table if not exists workers(
  name text primary key, stimulus text, reaction text, interval_s real, enabled int default 1, wake int default 1,
  created real, next_run real default 0, last_run real, last_error text, runs int default 0, fails int default 0);
create table if not exists notes(
  id integer primary key, worker text, ts real, text text, read int default 0, woke int default 0);
create table if not exists wakes(
  id integer primary key, worker text, note_id int, ts real, exit_code int, output text);
create table if not exists meta(key text primary key, value text);
-- manager mode: a mission is a goal + a time/budget window; Claude plans it into tasks and works them in order.
create table if not exists missions(
  id integer primary key, goal text, created real, run_until real, budget_wakes int,
  status text default 'planning',           -- planning -> running -> waiting/paused -> done/cancelled
  permissions text default '{}', summary text, wakes_used int default 0);
create table if not exists tasks(
  id integer primary key, mission_id int, title text, detail text,
  urgency int default 2,                     -- 1 low, 2 normal, 3 high, 4 urgent (deadline/time-sensitive)
  deadline real, needs_you int default 0, optional int default 0, depends_on text default '',
  status text default 'todo',                -- todo -> doing -> done/blocked/skipped
  result text, created real, done_at real);
create table if not exists checkins(
  id integer primary key, mission_id int, task_id int, kind text, question text,
  options text default '[]', answer text, ts real, answered_ts real, applied int default 0);
"""


def _migrate(con) -> None:
    cols = {r[1] for r in con.execute("pragma table_info(checkins)")}
    if "applied" not in cols:
        con.execute("alter table checkins add column applied int default 0")


def db() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    WORKERS.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=15, isolation_level=None)  # autocommit; the server and supervisor share it
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ValueError(f"worker names are lowercase letters, digits and dashes, like 'messenger-study-group', not {name!r}")
    return name


def ago(ts) -> str:
    if not ts:
        return "never"
    s = time.time() - ts
    return f"{s:.0f}s ago" if s < 120 else f"{s / 60:.0f}m ago" if s < 7200 else f"{s / 3600:.1f}h ago"
