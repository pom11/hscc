"""resid_probe.py — a pytest plugin that NAMES the residue, for debugging a leg
whose answer depends on file order (companion to
``docs/audits/daemon-alone-run-order-dependence_t_572b4856.md``).

Load it before any conftest is imported and run the leg once:

    RESID_OUT=/tmp/resid.txt PYTHONPATH=docs/audits/support/t_572b4856 \
      python -m pytest -q hscc_daemon/tests -p resid_probe

It records, with the caller's stack:

  A. every mutation of ``sys.path`` that adds or removes an entry resolving to
     the real agent tree (``$HERMES_AGENT_PATH`` or ``~/.hermes/hermes-agent``) —
     a window that opens AND closes shows up as two events;
  B. every FIRST import lookup of a hermes name (a ``meta_path`` finder, so it
     fires once per name: a name already in ``sys.modules`` never reaches a
     finder) and where it resolved.

At session finish it prints the final state. That is what separates the two
candidate residues, which need different fixes:

  * ``FINAL sys.path entries under the real tree`` non-empty -> the PATH leaked
    (an import window that did not restore).
  * it is EMPTY while ``FINAL sys.modules entries rooted in the real tree`` is
    not -> the MODULES leaked. A closed window restores the path but cannot
    un-import what it imported, and a resident package carries ``__path__`` into
    the tree, so every later ``import hermes_cli.<anything>`` resolves with the
    tree nowhere on the path. This is what the daemon leg actually did.

Read-only: it mutates no repo file and imports nothing from the repo.
"""
import os
import sys
import traceback

REAL = os.path.realpath(os.path.expanduser(
    os.environ.get("HERMES_AGENT_PATH", "~/.hermes/hermes-agent")))
OUT = os.environ.get("RESID_OUT", "")
_HERMES_NAMES = ("hermes_state", "hermes_constants", "utils", "hermes_yaml",
                 "toolsets")
_PATH_EVENTS = []
_IMPORT_EVENTS = []


def _under_real(p):
    try:
        rp = os.path.realpath(str(p)) if p else ""
    except Exception:
        return False
    return bool(rp) and (rp == REAL or rp.startswith(REAL + os.sep))


class _TracePath(list):
    """sys.path with the mutations logged. Behavioural twin of list."""

    def _log(self, op, args, extra=""):
        hits = [p for p in args if _under_real(p)]
        if hits:
            _PATH_EVENTS.append((op, hits, extra,
                                 "".join(traceback.format_stack()[-9:-1])))

    def insert(self, i, p):
        self._log("insert", (p,)); super().insert(i, p)

    def append(self, p):
        self._log("append", (p,)); super().append(p)

    def extend(self, seq):
        seq = list(seq); self._log("extend", seq); super().extend(seq)

    def remove(self, p):
        self._log("remove", (p,)); super().remove(p)

    def pop(self, i=-1):  # pyright: ignore[reportIncompatibleMethodOverride]
        p = self[i] if -len(self) <= i < len(self) else None
        self._log("pop", (p,)); return super().pop(i)

    def __setitem__(self, k, v):
        self._log("setitem", list(v) if isinstance(k, slice) else [v],
                  f"key={type(k).__name__}")
        super().__setitem__(k, v)

    def __delitem__(self, k):
        try:
            gone = list(self[k]) if isinstance(k, slice) else [self[k]]
        except Exception:
            gone = []
        self._log("delitem", gone); super().__delitem__(k)


_TP = _TracePath(sys.path)
sys.path = _TP
_START_HAS_REAL = any(_under_real(p) for p in _TP)
_START_MODULES = sorted(n for n in sys.modules
                        if n == "hermes_cli" or n.startswith("hermes_cli."))


class _Finder:
    """Logs the FIRST spec lookup for a hermes name, and where it resolved."""

    def find_spec(self, name, path=None, target=None):
        if name == "hermes_cli" or name.startswith("hermes_cli.") or \
                name in _HERMES_NAMES:
            _IMPORT_EVENTS.append((name, str(path),
                                   "".join(traceback.format_stack()[-11:-2])))
        return None            # never claims a spec; the chain carries on


sys.meta_path.insert(0, _Finder())


def _dump():
    L = [f"=== resid_probe  real={REAL}",
         f"real tree already on sys.path at plugin import: {_START_HAS_REAL}",
         f"hermes_cli* already in sys.modules at plugin import: {_START_MODULES}",
         f"--- sys.path events touching the real agent tree: {len(_PATH_EVENTS)}"]
    for i, (op, hits, extra, stack) in enumerate(_PATH_EVENTS):
        L += [f"[path {i}] op={op} {extra} hits={hits}", stack.rstrip()]
    L.append(f"--- first-import lookups of hermes names: {len(_IMPORT_EVENTS)}")
    for i, (name, path, stack) in enumerate(_IMPORT_EVENTS):
        L += [f"[import {i}] name={name} parent_path={path}", stack.rstrip()]
    L.append("--- FINAL sys.path entries under the real tree: "
             f"{[p for p in sys.path if _under_real(p)]}")
    L.append("--- FINAL sys.modules entries rooted in the real tree:")
    for name in sorted(sys.modules):
        mod = sys.modules.get(name)
        origin = getattr(mod, "__file__", None)
        if origin and _under_real(origin):
            L.append(f"    {name} <- {origin}")
    cli = sys.modules.get("hermes_cli")
    L.append("--- FINAL hermes_cli in sys.modules: "
             f"{cli is not None}  __path__={list(getattr(cli, '__path__', []))}")
    L.append(f"--- sys.path is still our subclass: {sys.path is _TP}")
    text = "\n".join(L) + "\n"
    if OUT:
        with open(OUT, "a") as fh:
            fh.write(text)
    else:
        sys.stderr.write(text)


def pytest_sessionfinish(session, exitstatus):
    _dump()
