"""The systemd unit emitter + the legacy-name purge (card t_d8f13693).

Three things are pinned here, and each is a bug that already happened:

  * **the unit file is RAW text, not a console view.** ``systemd-setup.sh`` used to
    run ``hscc.py plist > UNIT_FILE``. That redirected a *rendered Rich panel* into
    a unit file: box-drawing borders, and every long assignment soft-wrapped, so
    systemd's parser silently dropped ``start-daemon`` from ``ExecStart`` (the
    supervised daemon ran the module with no subcommand) and truncated
    ``Environment=PATH=`` at the wrap. The pin is on the SHIPPED TEXT, because the
    bug lived at the redirect, not inside the generator.

  * **exactly one name is ever written.** ``emit_systemd_unit()`` writes the
    canonical unit and nothing else; the retired name exists only in the purge.

  * **the purge cannot leave zero supervised units.** ``hscc install`` enables the
    canonical unit first and only then purges; ``cmd_unit_emit`` keeps the purge
    OPT-IN so a caller cannot accidentally delete the unit that is currently
    keeping the daemon supervised.

No test here runs a real ``systemctl``: ``subprocess.run`` is injected at the
``runner=`` seam, and the unit dir is aimed at a tmp path.
"""

import io
import json
import subprocess
from pathlib import Path

import pytest

from hscc_daemon import install
from hscc_daemon.unit_name import (LEGACY_UNIT_NAME, LAUNCHD_LABEL, UNIT_CANDIDATES,
                                   UNIT_NAME)

NEW = UNIT_NAME
OLD = LEGACY_UNIT_NAME


class Recorder:
    """The ``runner=`` seam: a small systemctl --user whose state is the real disk.

    Stateful on purpose. A fake that answered ``is-enabled`` from a fixed table
    would still say ``enabled`` after the purge ran ``disable`` — so the code under
    test could "verify" a state it never produced, and the assertion would pass on
    a broken purge. Instead:

      * ``is-enabled`` answers ``not-found`` once the unit FILE is gone from
        ``unit_dir`` (that is what systemd reports for an unknown name), so
        unlinking the file is what retires the name here, exactly as on a node;
      * ``disable`` moves the word to ``disabled``, ``stop`` to ``inactive``;
      * ``words`` seeds the starting word per ``(verb, unit)`` — e.g.
        ``{("is-enabled", OLD): "enabled"}`` for a box the old installer enabled.

    Anything unseeded and absent from disk is ``not-found``.
    """

    def __init__(self, words=None, unit_dir=None):
        self.words = dict(words or {})
        self.unit_dir = Path(unit_dir) if unit_dir else None
        self.calls = []

    def _word(self, verb, unit):
        if self.unit_dir is not None and not (self.unit_dir / unit).exists():
            return "not-found"
        return self.words.get((verb, unit), "not-found")

    def __call__(self, argv, capture_output=False, text=False, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[2] if len(argv) > 2 else ""
        unit = next((a for a in argv[3:] if not a.startswith("-")), "")
        if verb in ("is-enabled", "is-active"):
            out = self._word(verb, unit)
            good = out == "active" if verb == "is-active" else out == "enabled"
            return subprocess.CompletedProcess(argv, 0 if good else 1, out + "\n", "")
        if verb == "disable":
            self.words[("is-enabled", unit)] = "disabled"
        elif verb == "stop":
            self.words[("is-active", unit)] = "inactive"
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def verbs(self):
        return [c[2] for c in self.calls if len(c) > 2]

    def mutating_verbs(self):
        """Every systemctl verb invoked that is not a pure read."""
        return [v for v in self.verbs if v not in READ_ONLY_VERBS]

    def called(self, verb, unit=None):
        for c in self.calls:
            if len(c) > 2 and c[2] == verb:
                args = [a for a in c[3:] if not a.startswith("-")]
                if unit is None or (args and args[0] == unit):
                    return True
        return False


#: systemctl verbs that change nothing. A purge of a clean host — and a dry run —
#: may issue nothing else.
READ_ONLY_VERBS = frozenset({"is-enabled", "is-active", "show", "status", "cat"})


@pytest.fixture
def unit_dir(tmp_path, monkeypatch):
    """A tmp unit dir reached through the module global, so a test can never aim
    at the operator's real ~/.config/systemd/user."""
    d = tmp_path / "systemd-user"
    d.mkdir()
    monkeypatch.setattr(install, "SYSTEMD_USER_DIR", d)
    return d


# ── the unit file is raw text ───────────────────────────────────────────────

def test_generated_unit_is_raw_parseable_text(unit_dir):
    body = install.generate_systemd_unit()
    assert body.startswith("[Unit]")
    assert "[Service]" in body and "[Install]" in body
    assert "WantedBy=default.target" in body
    for line in body.splitlines():
        # box-drawing / border characters from a rendered console
        assert not any(ch in line for ch in "\u2502\u256d\u256e\u256f\u2570\u250c\u2510\u2518\u2524"), \
            f"a rendered console line reached a unit file: {line!r}"
        assert "\x1b" not in line, "ANSI escape in a unit file"
        assert not line.startswith("[bold") and "]" not in line.split("=")[0].replace(
            "[Unit]", "").replace("[Service]", "").replace("[Install]", ""), line


def test_execstart_survives_as_one_physical_line(unit_dir):
    """The exact regression: the Rich-rendered view soft-wrapped the assignment and
    systemd read ``ExecStart=<python> -m hscc_daemon`` with ``start-daemon`` on a
    line it skipped, so the "supervised" daemon ran the module with no subcommand.
    """
    body = install.generate_systemd_unit()
    execs = [l for l in body.splitlines() if l.startswith("ExecStart=")]
    assert len(execs) == 1, execs
    assert execs[0].rstrip().endswith("start-daemon"), execs[0]
    # a continuation line would be indented — none may exist after ExecStart
    idx = body.splitlines().index(execs[0])
    rest = body.splitlines()[idx + 1]
    assert not rest.startswith(" ") and rest.strip() != "start-daemon"


def test_environment_path_is_not_truncated(unit_dir):
    body = install.generate_systemd_unit()
    paths = [l for l in body.splitlines() if l.startswith("Environment=PATH=")]
    assert len(paths) == 1, paths
    value = paths[0].split("=", 2)[2]
    assert not value.endswith(("/", ":", "-")), f"PATH= looks soft-wrapped: {value!r}"
    assert ":" in value, "PATH must carry separators; a wrapped one lost them"
    assert "venv/bin" in value


def test_cmd_unit_emit_prints_json_only_and_writes_the_canonical_name(
        unit_dir, capsys, monkeypatch):
    """What systemd-setup.sh parses: one JSON object on stdout, and exactly one
    unit file in the dir — the canonical one."""
    monkeypatch.setattr(install.subprocess, "run", Recorder())
    assert install.cmd_unit_emit(["--unit-dir", str(unit_dir), "--json"]) == 0
    out = capsys.readouterr().out

    payload = json.loads(out)                      # exactly one JSON document
    assert payload["unit_name"] == NEW and payload["legacy_name"] == OLD
    assert payload["dry_run"] is False
    assert payload["legacy"]["found"] is False, "no --purge-legacy means no purge"
    assert "\x1b" not in out and "\u2502" not in out, "the emitter is machine-readable"

    written = sorted(p.name for p in unit_dir.iterdir())
    assert written == [NEW], f"emitter wrote the wrong set: {written}"
    assert (unit_dir / NEW).read_text().startswith("[Unit]")


def test_cmd_unit_emit_is_idempotent(unit_dir, capsys, monkeypatch):
    monkeypatch.setattr(install.subprocess, "run", Recorder())
    install.cmd_unit_emit(["--unit-dir", str(unit_dir)])
    first = (unit_dir / NEW).read_text()
    capsys.readouterr()
    assert install.cmd_unit_emit(["--unit-dir", str(unit_dir)]) == 0
    assert (unit_dir / NEW).read_text() == first
    assert json.loads(capsys.readouterr().out)["changed"] is False, \
        "a re-apply must report no change, so a bootstrap step can be re-run"


def test_cmd_unit_emit_missing_dir_argument_fails_loudly(capsys):
    assert install.cmd_unit_emit(["--unit-dir"]) == 1
    assert "--unit-dir needs a directory" in capsys.readouterr().err


def test_cmd_unit_emit_unwritable_dir_is_rc1_not_a_traceback(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install.subprocess, "run", Recorder())
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        rc = install.cmd_unit_emit(["--unit-dir", str(locked / "sub")])
    finally:
        locked.chmod(0o755)
    assert rc == 1
    assert "could not write the unit file" in capsys.readouterr().err
    assert "Traceback" not in capsys.readouterr().out, \
        "a set -e caller reads rc, and stderr must stay parseable"


def test_dry_run_reports_and_writes_nothing(unit_dir, monkeypatch):
    monkeypatch.setattr(install.subprocess, "run", Recorder())
    res = install.emit_systemd_unit(str(unit_dir), dry_run=True)
    assert res["changed"] is False
    assert not (unit_dir / NEW).exists()


# ── the legacy purge ────────────────────────────────────────────────────────

def test_purge_finds_and_retires_a_live_legacy_unit(unit_dir, monkeypatch):
    """The path that had no test and crashed: a node installed by the pre-fix
    helper has the legacy unit ENABLED and ACTIVE, and the purge must disable +
    remove it. ``probe()`` returns TEXT, so any tuple-unpacking here is a
    TypeError on the operator's box — caught by mutation-probing this card.
    """
    (unit_dir / OLD).write_text("[Unit]\nDescription=legacy\n")
    rec = Recorder({("is-enabled", OLD): "enabled", ("is-active", OLD): "active"},
                   unit_dir=unit_dir)
    monkeypatch.setattr(install.subprocess, "run", rec)

    res = install.purge_legacy_systemd_unit(str(unit_dir))

    assert res["found"] is True and res["changed"] is True
    assert res["after"] == "not-found", res   # the file is gone -> name unknown
    assert rec.called("disable", OLD), "an active legacy unit must be stopped, not just unlinked"
    assert rec.called("daemon-reload")
    assert not (unit_dir / OLD).exists()
    assert "retired" in res["detail"]


def test_purge_of_a_disabled_but_installed_legacy_unit(unit_dir, monkeypatch):
    (unit_dir / OLD).write_text("[Unit]\n")
    rec = Recorder({("is-enabled", OLD): "disabled", ("is-active", OLD): "inactive"},
                   unit_dir=unit_dir)
    monkeypatch.setattr(install.subprocess, "run", rec)

    res = install.purge_legacy_systemd_unit(str(unit_dir))

    assert res["found"] is True and res["changed"] is True
    assert not rec.called("disable"), "nothing to disable — a stale FILE is still removed"
    assert not (unit_dir / OLD).exists()


def test_purge_is_a_noop_on_a_clean_host(unit_dir, monkeypatch):
    rec = Recorder(unit_dir=unit_dir)
    monkeypatch.setattr(install.subprocess, "run", rec)

    res = install.purge_legacy_systemd_unit(str(unit_dir))

    assert res["found"] is False and res["changed"] is False
    # it may LOOK (is-enabled / is-active) but it must not ACT: no disable, no
    # daemon-reload, no unlink — a no-op that reloads the daemon on every install
    # is a churn bug, and `daemon-reload` is the one verb a fake can hide behind.
    assert rec.mutating_verbs() == [], rec.calls
    assert rec.called("is-enabled", OLD), "it must actually have looked"


def test_purge_dry_run_touches_nothing(unit_dir, monkeypatch):
    (unit_dir / OLD).write_text("[Unit]\n")
    rec = Recorder({("is-enabled", OLD): "enabled", ("is-active", OLD): "active"})
    monkeypatch.setattr(install.subprocess, "run", rec)

    res = install.purge_legacy_systemd_unit(str(unit_dir), dry_run=True)

    assert res["found"] is True and res["changed"] is False
    assert "dry-run" in res["detail"]
    assert (unit_dir / OLD).exists()
    assert rec.verbs == ["is-enabled", "is-active"], rec.calls


def test_purge_reports_a_file_it_could_not_remove(unit_dir, monkeypatch):
    """An unremovable file must be REPORTED, not swallowed — the name is still live
    on that host and the operator has to know the twin may come back."""
    (unit_dir / OLD).write_text("[Unit]\n")
    monkeypatch.setattr(install.subprocess, "run",
                        Recorder({("is-enabled", OLD): "enabled"}))
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self.name == OLD:
            raise PermissionError(13, "Read-only file system")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    res = install.purge_legacy_systemd_unit(str(unit_dir))

    assert res["changed"] is False
    assert "could not remove" in res["detail"], res


# ── ordering: never zero supervised units ──────────────────────────────────

def test_install_enables_before_purging(unit_dir, monkeypatch):
    """The purge must not run before the canonical unit is up: a box whose
    ``enable --now`` fails would otherwise be left with NO supervisor."""
    calls = []
    real_run = subprocess.run

    def spy(argv, *a, **k):
        argv = list(argv)
        calls.append(" ".join(argv[1:4]))
        if argv[2:4] == ["--user", "enable"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(argv) > 2 and argv[2] in ("is-enabled", "is-active"):
            word = "enabled" if argv[2] == "is-enabled" else "active"
            if len(argv) > 3 and argv[3] == OLD:
                word = "enabled" if argv[2] == "is-enabled" else "active"
            return subprocess.CompletedProcess(argv, 0, word + "\n", "")
        return real_run(["true"], capture_output=True, text=True, timeout=5)

    monkeypatch.setattr(install.subprocess, "run", spy)
    (unit_dir / OLD).write_text("[Unit]\n")
    monkeypatch.setattr(install, "_stop_running_daemon", lambda: None)

    install._install_systemd()

    enable_at = next(i for i, c in enumerate(calls) if c.startswith("--user enable"))
    purge_at = [i for i, c in enumerate(calls)
                if c.startswith("--user disable") or c.startswith("--user is-enabled " + OLD)]
    assert purge_at, "the legacy unit must still be retired"
    assert min(purge_at) > enable_at, f"purge ran before enable: {calls}"


def test_uninstall_removes_both_names(unit_dir, monkeypatch):
    (unit_dir / NEW).write_text("[Unit]\n")
    (unit_dir / OLD).write_text("[Unit]\n")
    rec = Recorder({("is-enabled", OLD): "enabled", ("is-active", OLD): "active",
                    ("is-enabled", NEW): "enabled", ("is-active", NEW): "active"},
                   unit_dir=unit_dir)
    monkeypatch.setattr(install.subprocess, "run", rec)
    monkeypatch.setattr(install, "_stop_running_daemon", lambda: None)
    monkeypatch.setattr(install, "_console",
                        lambda **kw: install.make_console(file=io.StringIO()))

    install._uninstall_systemd()

    assert not (unit_dir / NEW).exists() and not (unit_dir / OLD).exists(), \
        "a leftover legacy unit keeps the daemon enabled at next login"
    assert rec.called("disable", NEW) and rec.called("disable", OLD)


# ── vocabulary shared with the readers ─────────────────────────────────────

def test_unit_name_vocabulary_is_ordered_and_distinct():
    assert UNIT_CANDIDATES == (NEW, OLD)
    assert NEW != OLD
    assert NEW == f"{LAUNCHD_LABEL}.service", \
        "the systemd unit must be the launchd label + .service — one name, both platforms"
    assert OLD == "hscc_daemon.service"


def test_legacy_name_is_never_written_by_any_emitter(unit_dir, monkeypatch):
    """The retired name is read-only vocabulary: it may appear in a purge, never in
    a file an emitter writes."""
    monkeypatch.setattr(install.subprocess, "run", Recorder())
    install.emit_systemd_unit(str(unit_dir))
    install.cmd_unit_emit(["--unit-dir", str(unit_dir)])
    for p in unit_dir.iterdir():
        assert p.name != OLD
        assert OLD not in p.read_text()
