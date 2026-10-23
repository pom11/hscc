"""Tests for scripts/bump_formula.py — release-flow tap bump (t_a946834f).

Four layers, mirroring what the card asks for:

  1. the PURE rewrite — url/sha256 (and version, if the formula states one), the
     resource-block fence, the already-current no-op, malformed tags, malformed
     digests. No tap, no network.
  2. the tarball contract — the digest is the hash of the bytes actually fetched,
     and the tarball's own VERSION must agree with the tag (that is what
     `brew test hscc` asserts, so a mismatch is caught here, not on a user's Mac).
  3. the FLOW against a fabricated tap in tmp_path: a real git repo with a real
     local bare `origin` named homebrew-hscc, the REAL vendored guard copied in,
     the REAL ruby when present. Monkeypatched here is ONLY the network fetch —
     the hash, the VERSION read, the rewrite, ruby -c, the guard and every git
     step stay real, so these exercise the flow rather than a mock of it.
  4. the two guarantees the card names: a hash failure leaves the tap tree
     untouched, and the guard is part of the flow — a tap tree carrying a real-LAN
     line fails the bump with rc!=0 and produces NO commit.

Fixture addresses are ASSEMBLED AT RUNTIME (``_addr``), exactly as
``test_ledger_scrub.py`` does: this repo's own guard scans this very file, so a
contiguous real-shaped literal here would (correctly) block the commit.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import re
import shutil as sh
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
GUARD = SCRIPTS / "address_guard.py"
BUMP = SCRIPTS / "bump_formula.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import bump_formula as bf  # noqa: E402


def _addr(*parts):
    """Assemble a forbidden-shaped fixture address at runtime (see module docstring)."""
    return "".join(parts)


GIT_IDENTITY = {"GIT_AUTHOR_NAME": "bump-test",
                "GIT_AUTHOR_EMAIL": "bump@example.invalid",
                "GIT_COMMITTER_NAME": "bump-test",
                "GIT_COMMITTER_EMAIL": "bump@example.invalid"}


def _git(repo, args, check=True, env=None):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          env=env or dict(os.environ, **GIT_IDENTITY))
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: "
                             f"{proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout.decode("utf-8", "replace").strip()


# ── the formula shape this flow expects (the tap's real layout, prose trimmed) ──

_TEMPLATE = '''class Hscc < Formula
  include Language::Python::Virtualenv

  desc "Hermes Spark Cluster Control"
  homepage "https://github.com/pom11/hscc"
  url "https://github.com/pom11/hscc/archive/refs/tags/v{version}.tar.gz"
  sha256 "{release_sha}"
  license "MIT"

  livecheck do
    url "https://github.com/pom11/hscc/releases"
    strategy :github_latest
  end

  depends_on "python@3.13"

  resource "PyYAML" do
    url "https://files.pythonhosted.org/packages/aa/aa/pyyaml-6.0.3.tar.gz"
    sha256 "{pypi_sha}"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/bb/bb/rich-15.0.0.tar.gz"
    sha256 "{pypi_sha}"
  end

  def install
    odie "no VERSION" unless (buildpath/"VERSION").exist?
  end

  test do
    assert_equal "hscc #{{version}}", shell_output("#{{bin}}/hscc --version").strip
  end
end
'''

#: the digest the PyPI resources pin — a bump must NEVER move it
PYPI_SHA = "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
#: the release digest a fixture formula pins at the start of a test
OLD_SHA = "0746f5f8d406af344fd547f1c8daa5f5c33dbc293bb8d6a16d80b4bb88f59372"
#: a digest no fixture starts out carrying, so a rewrite to it is observable
NEW_SHA = "1" * 64


def formula_text(version="2.12.0", release_sha=OLD_SHA):
    return _TEMPLATE.format(version=version, release_sha=release_sha, pypi_sha=PYPI_SHA)


def make_tarball(path: Path, version: str) -> Path:
    """A real gzip tarball shaped like GitHub's archive: hscc-<version>/ at root."""
    root = f"hscc-{version}"
    payload = {f"{root}/VERSION": version + "\n",
               f"{root}/requirements.txt": "rich>=13,<16\n",
               f"{root}/README.md": "hscc\n"}
    with tarfile.open(path, "w:gz") as tf:
        for name, text in payload.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def tap(tmp_path):
    """A fabricated tap: git repo + the REAL vendored guard + a local bare origin.

    `origin` is named homebrew-hscc so validate_tap()'s remote check passes, and
    being local means the push leg is exercised for real without touching GitHub.
    """
    remote = tmp_path / "homebrew-hscc"
    subprocess.run(["git", "init", "--bare", "--quiet", "--initial-branch=main",
                    str(remote)], check=True, capture_output=True)
    work = tmp_path / "tap-work"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(work)],
                   check=True, capture_output=True,
                   env=dict(os.environ, **GIT_IDENTITY))
    (work / "Formula").mkdir(parents=True, exist_ok=True)
    (work / ".github").mkdir(parents=True, exist_ok=True)
    (work / "README.md").write_text("# homebrew-hscc\n\nThe hscc tap.\n",
                                    encoding="utf-8")
    (work / ".github" / "address_guard.py").write_text(
        GUARD.read_text(encoding="utf-8"), encoding="utf-8")
    _git(work, ["add", "-A"])
    _git(work, ["commit", "--quiet", "-m", "initial tap"])
    _git(work, ["push", "--quiet", "origin", "HEAD:refs/heads/main"])
    return work


def seed_formula(tap: Path, text: str) -> Path:
    f = tap / "Formula" / "hscc.rb"
    f.write_text(text, encoding="utf-8")
    _git(tap, ["add", "-A"])
    _git(tap, ["commit", "--quiet", "-m", "pin a release"])
    _git(tap, ["push", "--quiet", "origin", "HEAD:refs/heads/main"])
    return f


def peer_clone(tap: Path, name: str) -> Path:
    """A second checkout of the SAME origin (what another operator would push)."""
    url = _git(tap, ["remote", "get-url", "origin"])
    other = tap.parent / name
    subprocess.run(["git", "clone", "--quiet", url, str(other)],
                   check=True, capture_output=True,
                   env=dict(os.environ, **GIT_IDENTITY))
    return other


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Stand in for GitHub: fetch_tarball() writes a fabricated archive.

    Only the network is replaced — everything downstream of it is the real code.
    """
    class Server:
        def __init__(self):
            self.tarball = None
            self.calls: list[str] = []
            self.error = None

        def install(self, version, *, version_file=None):
            path = tmp_path / f"v{version}.tar.gz"
            make_tarball(path, version if version_file is None else version_file)
            self.tarball = path
            return path

        def fetch(self, url, dest, timeout=180.0):
            self.calls.append(url)
            if self.error:
                raise bf.CannotRun(self.error)
            if self.tarball is None:
                raise bf.CannotRun(f"no fixture archive served for {url}")
            dest.write_bytes(self.tarball.read_bytes())
            return dest

    server = Server()
    monkeypatch.setattr(bf, "fetch_tarball", server.fetch)
    return server


def env_for(monkeypatch):
    """Deterministic identity + interpreter for the child git / guard calls."""
    for k, v in GIT_IDENTITY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HSCC_ADDRESS_GUARD_PY", sys.executable)
    monkeypatch.delenv("HSCC_TAP_DIR", raising=False)
    monkeypatch.delenv("HSCC_RUBY_BIN", raising=False)


# ── 1. the pure rewrite ──────────────────────────────────────────────────────

def test_bump_rewrites_url_and_sha256_and_nothing_else():
    text = formula_text("2.12.0", OLD_SHA)
    new, changes = bf.rewrite_formula(text, "2.13.0", NEW_SHA)
    assert ('  url "https://github.com/pom11/hscc/archive/refs/tags/v2.13.0.tar.gz"'
            in new)
    assert f'  sha256 "{NEW_SHA}"' in new
    assert f'  sha256 "{OLD_SHA}"' not in new
    # exactly the two release-pinned lines moved, in place, indentation intact
    assert [(c[1], c[2]) for c in changes] == [
        ('  url "https://github.com/pom11/hscc/archive/refs/tags/v2.12.0.tar.gz"',
         '  url "https://github.com/pom11/hscc/archive/refs/tags/v2.13.0.tar.gz"'),
        (f'  sha256 "{OLD_SHA}"', f'  sha256 "{NEW_SHA}"'),
    ]
    # the PyPI resources keep BOTH their urls and their digests
    assert new.count(f'sha256 "{PYPI_SHA}"') == 2
    assert 'url "https://files.pythonhosted.org/packages/aa/aa/pyyaml-6.0.3.tar.gz"' in new
    assert 'url "https://files.pythonhosted.org/packages/bb/bb/rich-15.0.0.tar.gz"' in new
    assert new.count('sha256 "') == text.count('sha256 "')
    assert new.count("url ") == text.count("url ")
    assert len(new.split("\n")) == len(text.split("\n"))


def test_the_release_digest_rewritten_is_the_one_under_the_url():
    """The first sha256 in the file is not necessarily the release's — the fence is
    the url, and the PyPI digests below it must survive."""
    new, changes = bf.rewrite_formula(formula_text(), "2.13.0", NEW_SHA)
    assert [c[0] for c in changes] == [6, 7]          # 1-based: url line, its sha256
    assert PYPI_SHA in new


def test_rewrite_updates_an_explicit_version_line_when_the_formula_has_one():
    text = formula_text("2.12.0", OLD_SHA).replace(
        '  license "MIT"', '  version "2.12.0"\n  license "MIT"')
    new, changes = bf.rewrite_formula(text, "2.13.0", NEW_SHA)
    assert '  version "2.13.0"' in new
    # changes come back in line order: url(6), sha256(7), then the version line
    assert [(c[1].strip().split()[0], c[0]) for c in changes] == [
        ("url", 6), ("sha256", 7), ("version", 8)]


def test_rewrite_never_adds_a_version_line():
    """The tap's formula has no version line (Homebrew infers it from the url) and
    `brew audit --strict` treats a redundant one as noise — so do not invent one."""
    new, _ = bf.rewrite_formula(formula_text(), "2.13.0", NEW_SHA)
    assert not re.search(r'^\s*version\s+"', new.split("resource")[0], re.M)


def test_rewrite_is_a_noop_when_everything_already_matches():
    text = formula_text("2.13.0", NEW_SHA)
    new, changes = bf.rewrite_formula(text, "2.13.0", NEW_SHA)
    assert new == text and changes == []


@pytest.mark.parametrize("bad", ["", "deadbeef", "Z" * 64, "1" * 63, "1" * 65,
                                 "1" * 32 + " " * 32, None])
def test_rewrite_refuses_a_nonsense_digest(bad):
    with pytest.raises(bf.BumpError):
        bf.rewrite_formula(formula_text(), "2.13.0", bad)


def test_rewrite_refuses_a_formula_without_an_hscc_archive_url():
    text = formula_text().replace(
        '  url "https://github.com/pom11/hscc/archive/refs/tags/v2.12.0.tar.gz"',
        '  url "https://example.invalid/whatever.tar.gz"')
    with pytest.raises(bf.BumpError, match="no hscc release-archive url"):
        bf.rewrite_formula(text, "2.13.0", NEW_SHA)


def test_rewrite_refuses_a_formula_with_two_hscc_archive_urls():
    """Two candidate release urls = ambiguous which one pins the release; guessing
    could leave the OTHER one as what Homebrew downloads."""
    text = formula_text().replace(
        '  license "MIT"',
        '  url "https://github.com/pom11/hscc/archive/refs/tags/v2.11.0.tar.gz"\n'
        '  license "MIT"')
    with pytest.raises(bf.BumpError, match="two hscc archive urls"):
        bf.rewrite_formula(text, "2.13.0", NEW_SHA)


def test_rewrite_refuses_a_formula_with_no_release_digest():
    text = formula_text().replace(f'  sha256 "{OLD_SHA}"\n', "")
    with pytest.raises(bf.BumpError, match="no sha256 line for the release archive"):
        bf.rewrite_formula(text, "2.13.0", NEW_SHA)


def test_rewrite_never_writes_past_the_resource_fence():
    """If the release digest ever sat INSIDE a resource block, the rewrite must
    refuse rather than reach past the fence and clobber a PyPI digest."""
    text = formula_text().replace(f'  sha256 "{OLD_SHA}"\n  license "MIT"',
                                  '  license "MIT"')
    text = text.replace('  resource "PyYAML" do',
                        f'  resource "PyYAML" do\n    sha256 "{OLD_SHA}"')
    with pytest.raises(bf.BumpError, match="no sha256 line for the release archive"):
        bf.rewrite_formula(text, "2.13.0", NEW_SHA)
    assert f'sha256 "{PYPI_SHA}"' in text          # the PyPI digests were never the target


@pytest.mark.parametrize("tag", ["2.13.0", "v2.13", "v2.13.0-rc1", "vX.Y.Z", "vx", "",
                                 "v2.13.0.1", "V2.13.0", "v2.13.0 ", "latest", "v1.2",
                                 "v2.13.0/", "../v2.13.0"])
def test_non_semver_tag_is_rejected(tag):
    with pytest.raises(bf.CannotRun):
        bf.parse_tag(tag)


def test_valid_tags_and_the_documented_url_shape():
    assert bf.parse_tag("v2.13.0") == "2.13.0"
    assert bf.parse_tag("v0.1.0") == "0.1.0"
    assert bf.parse_tag("v10.20.30") == "10.20.30"
    assert bf.tarball_url("2.13.0") == \
        "https://github.com/pom11/hscc/archive/refs/tags/v2.13.0.tar.gz"


def test_formula_version_prefers_the_explicit_line_then_the_url():
    assert bf.formula_version(formula_text("2.12.0")) == "2.12.0"
    assert bf.formula_version(formula_text("2.12.0").replace(
        '  license "MIT"', '  version "9.9.9"\n  license "MIT"')) == "9.9.9"
    with pytest.raises(bf.BumpError):
        bf.formula_version("class Hscc < Formula\nend\n")


# ── 2. the tarball contract ──────────────────────────────────────────────────

def test_digest_is_the_hash_of_the_fetched_bytes(tmp_path):
    p = make_tarball(tmp_path / "v9.9.9.tar.gz", "9.9.9")
    assert bf.sha256_file(p) == sha256_of(p)
    assert bf.check_tarball(p, "9.9.9") == sha256_of(p)


def test_tarball_version_is_read_through_the_staged_root(tmp_path):
    assert bf.tarball_version(make_tarball(tmp_path / "a.tar.gz", "1.2.3")) == "1.2.3"


def test_version_file_disagreement_is_a_bump_error(tmp_path):
    """`brew test hscc` asserts `hscc #{version}` == the tarball's VERSION file, so
    a tag cut without bumping VERSION must stop here, not on a user's machine."""
    p = make_tarball(tmp_path / "v2.13.0.tar.gz", "2.12.0")
    with pytest.raises(bf.BumpError, match="VERSION file says '2.12.0'"):
        bf.check_tarball(p, "2.13.0")


def test_no_version_file_in_tarball_is_a_bump_error(tmp_path):
    p = tmp_path / "nope.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        data = b"x"
        info = tarfile.TarInfo("hscc-1.0.0/README.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(bf.BumpError, match="no VERSION file"):
        bf.check_tarball(p, "1.0.0")
    # and the documented debug hatch skips it without hiding anything else
    assert len(bf.check_tarball(p, "1.0.0", verify_version=False)) == 64


def test_a_non_gzip_file_is_not_silently_hashed(tmp_path):
    p = tmp_path / "broken.tar.gz"
    p.write_bytes(b"<html>rate limit exceeded</html>")
    with pytest.raises(bf.BumpError, match="not a readable gzip tarball"):
        bf.check_tarball(p, "2.13.0")


# ── 3. the flow ──────────────────────────────────────────────────────────────

def test_bump_against_a_fabricated_tap(tap, served, monkeypatch):
    env_for(monkeypatch)
    path = served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))

    res = bf.bump("v2.13.0", tap_dir=str(tap), push=True)
    assert res["status"] == "bumped" and res["pushed"] is True
    assert res["sha256"] == sha256_of(path)
    text = (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8")
    assert ('  url "https://github.com/pom11/hscc/archive/refs/tags/v2.13.0.tar.gz"'
            in text)
    assert f'  sha256 "{sha256_of(path)}"' in text
    assert f'sha256 "{PYPI_SHA}"' in text                    # resources untouched
    # exactly one file in the commit, and it is the formula
    assert _git(tap, ["log", "-1", "--pretty=%s"]) == \
        "hscc.rb: bump to 2.13.0 for release v2.13.0"
    assert _git(tap, ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]) == \
        "Formula/hscc.rb"
    # pushed for real: origin/main moved to the same commit
    assert _git(tap, ["rev-parse", "HEAD"]) == _git(tap, ["rev-parse", "origin/main"])
    assert served.calls == [bf.tarball_url("2.13.0")]


def test_bump_message_has_no_trailer_no_local_path_no_username(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    bf.bump("v2.13.0", tap_dir=str(tap), push=False)
    msg = _git(tap, ["log", "-1", "--pretty=%B"])
    for line in msg.strip().split("\n"):
        assert not re.match(r"^[A-Z][A-Za-z-]*:\s", line), f"trailer-shaped: {line!r}"
    for banned in ("Co-Authored-By", "Signed-off-by", "Generated with"):
        assert banned not in msg
    assert str(tap.parent) not in msg                        # no local paths
    user = os.environ.get("USER") or ""
    assert not user or user not in msg                       # no usernames


def test_already_current_is_a_noop(tap, served, monkeypatch, capsys):
    env_for(monkeypatch)
    path = served.install("2.13.0")
    seed_formula(tap, formula_text("2.13.0", sha256_of(path)))
    before = _git(tap, ["rev-parse", "HEAD"])

    res = bf.bump("v2.13.0", tap_dir=str(tap), push=True)
    assert res["status"] == "up-to-date" and res["committed"] is False
    assert "up to date" in capsys.readouterr().out
    assert _git(tap, ["rev-parse", "HEAD"]) == before
    assert not _git(tap, ["status", "--porcelain"])
    # it still paid the read-path price: the digest was re-derived from the bytes
    assert served.calls == [bf.tarball_url("2.13.0")]


def test_version_right_but_digest_wrong_is_a_bump_not_a_noop(tap, served, monkeypatch):
    """Silently accepting a stale digest would keep users pulling whatever that
    digest names — or failing the install. The digest is a pinned fact."""
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.13.0", OLD_SHA))
    res = bf.bump("v2.13.0", tap_dir=str(tap), push=False)
    assert res["status"] == "bumped"
    assert [c[1].strip().split()[0] for c in res["changes"]] == ["sha256"]


def test_dry_run_touches_nothing(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    before_text = (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8")
    before_head = _git(tap, ["rev-parse", "HEAD"])

    res = bf.bump("v2.13.0", tap_dir=str(tap), dry_run=True)
    assert res["status"] == "dry-run" and res["committed"] is False and not res["pushed"]
    assert len(res["changes"]) == 2
    assert (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8") == before_text
    assert _git(tap, ["rev-parse", "HEAD"]) == before_head
    assert not _git(tap, ["status", "--porcelain", "--untracked-files=all"])


def test_hash_failure_leaves_the_tree_untouched(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.error = "HTTP Error 404: Not Found"
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    before_text = (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8")
    before_head = _git(tap, ["rev-parse", "HEAD"])

    with pytest.raises(bf.CannotRun):
        bf.bump("v2.13.0", tap_dir=str(tap))
    assert (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8") == before_text
    assert _git(tap, ["rev-parse", "HEAD"]) == before_head
    assert not _git(tap, ["status", "--porcelain", "--untracked-files=all"])


def test_unreachable_tarball_exits_2(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.error = "connection refused"
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    assert bf.main(["--tag", "v2.13.0", "--tap", str(tap)]) == 2


def test_guard_is_part_of_the_flow(tap, served, monkeypatch):
    """A tracked tap file carrying a real-LAN line must fail the bump (rc!=0) and
    produce NO commit: the guard is a gate, not a report."""
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    (tap / "README.md").write_text(f"# tap\n\nmirror host {_addr('192', '.168.88.77')}\n",
                                   encoding="utf-8")
    _git(tap, ["add", "-A"])
    _git(tap, ["commit", "--quiet", "-m", "a leaky note"])
    _git(tap, ["push", "--quiet", "origin", "HEAD:refs/heads/main"])
    before_head = _git(tap, ["rev-parse", "HEAD"])
    before_text = (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8")

    with pytest.raises(bf.BumpError, match="address guard"):
        bf.bump("v2.13.0", tap_dir=str(tap))
    assert (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8") == before_text
    assert _git(tap, ["rev-parse", "HEAD"]) == before_head          # no commit
    assert _git(tap, ["rev-parse", "origin/main"]) == before_head   # no push
    assert bf.main(["--tag", "v2.13.0", "--tap", str(tap)]) == 1


def test_a_leak_in_the_formula_itself_is_caught_before_any_write(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.install("2.13.0")
    leaky = formula_text("2.12.0", OLD_SHA).replace(
        '  license "MIT"', f'  # mirror {_addr("100", ".64.77.8")}\n  license "MIT"')
    seed_formula(tap, leaky)
    before_head = _git(tap, ["rev-parse", "HEAD"])
    with pytest.raises(bf.BumpError, match="address guard rejected"):
        bf.bump("v2.13.0", tap_dir=str(tap), dry_run=True)
    assert _git(tap, ["rev-parse", "HEAD"]) == before_head
    assert (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8") == leaky


def test_a_dirty_tap_is_refused(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    (tap / "Formula" / "hscc.rb").write_text(formula_text("2.12.0", OLD_SHA)
                                             + "# operator scratch\n", encoding="utf-8")
    with pytest.raises(bf.BumpError, match="not clean"):
        bf.bump("v2.13.0", tap_dir=str(tap))
    assert (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8").endswith(
        "# operator scratch\n")          # operator dirt preserved, not clobbered


def test_origin_ahead_is_fast_forwarded_then_bumped(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    peer = peer_clone(tap, "peer")
    (peer / "README.md").write_text("# homebrew-hscc\n\nremote note.\n",
                                    encoding="utf-8")
    _git(peer, ["commit", "--quiet", "-am", "a remote commit"])
    _git(peer, ["push", "--quiet", "origin", "HEAD:refs/heads/main"])

    res = bf.bump("v2.13.0", tap_dir=str(tap))
    assert res["status"] == "bumped"
    assert "a remote commit" in _git(tap, ["log", "--pretty=%s", "-3"])
    assert _git(tap, ["rev-parse", "HEAD"]) == _git(tap, ["rev-parse", "origin/main"])
    # ff-only: no merge commit was invented in a public tap
    assert len(_git(tap, ["log", "--pretty=%P", "-1"]).split()) == 1


def test_a_non_ff_divergence_is_refused_and_leaves_the_tree_alone(tap, served,
                                                                  monkeypatch):
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.11.0", OLD_SHA))
    peer = peer_clone(tap, "peer2")
    _git(peer, ["reset", "--quiet", "--hard", "HEAD~1"])
    (peer / "README.md").write_text("# homebrew-hscc\n\nside note.\n",
                                    encoding="utf-8")
    _git(peer, ["commit", "--quiet", "-am", "conflicting remote note"])
    _git(peer, ["push", "--quiet", "--force", "origin", "HEAD:refs/heads/main"])

    with pytest.raises(bf.BumpError):
        bf.bump("v2.13.0", tap_dir=str(tap))
    assert (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8") == \
        formula_text("2.11.0", OLD_SHA)


def test_a_broken_formula_is_restored_and_never_committed(tap, served, monkeypatch):
    """ruby -c is a gate: force it to reject the rewrite and the tap must be
    byte-identical afterwards with no commit."""
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    before_text = (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8")
    before_head = _git(tap, ["rev-parse", "HEAD"])

    def reject(formula):
        raise bf.BumpError("ruby -c rejected the rewritten formula: syntax error")

    monkeypatch.setattr(bf, "ruby_check", reject)
    with pytest.raises(bf.BumpError, match="ruby -c"):
        bf.bump("v2.13.0", tap_dir=str(tap))
    assert (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8") == before_text
    assert _git(tap, ["rev-parse", "HEAD"]) == before_head
    assert not _git(tap, ["status", "--porcelain", "--untracked-files=all"])


def test_the_ruby_gate_runs_for_real_on_the_written_formula(tap, served, monkeypatch):
    if not (sh.which("ruby") or os.environ.get("HSCC_RUBY_BIN")):
        pytest.skip("no ruby on this host")
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    real, seen = bf.ruby_check, []
    monkeypatch.setattr(bf, "ruby_check",
                        lambda formula: (seen.append(formula), real(formula))[1])
    bf.bump("v2.13.0", tap_dir=str(tap), push=False)
    assert seen and seen[0].name == "hscc.rb" and seen[0].is_file()


def test_ruby_check_accepts_the_real_tap_formula_bytes(tmp_path):
    """The template above is a stand-in; the REAL tap formula must also survive the
    gate this script runs (same parser Homebrew uses)."""
    if not (sh.which("ruby") or os.environ.get("HSCC_RUBY_BIN")):
        pytest.skip("no ruby on this host")
    live = Path.home() / "dev" / "homebrew-hscc" / "Formula" / "hscc.rb"
    if not live.is_file():
        pytest.skip("no local tap checkout to read")
    copy = tmp_path / "hscc.rb"
    copy.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
    bf.ruby_check(copy)          # raises BumpError if it does not parse


def test_missing_ruby_is_cannot_run_never_a_silent_pass(tmp_path, monkeypatch):
    env_for(monkeypatch)
    monkeypatch.setenv("HSCC_RUBY_BIN", str(tmp_path / "no-such-ruby"))
    with pytest.raises(bf.CannotRun):
        bf.ruby_check(Path("Formula/hscc.rb"))


def test_missing_guard_is_cannot_run(tap, monkeypatch):
    env_for(monkeypatch)
    (tap / ".github" / "address_guard.py").unlink()
    monkeypatch.setattr(bf, "__file__", str(tap / "nowhere" / "bump_formula.py"))
    with pytest.raises(bf.CannotRun, match="address_guard"):
        bf.guard_path(tap)


def test_validate_tap_refuses_a_repo_that_is_not_the_tap(tmp_path):
    r = tmp_path / "not-the-tap"
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(r)],
                   check=True, capture_output=True)
    (r / "Formula").mkdir()
    (r / "Formula" / "hscc.rb").write_text("class Hscc < Formula\nend\n",
                                           encoding="utf-8")
    with pytest.raises(bf.CannotRun, match="no origin remote"):
        bf.validate_tap(r)
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin",
                    "https://github.com/pom11/some-other-repo.git"],
                   check=True, capture_output=True)
    with pytest.raises(bf.CannotRun, match="points at"):
        bf.validate_tap(r)


def test_validate_tap_refuses_a_non_git_dir(tmp_path):
    with pytest.raises(bf.CannotRun, match="not a git repository|does not exist"):
        bf.validate_tap(tmp_path / "nowhere")


def test_validate_tap_refuses_a_formula_less_repo(tmp_path):
    r = tmp_path / "empty-repo"
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(r)],
                   check=True, capture_output=True)
    with pytest.raises(bf.CannotRun, match="Formula/hscc.rb"):
        bf.validate_tap(r)


def test_resolve_tap_dir_falls_back_to_a_clone(tmp_path, monkeypatch):
    """On a release host with no tap checkout the script clones one — from the
    configured tap repo, into the cache dir it owns — and reuses it after."""
    env_for(monkeypatch)
    remote = tmp_path / "origin-homebrew-hscc"
    subprocess.run(["git", "init", "--bare", "--quiet", "--initial-branch=main",
                    str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(seed)],
                   check=True, capture_output=True,
                   env=dict(os.environ, **GIT_IDENTITY))
    (seed / "Formula").mkdir(parents=True)
    (seed / "Formula" / "hscc.rb").write_text(formula_text(), encoding="utf-8")
    _git(seed, ["add", "-A"])
    _git(seed, ["commit", "--quiet", "-m", "seed"])
    _git(seed, ["push", "--quiet", "origin", "HEAD:refs/heads/main"])

    monkeypatch.setattr(bf, "TAP_URL", str(remote))
    monkeypatch.setattr(bf, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(bf, "DEFAULT_TAP_DIR", tmp_path / "no-such-tap-dir")
    dest = bf.resolve_tap_dir(None)
    assert (dest / "Formula" / "hscc.rb").is_file()
    assert bf.resolve_tap_dir(None) == dest
    assert dest.parent == tmp_path / "cache"


def test_local_tarball_bypasses_the_network(tap, monkeypatch):
    env_for(monkeypatch)
    p = tap.parent / "v2.13.0.tar.gz"
    make_tarball(p, "2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    res = bf.bump("v2.13.0", tap_dir=str(tap), tarball=str(p), push=False)
    assert res["status"] == "bumped"
    assert res["sha256"] == sha256_of(p)
    assert f'sha256 "{sha256_of(p)}"' in (tap / "Formula" / "hscc.rb").read_text()


def test_version_file_mismatch_stops_the_bump_before_the_write(tap, served, monkeypatch):
    env_for(monkeypatch)
    served.install("2.13.0", version_file="2.12.0")     # a tag cut without VERSION
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    before_head = _git(tap, ["rev-parse", "HEAD"])
    with pytest.raises(bf.BumpError, match="VERSION file says"):
        bf.bump("v2.13.0", tap_dir=str(tap))
    assert _git(tap, ["rev-parse", "HEAD"]) == before_head
    assert not _git(tap, ["status", "--porcelain"])


def test_repeated_bumps_are_stable(tap, served, monkeypatch):
    """Release flow retries: bump, then re-run twice. One commit, one push."""
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    bf.bump("v2.13.0", tap_dir=str(tap))
    head = _git(tap, ["rev-parse", "HEAD"])
    assert bf.bump("v2.13.0", tap_dir=str(tap))["status"] == "up-to-date"
    assert bf.bump("v2.13.0", tap_dir=str(tap))["status"] == "up-to-date"
    assert _git(tap, ["rev-parse", "HEAD"]) == head
    assert _git(tap, ["rev-list", "--count", "origin/main"]) == "3"   # init+pin+bump


# ── 4. the CLI contract ──────────────────────────────────────────────────────

def test_cli_exit_codes_and_streams(tap, served, monkeypatch, capsys):
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))

    assert bf.main(["--tag", "not-a-tag", "--tap", str(tap)]) == 2
    assert bf.main(["--tag", "v2.13.0", "--tap", str(tap), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "v2.12.0 -> v2.13.0" in out and "dry run" in out
    assert bf.main(["--tag", "v2.13.0", "--tap", str(tap), "--no-push"]) == 0
    assert "committed (not pushed" in capsys.readouterr().out
    assert bf.main(["--tag", "v2.13.0", "--tap", str(tap)]) == 0
    assert "up to date" in capsys.readouterr().out
    # a failure goes to stderr with the tool's prefix; stdout stays clean
    served.error = "no fixture"
    assert bf.main(["--tag", "v2.13.0", "--tap", str(tap)]) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and "bump_formula:" in cap.err


def test_script_runs_as_a_real_program(tap, monkeypatch, tmp_path):
    """The release card calls this as a subprocess, so the PROCESS exit code is the
    contract. --tarball keeps it off the network without faking the hash."""
    env_for(monkeypatch)
    p = tmp_path / "v2.13.0.tar.gz"
    make_tarball(p, "2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    proc = subprocess.run([sys.executable, "-B", str(BUMP), "--tag", "v2.13.0",
                           "--tap", str(tap), "--tarball", str(p), "--no-push"],
                          capture_output=True, text=True,
                          env=dict(os.environ, **GIT_IDENTITY,
                                   HSCC_ADDRESS_GUARD_PY=sys.executable))
    assert proc.returncode == 0, proc.stderr
    assert "committed (not pushed" in proc.stdout
    text = (tap / "Formula" / "hscc.rb").read_text(encoding="utf-8")
    assert f'sha256 "{sha256_of(p)}"' in text and "v2.13.0.tar.gz" in text


def test_program_exit_code_is_2_when_the_tag_is_malformed(tap, monkeypatch):
    env_for(monkeypatch)
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    proc = subprocess.run([sys.executable, "-B", str(BUMP), "--tag", "banana",
                           "--tap", str(tap)], capture_output=True, text=True,
                          env=dict(os.environ, **GIT_IDENTITY))
    assert proc.returncode == 2 and "banana" in proc.stderr


def test_flow_leaves_no_stray_file_in_the_tap(tap, served, monkeypatch):
    """Importing the guard must not litter the tap with __pycache__: an untracked
    .pyc would trip this script's own clean-tree check on the next release."""
    env_for(monkeypatch)
    served.install("2.13.0")
    seed_formula(tap, formula_text("2.12.0", OLD_SHA))
    bf.bump("v2.13.0", tap_dir=str(tap), push=False)
    assert _git(tap, ["status", "--porcelain", "--untracked-files=all"]) == ""
    assert not list((tap / ".github").rglob("__pycache__"))


# ── 5. structural guarantees ─────────────────────────────────────────────────

def test_the_script_carries_no_address_pattern_of_its_own():
    """Same idiom as test_ledger_scrub.py: detection lives in the guard. This script
    imports the guard's find_in_text and shells out to the guard file — it must not
    re-derive the pattern, or the two gates can disagree about the same tree."""
    src = BUMP.read_text(encoding="utf-8")
    for frag in ("192\\.168", "100\\.6", "FORBIDDEN", "LAN_PLACEHOLDER"):
        assert frag not in src, f"bump_formula.py re-derives an address pattern: {frag}"
    assert "find_in_text" in src and "address_guard" in src
    assert "--tracked" in src        # the tap-wide gate is invoked, not just a text scan


def test_no_way_to_supply_a_digest_from_the_command_line(tmp_path, capsys):
    """The card's core requirement: the digest is computed from the archive, never
    supplied by the caller (a typed-in digest is a wrong digest waiting to happen).
    Behavioural, not textual: the CLI must REJECT such an option."""
    with pytest.raises(SystemExit) as exc:
        bf.main(["--tag", "v2.13.0", "--tap", str(tmp_path), "--sha256", "1" * 64])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    src = BUMP.read_text(encoding="utf-8")
    assert "add_argument(\"--sha256\"" not in src and "add_argument('--sha256'" not in src
    assert "hashlib.sha256" in src


def test_repo_address_guard_passes_over_the_new_files():
    importlib  # (kept explicit: the guard is loaded the same way the hook loads it)
    spec = importlib.util.spec_from_file_location("ag_for_bump_test", GUARD)
    assert spec is not None and spec.loader is not None
    ag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ag)
    for f in (BUMP, Path(__file__).resolve()):
        assert ag.find_in_text(f.read_text(encoding="utf-8")) == [], f
    out = subprocess.run([sys.executable, "-B", str(GUARD), "--tracked",
                          "--repo", str(REPO)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
