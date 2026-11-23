"""Tests for the landing page at the gh-pages root (t_107669bd).

Four things are pinned here, because each one is a promise the page makes to a
public audience:

  1. **Publishing the root must not touch /ios/ or /macos/.** Those subtrees are
     the only install path for the two apps. The plant/verify tests drive
     ``scripts/publish_site.sh`` against a throwaway remote that already has a
     marker under each subtree and fail if either marker disappears, changes, or
     if the root page is missing.
  2. **The two release scripts must not wipe the root page.** Three legs run the
     real ``macos-app/scripts/publish_macos.sh`` / ``ios-app/scripts/publish_pages.sh``
     against a throwaway remote that has a root page in it, always from a decoy
     repo: one through the publisher's default ``origin``, two through
     ``HSCC_PAGES_REMOTE`` with no ``origin`` in the fixture at all. A fourth
     asserts the shared preservation mechanism (reuse the branch, stage only your
     own subtree) is still what both scripts do, and that neither has acquired a
     destructive git operation.
  3. **The page renders site/COPY.md, not a paraphrase of it.** The card's
     sentences and every template row must appear in the page.
  4. **The command list cannot drift from the CLI** — scripts/gen_site_commands.py
     — and the page stays self-contained (no CDN, no web font, no private
     address) and usable at phone width. Two separate agreements live here: the
     fixture is the repo's own `hscc --help` (an installed bottle lags main and
     once agreed with a stale fixture, which read as a passing guard), and the
     copy card may not advertise an argument the CLI does not take — only the
     wording and the presentation of a signature are its own.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SITE = REPO / "site"
INDEX_HTML = SITE / "index.html"
SITE_CSS = SITE / "site.css"
SITE_ICON = SITE / "assets" / "hscc-icon.png"
APP_ICON = REPO / "ios-app" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon.png"
COPY_MD = SITE / "COPY.md"
PUBLISH_SITE = SCRIPTS / "publish_site.sh"
IOS_PUBLISH = REPO / "ios-app" / "scripts" / "publish_pages.sh"
MACOS_PUBLISH = REPO / "macos-app" / "scripts" / "publish_macos.sh"

HELP_FIXTURE = Path(__file__).parent / "fixtures" / "hscc_help_landing.txt"

BEGIN = "<!-- COMMANDS:BEGIN"
END = "<!-- COMMANDS:END -->"


# ── helpers ──────────────────────────────────────────────────────────────────

def git(*args, cwd=None, check=True) -> str:
    """git with a fixed identity, so the test never depends on the operator."""
    out = subprocess.run(
        ["git", "-c", "user.name=site-test", "-c", "user.email=site@test.invalid",
         *args],
        cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False,
    )
    if check and out.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({out.returncode}): {out.stderr.strip()}"
        )
    return out.stdout


def make_remote_with_pages(tmp_path: Path, pages: dict[str, str],
                           name: str = "remote.git") -> Path:
    """A bare 'remote' whose gh-pages branch already holds `pages`.

    This is the state the task calls CRITICAL: /ios/ and /macos/ are already
    being served when the root page is published.

    `name` exists so one test can hold two independent remotes — the real one
    and the decoy a publisher must not be able to reach (decoy_caller_repo).
    """
    bare = tmp_path / name
    bare.mkdir()
    git("init", "--quiet", "--bare", str(bare))
    seed = tmp_path / f"{name}.seed"
    seed.mkdir()
    git("init", "--quiet", str(seed))
    for rel, body in pages.items():
        target = seed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "--quiet", "-m", "seed gh-pages", cwd=seed)
    git("push", "--quiet", str(bare), "HEAD:refs/heads/gh-pages", cwd=seed)
    return bare


def remote_files(bare: Path, branch: str = "gh-pages") -> dict[str, bytes]:
    """Every tracked path on the remote branch, with its raw bytes."""
    names = git("ls-tree", "-r", "--name-only", branch, cwd=bare).split()
    out: dict[str, bytes] = {}
    for n in names:
        proc = subprocess.run(
            ["git", "show", f"{branch}:{n}"], cwd=str(bare),
            capture_output=True, check=True,
        )
        out[n] = proc.stdout
    return out


def b(text: str) -> bytes:
    return text.encode("utf-8")


def run_publish(bare: Path, env_extra: dict[str, str] | None = None):
    env = {"HSCC_PAGES_REMOTE": str(bare), "HSCC_PAGES_BRANCH": "gh-pages"}
    env.update(env_extra or {})
    import os
    full = {**os.environ, **env}
    # cwd is the tmp dir, NOT the repo: publish_site.sh pins every git call to
    # its own $REPO_ROOT, so running it from somewhere else is the point (see
    # decoy_caller_repo for what an unpinned call would do).
    return subprocess.run(
        ["bash", str(PUBLISH_SITE)], capture_output=True, text=True,
        cwd=str(bare.parent), env=full,
    )


# ── 1. the root publish preserves /ios/ and /macos/ ──────────────────────────

PLANTED = {
    ".nojekyll": "",
    "ios/index.html": "<!doctype html><title>ios install</title>",
    "ios/manifest.plist": "<plist>DO NOT TOUCH ME</plist>",
    "ios/keep-marker.txt": "ios-marker-9137",
    "ios/icons/icon-57.png": "not-a-real-png-but-must-survive",
    "macos/index.html": "<!doctype html><title>macos install</title>",
    "macos/appcast.xml": "<rss>DO NOT TOUCH ME</rss>",
    "macos/keep-marker.txt": "macos-marker-4820",
}


def test_publish_site_keeps_every_ios_and_macos_file(tmp_path):
    """Plant/verify: markers under /ios/ and /macos/ survive a root publish."""
    bare = make_remote_with_pages(tmp_path, PLANTED)

    proc = run_publish(bare)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = remote_files(bare)
    for rel, body in PLANTED.items():
        assert rel in after, f"{rel} was deleted by the root publish"
        assert after[rel] == b(body), f"{rel} was rewritten by the root publish"
    assert after["ios/keep-marker.txt"] == b("ios-marker-9137")
    assert after["macos/keep-marker.txt"] == b("macos-marker-4820")

    # ...and the root page is actually there, with the repo's own content.
    assert after["index.html"] == INDEX_HTML.read_bytes()
    assert after["site.css"] == SITE_CSS.read_bytes()
    assert after["assets/hscc-icon.png"] == SITE_ICON.read_bytes()
    assert after["ios/icons/icon-57.png"] == b("not-a-real-png-but-must-survive")
    assert ".nojekyll" in after


def test_publish_site_creates_the_branch_when_there_is_none(tmp_path):
    """No branch means nothing to preserve — but the page still has to land."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git("init", "--quiet", "--bare", str(bare))

    proc = run_publish(bare)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    after = remote_files(bare)
    assert "index.html" in after and "site.css" in after
    # It must not have invented the app subtrees either.
    assert not [p for p in after if p.startswith(("ios/", "macos/"))]


def test_publish_site_twice_is_stable_and_still_preserves(tmp_path):
    bare = make_remote_with_pages(tmp_path, PLANTED)
    first = run_publish(bare)
    assert first.returncode == 0, first.stdout + first.stderr
    mid = remote_files(bare)

    second = run_publish(bare)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "no change to publish" in second.stdout
    assert remote_files(bare) == mid


def test_publish_site_refuses_a_page_that_loads_app_subtree_assets(tmp_path):
    """The self-containment guard fires, and it publishes nothing."""
    planted = dict(PLANTED)
    planted["index.html"] = "<html><body><img src=\"ios/icons/icon-57.png\">"
    bare = make_remote_with_pages(tmp_path, planted)

    fake_site = tmp_path / "site"
    shutil.copytree(SITE, fake_site)
    page = fake_site / "index.html"
    page.write_text(page.read_text(encoding="utf-8")
                    + '<img src="macos/appcast.xml">\n', encoding="utf-8")

    proc = run_publish(bare, {"HSCC_SITE_DIR": str(fake_site)})
    assert proc.returncode != 0
    assert "subtree this" in (proc.stdout + proc.stderr) or "self-contained" in (
        proc.stdout + proc.stderr)
    # The failed run left the remote exactly as it was.
    assert remote_files(bare) == {k: b(v) for k, v in planted.items()}


def test_publish_site_script_never_uses_a_destructive_git_operation():
    """The guard is cheap enough to also just be forbidden in the script."""
    text = PUBLISH_SITE.read_text(encoding="utf-8")
    # The header comments say out loud what the script refuses to do, so scan
    # only the lines that can actually run.
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for banned in ("git rm", "git clean", "push --force", "push -f",
                   "git checkout -B", "rm -rf \"$PAGES_DIR/"):
        assert banned not in code, f"publish_site.sh runs {banned!r}"
    assert 'add -- "${ROOT_FILES[@]}" "${ROOT_DIRS[@]}"' in text, (
        "publish_site.sh must stage its own root paths, not `git add -A`")


# ── 2. the release scripts must keep the root page ───────────────────────────

def _stub_gh(tmp_path: Path) -> Path:
    """A `gh` that always succeeds: these tests must never reach GitHub."""
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "gh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub / "gh").chmod(0o755)
    return stub


def _publisher_env(tmp_path: Path, pages_remote: Path | None = None) -> dict[str, str]:
    """Environment a release leg runs a publisher under.

    `gh` is stubbed so no leg can reach GitHub, and TMPDIR stays inside the test
    so the throwaway worktree belongs to it. With `pages_remote` the leg aims the
    publisher at that bare repo through HSCC_PAGES_REMOTE — the same knob
    scripts/publish_site.sh has always had — rather than naming the fixture
    remote `origin` to appease the script.
    """
    import os
    env = {**os.environ,
           "PATH": f"{_stub_gh(tmp_path)}{os.pathsep}{os.environ['PATH']}",
           "TMPDIR": str(tmp_path)}
    if pages_remote is not None:
        env["HSCC_PAGES_REMOTE"] = str(pages_remote)
    return env


def assert_no_origin_remote(root: Path) -> None:
    """A leg that passes HSCC_PAGES_REMOTE is vacuous if `origin` also exists."""
    remotes = git("remote", cwd=root).split()
    assert "origin" not in remotes, (
        f"{root} has an `origin` remote again, so this leg can no longer tell "
        "whether the publisher followed HSCC_PAGES_REMOTE or hardcoded origin")


DECOY_MARKER = "decoy-must-never-ship.txt"


def decoy_caller_repo(tmp_path: Path) -> Path:
    """A *different*, already-fetched repository to invoke a publisher from.

    The publishers have to run from here, not from their own repo, for the leg
    to mean anything. A `git ls-remote origin` / `git fetch origin` that is not
    pinned with `-C "$REPO_ROOT"` resolves the remote and writes FETCH_HEAD in
    whichever repo owns the current working directory — so an unpinned publisher
    either dies on "fatal: invalid reference 'FETCH_HEAD'" here, or, when the
    caller's repo has a FETCH_HEAD lying around (as this one does), feeds the
    worktree the CALLER's commit and pushes it onto the branch that serves the
    install pages. DECOY_MARKER is how the second outcome gets caught.
    """
    make_remote_with_pages(tmp_path, {DECOY_MARKER: "decoy-marker-7731"},
                           name="decoy.git")
    caller = tmp_path / "caller"
    caller.mkdir()
    git("init", "--quiet", str(caller))
    (caller / "unrelated.txt").write_text("unrelated work\n", encoding="utf-8")
    git("add", "-A", cwd=caller)
    git("commit", "--quiet", "-m", "unrelated work", cwd=caller)
    git("remote", "add", "origin", str(tmp_path / "decoy.git"), cwd=caller)
    git("fetch", "--quiet", "origin", "gh-pages", cwd=caller)   # leaves FETCH_HEAD
    return caller


def _macos_release_fixture(tmp_path: Path, bare: Path, *,
                           use_origin_remote: bool = True) -> tuple[Path, list[str]]:
    """A throwaway repository holding the real publish_macos.sh, + its argv.

    Committed, because the branch-missing path forks off HEAD.

    `use_origin_remote` picks which half of the pages-remote knob a leg exercises.
    True leaves the production setup — a remote literally named `origin`, and no
    env var, which is what `release.sh` and RELEASING.md run with. False leaves
    the repo with NO remote at all, so the publisher has to be told where to
    publish through HSCC_PAGES_REMOTE and a hardcoded `origin` would fail loudly
    rather than land somewhere nobody is looking.
    """
    root = tmp_path / "repo"
    (root / "macos-app" / "scripts").mkdir(parents=True)
    (root / "macos-app" / "site").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    shutil.copy(MACOS_PUBLISH, root / "macos-app" / "scripts" / "publish_macos.sh")
    shutil.copy(REPO / "macos-app" / "site" / "index.html",
                root / "macos-app" / "site" / "index.html")
    for name in ("apps_release.py", "changelog_fragments.py"):
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    zip_path = tmp_path / "HSCC-Widgets-9.9.9.zip"
    zip_path.write_bytes(b"zip-bytes-for-the-length-field")

    git("init", "--quiet", str(root))
    git("add", "-A", cwd=root)
    git("commit", "--quiet", "-m", "release fixture", cwd=root)
    if use_origin_remote:
        git("remote", "add", "origin", str(bare), cwd=root)
    return root, ["9.9.9", "999", str(zip_path), "dummysignature"]


def test_macos_release_script_keeps_the_root_page(tmp_path):
    """Behavioural: run the real publish_macos.sh against a throwaway remote.

    `gh` is stubbed (the script only needs `gh auth status` to succeed) so no
    GitHub call happens; everything else — the worktree, the subtree staging,
    the push — is the production code path. And it runs from a DECOY repo, so a
    fetch that is not pinned to the script's own repo cannot slip through.

    This leg deliberately keeps the DEFAULT setup — the fixture's remote is named
    `origin` and no HSCC_PAGES_REMOTE is passed — because that is exactly what
    `release.sh` and RELEASING.md run today. The knob itself is the other two legs.
    """
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed; the script's tool probe would abort")

    planted = dict(PLANTED)
    planted["index.html"] = "<!doctype html><title>the landing page</title>"
    bare = make_remote_with_pages(tmp_path, planted)
    root, args = _macos_release_fixture(tmp_path, bare)

    caller = decoy_caller_repo(tmp_path)
    env = _publisher_env(tmp_path)
    proc = subprocess.run(
        ["bash", str(root / "macos-app" / "scripts" / "publish_macos.sh"), *args],
        capture_output=True, text=True, cwd=str(caller), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = remote_files(bare)
    assert DECOY_MARKER not in after, (
        "publish_macos.sh read the CALLER's repo (cwd) instead of its own: "
        "an unpinned `git fetch` writes FETCH_HEAD into whatever repo owns the "
        "cwd, and the worktree was built from that commit")
    assert after["index.html"] == b(planted["index.html"]), (
        "publish_macos.sh rewrote or dropped the landing page at the root")
    assert after["ios/keep-marker.txt"] == b("ios-marker-9137")
    assert after["macos/keep-marker.txt"] == b("macos-marker-4820")
    # The release itself landed.
    assert after["macos/index.html"].startswith(b("<!doctype html>"))
    assert b("9.9.9") in after["macos/appcast.xml"]


def test_macos_release_script_branch_missing_path_survives_a_lingering_branch(tmp_path):
    """The orphan path, which the happy leg above can never reach.

    It holds two hazards. `git checkout --orphan gh-pages` is fatal if the
    repository holds ANY local branch of that name — a developer's, or a stale
    one from before this script existed — even though the remote has no
    gh-pages at all. And the path empties the tree, so when it does run it must
    publish only its own subtree: with no branch there is nothing of anyone
    else's to preserve, and it must not invent any either.

    It is also the CI/mirror setup: the publisher gets `HSCC_PAGES_REMOTE` and the
    fixture has no `origin` remote at all, which is the whole point of the knob —
    publishing somewhere that is not where the checkout was cloned from.
    """
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed; the script's tool probe would abort")

    bare = tmp_path / "empty.git"               # a remote with NO gh-pages branch
    bare.mkdir()
    git("init", "--quiet", "--bare", str(bare))
    root, args = _macos_release_fixture(tmp_path, bare, use_origin_remote=False)
    # The hazard itself: a local branch that has nothing to do with the remote.
    git("branch", "gh-pages", cwd=root)
    assert_no_origin_remote(root)

    caller = decoy_caller_repo(tmp_path)
    env = _publisher_env(tmp_path, pages_remote=bare)
    proc = subprocess.run(
        ["bash", str(root / "macos-app" / "scripts" / "publish_macos.sh"), *args],
        capture_output=True, text=True, cwd=str(caller), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "creating orphan gh-pages" in proc.stdout, (
        "the branch-missing path was not taken; the leg proves nothing")

    after = remote_files(bare)
    assert DECOY_MARKER not in after
    assert after["macos/index.html"].startswith(b("<!doctype html>"))
    assert b("9.9.9") in after["macos/appcast.xml"]
    # Nothing else was invented: no root page, no sibling subtree.
    assert "index.html" not in after
    assert not [p for p in after if p.startswith("ios/")]
    # The throwaway orphan branch is gone, and the lingering one is untouched.
    branches = git("branch", "--format=%(refname:short)", cwd=root).split()
    assert [b for b in branches if b.startswith("hscc-macos-publish-")] == []
    assert "gh-pages" in branches, (
        "the release deleted a local branch that was never its own")


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="publish_pages.sh renders icons with sips (macOS)")
def test_ios_release_script_keeps_the_root_page(tmp_path):
    """Same proof for the iOS leg: a real publish, a throwaway remote.

    The iOS publisher writes the most files of the three (page, manifest,
    versioned manifests, notes, feed, three icon sizes), so it is the leg most
    likely to grow a `git add -A` one day. The throwaway remote is passed as
    HSCC_PAGES_REMOTE and the fixture has no `origin` at all, so this leg is the
    CI/mirror case: the site lands somewhere the checkout was not cloned from.
    """
    for tool in ("gh", "sips", "unzip"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed; the script's probe would abort")

    root = tmp_path / "repo"
    (root / "ios-app" / "scripts").mkdir(parents=True)
    (root / "ios-app" / "site").mkdir(parents=True)
    (root / "ios-app" / "Assets.xcassets" / "AppIcon.appiconset").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "apps").mkdir(parents=True)
    shutil.copy(IOS_PUBLISH, root / "ios-app" / "scripts" / "publish_pages.sh")
    shutil.copy(REPO / "ios-app" / "scripts" / "make_manifest.py",
                root / "ios-app" / "scripts" / "make_manifest.py")
    shutil.copy(REPO / "ios-app" / "site" / "index.html",
                root / "ios-app" / "site" / "index.html")
    shutil.copy(APP_ICON, root / "ios-app" / "Assets.xcassets"
                / "AppIcon.appiconset" / "AppIcon.png")
    for name in ("apps_release.py", "changelog_fragments.py"):
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    (root / "apps" / "BUILD_NUMBER").write_text("999\n", encoding="utf-8")
    ipa = tmp_path / "HSCC-9.9.9.ipa"
    ipa.write_bytes(b"fake-ipa-bytes")

    planted = dict(PLANTED)
    planted["index.html"] = "<!doctype html><title>the landing page</title>"
    bare = make_remote_with_pages(tmp_path, planted)

    git("init", "--quiet", str(root))
    git("add", "-A", cwd=root)
    git("commit", "--quiet", "-m", "release fixture", cwd=root)
    assert_no_origin_remote(root)

    caller = decoy_caller_repo(tmp_path)
    env = _publisher_env(tmp_path, pages_remote=bare)
    proc = subprocess.run(
        ["bash", str(root / "ios-app" / "scripts" / "publish_pages.sh"),
         "9.9.9", str(ipa)],
        capture_output=True, text=True, cwd=str(caller), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = remote_files(bare)
    assert DECOY_MARKER not in after, (
        "publish_pages.sh read the CALLER's repo (cwd) instead of its own: "
        "an unpinned `git fetch` writes FETCH_HEAD into whatever repo owns the "
        "cwd, and the worktree was built from that commit")
    assert after["index.html"] == b(planted["index.html"]), (
        "publish_pages.sh rewrote or dropped the landing page at the root")
    assert after["macos/keep-marker.txt"] == b("macos-marker-4820")
    assert after["ios/keep-marker.txt"] == b("ios-marker-9137")
    # The release itself landed.
    assert after["ios/index.html"].startswith(b("<!doctype html>"))
    assert b("9.9.9") in after["ios/version.json"]
    assert "ios/manifests/9.9.9.plist" in after


@pytest.mark.parametrize("script", [IOS_PUBLISH, MACOS_PUBLISH],
                         ids=["ios", "macos"])
def test_release_scripts_share_the_preservation_mechanism(script):
    """Static guard for the mechanism both release legs rely on.

    Reusing an existing gh-pages branch and staging only your own subtree is
    what keeps the OTHER publishers' files alive. If either half of that is
    edited away — an orphan checkout that no longer checks for the branch, a
    `git add -A`, a `git clean` — the root page (and the other app's subtree)
    starts getting wiped on release.
    """
    text = script.read_text(encoding="utf-8")
    # The header comments say out loud what the scripts refuse to do, so the
    # pins below scan only the lines that can actually run.
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))

    assert 'git -C "$REPO_ROOT" ls-remote --exit-code --heads "$REMOTE" gh-pages' in code, (
        f"{script.name} no longer checks whether gh-pages exists on $REMOTE "
        "before choosing the orphan path")
    assert "reusing existing gh-pages" in text

    # The trap this card exists for: a git call without `-C "$REPO_ROOT"` reads
    # and writes the repository that owns the CWD, so whoever invoked the
    # release gets to decide which commit lands on the install branch.
    for verb in ("ls-remote", "fetch"):
        lines = [l for l in code.splitlines() if verb in l]
        assert lines, f"{script.name} no longer does a {verb} of the pages branch"
        for line in lines:
            assert '-C "$REPO_ROOT"' in line, (
                f'{script.name}: `{line.strip()}` resolves the remote and writes '
                'FETCH_HEAD in the CALLER\'s repo, not this one — pin it with '
                '-C "$REPO_ROOT"')

    # ...and every one of those calls names $REMOTE instead of baking a remote
    # into the script. HSCC_PAGES_REMOTE (default origin) is what lets a CI run or
    # a mirror release publish somewhere other than where the checkout was cloned
    # from — the knob scripts/publish_site.sh has always had, and the reason these
    # legs can aim the publisher at a throwaway remote instead of at `origin`.
    assert 'REMOTE="${HSCC_PAGES_REMOTE:-origin}"' in code, (
        f"{script.name} must read HSCC_PAGES_REMOTE with `origin` as the default: "
        "that default is what release_adhoc.sh, release.sh and RELEASING.md rely "
        "on by setting nothing")
    for verb in ("ls-remote", "fetch", "push"):
        lines = [l for l in code.splitlines() if re.search(rf"\bgit\b.*\b{verb}\b", l)]
        assert lines, f"{script.name} no longer does a {verb} of the pages branch"
        for line in lines:
            assert '"$REMOTE"' in line, (
                f'{script.name}: `{line.strip()}` hardcodes which remote it '
                'publishes to — use "$REMOTE" so a mirror or a CI release can '
                "redirect it without rewriting the remote of the checkout")

    assert 'worktree add --quiet --detach "$PAGES_DIR" FETCH_HEAD' in code
    assert "checkout --quiet -B gh-pages" not in code, (
        f"{script.name} must stay detached and push HEAD:gh-pages; `checkout -B "
        "gh-pages` resets, or fails on, a gh-pages branch another worktree holds")
    assert 'push --quiet "$REMOTE" HEAD:gh-pages' in code

    assert re.search(r'TMP_BRANCH="hscc-[a-z]+-publish-\$\$-\$\{RANDOM\}"', code), (
        f"{script.name} must create the orphan on a unique throwaway name: any "
        "local gh-pages branch makes `--orphan gh-pages` fatal")
    assert re.search(r'checkout --quiet --orphan "\$TMP_BRANCH"', code), (
        f"{script.name} does not check out the throwaway orphan branch")
    assert 'git -C "$REPO_ROOT" branch -D "$TMP_BRANCH"' in code, (
        f"{script.name} must delete its throwaway orphan branch on cleanup")

    before_orphan = text.split("git rm -rf")[0]
    assert "checkout --quiet --orphan" in before_orphan, (
        f"{script.name}: the orphan checkout must sit in the branch-missing "
        "else-branch, before the tree is emptied")

    adds = re.findall(r"git -C \"\$PAGES_DIR\" add ([^\n]+)", text)
    assert adds, f"{script.name} stages nothing — how does it publish?"
    subtree = script.parent.parent.name          # ios-app / macos-app
    own = subtree.split("-")[0]                  # ios / macos
    for add in adds:
        assert own in add, f"{script.name} stages {add!r}, not its own subtree"
        assert "-A" not in add and "." not in add.replace(".nojekyll", "")

    for banned in ("git clean", "push --force", "push -f", "push --delete"):
        assert banned not in text, f"{script.name} contains {banned!r}"


# ── 3. the page renders the copy card ────────────────────────────────────────

def normalize(text: str) -> str:
    """Compare copy without markup noise.

    `**bold**` and `backticks` are markdown conventions, `—` is how the card
    separates a bullet's lead-in from its explanation, and the page renders
    both as structure (a card title, a <code> span) rather than characters.
    Case is ignored for the same reason: a card title may start a sentence.
    Markdown links keep their visible text and drop their target; an autolink
    (<https://…>) has the URL as its visible text, so it keeps the URL — which
    is what the page must show for those two links.
    """
    text = html_mod.unescape(text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)      # [text](url)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)          # <url>
    text = re.sub(r"<[^>]+>", " ", text)                       # html tags
    text = text.replace("**", "").replace("`", "").replace("—", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip().lower()


@pytest.fixture(scope="module")
def page_text() -> str:
    return normalize(INDEX_HTML.read_text(encoding="utf-8"))


def copy_sections() -> dict[str, str]:
    """The numbered copy sections of site/COPY.md, keyed by their heading.

    Split on EVERY `## ` heading so the trailing "REVIEW notes" block (which is
    reviewer text, not page copy) cannot leak into section 7.
    """
    text = COPY_MD.read_text(encoding="utf-8")
    parts = re.split(r"^## ([^\n]+)$", text, flags=re.M)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        if re.match(r"\d+\.", parts[i]):
            out[parts[i].strip()] = parts[i + 1]
    return out


def copy_bullets(section: str) -> list[str]:
    """Each `- ...` bullet of a section, continuations joined."""
    lines = section.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("- "):
            out.append(line[2:].strip())
        elif line.startswith("  ") and out:
            out[-1] += " " + line.strip()
        elif not line.strip():
            continue
    return out


def test_every_copy_bullet_appears_in_the_page(page_text):
    sections = copy_sections()
    wanted: list[str] = []
    for key in ("1. What it is (ELI5)", "2. Why", "3. What it does",
                "6. How it looks"):
        assert key in sections, f"site/COPY.md no longer has section {key!r}"
        wanted += copy_bullets(sections[key])
    assert len(wanted) >= 15, "the copy card changed shape; teach this test"

    missing = [w for w in wanted if normalize(w) not in page_text]
    assert not missing, "these copy lines are not in the page verbatim: " + \
        "\n  ".join(missing)


def test_the_copy_prose_of_the_other_sections_appears(page_text):
    sections = copy_sections()
    for key in ("4. Templates", "7. Get it"):
        body = sections[key]
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            # Tables, fenced blocks and rules have their own tests below.
            if not para or para.startswith(("|", "```", "<!--", "`hscc")):
                continue
            if set(para) <= {"-"}:      # a `---` rule carries no copy
                continue
            if para.startswith("## "):
                continue
            assert normalize(para) in page_text, (
                f"section {key!r} paragraph is not in the page: {para[:70]}...")


def test_every_shipped_template_row_appears_in_the_page(page_text):
    sections = copy_sections()
    rows = [r for r in sections["4. Templates"].splitlines()
            if r.startswith("| `")]
    assert len(rows) == 23, (
        f"expected the 23 shipped templates, found {len(rows)} — the copy card "
        "changed and the page table did not follow")
    for row in rows:
        name, desc = [c.strip() for c in row.strip().strip("|").split("|")[0:2]]
        line = normalize(f"{name} {desc}")
        assert line in page_text, f"template row not rendered verbatim: {row[:80]}"


def test_install_commands_are_the_real_ones(page_text):
    for line in ("brew tap pom11/hscc", "brew install hscc",
                 "hscc bootstrap --yes",
                 "brew install --cask pom11/hscc/hscc-widgets"):
        assert normalize(line) in page_text, f"install line missing: {line}"


def test_the_ad_hoc_gate_is_stated_next_to_the_ios_link(page_text):
    """REVIEW line from the copy card: the page must not imply self-serve."""
    assert "ios/" in INDEX_HTML.read_text(encoding="utf-8")
    assert normalize("It's an Ad Hoc build, so your device has to be registered "
                     "with the maintainer first") in page_text


# ── 4. commands cannot drift, page stays self-contained ──────────────────────

def test_page_command_region_matches_the_generated_one():
    """The committed list is what the real CLI generates, byte for byte."""
    import gen_site_commands as g

    assert g.main(["--help-file", str(HELP_FIXTURE), "--page", str(INDEX_HTML),
                   "--quiet"]) == 0, "site/index.html drifted from `hscc --help`"


# ── a placeholder must be escaped exactly once ───────────────────────────────

def test_no_double_escaped_entity_in_any_shipped_site_file():
    """`&amp;lt;host&amp;gt;` prints the literal text `&lt;host&gt;`.

    The resume example did exactly that, and every gate in this file stayed
    green while it did: the page matched the copy card, the copy card matched
    the CLI, and the generator agreed with itself. What was wrong was a markup
    step no one reads as a claim — the generator escaped the card's prose and
    then escaped the captured `code` span again on top of it. So the only
    guard for it is to look for the double escape itself, in every text file
    the site publishes.
    """
    offenders = []
    for path in sorted(SITE.rglob("*")):
        if not path.is_file() or path.suffix in {".png", ".jpg"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for entity in ("&amp;lt;", "&amp;gt;", "&amp;amp;", "&amp;quot;",
                       "&amp;#"):
            if entity in text:
                offenders.append(f"{path.relative_to(SITE)}: {entity}")
    assert not offenders, ("double-escaped HTML entity in site/ — the browser "
                           "will show the entity text: "
                           + ", ".join(offenders))
    # The one line this test was written for, asserted as published: one
    # escape, so the reader sees `<host>` like its sibling bullet.
    assert "<code>hscc node resume &lt;host&gt;</code>" in INDEX_HTML.read_text(
        encoding="utf-8"), "the resume example lost its single-escaped placeholder"


def test_a_placeholder_in_prose_escapes_exactly_once():
    """The generator half, at the function that broke.

    `inline()` is what hands the copy card's prose to the page. A placeholder
    inside a `code` span has to arrive as `&lt;host&gt;` — escaped once, so the
    browser prints `<host>` — and prose still has to be escaped at all, which
    is the other way a per-segment rewrite could go wrong.
    """
    import gen_site_commands as g

    rendered = g.inline("then `hscc node resume <host>` starts it back up")
    assert rendered == ("then <code>hscc node resume &lt;host&gt;</code> "
                        "starts it back up"), rendered
    assert "&amp;" not in rendered, rendered
    assert g.inline("`a & b` < c") == "<code>a &amp; b</code> &lt; c"


# ── what the fixture is allowed to be an oracle for (t_88d896f3) ────────────
#
# The fixture used to be checked against one thing: the installed bottle. That
# comparison cannot fail in the way it was written to catch. Measured on this
# host at `ce8e87f3`: `/opt/homebrew/bin/hscc` reports `hscc 2.15.13`, the same
# version string as the repo's `VERSION`, and still prints `notify <message>`
# with no `main` line at all — while `hscc_daemon/hscc.py` publishes
# `notify <title> <body>` and `main start|status|wakeups`. The fixture was
# `notify <message>` with no `main` line either. Two stale things agreeing is
# a green light, not a guard, and the drift it let through is the one the page
# carried: the public page advertised a `notify` argument the CLI had already
# renamed, and the missing `main` verb never appeared at all.
#
# So the fixture's oracle is now the repo's own help — the only tree the page
# can be held to at HEAD. The bottle stays checked, but only for what it can
# honestly answer, and it says so out loud when it cannot.

def _parse(text: str) -> list[dict]:
    import gen_site_commands as g

    return g.parse_help(text)


def _render_repo_help() -> str:
    """The repo's own ``hscc --help``, as an operator would see it.

    Same posture ``scripts/hscc_cli_help_audit.py``'s ``Prober._env`` uses for
    every help probe — sandboxed HOME, 80 columns, no colour, the repo first on
    PYTHONPATH — because that is the posture the help-surface gate measures in.
    The Prober itself is not instantiated: it holds ports, seeds canaries and
    arms a tripwire for a walk of 331 probes, and this renders one inert
    top-level ``--help``.
    """
    import gen_site_commands as g

    home = Path(tempfile.mkdtemp(prefix="hscc-site-help-"))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CONDA_")
           and k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                         "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_SPARKRUN_BIN",
                         "COLUMNS")}
    env.update({"HOME": str(home), "PYTHONPATH": str(REPO), "COLUMNS": "80",
                "NO_COLOR": "1", "TERM": "dumb", "HSCC_THEME": "dark"})
    proc = subprocess.run([sys.executable, "-m", "hscc_daemon.hscc", "--help"],
                          env=env, cwd=str(home), capture_output=True,
                          text=True, timeout=120, check=False)
    assert proc.returncode == 0, (
        f"rendering the repo's own help failed ({proc.returncode}): "
        f"{proc.stderr.strip()[:300]}")
    return g.strip_ansi(proc.stdout)


@pytest.fixture(scope="module")
def repo_help_groups() -> list[dict]:
    return _parse(_render_repo_help())


def test_fixture_is_the_repos_own_help(repo_help_groups):
    """The anti-rot guard: the fixture is the repo's help, not a bottle's.

    Groups, order and the full entry text — argument shapes included, since
    `command_key()` throws them away and that is exactly what let a stale
    signature survive. The version line is deliberately not compared: it is
    not parsed, so a release bump must not redden this gate.
    """
    fixture = _parse(HELP_FIXTURE.read_text(encoding="utf-8"))
    assert fixture == repo_help_groups, (
        "scripts/tests/fixtures/hscc_help_landing.txt no longer describes "
        "THIS repo's `hscc --help`. Refresh it from the repo's own help "
        "(not the installed bottle, which lags main), then regenerate the "
        "page: python3 scripts/gen_site_commands.py --help-file "
        "scripts/tests/fixtures/hscc_help_landing.txt --write\n"
        f"fixture says:   {fixture}\n"
        f"repo help says: {repo_help_groups}")


def _real_home() -> str:
    """The account's real home, independent of what ``HOME`` currently says.

    The leg's autouse fixture replaces ``HOME`` with a sandbox before any test
    body runs (scripts/tests/conftest.py), so ``HOME`` and anything derived
    from it — including ``Path.home()`` — is the sandbox by the time we get
    here. ``pwd`` answers for the uid, which the fixture cannot lie about.
    """
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):        # Windows, or an unmapped uid
        return os.path.expanduser("~")


def _installed_cli_env(home_root: Path) -> dict:
    """The environment the INSTALLED ``hscc`` is spawned with by the oracle.

    The leg's autouse fixture sandboxes ``HOME`` so a watcher stamp can never
    reach the operator's live ``~/.hscc/state``. That is right for the rest of
    the leg, but the installed console script locates its payload *through*
    ``HOME``: ``hscc_cli/__init__.py`` probes ``Path.home()/".hermes"/"plugins"``
    (and ``~/dev/hscc`` as the fallback) before ``from hscc_daemon.hscc import
    main``. Point ``HOME`` at an empty directory and every candidate misses, so
    the child dies inside its own import, before argparse runs. Measured at
    8eb96984 on this host, same binary, ``HOME`` the only variable: rc=0 / 4819
    chars with the real home, rc=1 / ``ModuleNotFoundError: No module named
    'hscc_daemon'`` with a sandbox — which is exactly why ``hscc --help`` looks
    healthy from a plain shell and reds only from inside pytest.

    So the child keeps a sandbox ``HOME`` — the leg's hermeticity guarantee is
    not traded away — with only the payload directory linked in, so what
    actually runs is the real installed bottle. A ``HOME`` write by the child
    still lands under ``home_root``; measured, three ``--help`` runs under
    exactly this environment created zero files there.
    """
    env = dict(os.environ)
    real_plugins = Path(_real_home()) / ".hermes" / "plugins"
    cli_home = home_root / "cli-home"
    (cli_home / ".hermes").mkdir(parents=True, exist_ok=True)
    link = cli_home / ".hermes" / "plugins"
    if real_plugins.is_dir() and not link.is_symlink():
        link.symlink_to(real_plugins)
    env["HOME"] = str(cli_home)
    return env


def test_installed_cli_agrees_when_it_is_this_tree(repo_help_groups, tmp_path):
    """The bottle is a lagging artifact — so it is only an oracle at all when
    it renders exactly what this tree renders.

    The old form of this test compared the bottle with the fixture and passed
    while both were stale (see the block above). Now the three must agree, and
    when the installed CLI is demonstrably not this tree the test says which
    one it is instead of quietly green-lighting a two-stale comparison.

    The spawn uses ``_installed_cli_env`` — an environment in which the
    installed script can resolve its payload — because the leg's HOME sandbox
    otherwise kills the CLI's own import and the oracle reports a crash it
    cannot attribute. What is COMPARED is untouched by that: still the
    installed CLI's help against this tree's help.
    """
    import gen_site_commands as g

    if shutil.which("hscc") is None:
        pytest.skip("hscc is not installed on this host")
    live = _parse(g.fetch_help(None, env=_installed_cli_env(tmp_path)))
    if live != repo_help_groups:
        n_live = sum(len(x["sigs"]) for x in live)
        n_repo = sum(len(x["sigs"]) for x in repo_help_groups)
        pytest.skip(
            f"the installed hscc lists {n_live} commands and this tree's help "
            f"lists {n_repo}: the bottle is not this tree, and a lagging "
            "bottle cannot gate main. The fixture's oracle is the repo's own "
            "help, pinned by test_fixture_is_the_repos_own_help")
    assert _parse(HELP_FIXTURE.read_text(encoding="utf-8")) == live


def test_oracle_child_home_is_still_a_sandbox(tmp_path):
    """The fix's own guard: recovering the payload must not cost hermeticity.

    The tempting simplification is to hand the child the operator's real
    ``HOME`` so the CLI "just works". That re-opens the exact write path the
    leg's conftest exists to close, and it would pass every other test in the
    file. So pin that the oracle's child ``HOME`` is under ``tmp_path`` and is
    not the real home, and that the payload dir is reached by a link rather
    than by an unwritten ``HOME``.
    """
    env = _installed_cli_env(tmp_path)
    child_home = Path(env["HOME"])
    assert child_home == tmp_path / "cli-home"
    assert child_home.is_dir()
    assert str(child_home) != _real_home()
    assert child_home.is_relative_to(tmp_path), (
        "the oracle's child HOME escaped the leg's tmp root — it is writing "
        "somewhere the leg does not own")
    link = child_home / ".hermes" / "plugins"
    real_plugins = Path(_real_home()) / ".hermes" / "plugins"
    if real_plugins.is_dir():
        assert link.is_symlink() and link.resolve() == real_plugins.resolve(), (
            "the installed CLI resolves its payload through HOME; without the "
            "link the oracle dies in its own import and reports a crash "
            "instead of a comparison")


def test_fetch_help_names_the_module_that_failed(tmp_path, monkeypatch):
    """The diagnosability pin: the cause has to survive the handback.

    ``fetch_help`` used to cut the child's stderr at 200 characters. A Python
    traceback puts the decisive line LAST, so the cut landed mid-frame and the
    red read `exited 1: Traceback ... File ".../venv/lib/python"` — with the
    failing module never named, which is precisely how the red this card fixed
    stayed unattributed for a gate run. Drives the real function with a fake
    ``hscc`` on PATH whose stderr is shaped like the real one: frames first,
    the exception last, comfortably past the old cut.
    """
    import gen_site_commands as g

    bindir = tmp_path / "bin"
    bindir.mkdir()
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/opt/hscc/venv/bin/hscc", line 4, in <module>\n'
        "    from hscc_cli import main\n"
        '  File "/opt/hscc/venv/lib/python3.14/site-packages/hscc_cli/'
        '__init__.py", line 75, in <module>\n'
        "    from hscc_daemon.hscc import main\n"
        '  File "/opt/hscc/venv/lib/python3.14/site-packages/hscc_daemon/'
        'hscc.py", line 12, in <module>\n'
        "    import hscc_daemon.state\n"
        "ModuleNotFoundError: No module named 'hscc_daemon'"
    )
    # Past the 200-char cut by construction, so a reintroduced cap cannot
    # pass this test while looking long enough.
    assert len(traceback) > 200
    (bindir / "hscc.stderr").write_text(traceback + "\n", encoding="utf-8")
    fake = bindir / "hscc"
    fake.write_text('#!/bin/sh\ncat "$0.stderr" >&2\nexit 1\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(SystemExit) as exc:
        g.fetch_help(None)
    msg = str(exc.value)
    assert "ModuleNotFoundError: No module named 'hscc_daemon'" in msg, (
        f"the handback lost the cause: {msg!r}")
    assert msg.index("ModuleNotFoundError") > 200, (
        "the cause is only reachable past the old cut — if this moved below "
        "200 the fake stopped exercising the truncation")



# ── the copy card cannot advertise an argument the CLI does not take ────────
#
# align() renders BOTH the signature and the prose out of site/COPY.md; the CLI
# supplied only the group, the order and the count. The one cross-check was
# key_matches() over command_key(), which stops at the first non-bare-lowercase
# word — so `notify <message>`, `notify "<title>" "<body>"` and
# `notify --frobnicate XYZ` all keyed to `notify`, a nonsense signature passed
# the gate, and a CLI-side reword was invisible to every gate in the leg. Both
# were measured at `ce8e87f3`; the two tests below are those probes, kept.

def _copy_groups():
    import gen_site_commands as g

    return g.parse_copy_block(COPY_MD.read_text(encoding="utf-8"))


def _rows_and_problems(help_text: str, copy_groups=None):
    import gen_site_commands as g

    return g.align(g.parse_help(help_text),
                   copy_groups if copy_groups is not None else _copy_groups())


def test_generator_refuses_a_signature_the_cli_does_not_take(tmp_path):
    """The nonsense-sig probe: `--frobnicate` is not presentation, it is a lie.

    Rewriting the copy card's notify bullet to an option the CLI has never
    had used to align() cleanly and render straight onto the page — only the
    `--write` step stood between it and a published false claim. So this drives
    the real entry point with `--write` against a copy of the page: the
    generator has to refuse, leave the page byte-identical, and name the
    argument it refused.
    """
    import gen_site_commands as g

    copy = _copy_groups()
    bullet = [it for grp in copy for it in grp["items"]
              if it["sig"].startswith("notify")]
    assert bullet, "the copy card no longer has a notify bullet; teach this test"
    bullet[0]["sig"] = "notify --frobnicate XYZ"
    rows, problems = _rows_and_problems(
        HELP_FIXTURE.read_text(encoding="utf-8"), copy)
    assert problems, "a signature the CLI does not have aligned cleanly"
    assert "--frobnicate" in " ".join(problems)
    assert [r["sig"] for r in rows if r["sig"].startswith("notify")] == [
        "notify --frobnicate XYZ"], "the row is still built from the copy card"

    # ...and --write must not publish it.
    card = tmp_path / "COPY.md"
    card.write_text(COPY_MD.read_text(encoding="utf-8").replace(
        '- `hscc notify "<title>" "<body>"`', '- `hscc notify --frobnicate XYZ`'),
        encoding="utf-8")
    page = tmp_path / "index.html"
    before = INDEX_HTML.read_text(encoding="utf-8")
    page.write_text(before, encoding="utf-8")
    assert g.main(["--help-file", str(HELP_FIXTURE), "--copy", str(card),
                   "--page", str(page), "--write", "--quiet"]) == 1, (
        "--write published a signature the CLI does not have")
    assert page.read_text(encoding="utf-8") == before, "--write touched the page"


def test_generator_refuses_an_argument_the_cli_dropped(tmp_path):
    """The help-side probe, in the direction that can actually mislead.

    The CLI renaming an argument the page still advertises is the drift that
    reaches a user: they type what the page says and the CLI rejects it. A
    cosmetic reword of the help side (same arguments, different presentation)
    stays invisible on purpose — the copy card owns presentation, and the page
    stays true. See the module docstring of gen_site_commands.py.
    """
    grown = HELP_FIXTURE.read_text(encoding="utf-8").replace(
        "  notify <title> <body>", "  notify <title>")
    _, problems = _rows_and_problems(grown)
    assert problems, "the page still advertises <body> and the gate looked away"
    assert "body" in " ".join(problems)


def test_argument_check_is_not_vacuous():
    """The rule bites on the committed pair, and is not an equality rule.

    Both halves matter. Without the first the check could pass because nothing
    ever has arguments; without the second someone "tightens" it back to
    comparing the CLI's argument list exactly and the copy card has to be
    given an exception per verb. Measured at `ce8e87f3`: 17 of the 40 bullets
    then live would fail equality — `hscc cluster down` for the CLI's
    `cluster down [--dry-run]`, `hscc api start` for `api <cmd>`, `hscc
    template apply <name> --confirm` for `template apply <name> [--confirm]
    [--force-recreate]`, and 14 more.
    """
    import gen_site_commands as g

    cli_flat = [(gr["name"], s) for gr in _parse(
        HELP_FIXTURE.read_text(encoding="utf-8")) for s in gr["sigs"]]
    copy_flat = [it for gr in _copy_groups() for it in gr["items"]]
    assert len(cli_flat) == len(copy_flat)

    both = [c["sig"] for (hg, hsig), c in zip(cli_flat, copy_flat)
            if g.argument_vocabulary(hsig) and g.argument_vocabulary(c["sig"])]
    assert both, ("no row has arguments on both sides — the vocabulary check "
                  "is comparing nothing and cannot be trusted")

    unequal = [c["sig"] for (hg, hsig), c in zip(cli_flat, copy_flat)
               if g.argument_vocabulary(hsig) != g.argument_vocabulary(c["sig"])]
    assert len(unequal) >= 10, (
        f"only {len(unequal)} bullets differ from the CLI's argument list; "
        "the copy card stopped enriching signatures — if you are about to "
        "make this an equality rule, count them first (it was 17 of 40)")


def test_copy_page_agreement_is_clean():
    """The committed fixture and copy card agree with no problems at all."""
    _, problems = _rows_and_problems(HELP_FIXTURE.read_text(encoding="utf-8"))
    assert problems == [], "site/COPY.md and the CLI disagree:\n  " + "\n  ".join(
        problems)



def test_generator_fails_when_the_page_is_missing_a_command(tmp_path):
    """The drift test is not vacuous: removing one command breaks it."""
    import gen_site_commands as g

    page = tmp_path / "index.html"
    page.write_text(INDEX_HTML.read_text(encoding="utf-8").replace(
        '<li><code>hscc triggers</code>', '<li><code>hscc triggerz</code>'),
        encoding="utf-8")
    assert g.main(["--help-file", str(HELP_FIXTURE), "--page", str(page),
                   "--quiet"]) == 1


def test_generator_refuses_to_invent_copy_for_an_unknown_command(tmp_path):
    """A new CLI command must not be answered with a made-up sentence."""
    import gen_site_commands as g

    grown = HELP_FIXTURE.read_text(encoding="utf-8").replace(
        "  triggers             Show trigger-engine rules and recent firings",
        "  triggers             Show trigger-engine rules and recent firings\n"
        "  frobnicate           Brand new command nobody has described",
    )
    fixture = tmp_path / "grown.txt"
    fixture.write_text(grown, encoding="utf-8")
    proc_out = []
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = g.main(["--help-file", str(fixture), "--page", str(INDEX_HTML),
                     "--quiet"])
    proc_out.append(buf.getvalue())
    assert rc == 1
    assert "frobnicate" in proc_out[0], "the error must name the undescribed command"


def test_page_has_no_remote_or_framework_references():
    text = INDEX_HTML.read_text(encoding="utf-8")
    css = SITE_CSS.read_text(encoding="utf-8")
    # The one script allowed is JSON-LD structured data for search: data, not
    # code, so the page still needs no javascript.
    scripts = re.findall(r"<script[^>]*>", text)
    assert scripts == ['<script type="application/ld+json">'], scripts
    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
    json.loads(ld.group(1))
    assert "@import" not in css
    assert "<link" in text and "stylesheet" in text
    allowed = ("site.css", "hscc-icon.png", 'rel="canonical"', "llms.txt",
               "sitemap.xml")
    for tag in re.findall(r"<link[^>]*>", text):
        assert any(a in tag for a in allowed), tag
    for url in re.findall(r"https?://[^\s\"'()<>]+", text + css):
        # schema.org is the JSON-LD vocabulary id, never fetched.
        assert re.match(r"https?://(github\.com/|pom11\.github\.io/|schema\.org$|opensource\.org/licenses/)", url), (
            f"unexpected external reference: {url}")
    assert "fonts." not in text + css and "@font-face" not in css


def test_page_and_css_carry_no_private_address():
    """Same detector as the commit-time and pytest gates, not a second regex."""
    import address_guard as ag

    offenders = ag.scan_paths(SITE, ["index.html", "site.css", "COPY.md",
                                     "llms.txt", "llms-full.txt", "sitemap.xml",
                                     "assets/hscc-icon.png",
                                     "assets/hscc-banner.jpg"])
    assert not offenders, f"private address in the site files: {offenders}"


def test_page_is_usable_at_phone_width():
    text = INDEX_HTML.read_text(encoding="utf-8")
    css = SITE_CSS.read_text(encoding="utf-8")
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in text
    assert "max-width: 760px" in css
    assert "@media (max-width: 619px)" in css, (
        "the phone layout is what makes this readable at 360px")
    # A `display: table-caption` inside the block-stacked table shrink-wraps to
    # a few words per line, so the caption has to be switched too.
    assert "table.tpl caption" in css
    # Nothing may be wider than a phone's content box minus padding.
    for width in re.findall(r"(?<![-\w])width:\s*(\d+)px", css):
        assert int(width) <= 112, f"fixed width {width}px overflows a 360px phone"
    for prop in re.findall(r"min-width:\s*(\d+)px", css):
        assert int(prop) >= 620, f"min-width {prop}px gates the desktop layout"


def test_the_icon_is_the_app_icon_not_a_redraw():
    assert SITE_ICON.read_bytes() == APP_ICON.read_bytes(), (
        "site/assets/hscc-icon.png must stay a byte-for-byte copy of the app "
        "icon: cp ios-app/Assets.xcassets/AppIcon.appiconset/AppIcon.png "
        "site/assets/hscc-icon.png")


def test_page_has_exactly_the_contracted_sections():
    """The copy card contracts seven sections in order; the page renders them."""
    order = ["What it is", "Why", "What it does", "Templates", "Commands",
             "How it looks", "Get it"]
    text = INDEX_HTML.read_text(encoding="utf-8")
    heads = re.findall(r"<h2[^>]*>([^<]+)</h2>", text)
    assert heads == order, f"section order changed: {heads}"
    assert "REVIEW" not in text, "reviewer notes are not page copy"


def test_commands_markers_survive_in_both_files():
    for path in (COPY_MD, INDEX_HTML):
        body = path.read_text(encoding="utf-8")
        assert BEGIN in body and END in body, f"{path.name} lost its COMMANDS markers"
        assert body.index(BEGIN) < body.index(END)


def test_search_and_ai_indexing_files_ship_with_the_page():
    """llms.txt / llms-full.txt / sitemap.xml are published at the site root."""
    import xml.etree.ElementTree as ET

    llms = (SITE / "llms.txt").read_text(encoding="utf-8")
    assert llms.startswith("# hscc") and "\n> " in llms
    full = (SITE / "llms-full.txt").read_text(encoding="utf-8")
    assert "## 1. What it is" in full and "REVIEW" not in full
    root = ET.parse(SITE / "sitemap.xml").getroot()
    locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert "https://pom11.github.io/hscc/" in locs
    script = (REPO / "scripts" / "publish_site.sh").read_text(encoding="utf-8")
    for f in ("llms.txt", "llms-full.txt", "sitemap.xml"):
        assert f in script, f"publish_site.sh does not publish {f}"
