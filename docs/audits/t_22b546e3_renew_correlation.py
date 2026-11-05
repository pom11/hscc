#!/usr/bin/env python3
"""Chunked RENEW-mark extraction + burst correlation (independent verification)."""
import glob
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

PROFILES = os.path.expanduser("~/.hermes/profiles")
STALE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[^\n]*Stream stale for (\d+)")
TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+")


def parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def renew_marks(day):
    out, prev = [], None
    cur = parse(day + " 00:00:00")
    stop = parse(day + " 23:59:59")
    while cur < stop:
        nxt = min(cur + timedelta(hours=3), stop)
        r = subprocess.run(
            ["log", "show", "--style", "compact", "--start",
             cur.strftime("%Y-%m-%d %H:%M:%S"), "--end", nxt.strftime("%Y-%m-%d %H:%M:%S"),
             "--predicate", 'process == "configd" AND eventMessage CONTAINS "DHCP en0: RENEW"'],
            capture_output=True, text=True, timeout=900)
        for line in r.stdout.splitlines():
            m = TS.match(line)
            if not m:
                continue
            t = parse(m.group(1))
            if prev and (t - prev) <= timedelta(seconds=60):
                continue
            out.append(t)
            prev = t
        cur = nxt
    return sorted(set(out))


def kills(day):
    hits = []
    for f in sorted(glob.glob(os.path.join(PROFILES, "*", "logs", "agent.log"))):
        prof = f.split(os.sep)[-3]
        try:
            fh = open(f, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = STALE.search(line)
                if m and m.group(1) == day:
                    hits.append((parse(f"{m.group(1)} {m.group(2)}"), prof, int(m.group(3))))
    hits.sort()
    return hits


def bursts(hits, gap=15):
    out, cur = [], []
    for h in hits:
        if cur and (h[0] - cur[-1][0]) > timedelta(seconds=gap):
            out.append(cur)
            cur = []
        cur.append(h)
    if cur:
        out.append(cur)
    return out


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-11-05"
    marks = renew_marks(day)
    print(f"RENEW marks: {len(marks)}")
    gaps = [(b - a).total_seconds() for a, b in zip(marks, marks[1:])]
    if gaps:
        g = sorted(gaps)
        print(f"  interval min/median/max = {g[0]:.0f}/{g[len(g)//2]:.0f}/{g[-1]:.0f} s")
    print("  " + ", ".join(t.strftime("%H:%M:%S") for t in marks))

    hits = kills(day)
    print(f"\nstale kills {day}: {len(hits)} across {len({h[1] for h in hits})} profiles")
    bs = bursts(hits)
    print(f"bursts: {len(bs)}\n")
    print(f"{'burst':<13}{'n':>3} {'prof':>5}  onset(-900s)  nearest mark  delta   verdict")
    cor = unc = 0
    for b in bs:
        n, profs = len(b), len({x[1] for x in b})
        th = b[0][2] or 900
        onset = b[0][0] - timedelta(seconds=th)
        near = min(marks, key=lambda m: abs((m - onset).total_seconds()))
        d = (near - onset).total_seconds()
        ok = abs(d) <= 60 and n >= 2
        cor += ok
        unc += (not ok and n >= 2)
        print(f"{b[0][0].strftime('%H:%M:%S'):<13}{n:>3} {profs:>5}  "
              f"{onset.strftime('%H:%M:%S')}     {near.strftime('%H:%M:%S')}    {d:+6.0f}s  "
              f"{'CORR' if ok else ('uncorrelated' if n >= 2 else '-')}", flush=True)
    multi = [b for b in bs if len(b) >= 2]
    print(f"\nbursts>=2 kills: {len(multi)}  correlated: {cor}  UNCORRELATED: {unc}")


if __name__ == "__main__":
    main()
