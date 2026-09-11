# t_fc2e955d — daemon streams FAIL every tick but PASS fresh: ROOT CAUSE

## Verdict: NOT a code discrepancy — macOS Local Network (TCC) permission denies
the python interpreters hscc runs under, so every LAN connect() returns
errno 65 "No route to host" while the fleet is healthy and reachable from other
processes.

(Live addresses are redacted to placeholders per the public-repo rule; the live
gateway LAN IP is referred to as <GATEWAY_IP>.)

## Decisive evidence (all commands under the daemon's own interpreter,
~/.hermes/hermes-agent/venv/bin/python, cwd=/Users/desac)

1. Same host, different processes — the endpoint is UP and routable:
     nc -vz <GATEWAY_IP> 8000      -> "Connection ... port 8000 ... succeeded!"
     ssh ... <GATEWAY_IP> "echo reachable" -> reachable
     ping <GATEWAY_IP>              -> 0% loss
     Apple-signed /usr/bin/python3 (CLT 3.9.6) socket.create_connection -> CONNECTED

2. The two interpreters hscc actually uses are blocked from LAN TCP:
     hermes venv python (3.11.16, ad-hoc signed):  TCP 8000/22/443 -> errno 65
     miniconda p313 python (3.13.12):              TCP 8000/22    -> errno 65
   (urllib AND raw socket both fail with OSError(65, 'No route to host'))

3. The block is LAN-scoped, not process-wide — public internet works from the
   same blocked interpreter:
     venv python: 1.1.1.1:53 CONNECTED, 8.8.8.8:53 CONNECTED, <GATEWAY_IP>:8000 errno 65

   This exact signature — internet OK, LAN blocked, EHOSTUNREACH (errno 65) — is
   the documented macOS "Local Network" privacy-permission denial (System
   Settings > Privacy & Security > Local Network). No app firewall
   (socketfilterfw globalstate=0, blockall=0, stealth=0), no network extensions
   (systemextensionsctl list: 0), no proxy env vars.

4. What this means for the task's "same cluster, same code" paradox:
   - The code IS identical between daemon and fresh CLI (both import the plugin
     at ~/.hermes/plugins; diff -q confirms byte-identical health.py/serving.py).
   - The URL IS identical (serving.VLLM_HEALTH_URL resolves to
     http://<GATEWAY_IP>:8000/health in every fresh process).
   - The daemon's FAIL is CORRECT given its environment: from the hermes venv
     python process, the LAN is unreachable (TCC denies connect()).
   - The task author's "fresh OK" was observed earlier while the permission was
     still granted (or via an allowed path); by the time of this run (~15:25 UTC)
     a fresh `hscc check gateway`/`hscc check dgx` ALSO returns FAIL — the
     discrepancy collapsed because both interpreters are now denied.
   - The earlier "PASS under both interpreters" (ruled-out #3) is consistent: at
     that earlier moment the permission was granted; it has since been revoked.

## Why the watchdog went red (the real operational concern)
watchdog does auto_restart_count=192 force-restarts fighting the ghost failure.
It restarts vLLM whenever the gateway/dgx vllm probe fails. Under a TCC-denied
interpreter every probe fails, so it enters a forever restart loop — harmless to
the (healthy, unreachable-from-the-mac) cluster, but it burns restart attempts
and keeps the watchdog permanently red. The probe now surfaces the actionable
hint instead of bare "No route to host".

## Fix shipped (commit b48548c)
hscc_daemon/util.py — http_check() now detects errno 65/64/51 to a private/LAN
host and appends:
    " (if host is a LAN/private IP, macOS may be blocking this app's Local
      Network access: grant it in System Settings > Privacy & Security > Local
      Network, or verify the host is actually up)"
so the daemon's state files and logs carry the real cause instead of a
misleading "No route to host". Public-IP failures and non-host-unreach errors
are untouched (additive, fail-safe: only appends when errno in (64,65,51) AND
the host is private/localhost).
Tests: +3 in hscc_daemon/tests/test_util.py. Full hscc_daemon suite: 1064 passed.

## The ACTUAL fix (user/system action — not code, cannot be done in-repo)
Grant macOS Local Network permission to the python interpreters hscc runs under:
    System Settings > Privacy & Security > Local Network
    -> enable for: hermes-agent venv python3.11 and miniconda p313 python3.13
(They may appear as "python3.11" / "python3.13" or under the venv/conda names.)
Alternatively `tccutil reset LocalNetwork` and answer the prompt "Allow", or
(right-click the denials) re-enable them. No code change can bypass TCC from a
denied process — it is enforced at connect() by the kernel. After granting,
restart the daemon and every health stream will go green; the watchdog stops its
restart loop.

## What I did NOT do and why
- Did NOT raise timeouts/thresholds to turn lights green — the check is
  correctly reporting a real (per-process) reachability problem; masking it
  would hide the actual outage the watchdog exists to catch.
- Did NOT run `hscc doctor`/bootstrap against live runtime (task rule).
- Did NOT mutate live state (no mutating POSTs; only read the state files and
  ran the same read-only `hscc check` the task author already ran).
- Did NOT patch the installed plugin at ~/.hermes/plugins — the fix is committed
  in the worktree; deploying to the live plugin is an operator action.

## Traceability
- commit b48548c — fix(daemon): surface macOS Local Network hint on LAN errno 65
- All probes were throwaway scripts in the worktree (now removed); none committed.
- No real host/IP committed (tests and this report use 10.0.0.10 / 1.1.1.1 /
  <GATEWAY_IP> placeholders); test_no_real_addresses_committed.py passes.
