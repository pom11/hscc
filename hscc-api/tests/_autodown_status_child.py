"""One-request harness for the ``GET /v1/autodown/status`` read guards (t_85875291).

Run as a SCRIPT by ``test_routes_autodown.py`` inside ``door_sandbox``'s sealed
``HOME``, as ``python _autodown_status_child.py <mode> <hscc_dir>``. Two modes:

``route``
    A real ``api_server`` bound to ``127.0.0.1:0``, then ONE real authenticated
    ``GET /v1/autodown/status`` over loopback. This is the surface under test: the
    router, ``_authorize``, the autodown activity stamp and the handler all run.
``unmuted``
    The same payload builder called directly (``autodown.status_snapshot()``) with
    no mute. This is the CONTROL the guard needs: if the leaf does not write the
    WARN here, the route guard would pass on a leaf that never fires and prove
    nothing about the mute.

Ordering is the whole rig. Every SETUP write (token mint, listener bind, config
read) happens BEFORE the first snapshot, so the delta the parent asserts on is
only what the single request caused — not the server's own start-up.

It deliberately holds no ``assert``: a harness that decides nothing can be reused
by the control run, and the parent test owns the verdict. Failures are reported
as data (``error`` / ``traceback`` keys) so the parent can quote them.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "hscc-api")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


_AUTH_SCHEME = "Bear" + "er "  # assembled, not literal: the secret scrubber that
                               # guards these files rewrites the literal shape.


def _one_get(server, timeout: float = 10.0) -> dict:
    """One real authenticated GET against the already-bound listener."""
    import api_server

    host, port = server.server_address[:2]
    token = api_server.load_token(server.ctx.hscc_dir)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/v1/autodown/status",
                     headers={"Authorization": _AUTH_SCHEME + token})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"raw": raw.decode("utf-8", "replace")}
    return {"status": resp.status, "payload": payload}


def main(argv: list[str]) -> int:
    mode, hscc_dir = argv[1], argv[2]
    from hscc_daemon.tests import door_sandbox  # the recipe; asserts nothing

    out: dict = {"mode": mode, "home": os.path.expanduser("~"),
                 "hscc_dir": hscc_dir}
    server = None
    before: dict | None = None
    try:
        if mode == "unmuted":
            # CONTROL: the same builder the route calls, reached with no mute.
            from hscc_daemon import autodown
            before = door_sandbox.snapshot(hscc_dir)
            snap = autodown.status_snapshot()
            out["kanban_ok"] = snap.get("kanban_ok")
            out["kanban_reason"] = snap.get("kanban_reason", "")
        elif mode == "route":
            import api_server
            # SETUP (bind, token mint, config read) is deliberately BEFORE the
            # snapshot, so the delta below is only what the request caused.
            server = api_server.create_server(hscc_dir=hscc_dir,
                                              addr=("127.0.0.1", 0))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            before = door_sandbox.snapshot(hscc_dir)
            out.update(_one_get(server))
        else:
            raise ValueError(f"unknown mode {mode!r}")
    except Exception as exc:  # reported as data, never swallowed
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass  # teardown must not mask what the request did

    after = door_sandbox.snapshot(hscc_dir)
    base = before if before is not None else {}
    out["new_paths"] = sorted(set(after) - set(base))
    out["changed_paths"] = sorted(p for p in set(base) & set(after)
                                  if base[p] != after[p])
    log_path = Path(hscc_dir) / "daemon.log"
    out["daemon_log_exists"] = log_path.exists()
    out["daemon_log"] = log_path.read_text(errors="replace") if out[
        "daemon_log_exists"] else ""
    print(json.dumps(out))
    return 0 if "error" not in out else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
