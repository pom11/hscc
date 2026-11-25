#!/bin/sh
# probe_stalled.sh — read a stalled load_burners.sh harness's signal state
# WITHOUT a debugger (t_44a09412).
#
# Why this shape. The card asked for one lldb attach reading the blocked mask
# and the pending set. Both are unreachable on this host, measured:
#   * lldb cannot attach at all (SIP on, Developer mode disabled, no sudo), and
#     /bin/sh is a harden platform binary anyway;
#   * sysctl KERN_PROC_PID's p_sigmask and p_siglist read ZERO against a child
#     that provably blocked SIGINT and had one sent (sigprobe --selftest /
#     --selftest-pending) — those two fields are deprecated and unpopulated on
#     this XNU. p_sigignore/p_sigcatch ARE populated, so dispositions are
#     readable and are reported here.
#
# So the blocked mask is read BEHAVIOURALLY, which is what actually answers the
# card's binary question. bash 3.2's own `block_ints()` blocks SIGINT, SIGQUIT,
# SIGTSTP, SIGTTIN, SIGTTOU — and NOT SIGTERM/SIGHUP. That set is exactly the
# observed asymmetry (INT lost, TERM and HUP healthy). Therefore:
#
#   TSTP stops the stalled harness   -> kernel mask is clear of the int set
#                                        -> SIGINT was DELIVERED and is stuck
#                                        inside bash  (verdict B)
#   TSTP does not stop it            -> the int set is blocked in the kernel
#                                        mask, so SIGINT was never delivered
#                                        (verdict A)
#   QUIT after that is the confirming read: QUIT is in the same blocked set and
#   has a default-terminate disposition, so a blocked QUIT (harness survives)
#   corroborates A and a delivered QUIT (harness dies) corroborates B.
#   A second SIGINT is sent before QUIT: if the trap fires late, the loss was
#   transient rather than a permanently blocked mask.
#
# usage: probe_stalled.sh <trace-file> <out-log> [max-hits] [min-age-s]
# Runs until max-hits stalled harnesses have been probed (default 1), then exits.
set -u
TRACE=${1:?trace file}
OUT=${2:?output log}
MAXHITS=${3:-1}
MINAGE=${4:-4}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SIGPROBE="$HERE/sigprobe"
SAMPLE=/usr/bin/sample          # PATH `sample` is a conda shim on this host

log() { printf '%s %s\n' "$(date +%s.%N)" "$*" >>"$OUT"; }
alive() { kill -0 "$1" 2>/dev/null; }

stat_of() { ps -o stat= -p "$1" 2>/dev/null | tr -d ' '; }

[ -x "$SIGPROBE" ] || cc -O1 -o "$SIGPROBE" "$HERE/sigprobe.c" 2>>"$OUT"

log "probe_stalled start trace=$TRACE maxhits=$MAXHITS minage=$MINAGE"
hits=0
seen=""
first=""

while [ "$hits" -lt "$MAXHITS" ]; do
    for pid in $(pgrep -f "load_burners.sh" 2>/dev/null); do
        alive "$pid" || continue
        case " $seen " in *" $pid "*) continue ;; esac
        now=$(date +%s)
        born=$(ps -o lstart= -p "$pid" 2>/dev/null)
        # etime has 1 s granularity and no sub-second; use it, MINAGE is 4 s.
        et=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
        secs=$(awk -v e="$et" 'BEGIN{n=split(e,a,":");s=a[n]+60*a[n-1];
            if(n==3)s=s+3600*a[n-2]; if(n==4)s=s+86400*a[n-3]; print s}')
        [ "${secs:-0}" -lt "$MINAGE" ] && continue
        # A stalled harness is a signal-arm harness (tag hscc-load-test-*) whose
        # last stage is in-wait with no int-trap; hscc-loadstress-guard-* pids
        # stop at in-wait BY DESIGN (that guard SIGKILLs them). Trace lines are
        # "<pid> <tag> <stage> [detail]", so the pid is anchored at ^.
        grep -q "^$pid hscc-load-test-" "$TRACE" 2>/dev/null || continue
        grep -q "^$pid .* int-trap" "$TRACE" 2>/dev/null && continue
        seen="$seen $pid"
        hits=$((hits + 1))
        log "STALL pid=$pid etime=$et stat=$(stat_of "$pid") first_seen_delta=${first:-na}"
        log "--- sysctl readout pid=$pid (x3, over ~1s) ---"
        "$SIGPROBE" --sample "$pid" 400 3 >>"$OUT" 2>&1
        log "--- TSTP probe pid=$pid (in bash block_ints set; default=stop) ---"
        kill -TSTP "$pid" 2>>"$OUT"
        i=0
        while [ "$i" -lt 12 ]; do
            st=$(stat_of "$pid")
            case "$st" in T*) log "TSTP DELIVERED pid=$pid stat=$st"; break ;; esac
            i=$((i + 1)); sleep 0.1
        done
        [ "$i" -ge 12 ] && log "TSTP NOT DELIVERED pid=$pid stat=$(stat_of "$pid") (blocked?)"
        kill -CONT "$pid" 2>/dev/null
        log "CONT sent pid=$pid stat=$(stat_of "$pid")"
        log "--- second SIGINT pid=$pid (late delivery test) ---"
        kill -INT "$pid" 2>>"$OUT"
        sleep 1.5
        if alive "$pid"; then
            log "SECOND SIGINT NOT DELIVERED pid=$pid stat=$(stat_of "$pid")"
        else
            log "SECOND SIGINT DELIVERED-LATE pid=$pid (trap ran, harness exited)"
        fi
        log "--- QUIT probe pid=$pid (same blocked set; default=terminate) ---"
        if alive "$pid"; then
            kill -QUIT "$pid" 2>>"$OUT"
            sleep 1.5
            if alive "$pid"; then
                log "QUIT NOT DELIVERED pid=$pid stat=$(stat_of "$pid") (blocked)"
            else
                log "QUIT DELIVERED pid=$pid (harness died to QUIT)"
            fi
        fi
        log "--- stack sample pid=$pid (last-ditch corroboration) ---"
        if alive "$pid"; then
            "$SAMPLE" "$pid" 1 -f "$OUT.sample.$pid" >/dev/null 2>&1 || true
            grep -m4 -E "__wait4|wait_for|sleep|bash|__semwait" "$OUT.sample.$pid" \
                >>"$OUT" 2>/dev/null || log "(no stack match)"
            kill -TERM "$pid" 2>/dev/null
            sleep 1.5
            alive "$pid" && kill -KILL "$pid" 2>/dev/null
        fi
        log "probe done pid=$pid"
    done
    [ -n "$seen" ] && [ "$hits" -ge "$MAXHITS" ] && break
    sleep 0.2
done
log "probe_stalled exit hits=$hits"
