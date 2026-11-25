"""Set SIGINT/SIGQUIT (and optionally others) dispositions, then exec.

Measurement instrument for t_8bb39d12: manufactures the two dispositions the
launcher-frame experiment needs — the POSIX async-command ignore (`ign`) and
the reset-to-default (`dfl`) — so the same run can be observed with and
without it.

usage: setdisp.py {ign|dfl|keep} [sig ...] -- <cmd> [args...]
       signals by name: int quit hup tstp term chld   (default: int quit)
"""
import os
import signal
import sys

NAMES = {"int": signal.SIGINT, "quit": signal.SIGQUIT,
         "hup": signal.SIGHUP, "tstp": signal.SIGTSTP,
         "term": signal.SIGTERM, "chld": signal.SIGCHLD}


def main(argv):
    mode = argv[1]
    rest = argv[2:]
    sigs = ["int", "quit"]
    if rest and rest[0] != "--":
        sigs = []
        while rest and rest[0] != "--":
            sigs.append(rest.pop(0))
    rest = rest[1:] if rest and rest[0] == "--" else rest
    for name in sigs:
        sig = NAMES[name]
        if mode == "ign":
            signal.signal(sig, signal.SIG_IGN)
        elif mode == "dfl":
            signal.signal(sig, signal.SIG_DFL)
        elif mode != "keep":
            raise SystemExit("bad mode %r" % mode)
    os.execvp(rest[0], rest)


if __name__ == "__main__":
    main(sys.argv)
