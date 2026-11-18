"""Tests for scripts/bump_cask.py — the HSCC Widgets Homebrew cask (t_aad44644).

The card's cask contract is pinned here:

  1. the PURE render — version/sha256/url, name/app/depends_on/zap, malformed
     versions and digests, unresolved template placeholders. No tap, no network.
  2. the digest contract — the sha256 written into the cask is the hash of the
     local zip that is uploaded as the release asset.
  3. the two gates — the real ``scripts/address_guard.py`` on the rendered cask,
     and real ``ruby -c`` (skipped only when this host has no ruby).
  4. the gh-api push against a FAKE ``gh`` on PATH: create, update-with-blob-sha,
     idempotent no-op, dry-run never calls gh, and a rejected PUT exits 1 without
     claiming the tap changed.
  5. the CLI — cask on stdout, supplied digest honoured, usage errors exit 2.

Fixture addresses are assembled at runtime (``_addr``) because the repo's own
guard scans this file, exactly as test_bump_formula.py does.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
CASK = SCRIPTS / "bump_cask.py"
TEMPLATE = SCRIPTS / "templates" / "hscc-widgets.cask.rb.tmpl"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import bump_cask as bc  # noqa: E402


def _addr(*parts):
    return "".join(parts)


SHA = "a" * 64
OTHER_SHA = "b" * 64


# ── the render contract ──────────────────────────────────────────────────────

def test_render_carries_the_whole_cask_contract():
    text = bc.render_cask("1.2.3", SHA)
    assert 'cask "hscc-widgets" do' in text
    assert 'version "1.2.3"' in text
    assert f'sha256 "{SHA}"' in text
    assert ('url "https://github.com/pom11/hscc/releases/download/'
            'macos-widgets-v1.2.3/HSCC-Widgets-1.2.3.zip"') in text
    assert 'name "HSCC Widgets"' in text
    assert 'app "HSCC Widgets.app"' in text
    assert 'depends_on macos: ">= :sonoma"' in text


def test_render_zaps_the_app_group_container_and_preferences():
    text = bc.render_cask("1.2.3", SHA)
    zap = text.split("zap trash:", 1)[1]
    assert "~/Library/Group Containers/group.com.hscc.ios" in zap
    assert "~/Library/Preferences/com.hscc.mac.plist" in zap
    assert "~/Library/Containers/com.hscc.mac" in zap
    assert "com.hscc.mac.widgets" in zap


def test_version_accepts_an_optional_v_and_normalizes():
    assert bc.normalize_version("v1.2.3") == "1.2.3"
    assert bc.normalize_version("1.2.3") == "1.2.3"
    assert 'version "1.2.3"' in bc.render_cask("v1.2.3", SHA)


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4", "1.2.3-rc1", "1.2.x", "", "v"])
def test_bad_versions_are_cannot_run(bad):
    with pytest.raises(bc.CannotRun):
        bc.normalize_version(bad)


@pytest.mark.parametrize("bad", ["", "A" * 64, "a" * 63, "a" * 65, "z" * 64, None])
def test_bad_digests_are_refused(bad):
    with pytest.raises(bc.CaskError):
        bc.render_cask("1.2.3", bad)


def test_asset_name_and_url_shape():
    assert bc.asset_name("1.2.3") == "HSCC-Widgets-1.2.3.zip"
    assert bc.asset_url("1.2.3") == (
        "https://github.com/pom11/hscc/releases/download/"
        "macos-widgets-v1.2.3/HSCC-Widgets-1.2.3.zip")


def test_unresolved_template_placeholder_fails_loudly():
    bad = ('cask "hscc-widgets" do\n  version "__HSCC_VERSION__"\n'
           '  sha256 "__HSCC_SHA25__"\n  url "__HSCC_URL__"\nend\n')
    with pytest.raises(bc.CaskError):
        bc.render_cask("1.2.3", SHA, template_text=bad)


def test_template_without_the_cask_header_is_rejected():
    with pytest.raises(bc.CaskError):
        bc.render_cask("1.2.3", SHA, template_text="version 1\n")


# ── digest = the uploaded zip ───────────────────────────────────────────────

def test_sha256_is_the_hash_of_the_local_zip(tmp_path):
    zip_path = tmp_path / "HSCC-Widgets-1.2.3.zip"
    payload = b"PK\x03\x04 pretend this is the release asset"
    zip_path.write_bytes(payload)
    import hashlib
    digest = bc.sha256_file(zip_path)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert f'sha256 "{digest}"' in bc.render_cask("1.2.3", digest)


def test_missing_zip_is_cannot_run(tmp_path):
    with pytest.raises(bc.CannotRun):
        bc.sha256_file(tmp_path / "absent.zip")


# ── the real guard + real ruby ───────────────────────────────────────────────

def test_template_and_rendered_cask_are_clean_for_the_real_guard():
    guard = bc.load_guard()
    assert guard.find_in_text(bc.load_template()) == []
    assert guard.find_in_text(bc.render_cask("1.2.3", SHA)) == []


def test_guard_rejects_a_runtime_assembled_address():
    guard = bc.load_guard()
    leak = _addr("192.168.88.", "42")
    with pytest.raises(bc.CaskError):
        bc.check_text(guard, f"# {leak}\n" + bc.render_cask("1.2.3", SHA))


def _ruby_bin():
    ruby = os.environ.get("HSCC_RUBY_BIN") or shutil.which("ruby")
    return ruby


def test_real_ruby_accepts_the_rendered_cask(monkeypatch):
    ruby = _ruby_bin()
    if not ruby:
        pytest.skip("no ruby on this host")
    monkeypatch.setenv("HSCC_RUBY_BIN", ruby)
    bc.ruby_check(bc.render_cask("1.2.3", SHA))


def test_ruby_rejects_a_broken_cask(monkeypatch):
    ruby = _ruby_bin()
    if not ruby:
        pytest.skip("no ruby on this host")
    monkeypatch.setenv("HSCC_RUBY_BIN", ruby)
    with pytest.raises(bc.CaskError):
        bc.ruby_check('cask "hscc-widgets" do\n  version "1.2.3\nend\n')


# ── gh api push against a fake gh ───────────────────────────────────────────

FAKE_GH = """#!/bin/bash
{ echo "CALL"; printf '%s\\n' "$@"; } >> "$GH_LOG"
if [ "$1" = "api" ] && [ "$2" = "-X" ]; then
  if [ -n "${GH_PUT_FAIL:-}" ]; then
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
  fi
  cat "$GH_PUT_RESPONSE"
else
  if [ -n "${GH_GET_BODY:-}" ]; then
    cat "$GH_GET_BODY"
  else
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
  fi
fi
"""


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "gh.log"
    put = tmp_path / "put.json"
    put.write_text(json.dumps({"commit": {"sha": "c0ffee1234567890"}}),
                   encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("GH_PUT_RESPONSE", str(put))
    monkeypatch.delenv("GH_GET_BODY", raising=False)
    monkeypatch.delenv("GH_PUT_FAIL", raising=False)
    return {"log": log, "put": put, "tmp": tmp_path}


def _calls(log: Path):
    chunks = log.read_text(encoding="utf-8").split("CALL\n")
    return [chunk.rstrip("\n").split("\n") for chunk in chunks if chunk.strip()]


def _remote_body(tmp_path, text, sha="deadbeef"):
    body = tmp_path / "get.json"
    body.write_text(json.dumps({
        "content": base64.b64encode(text.encode()).decode() + "\n",
        "sha": sha,
    }), encoding="utf-8")
    return body


def test_push_creates_the_cask_without_a_local_clone(fake_gh, monkeypatch, capsys):
    monkeypatch.delenv("GH_GET_BODY", raising=False)
    text = bc.render_cask("1.2.3", SHA)
    result = bc.push_cask(text, "1.2.3", tap_repo="pom11/homebrew-hscc",
                          branch="main")
    assert result["created"] is True and result["pushed"] is True
    calls = _calls(fake_gh["log"])
    assert len(calls) == 2
    assert calls[0][:2] == ["api", "repos/pom11/homebrew-hscc/contents/Casks/hscc-widgets.rb?ref=main"]
    put = calls[1]
    assert put[:3] == ["api", "-X", "PUT"]
    assert "repos/pom11/homebrew-hscc/contents/Casks/hscc-widgets.rb" in put
    assert "branch=main" in put
    assert "message=hscc-widgets.rb: add 1.2.3" in put
    assert not any(a.startswith("sha=") for a in put), "create must not send a blob sha"
    content = next(a for a in put if a.startswith("content=")).split("=", 1)[1]
    assert base64.b64decode(content).decode("utf-8") == text
    out = capsys.readouterr().out
    assert "created Casks/hscc-widgets.rb" in out
    assert "c0ffee123456" in out


def test_push_updates_with_the_blob_sha(fake_gh, monkeypatch, capsys):
    current = bc.render_cask("1.2.2", OTHER_SHA)
    body = _remote_body(fake_gh["tmp"], current, sha="deadbeefcafe")
    monkeypatch.setenv("GH_GET_BODY", str(body))
    text = bc.render_cask("1.2.3", SHA)
    result = bc.push_cask(text, "1.2.3", tap_repo="pom11/homebrew-hscc")
    assert result["created"] is False
    put = _calls(fake_gh["log"])[1]
    assert "sha=deadbeefcafe" in put
    assert "message=hscc-widgets.rb: bump to 1.2.3" in put
    assert "updated Casks/hscc-widgets.rb" in capsys.readouterr().out


def test_push_is_idempotent_when_the_tap_already_matches(fake_gh, monkeypatch, capsys):
    text = bc.render_cask("1.2.3", SHA)
    body = _remote_body(fake_gh["tmp"], text, sha="same")
    monkeypatch.setenv("GH_GET_BODY", str(body))
    result = bc.push_cask(text, "1.2.3", tap_repo="pom11/homebrew-hscc")
    assert result == {"status": "up-to-date", "pushed": False,
                      "tap_repo": "pom11/homebrew-hscc", "branch": "main"}
    assert len(_calls(fake_gh["log"])) == 1, "no PUT when the tap already matches"
    assert "up to date" in capsys.readouterr().out


def test_push_dry_run_never_calls_gh(fake_gh, capsys):
    result = bc.push_cask(bc.render_cask("1.2.3", SHA), "1.2.3", dry_run=True)
    assert result["pushed"] is False
    assert not fake_gh["log"].exists()
    assert "nothing pushed" in capsys.readouterr().out


def test_push_failure_is_rc1_and_does_not_claim_success(fake_gh, monkeypatch, capsys):
    monkeypatch.setenv("GH_PUT_FAIL", "1")
    with pytest.raises(bc.CaskError) as exc:
        bc.push_cask(bc.render_cask("1.2.3", SHA), "1.2.3",
                     tap_repo="pom11/homebrew-hscc")
    assert getattr(type(exc.value), "RC", 1) == 1
    assert "NOT changed" in str(exc.value)
    assert "created" not in capsys.readouterr().out


def test_remote_read_failure_that_is_not_404_is_cannot_run(fake_gh, monkeypatch, tmp_path):
    bindir = Path(os.environ["PATH"].split(os.pathsep)[0])
    gh = bindir / "gh"
    gh.write_text("#!/bin/bash\necho 'gh: Not logged in' >&2\nexit 1\n", encoding="utf-8")
    with pytest.raises(bc.CannotRun):
        bc.remote_cask(tap_repo="pom11/homebrew-hscc")


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_prints_the_cask_and_supplied_digest(tmp_path):
    zip_path = tmp_path / "HSCC-Widgets-1.2.3.zip"
    zip_path.write_bytes(b"release bytes")
    proc = subprocess.run([sys.executable, str(CASK), "--version", "1.2.3",
                           "--zip", str(zip_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert 'cask "hscc-widgets" do' in proc.stdout
    assert 'version "1.2.3"' in proc.stdout
    import hashlib
    assert hashlib.sha256(b"release bytes").hexdigest() in proc.stdout
    assert "Casks/hscc-widgets.rb" in proc.stderr


def test_cli_requires_exactly_one_digest_source(tmp_path):
    proc = subprocess.run([sys.executable, str(CASK), "--version", "1.2.3"],
                          capture_output=True, text=True)
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()


@pytest.mark.parametrize("args", [
    ["--version", "1.2", "--sha256", SHA],
    ["--version", "1.2.3", "--sha256", "nope"],
    ["--version", "1.2.3", "--zip", "/definitely/absent.zip"],
])
def test_cli_usage_and_missing_inputs_exit_2(args):
    proc = subprocess.run([sys.executable, str(CASK), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.stderr


def test_cli_push_dry_run_prints_and_pushes_nothing(tmp_path):
    proc = subprocess.run([sys.executable, str(CASK), "--version", "1.2.3",
                           "--sha256", SHA, "--push", "--dry-run"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert 'cask "hscc-widgets" do' in proc.stdout
    assert "dry run: would commit Casks/hscc-widgets.rb" in proc.stdout


def test_the_generator_carries_no_address_pattern_of_its_own():
    source = CASK.read_text(encoding="utf-8")
    assert "192\\.168" not in source
    assert "100\\.64" not in source
    assert "FORBIDDEN" not in source
