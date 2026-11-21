#!/usr/bin/env python3
"""Claude Code tokens of every session on this machine, per day.

Reads ~/.claude/projects/**/*.jsonl incrementally (only bytes appended since the last run) and keeps daily totals in a
small cache, so the orchestrator can ask every few minutes. Prints one JSON line:

    {"days": {"2026-11-21": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "msgs": 5}}, "partial": false}

`partial` is true while the first run is still working through old transcripts (it stops after a time budget and goes on
next time). Python 3.6+, standard library only. The orchestrator runs the same file for its own machine.
"""
import calendar
import glob
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
BASE = os.environ.get("SCALEOUT_NODE_HOME") or os.path.join(HOME, ".scaleout")
CACHE = os.environ.get("SCALEOUT_USAGE_CACHE") or os.path.join(BASE, "usage-days.json")
BUDGET = float(os.environ.get("SCALEOUT_USAGE_BUDGET") or 20)   # seconds one run may read
KEEP_DAYS = 400
KEYS = (("in", "input_tokens"), ("out", "output_tokens"), ("cache_read", "cache_read_input_tokens"),
        ("cache_write", "cache_creation_input_tokens"))


def transcripts():
    roots = [os.path.join(HOME, ".claude", "projects")]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        roots.append(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects"))
    seen = set()
    for root in roots:
        for pat in ("*/*.jsonl", "*/*/subagents/*.jsonl"):   # sessions and their subagents
            for f in glob.glob(os.path.join(root, pat)):
                real = os.path.realpath(f)   # account folders that share one projects folder count once
                if real not in seen:
                    seen.add(real)
                    yield real


def main():
    try:
        with open(CACHE) as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        c = {}
    offsets, days, tails = c.get("offsets", {}), c.get("days", {}), c.get("tails", {})
    start, partial, alive = time.time(), False, set()
    for f in sorted(transcripts(), key=lambda p: offsets.get(p, 0) > 0):   # never-read files first
        alive.add(f)
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        off = offsets.get(f, 0)
        if size < off:
            off = 0
        if size == off:
            continue
        if time.time() - start > BUDGET:
            partial = True
            continue
        seen = set(tails.get(f, []))   # a message is written once per content block, with the same id and usage
        order = list(tails.get(f, []))
        try:
            with open(f, "rb") as fh:
                fh.seek(off)
                data = fh.read()
        except OSError:
            continue
        end = data.rfind(b"\n") + 1   # a half-written last line waits for the next run
        for line in data[:end].splitlines():
            if b'"usage"' not in line:
                continue
            try:
                r = json.loads(line)
                m = r.get("message") or {}
                u = m.get("usage") or {}
                mid = m.get("id") or r.get("requestId") or ""
                if not u or (mid and mid in seen):
                    continue
                ts = r.get("timestamp", "")
                t = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) if ts else time.time()
                day = time.strftime("%Y-%m-%d", time.localtime(t))
                b = days.setdefault(day, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "msgs": 0})
                for k, src in KEYS:
                    b[k] += int(u.get(src) or 0)
                b["msgs"] += 1
            except (ValueError, TypeError, OverflowError, AttributeError):
                continue
            if mid:
                seen.add(mid)
                order.append(mid)
        offsets[f] = off + end
        tails[f] = order[-40:]
    oldest = time.strftime("%Y-%m-%d", time.localtime(time.time() - KEEP_DAYS * 86400))
    days = {d: v for d, v in days.items() if d >= oldest}
    if not partial:   # transcripts Claude Code has deleted: their totals stay, their bookkeeping goes
        offsets = {f: o for f, o in offsets.items() if f in alive}
    tails = {f: v for f, v in tails.items() if f in alive and os.path.exists(f) and time.time() - os.path.getmtime(f) < 2 * 86400}
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump({"offsets": offsets, "days": days, "tails": tails}, fh)
        os.replace(tmp, CACHE)
    except OSError:
        pass
    sys.stdout.write(json.dumps({"days": days, "partial": partial}) + "\n")


if __name__ == "__main__":
    main()
