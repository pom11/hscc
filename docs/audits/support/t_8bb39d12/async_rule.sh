#!/bin/sh
# Minimal repro of the POSIX asynchronous-command rule, with no pytest in the
# picture: the same one-line child, run in the foreground and as `cmd &`,
# reporting its own SIGINT/SIGQUIT disposition.
#
# POSIX (sh spec, "Asynchronous Command"): when job control is OFF (a
# non-interactive shell), the shell shall arrange for SIGINT and SIGQUIT to be
# ignored in the asynchronous command. Nothing in load_burners.sh is involved.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${PY:-$HOME/.hermes/hermes-agent/venv/bin/python}
WHO='import signal,os
print("  pid=%d ppid=%d INT=%s QUIT=%s" % (
    os.getpid(), os.getppid(),
    "IGN" if signal.getsignal(signal.SIGINT) is signal.SIG_IGN else
    ("DFL" if signal.getsignal(signal.SIGINT) is signal.SIG_DFL else "handler"),
    "IGN" if signal.getsignal(signal.SIGQUIT) is signal.SIG_IGN else
    ("DFL" if signal.getsignal(signal.SIGQUIT) is signal.SIG_DFL else "handler")))'

echo "shell=$0  /bin/sh -> $(ls -l /bin/sh | awk '{print $NF}')"
echo "A) foreground child:"
"$PY" -c "$WHO"
echo "B) asynchronous child (\`cmd &\`, job control off):"
"$PY" -c "$WHO" &
wait
echo "C) asynchronous through a subshell, the pair_chase.sh shape ( '( ... ) & ' ):"
( "$PY" -c "$WHO" ) &
wait
echo "D) asynchronous child of a job-controlled shell (set -m => no ignore):"
set -m
"$PY" -c "$WHO" &
wait
