"""Run one check of one worker: load its state, call check(state), save the state, print its notes as JSON.

One process per check, so a crashing or hanging worker can't take the supervisor down.
"""
import importlib.util
import json
import sys

from queue_core import STATE, WORKERS, check_name

# Workers OBSERVE; acting is the reaction's job. Block the modules that move the mouse/keyboard or drive Jitter,
# so a worker that tries to act fails loudly in test_worker() instead of fighting the user for the screen.
# ponytail: a guard, not a sandbox, and it does not pretend to be one. A worker is LLM-written Python running as
# the user; os/subprocess/ctypes/urllib are all reachable, so assume a hostile worker can do anything the user
# can. This only catches the honest mistake (a worker written to click instead of watch). The audit hook (vs a
# __import__ wrapper) is so importlib.import_module and __import__ are both covered, not just the builtin.
BLOCKED_IMPORTS = ("pyautogui", "pynput", "pydirectinput", "keyboard", "mouse", "server")


def _guard_imports() -> None:
    def hook(event, args):
        if event == "import" and args and str(args[0]).split(".")[0] in BLOCKED_IMPORTS:
            raise ImportError(f"workers may not import {args[0]!r}: they observe only, they never act. "
                              "Put any action in the worker's reaction, which a woken Claude carries out.")
    sys.addaudithook(hook)


def main(name: str) -> None:
    _guard_imports()
    spec = importlib.util.spec_from_file_location(f"worker_{name.replace('-', '_')}", WORKERS / f"{name}.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    state_file = STATE / f"{name}.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    out = worker.check(state)
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    notes = [] if out is None else [out] if isinstance(out, str) else list(out)
    print(json.dumps([str(n) for n in notes if str(n).strip()], ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(check_name(sys.argv[1]))
