"""No-ANSI + byte-identity regression test for sparkrun-hermes.

The RICH CLI epic's per-package card asked each hscc-* plugin to route its
human-facing output through the themed Rich layer (_theme.py) and pin the
HARD INVARIANTS with a no-ANSI test. This card (sparkrun-hermes, "1 raw
site") is the special case that proves the invariant: sparkrun-hermes is a
``kind: backend`` PLUGIN TOOL, not a human CLI. It exposes one ``sparkrun_exec``
tool to the agent runtime via ``register(ctx)``; there is no ``main()``, no
argparse, no terminal surface.

The one ``json.dumps`` in NON-TEST code (__init__.py:_stringify) is NOT a
human-facing print: it is the MACHINE WIRE-FORMAT path that converts the tool's
return dict to the a string the vLLM/OpenAI tool layer requires
(role:"tool" content must be a string). Per the card's own HARD INVARIANT
("--json BYTE-IDENTICAL; daemon/scripts parse it") this path must STAY raw and
BYTE-IDENTICAL and must NEVER be routed through a themed Rich Console — doing
so would corrupt the tool's wire format for no human reader.

So the correct deliverable for this package is exactly what this file pins:

- The ``_stringify`` output (the package's ONLY non-test output surface) is
  BYTE-IDENTICAL to a raw ``json.dumps(..., ensure_ascii=False, default=str)``.
- That output carries NO ANSI (asserted via the named ``_ESC`` constant, never
  a literal escape byte).

These guard the invariant that actually matters here — the agent-parseable tool
payload stays clean and stable — without fabricating a themed human surface
that this backend plugin does not have.
"""

import json

import __init__ as plugin


# The escape character, as one named constant — asserted, never a literal.
_ESC = "\x1b"


def test_stringify_output_is_byte_identical():
    """_stringify's JSON string equals the raw json.dumps re-encoded from the
    same payload — the byte-identity the wire format depends on."""
    payload = {"ok": True, "exit_code": 1, "command": "sparkrun status",
               "stdout": "all good", "stderr": "boom", "unicode": "qwen3-1.7b"}
    wrapped = plugin._stringify(lambda a, **k: payload)
    out = wrapped({"command": "sparkrun status"})
    assert isinstance(out, str)
    expected = json.dumps(payload, ensure_ascii=False, default=str)
    assert out == expected, "_stringify must stay byte-identical to raw json.dumps"


def test_stringify_output_has_no_ansi():
    """The wire payload (the only non-test output surface) must never carry an
    ANSI escape byte — scripts/agent runtime parse it as plain JSON."""
    payload = {"ok": True, "stdout": "all good"}
    wrapped = plugin._stringify(lambda a, **k: payload)
    out = wrapped({"command": "sparkrun status"})
    assert _ESC not in out, f"ANSI escape found in tool payload: {out!r}"


def test_stringify_preserves_default_str_for_non_serializable():
    """A non-JSON-serializable value still serializes (via default=str), with
    no ANSI — exactly the raw behavior at __init__.py, unchanged."""
    class _Obj:
        def __str__(self):
            return "obj-repr"
    payload = {"ok": True, "thing": _Obj()}
    wrapped = plugin._stringify(lambda a, **k: payload)
    out = wrapped({"command": "sparkrun status"})
    assert _ESC not in out
    assert "obj-repr" in out


def test_no_human_terminal_surface_to_theme():
    """sparkrun-hermes is a backend plugin tool: the non-test modules must not
    emit to a terminal at all. This guards the epic's real invariant — there is
    no raw print() left to theme and none may be introduced — by asserting the
    package's non-test modules contain no bare print( call and no rich/console
    import (anything a human CLI would need)."""
    import os
    base = os.path.dirname(os.path.abspath(plugin.__file__))
    for mod_name in ("__init__", "execlib"):
        with open(os.path.join(base, f"{mod_name}.py")) as fh:
            src = fh.read()
        assert "print(" not in src, f"{mod_name}.py must contain no print()"
        assert "rich" not in src, f"{mod_name}.py must contain no rich import"
        assert "_theme" not in src, f"{mod_name}.py needs no _theme (no CLI)"
