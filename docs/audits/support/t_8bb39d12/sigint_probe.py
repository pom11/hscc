"""Is SIGINT inherited as SIG_IGN? Exit 0 if yes, 1 if not.

The harness's own detection primitive, as a file (a `python3 -c` one-liner is
not available in the worker's non-interactive mode, and the harness has to
call the same thing either way). Cost is measured in cost_probe.sh.
"""
import signal
import sys

sys.exit(0 if signal.getsignal(signal.SIGINT) is signal.SIG_IGN else 1)
