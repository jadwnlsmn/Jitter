"""The shape every stimulus-watcher takes. Copy it, fill the three blanks, pass the finished file to add_worker().
A worker only OBSERVES: it returns a short note when something new happens, or None when nothing has.

Every stimulus is three choices from the palette in helpers.py:
  SOURCE    where to look   -> fetch / window_text / read_file / dir_list / ping / (anything Python can read)
  EXTRACT   what to pull    -> match / extract / json_path / count / (or just use the raw text)
  CONDITION what fires it   -> new_items / changed / matched / crossed / every / at_time

The first check only records a baseline and reports nothing (new_items/changed do this on purpose) — that is the
worker working, not a broken one. Don't "fix" a quiet first run.

Worked example (fires when a grade shows up in a Canvas tab):

    from helpers import window_text, new_items
    def check(state):
        text = "\\n".join(window_text("Canvas") or [])                  # SOURCE
        hits = [l for l in text.splitlines() if "grade" in l.lower()]   # EXTRACT
        new = new_items(state, "grades", hits)                          # CONDITION
        if new:
            return new
        return None
"""
from helpers import (fetch, window_text, read_file, dir_list, ping,
                     match, extract, json_path, count,
                     new_items, changed, matched, crossed, every, at_time)


def check(state):
    raise NotImplementedError("fill in SOURCE / EXTRACT / CONDITION below, then delete this line")
    source = ...       # SOURCE: read where the stimulus lives
    signal = ...       # EXTRACT: pull out the piece you care about
    if ...:            # CONDITION: did it appear / change / cross?
        return "..."   # a short, self-contained note that becomes Claude's prompt
    return None
