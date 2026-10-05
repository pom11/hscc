"""PROBE (t_163fa09f) — TEMPORARY instrumentation. Not part of the fix; delete.

Logs every create / scan / clear of the process-global chat-job store together
with the thread that did it and the test that was executing, so the ordering
that makes test_stop_kind_with_nothing_in_flight_is_noop fail can be read off
the log instead of guessed.

Enable:  PYTHONPATH=<hscc-api> pytest -p jobprobe ...  with HSCC_JOB_PROBE=<file>
"""
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_STATE = {"test": "<none>"}
_path = os.environ.get("HSCC_JOB_PROBE")


def _log(kind, detail=""):
    if not _path:
        return
    rec = "%13.3f | %-13s | %-13s | %-55s | %s" % (
        time.time() % 100000, threading.current_thread().name[:13], kind,
        _STATE["test"], detail)
    with open(_path, "a", encoding="utf-8") as fh:
        fh.write(rec + "\n")


def pytest_runtest_logreport(report):
    if report.when == "setup" and report.passed:
        _STATE["test"] = report.nodeid.split("::")[-1][:55]
        _log("TEST-START")


def pytest_configure(config):
    if not _path:
        return
    import routes_orchestrator as ro

    _new_job = ro._new_job

    def _new_job_p(project, *a, **k):
        job = _new_job(project, *a, **k)
        _log("NEW_JOB", "project=%s id=%s" % (project, job.job_id))
        return job

    ro._new_job = _new_job_p

    _in_flight = ro._in_flight_job

    def _in_flight_p(project):
        job = _in_flight(project)
        with ro._jobs_lock:
            store = sorted((j.project, j.status, j.finished_at is None)
                           for j in ro._jobs.values())
        _log("IN_FLIGHT", "proj=%s -> %s | store=%r" %
             (project, job.job_id if job else None, store))
        return job

    ro._in_flight_job = _in_flight_p

    _run_job = ro._run_job

    def _run_job_p(job):
        _log("RUN_JOB-ENTER", "id=%s" % job.job_id)
        try:
            return _run_job(job)
        finally:
            _log("RUN_JOB-EXIT", "id=%s status=%s" % (job.job_id, job.status))

    ro._run_job = _run_job_p

    _clear = ro._jobs.clear  # reference only; the fixture clears via _jobs.clear()

    class _WatchingDict(dict):
        """Same dict semantics, but every clear() reports who + what was in it.

        Deliberately takes NO lock: the conftest fixture clears while holding
        the (non-reentrant) _jobs_lock, and re-entering it from here deadlocks
        the whole run (learned the hard way). Snapshot best-effort instead.
        """

        def clear(self):
            try:
                live = [(j.job_id, j.project, j.status) for j in self.values()]
            except RuntimeError:      # dict mutated during iteration
                live = ["<racy>"]
            _log("JOBS.CLEAR", "was=%r" % (live,))
            return dict.clear(self)

    ro._jobs = _WatchingDict(ro._jobs)
    _log("PROBE-ARMED", "type=%s" % type(ro._jobs).__name__)


def pytest_runtest_teardown(item):
    if not _path:
        return
    import routes_orchestrator as ro
    with ro._jobs_lock:
        live = [(j.job_id, j.project, j.status) for j in ro._jobs.values()]
    _log("TEARDOWN", "jobs_left=%r" % (live,))
