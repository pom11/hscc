#!/usr/bin/env python3
"""Cross-profile stream forensics 2026-11-05 09:30-11:10: stale kills + stream drops."""
import glob, re, os

profiles = glob.glob(os.path.expanduser("~/.hermes/profiles/*/logs/agent.log"))
rows = []
for p in profiles:
    prof = p.split(os.sep)[-3]
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if not ln.startswith("2026-11-05 "):
                    continue
                t = ln[11:19]
                if not ("11:10" <= t <= "14:00"):
                    continue
                if "Stream stale for" in ln:
                    m = re.search(r"Stream stale for (\d+)s.*?model=(\S+) context=~([\d,]+)", ln)
                    rows.append((t, prof, "STALE", ln.split("] ")[-1][:110]))
                elif "Stream drop on attempt" in ln:
                    m = re.search(r"elapsed=([\d.]+)s ttfb=([\d.]+)s.*?bytes=(\d+) chunks=(\d+)", ln)
                    if m:
                        rows.append((t, prof, "DROP", "elapsed=%s ttfb=%s bytes=%s chunks=%s" % (m.group(1), m.group(2), m.group(4), m.group(3))))
                    else:
                        rows.append((t, prof, "DROP", ln.split("] ")[-1][:110]))
    except OSError:
        pass

rows.sort()
for t, prof, kind, det in rows:
    print("%s %-22s %s  %s" % (t, prof, kind, det))
print("total events:", len(rows))
