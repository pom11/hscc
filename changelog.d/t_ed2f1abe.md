kind: Fixed
task: t_ed2f1abe

* a SIGKILLed HSCC API process no longer orphans the live gateway sidecar.
  ``gateway_bridge.start_serve`` spawns ``hermes serve`` under a new
  ``gateway_serve_watchdog.py``, which holds the read end of a pipe whose write
  end only the API process keeps: the kernel returns EOF to it the instant that
  process is gone, whatever removed it — the SIGKILL escalation in
  ``hscc api stop``, a crash, or a teardown that never ran — and the watchdog
  then terminates the serve's process group and reaps it. macOS has no
  ``PR_SET_PDEATHSIG``, so the ``os.getppid()`` reparenting poll stays as the
  fallback for the case where no usable fd arrives. The API-side stop path is
  unchanged: the signal handler still makes no calls and ``shutdown()`` stays
  the cooperative door, not the only one.
---
kind: Verified
task: t_ed2f1abe

* measured on macOS against the shipped watchdog, 5 trials per arm, real SIGKILL
  of a real parent (chain parent -> watchdog -> serve): detection 0.0008-0.0010 s
  on the liveness-pipe arm, 0.2366-0.3387 s on the 0.25 s reparenting-poll arm,
  0.8962-1.0397 s at a 1.0 s tick; the serve is truly gone 0.065-0.174 s after
  the parent's death on the pipe arm and 0.290-0.504 s on the fallback arm.
  Accepted blind spot: the fallback's 0.25 s tick, plus up to 2.0 s of SIGTERM
  and 1.0 s of SIGKILL grace while a stubborn serve goes down — the bound the
  new tests assert is 4.0 s. The watchdog's whole teardown budget stays below the
  API's wait, and the test file pins that inequality in both directions.
* ``hscc-api/tests/test_gateway_serve_watchdog.py`` (15 items) SIGKILLs a real
  API-side parent whose sidecar is a real subprocess and asserts the pid is gone
  and its loopback port released, not that a flag was set. Red on pristine
  ``origin/main`` at ``8a36d579``, where the sidecar is still alive 4.0 s after
  its parent was killed.
