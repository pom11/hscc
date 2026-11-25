"""Record this pid, then exec — so a watcher knows the pid of the process it is
about to inspect, without changing that process's signal state.

Needed because `$$` inside `( ... )` expands to the *parent* shell's pid in
bash 3.2, which sent the first version of harness_shape.sh probing the wrong
process (it read `caught INT=1` on the harness for every shape, including the
ones whose harness was demonstrably deaf).

execve preserves an inherited SIG_IGN and resets installed handlers to
SIG_DFL, and CPython only installs its own SIGINT handler when the inherited
disposition is SIG_DFL — so wrapping a launch in this file is disposition-
neutral for the exec'd program.

usage: pidexec.py <pidfile> <cmd> [args...]
"""
import os
import sys

pidfile, cmd = sys.argv[1], sys.argv[2]
with open(pidfile, "w") as fh:
    fh.write("%d\n" % os.getpid())
os.execvp(cmd, sys.argv[2:])
