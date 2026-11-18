kind: Added
task: t_eaedbd6a

- **`hscc api` can listen on several addresses at once.** `--bind <ip>` is
  repeatable and combines with `--tailscale`, so one process serves the same
  port, token and handlers on the LAN and the tailnet (first `--bind` is the
  primary). Default stays loopback; `0.0.0.0`/`::` are refused per address and
  every explicit address must be an interface on this host. A failed non-tailnet
  bind fails the whole start (earlier listeners are closed), while a tailnet
  bind with Tailscale down leaves the others serving and is reported by
  `hscc api status`. `install-service` carries all the flags into the launchd
  unit, the QR payload adds a `hosts` array (legacy `host` stays primary), and
  `~/.hscc/api-state.json` records the runtime addresses with the same
  back-compatible `host` field.
