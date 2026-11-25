"""Launch the real harness the way scripts/tests does, and hold both pids
alive so sigprobe can read each link of the chain from outside.

The stalled harness read `ignored INT=1 / caught INT=0` while TERM/HUP were
caught — i.e. bash 3.2 never had an INT handler to run. This says which link
handed it that ignore: a process that inherits SIGINT as SIG_IGN keeps it
(CPython only installs its SIGINT handler when the inherited disposition is
SIG_DFL, and execve preserves an ignore), and bash 3.2 then cannot `trap` INT.
"""
import os
import subprocess
import sys
import time

harness = sys.argv[1]
out = sys.argv[2]
proc = subprocess.Popen(
    ["/bin/sh", harness, "1", "60", "--", "sleep", "60"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    env={**os.environ, "HSCC_LOAD_BURNER_TAG": "hscc-load-attr"},
    start_new_session=True,          # exactly what test_load_burners._running does
)
with open(out, "w") as fh:
    fh.write("launcher_pid=%d\npython_pid=%d\nharness_pid=%d\n"
             % (os.getpid(), os.getpid(), proc.pid))
    fh.flush()
time.sleep(40)
proc.terminate()
proc.wait(timeout=10)
