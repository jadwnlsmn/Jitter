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

## Safety

- A woken Claude runs with `--permission-mode dontAsk` and only the tools in `wake_tools` (default: Jitter Screen
  and Queue). Anything else is refused, not prompted.
- Notes are passed as data, on stdin, never on the command line, and the prompt tells Claude not to follow
  instructions inside them.
- At most `max_wakes_per_hour` wakes per worker (default 6). `pause()` stops everything.
- Settings live in `settings.json`: `paused`, `wake_tools`, `max_wakes_per_hour`, `wake_timeout_s`.

## Setup

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt
claude mcp add --scope user jitter-queue -- <path>\.venv\Scripts\python.exe <path>\server.py
```

Put a shortcut to `.venv\Scripts\pythonw.exe supervisor.py` in the Startup folder. Headless wakes need the Claude
Code command line to be logged in (`claude auth status`); for unattended use, `claude setup-token`.

Tests: `.venv\Scripts\python -m pytest`
