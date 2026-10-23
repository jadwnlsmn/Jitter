"""Jitter: Queue watchdog: keeps the supervisor running. If it crashes, restart it (backing off so a supervisor
that dies on startup doesn't spin). This is what the Startup shortcut launches, not the supervisor directly.
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
PYTHON = sys.executable.replace("python.exe", "pythonw.exe")
NO_WINDOW = 0x08000000


def main() -> None:
    # one watchdog at a time (survives a second Startup fire or a manual launch)
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JitterQueueWatchdog")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    backoff = 2
    while True:
        started = time.time()
        p = subprocess.Popen([PYTHON, str(HERE / "supervisor.py")], cwd=HERE, creationflags=NO_WINDOW)
        p.wait()
        # ran a while before dying: a transient crash, restart promptly. Died fast: back off up to 5 min.
        backoff = 2 if time.time() - started > 120 else min(backoff * 2, 300)
        time.sleep(backoff)


if __name__ == "__main__":
    main()
