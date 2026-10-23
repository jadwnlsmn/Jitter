<p align="center"><img src="assets/queue.png" width="96" alt="Jitter: Queue"></p>

# Jitter: Queue

Lets Claude act without you. You give a stimulus ("tell me when Sam replies", "when grades post, summarize
them"), Claude writes a **worker** for it, and the always-on **supervisor** runs that worker on an interval. When
the worker notices something it records a **note** and, if you approved a reaction, wakes Claude headless to carry
it out.

Part of the Jitter family (Screen, Queue, Agentic). Works alone; Claude is the only bridge between them.

## How it fits together

| Part | Role |
|---|---|
| `server.py` | MCP server `jitter-queue`: Claude's tools to add, test, list, change and remove workers, read notes and wakes, pause |
| `supervisor.py` | Always-on (starts with Windows): runs due workers, stores notes, wakes Claude |
| `run_worker.py` | Runs one check of one worker in its own process |
| `helpers.py` | Optional for workers: `fetch(url)`, `window_text(title)` (background read), `new_items(state, key, items)` |
| `workers/`, `data/` | Your workers and their notes, memory and wake log (not in git) |

A worker is a Python file defining `check(state)` that returns a note, a list of notes, or `None`. `state` persists
between checks so it reports only what's new. Workers only observe; acting is the reaction's job.

## Safety — read this honestly

A worker is Python that Claude wrote, run **as you, headless, on an interval, forever**. The import guard in
`run_worker.py` blocks the obvious GUI-automation libraries so an honest "watch" worker that was mistakenly
written to "act" fails in `test_worker()` instead of at 3am. It is **a guard, not a sandbox**: `os`, `subprocess`,
`ctypes` and outbound `urllib` are all reachable, so treat a worker (and the note text it produces) as able to do
anything you can. Review a worker's reaction before you approve it, and keep `permissions` tight on missions.

What the mechanism actually gives you:

- A woken Claude runs with `--permission-mode dontAsk` and only the whole tool-servers in `wake_tools` (default:
  Jitter Screen and Queue). This refuses *other* servers, but `mcp__jitter` is full control of the mouse,
  keyboard and screen — so this bounds *which* integrations a wake can touch, not how much it can do on the
  desktop. It is coarse on purpose; it is not a fine-grained permission.
- Note text is passed as data on stdin (never the command line, so it can't become a shell command) inside a
  random per-wake fence, and the prompt tells Claude it is data. That defeats a shell-injection and a
  fence-forging note; it is **not** a guarantee against prompt-injection of an LLM. Assume a hostile note can try
  to steer the wake, and set reactions you'd trust with that.
- At most `max_wakes_per_hour` wakes per worker (default 6); a burst in one check is batched into a single wake so
  it isn't lost to that cap. `quiet_hours` holds screen wakes overnight (and delivers held notes after). `pause()`
  stops everything.

Settings live in `settings.json`: `paused`, `wake_tools`, `max_wakes_per_hour`, `wake_timeout_s`,
`quiet_hours` (e.g. `[23, 7]`), `always_ask` (phrases the mission planner must hand back to you, never do alone).

## Setup

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt
claude mcp add --scope user jitter-queue -- <path>\.venv\Scripts\python.exe <path>\server.py
```

Put a shortcut to `.venv\Scripts\pythonw.exe supervisor.py` in the Startup folder. Headless wakes need the Claude
Code command line to be logged in (`claude auth status`); for unattended use, `claude setup-token`.

Tests: `.venv\Scripts\python -m pytest`
