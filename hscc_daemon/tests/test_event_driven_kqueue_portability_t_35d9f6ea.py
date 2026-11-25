"""t_35d9f6ea — event_driven must IMPORT on a host without kqueue.

Found while the launchd path seal (see test_event_driven_plist_seal_t_35d9f6ea)
was being sealed: that seal's fixture reads ``hscc_daemon.event_driven`` back,
so importing the module became part of every case in the package — and on the
ubuntu CI leg that import raised::

    hscc_daemon/event_driven.py:240: AttributeError
        def _open_kqueue(self) -> Optional[select.kqueue]:
    AttributeError: module 'select' has no attribute 'kqueue'

24 cases in test_plugin_manifest_drift.py errored at setup in the cross-leg run
(job 139136721227, PR #811). The seal did not create the bug; it put the module
on the import path of a leg that had never imported it.

The bug is that a return annotation is an EXPRESSION. On CPython < 3.14 it is
evaluated when the ``def`` runs, i.e. while the class body executes, so on any
host where ``select.kqueue`` does not exist (every Linux DGX node, every ubuntu
runner) the class could never even be defined — although the module is written
to degrade to polling exactly there (``_open_kqueue`` catches AttributeError and
``_poll_fallback`` takes over).

Only the ``def``-time site was real. The other candidate — the
``self._kqueue: Optional[select.kqueue] = None`` annotation inside ``__init__`` —
was probed and left alone: CPython does not evaluate the annotation of a
non-simple assignment target at all (measured with ``self.x: missing.attr =
None`` on 3.12, 3.13 and 3.14 — construction succeeds), so quoting there would
have been a guess the pin could not have kept honest.

Why a child process: the parent cannot un-import a module it has already
imported, and ``select.kqueue`` exists on this (macOS) host, so the only honest
way to run the import is a fresh interpreter that deleted the attribute first —
the state a Linux box is simply born into.

Why ``__annotations__`` and not ``typing.get_type_hints``: the pin has to hold
on both interpreters. CPython 3.14 (PEP 649) defers annotations, so an unquoted
one no longer fails at ``def`` time there; touching ``__annotations__`` forces
the same evaluation 3.13 did implicitly. ``get_type_hints`` would also resolve
the *quoted* (fixed) form and so would stay red on correct code.
"""

import os
import subprocess
import sys

# The child program, run by sys.executable so the pin follows whatever
# interpreter the leg uses. __REPO_ROOT__ is substituted below; nothing else in
# here is templated, so the child reads as an ordinary program.
_CHILD = '''
import os
import sys
import tempfile

sys.path.insert(0, __REPO_ROOT__)

import select

had_kqueue = hasattr(select, "kqueue")
if had_kqueue:
    del select.kqueue          # the Linux posture: the symbol was never there
assert not hasattr(select, "kqueue"), "could not simulate a kqueue-less host"

try:
    from hscc_daemon import event_driven as ed
except AttributeError as exc:
    print("IMPORT_ERROR %s" % exc)
    raise SystemExit(1)

# Every callable defined in this module must still have annotations that can be
# EVALUATED on this host. On 3.14 an unquoted `Optional[select.kqueue]` no
# longer explodes at def time, so touch them explicitly -- that is the same
# evaluation 3.13 performed implicitly while defining the class.
offenders = []
seen = set()


def touch(label, obj):
    if obj is None or id(obj) in seen:
        return
    seen.add(id(obj))
    try:
        obj.__annotations__
    except AttributeError as exc:
        if "select" in str(exc) or "kqueue" in str(exc):
            offenders.append("%s: %s" % (label, exc))
    except Exception:
        pass                   # an unrelated annotation failure is not this pin


for name, value in list(vars(ed).items()):
    if (isinstance(value, type)
            and getattr(value, "__module__", None) == ed.__name__):
        touch(name, value)
        for mname, mval in list(vars(value).items()):
            if isinstance(mval, property):
                touch("%s.%s" % (name, mname), mval.fget)
                touch("%s.%s" % (name, mname), mval.fset)
            elif callable(mval):
                touch("%s.%s" % (name, mname), mval)
    elif callable(value) and getattr(value, "__module__", None) == ed.__name__:
        touch(name, value)

# And the degradation the module promises on a host with no kqueue has to still
# work: the watcher is constructible and says so, rather than half-working.
watcher = ed.KqueueWatcher(tempfile.mkdtemp(), lambda path: None)
opened = watcher._open_kqueue()
print("RESULT had_kqueue=%s imported=1 opened=%r using_kqueue=%s offenders=%r"
      % (had_kqueue, opened, watcher.using_kqueue, offenders))
raise SystemExit(1 if offenders or opened is not None or watcher.using_kqueue
                 else 0)
'''


def _child_source():
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(tests_dir))
    return _CHILD.replace("__REPO_ROOT__", repr(repo_root))


def test_event_driven_imports_on_a_host_without_kqueue(tmp_path):
    script = tmp_path / "child_no_kqueue.py"
    script.write_text(_child_source())

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pyc")

    proc = subprocess.run(
        [sys.executable, str(script)],
        env=env, cwd=str(tmp_path), capture_output=True, text=True,
        timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "hscc_daemon.event_driven is not usable on a host without kqueue "
        f"(rc={proc.returncode}):\n{out}")
    assert "imported=1" in out, out
    # The posture was real on either host: this one had kqueue and the child
    # deleted it, or it never had it (Linux). Either way the import ran with the
    # symbol absent, which is what the assertion above is about.
    assert "had_kqueue=True" in out or "had_kqueue=False" in out, out
    assert "opened=None" in out, out
    assert "offenders=[]" in out, out
