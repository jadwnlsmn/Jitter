"""Optional helpers for workers: the common ways of watching something. Workers can use these, or anything else.

    from helpers import fetch, window_text, new_items
"""
import json
import os
import re
import socket
import time
import urllib.request
from pathlib import Path


def fetch(url: str, timeout: float = 20, as_json: bool = False):
    """GET a URL; the body as text, or parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Jitter-Queue"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    return json.loads(body) if as_json else body


def window_text(title: str) -> list[str] | None:
    """The text of a window whose title contains `title`, read in the background without focusing it (browsers
    expose only the tab showing in each window). One string per named element, in reading order. None if no such
    window is open."""
    import uiautomation as uia  # local, so a network-only worker needn't load the GUI automation stack
    from uiautomation.uiautomation import _AutomationClient
    ua = _AutomationClient.instance().IUIAutomation
    for w in uia.GetRootControl().GetChildren():
        if title.lower() in (w.Name or "").lower():
            cache = ua.CreateCacheRequest()
            cache.AddProperty(30005)  # Name
            named = ua.CreateNotCondition(ua.CreatePropertyCondition(30005, ""))
            arr = w.Element.FindAllBuildCache(7, named, cache)
            return [arr.GetElement(i).CachedName for i in range(arr.Length)]
    return None


def new_items(state: dict, key: str, items: list[str], keep: int = 500) -> list[str]:
    """The items not seen on earlier checks. The first check only records a baseline and returns nothing, so a
    worker reports what changes after it starts, not everything already there."""
    seen = state.get(key)
    state[key] = (list(seen or []) + [i for i in items if i not in (seen or [])])[-keep:]
    return [] if seen is None else [i for i in items if i not in seen]


# The palette. Every stimulus is three choices: a SOURCE to look at, an EXTRACT to pull the piece you care about,
# and a CONDITION that decides when to fire. Mix and match; a worker is usually one of each. See templates/worker.py.

# --- SOURCE: where to look (fetch and window_text, above, are sources too) ---

def read_file(path, encoding="utf-8"):
    """A text file's contents, or None if it isn't there."""
    try:
        return Path(path).read_text(encoding, "replace")
    except OSError:
        return None


def dir_list(path, pattern="*"):
    """Names in a folder (optionally glob-filtered), sorted. [] if the folder is gone."""
    try:
        return sorted(p.name for p in Path(path).glob(pattern))
    except OSError:
        return []


def ping(url_or_host, timeout=10):
    """True if an http(s) URL responds, or a 'host:port' accepts a TCP connection (port defaults to 80)."""
    if "://" in url_or_host:
        try:
            urllib.request.urlopen(
                urllib.request.Request(url_or_host, headers={"User-Agent": "Jitter-Queue"}), timeout=timeout)
            return True
        except Exception:
            return False
    host, _, port = url_or_host.partition(":")
    try:
        socket.create_connection((host, int(port or 80)), timeout).close()
        return True
    except OSError:
        return False


# --- EXTRACT: what to pull out (or just use the raw text) ---

def match(text, pattern):
    """The first regex match (its first group if it has one), or None."""
    m = re.search(pattern, text or "")
    return (m.group(1) if m.groups() else m.group(0)) if m else None


def extract(text, pattern):
    """Every regex match, in order."""
    return re.findall(pattern, text or "")


def json_path(obj, path):
    """Walk a dotted path into parsed JSON ('a.b.0.c'); None if any step is missing."""
    cur = obj
    for key in path.split("."):
        try:
            cur = cur[int(key)] if isinstance(cur, list) else cur[key]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return cur


def count(items, pattern=None):
    """How many: regex matches when `items` is text and a pattern is given, else the length of the list/text."""
    if pattern is not None and isinstance(items, str):
        return len(re.findall(pattern, items))
    return len(items or [])  # None -> 0, rather than throwing


# --- CONDITION: what makes the worker fire (these remember across checks via `state`) ---

def changed(state, key, value):
    """True when `value` differs from the last check. First check records a baseline and returns False."""
    had, old = key in state, state.get(key)
    state[key] = value
    return had and value != old


def matched(text, pattern):
    """True if the pattern appears anywhere in the text."""
    return re.search(pattern, text or "") is not None


def crossed(state, key, value, threshold, over=True):
    """Fire once when `value` crosses `threshold` (over=True: rises above; False: falls below). Only on the
    transition, not on every check while it stays past the line."""
    now = value > threshold if over else value < threshold
    was = state.get(key, False)
    state[key] = now
    return now and not was


def every(state, key, seconds):
    """True at most once per `seconds` — a rate gate for a noisy source."""
    now = time.time()
    if now - state.get(key, 0) >= seconds:
        state[key] = now
        return True
    return False


def at_time(state, key, hhmm):
    """True on the first check at or after HH:MM (24h) each day, once per day. Accepts '9:00' or '09:00'."""
    h, _, m = hhmm.partition(":")
    target = int(h) * 60 + int(m or 0)                      # minutes past midnight, so '9:00' and '09:00' are equal
    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    if now.tm_hour * 60 + now.tm_min >= target and state.get(key) != today:
        state[key] = today
        return True
    return False


if __name__ == "__main__":  # smallest checks that fail if a block breaks: `python helpers.py`
    assert json_path({"a": [{"b": 5}]}, "a.0.b") == 5 and json_path({"a": 1}, "a.x") is None
    assert match("price 42 usd", r"(\d+)") == "42" and count("a1b2c3", r"\d") == 3 and count(["a", "b"]) == 2
    assert count(None) == 0 and matched("abc", "b") and not matched("abc", "z")
    assert extract("a1b2c", r"\d") == ["1", "2"]
    st = {}
    assert new_items(st, "k", ["a"]) == [] and new_items(st, "k", ["a", "b"]) == ["b"]  # baseline quiet, then new
    s = {}
    assert at_time(s, "t", "00:00") is True and at_time(s, "t", "00:00") is False  # once per day, zero-pad agnostic
    s = {}
    assert changed(s, "v", 1) is False and changed(s, "v", 2) is True and changed(s, "v", 2) is False
    s = {}
    assert crossed(s, "p", 70, 60) is True and crossed(s, "p", 71, 60) is False
    s = {}
    assert every(s, "g", 999) is True and every(s, "g", 999) is False
    print("blocks ok")
