import re
log = "/Users/desac/.hscc/daemon.log"
lines = open(log).read().splitlines()

def ts(l):
    m = re.search(r"\[(\d{4}-\d{2}-\d{2}T[\d:.]+)", l)
    return m.group(1) if m else None

def secs(a, b):
    # approximate seconds between two timestamps
    from datetime import datetime
    def p(x): return datetime.fromisoformat(x)
    try:
        return (p(b)-p(a)).total_seconds()
    except Exception:
        return None

sig = [i for i,l in enumerate(lines) if "Received signal 15" in l]
print(f"{'#':>2} {'signal15 time (UTC)':<24} {'stop_req?':<9} {'restart_gap':<12} {'next_restart_line'}")

starts = [i for i,l in enumerate(lines) if "Daemon starting" in l or "start-daemon invoked" in l]
stopreq = [i for i,l in enumerate(lines) if "Daemon stop requested" in l]

import bisect
for n,i in enumerate(sig):
    t = ts(lines[i])
    # preceding stop-request within 5s
    sr = [j for j in stopreq if 0 <= i-j <= 40]
    has_sr = "YES" if sr else "no"
    sr_line = lines[sr[0]] if sr else ""
    # next restart after this signal
    nxt = bisect.bisect_right(starts, i)
    rgap = ""
    rline = ""
    if nxt < len(starts):
        rline = lines[starts[nxt]]
        rt = ts(rline)
        rgap = f"{secs(t,rt):.0f}s" if rt else ""
    print(f"{n+1:>2} {t:<24} {has_sr:<9} {rgap:<12} {rline}")
    if has_sr=="YES":
        print(f"      stop-request line: {sr_line}")
