#!/usr/bin/env python3
"""t_5db4cbba Q1/Q2: what does the SHELL itself do when a pipe consumer closes early?

The card's stated fear is that a `trap ... PIPE` converts "shell died with 141" into
"shell ran your handler" at pipe sites nobody enumerated. That fear has a precise
shape: only a write performed BY THE SHELL (a builtin) into a pipe whose read end is
closed can raise SIGPIPE in the shell; an external producer's write kills the
producer, not the shell. So each case answers two things, measured:

  who_died : the runner's rc (a signal death is -N here, 128+N in a shell)
  handler  : did a `trap ... PIPE` run for that same event?

Cases cover the three shapes that exist in run_tests.sh plus the two the card
worries about: `echo … | tr …`/`| head` (a builtin writing into a pipe) and the
runner's OWN stdout being a pipe whose reader closes.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

BASH = "/bin/bash"
REPO = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
HANDLER = "echo 'HANDLER-RAN' >&2; trap - PIPE; exit 141"

# (name, body, close_stdout_after_n_bytes_or_None)
CASES = [
    # The ONLY pipe shape in run_tests.sh: `$(ps … | tr -s ' ')`. Producer is an
    # EXTERNAL command; the consumer is the shell's own command substitution,
    # which reads to EOF (there is no early close available).
    ("ps_pipe_tr_as_shipped",
     "p=$(ps -o pid=,ppid=,command= -p $$ 2>/dev/null | tr -s ' ')\n"
     "echo \"SURVIVED rc=$? len=${#p}\"", None),

    # Same shape but the CONSUMER closes early — the card's `| head` worry. Here
    # `head` is the consumer and the PRODUCER is the external `ps`.
    ("external_producer_head",
     "p=$(seq 1 20000 | ps -o command= -p $$ >/dev/null; seq 1 20000 | head -c 4)\n"
     "echo \"SURVIVED rc=$? p_len=${#p}\"", None),

    # A BUILTIN (echo) writing into a pipe whose consumer closes early. This is
    # the self-inflicted class. Inside $( ) so the reader is the subshell.
    ("builtin_writer_head",
     "out=$(for i in $(seq 1 5000); do echo line$i; done | head -c 4)\n"
     "echo \"SURVIVED rc=$? out=[$out]\"", None),

    # The runner's OWN stdout is a pipe and the reader closes. Every `echo` the
    # pack loop prints goes through this.
    ("main_stdout_closes",
     "i=0\nwhile [ $i -lt 200000 ]; do echo x$i; i=$((i+1)); done\n"
     "echo SURVIVED-LOOP >&2", 4),

    # `| tr` with a builtin writer (the other shape named in the card title).
    ("builtin_writer_tr",
     "p=$(echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | tr -d a)\n"
     "echo \"SURVIVED rc=$? p=[$p]\"", None),
]


def run(body, close_after, trap):
    script = textwrap.dedent(body).strip()
    if trap:
        script = f"trap '{HANDLER}' PIPE\n" + script
    p = subprocess.Popen([BASH, "-c", script], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=str(REPO))
    got = b""
    if close_after:
        try:
            got = p.stdout.read(close_after)
        except Exception:
            got = b""
        p.stdout.close()
    try:
        rc = p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = "TIMEOUT"
    err = p.stderr.read().decode(errors="replace")
    return rc, ("HANDLER-RAN" in err), got


def main():
    ver = subprocess.run([BASH, "--version"], capture_output=True, text=True).stdout
    print(f"bash={ver.splitlines()[0]}  repo={REPO}")
    for name, body, close_after in CASES:
        for trap in (False, True):
            rc, fired, got = run(body, close_after, trap)
            tag = "TRAP  " if trap else "NOTRAP"
            print(f"  {name:24} {tag}  rc={str(rc):>8}  handler={'YES' if fired else 'no'}"
                  f"  first={got!r}")


if __name__ == "__main__":
    main()
