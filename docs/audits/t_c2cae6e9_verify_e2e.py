"""e2e proof: converted commands emit NO ANSI on a non-tty stdout; --json paths
stay byte-identical / machine-pure. Run under BOTH interpreters the suite gates
on. Non-tty pipe: pure plain text, no \\x1b, content preserved.

    ~/.hermes/hermes-agent/venv/bin/python docs/audits/t_c2cae6e9_verify_e2e.py
    /Users/desac/miniconda3/envs/p313/bin/python docs/audits/t_c2cae6e9_verify_e2e.py
"""
import argparse
import contextlib as _contextlib
import io as _io
import json as _json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hscc-project"))

from flightdeck.commands import ingest as ingest_cmd
from flightdeck.commands import metrics as metrics_cmd
from flightdeck.commands import release as release_cmd
from flightdeck.commands import sync as sync_cmd
from flightdeck.core import registry


def no_ansi(fn):
    buf = _io.StringIO()
    with _contextlib.redirect_stdout(buf):
        fn()
    out = buf.getvalue()
    assert "\x1b[" not in out, f"ANSI[ found: {out!r}"
    assert "\x1b" not in out, f"ANSI found: {out!r}"
    return out


def _metrics_args(**kw):
    base = dict(registry="/tmp/none", json=False, project=None, since="4000s",
                cwd=None, run=None, events=None, now=None, cards=None, stderr=None)
    base.update(kw)
    return argparse.Namespace(**base)


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def _ok_run(c, repo):
    p = _Proc()
    if c[1] == "rev-list":
        p.stdout = "3"
    return p


def metric_card(cid, **kw):
    d = {"id": cid, "title": f"task {cid}", "status": "done", "board": "flightdeck",
         "branch": f"wt/{cid}", "started_at": 1000, "completed_at": 2500,
         "workspace_path": f"/repo/.worktrees/{cid}"}
    d.update(kw)
    return d


proj = registry.Project(name="flightdeck", repo="/repo", board="flightdeck")
cards = [metric_card(f"c{i}") for i in (1, 2, 3)]
events = lambda cid: [{"kind": "blocked", "created_at": 2000}]

# ---- metrics human: no ANSI, content preserved ----
out = no_ansi(lambda: metrics_cmd.cmd_metrics(
    _metrics_args(cards=cards, run=_ok_run, events=events,
                  now=lambda: 5000, since="4000s"), [proj]))
assert "first-time-pass" in out and "n=3" in out, out
print("[metrics] human: no-ANSI + content OK")

# ---- metrics --json: pure JSON, no ANSI, raw print preserved ----
json_buf = _io.StringIO()
with _contextlib.redirect_stdout(json_buf):
    rc = metrics_cmd.cmd_metrics(
        _metrics_args(cards=cards, run=_ok_run, events=events,
                      now=lambda: 5000, since="4000s", json=True), [proj])
assert rc == 0
payload = _json.loads(json_buf.getvalue())  # valid JSON => machine-pure
assert payload["reviewed"] == 3 and payload["first_time_pass"]["count"] == 3
assert "\x1b" not in json_buf.getvalue()
assert json_buf.getvalue() == _json.dumps(payload) + "\n"  # raw, no panel border
print("[metrics] --json: byte-identical raw print, no ANSI, valid JSON OK")

# ---- sync human + --json ----
import argparse as _ap
from flightdeck.commands import sync as _sync_mod


def _reg(tmp_path=None):
    return "/tmp/hscc-sync-none.yaml"


_sync_args = _ap.Namespace(
    project_cmd="sync", apply=False, json=False,
    repos=["~/dev/hscc"], _run=None, _boards={"hscc": 0}, client=object(),
    roots=None, registry=_reg(), ignore_topic=None, create_boards=False, _kdb=None)
_sync_mod.discover_repos = lambda roots=None, _run=None: _sync_args.repos
_sync_mod.discover_boards = lambda: ["hscc"]
_sync_mod.discover_topics = lambda _client=None: [sync_cmd.Topic(140, "HSCC cluster")]

out = no_ansi(lambda: sync_cmd.cmd_sync(_sync_args))
assert "MATCHED" in out and "--apply" in out, out
print("[sync] human: no-ANSI + MATCHED/--apply content OK")

_sync_args.json = True
out_json = no_ansi(lambda: sync_cmd.cmd_sync(_sync_args))
parsed = _json.loads(out_json)  # pure JSON, no panel border
assert parsed["matched"] and parsed["matched"][0]["name"] == "hscc"
assert "\x1b" not in out_json
print("[sync] --json: machine-pure raw print, no ANSI OK")

# ---- release human (dry-run plan) ----
import tempfile
import yaml

_tmp = tempfile.mkdtemp()
_repo = os.path.join(_tmp, "repo")
os.makedirs(_repo, exist_ok=True)
open(os.path.join(_repo, "VERSION"), "w").write("1.8.1\n")
open(os.path.join(_repo, "CHANGELOG.md"), "w").write(
    "# Changelog\n\n## [1.8.1] — prev\n\n### Fixed\n- x\n\n## [1.9.0] — upcoming\n\n### Changed\n- y\n")
_regp = os.path.join(_tmp, "registry.yaml")
open(_regp, "w").write(yaml.safe_dump(
    {"projects": [{"name": "acme", "repo": _repo, "verify": "true"}]}, sort_keys=False))

def _run_rel(cmd, cwd):
    p = _Proc()
    if cmd == "git rev-parse --abbrev-ref HEAD":
        p.stdout = "main\n"
    return p

rout = no_ansi(lambda: release_cmd.run(
    argparse.Namespace(project="acme", version="1.9.0", registry=_regp, run=_run_rel, apply=False),
    _regp))
assert "release plan for acme 1.9.0" in rout, rout
assert "1. bump VERSION" in rout
print("[release] human: no-ANSI + plan content OK")

# ---- ingest human ----
ROADMAP = ("# Subproject: demo\n\n## Milestone: auth-hardening <!-- id: auth-hardening -->\n"
           "status: now\n- [x] password reset restricted to self/admin\n- [ ] server-side key enforcement\n\n"
           "## Milestone: billing <!-- id: billing -->\nstatus: next\n- [x] stripe checkout wired\n- [ ] chargeback handling\n")

def fake_read_refs(project):
    return ("reference about demo\n", "ok (1 files)")
def fake_read(path):
    if path.endswith("README.md"):
        return "# demo\nreadme content\n"
    return None
class FakeGit:
    def __call__(self, c, repo):
        p = _Proc()
        p.stdout = "first commit\nsecond\n"
        return p
def ask_returns(prompt, topic_id, client=None):
    return "prose prefix\n```markdown\n" + ROADMAP + "\n```\nsuffix\n"

os.makedirs("/tmp/repo", exist_ok=True)  # ingest round-trips through a temp file IN the repo
iargs = argparse.Namespace(
    project="demo", registry="/tmp/reg.yaml", read_refs=fake_read_refs,
    read=fake_read, run=FakeGit(), client=None, ask=ask_returns,
    limit=200, timeout=300, context_dir="/tmp", ask_inline=True, apply=False, _kdb=None)
o = no_ansi(lambda: ingest_cmd.cmd_ingest(iargs, [registry.Project(name="demo", repo="/tmp/repo", topic=140)]))
assert "PROPOSED ROADMAP" in o, o
assert "auth-hardening" in o and "billing" in o, o
assert "```" not in o and "prose prefix" not in o, o
assert "- [x]" in o and "- [ ]" in o  # checklist brackets render literally
print("[ingest] human: no-ANSI + PROPOSED ROADMAP content OK")

print("\nALL E2E CHECKS PASSED")
