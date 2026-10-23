"""Optional helpers for workers: the common ways of watching something. Workers can use these, or anything else.

    from helpers import fetch, window_text, new_items
"""
import json
import urllib.request

import uiautomation as uia
from uiautomation.uiautomation import _AutomationClient


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
