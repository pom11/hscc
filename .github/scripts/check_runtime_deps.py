#!/usr/bin/env python3
"""Check HSCC's runtime dependencies (hermes-agent, sparkrun) for new releases.

Reads hscc-bootstrap/runtime-versions.json (the versions HSCC is verified
against), queries each upstream repo's latest GitHub release, and — if any
moved — rewrites the lock file and emits a summary + PR body. Stdlib only.

Outputs (to $GITHUB_OUTPUT when set, else stdout):
  updated=true|false
  summary=<one-line>            (e.g. "sparkrun v0.2.40 -> v0.2.41")
  branch=deps/runtime-bump
A PR body is written to the path in $DEP_PR_BODY_FILE (default: pr_body.md).

CLI mode:
  --find-open-pr   Print the OPEN PR number for the bump branch (or nothing).
                   Used by the workflow's edit-vs-create step.

Exit code 0 when no deps need updating. Exits 1 when a dep's latest release
tag could not be fetched — a persistent API failure should fail the workflow,
not slip by silently.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

LOCK_PATH = os.environ.get(
    "RUNTIME_VERSIONS_FILE", "hscc-bootstrap/runtime-versions.json")
BRANCH = "deps/runtime-bump"


def _parse_version(tag):
    """Parse 'v0.2.41' → (0, 2, 41) for numeric comparison.

    Strips a leading v/V, splits on '.', keeps leading digits in each
    segment.  Unparseable tags return (0,) so they never trigger an
    upgrade."""
    try:
        s = tag.lstrip("vV")
        parts = []
        for seg in s.split("."):
            num = ""
            for ch in seg:
                if ch.isdigit():
                    num += ch
                elif num:
                    break
            if num:
                parts.append(int(num))
        return tuple(parts) if parts else (0,)
    except (ValueError, AttributeError):
        return (0,)


def _latest_release_tag(repo):
    """Return the latest release tag for a GitHub repo, or None on failure."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "hscc-dep-check",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return (json.load(r) or {}).get("tag_name")
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def _emit(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}"
    if out:
        with open(out, "a") as f:
            f.write(line + "\n")
    else:
        print(line)


def find_open_pr(branch=BRANCH):
    """Return the number of the OPEN PR for ``branch``, else None.

    ``gh pr view`` returns a PR even once it is MERGED or CLOSED, which is the
    bug this guards against: the old workflow would force-push the branch and
    ``gh pr edit`` the already-merged PR #19 forever, never opening a fresh one.
    We therefore only "edit" when the found PR's state is exactly "OPEN";
    any other state (MERGED, CLOSED) or absence means a new PR must be created.

    Best-effort: any gh/parse failure yields None so a transient error does not
    wedge the daily run — the caller falls through to ``gh pr create``.
    """
    try:
        out = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "number,state"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "{}")
        if data.get("state") == "OPEN":
            return data.get("number")
        return None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def cli_find_open_pr():
    """--find-open-pr entrypoint: print the OPEN PR number (or nothing).

    Lets the workflow step resolve edit-vs-create via the same tested code path
    that the unit tests exercise, instead of a bespoke shell one-liner.
    """
    num = find_open_pr()
    if num is not None:
        print(num)
    return 0


def main():
    with open(LOCK_PATH) as f:
        lock = json.load(f)
    deps = lock.get("dependencies", {})

    changes = []  # (name, repo, old, new)
    fetch_failed = False
    for name, meta in deps.items():
        repo = meta.get("repo")
        cur = meta.get("tag")
        if not repo:
            continue
        latest = _latest_release_tag(repo)
        if latest is None:
            # A transient/permission failure — skip this dep (never open a
            # spurious PR); surface it so the Action log shows the miss.
            print(f"warning: could not check {name} ({repo})", file=sys.stderr)
            fetch_failed = True
            continue
        if _parse_version(latest) > _parse_version(cur):
            changes.append((name, repo, cur, latest))
            meta["tag"] = latest  # stage the bump

    if fetch_failed:
        return 1

    if not changes:
        _emit("updated", "false")
        print("No runtime dependency updates.")
        return 0

    # Rewrite the lock file with the bumped tags.
    with open(LOCK_PATH, "w") as f:
        json.dump(lock, f, indent=2)
        f.write("\n")

    summary = ", ".join(f"{n} {o} -> {new}" for n, _, o, new in changes)
    _emit("updated", "true")
    _emit("summary", summary)
    _emit("branch", BRANCH)

    lines = [
        "## Runtime dependency update",
        "",
        "A newer upstream release is available for a runtime HSCC depends on. "
        "This PR bumps the verified-versions lock; **the cluster should verify "
        "compatibility before merge.**",
        "",
        "| dependency | from | to | release notes |",
        "|---|---|---|---|",
    ]
    for name, repo, old, new in changes:
        lines.append(
            f"| `{name}` | {old} | **{new}** | "
            f"https://github.com/{repo}/releases/tag/{new} |")
    lines += [
        "",
        "### Cluster verification checklist",
        "- [ ] Upgrade the runtime "
        "(`hermes update` / rebase-bump sparkrun) with a recovery point",
        "- [ ] Re-bootstrap HSCC: `hscc-bootstrap/bootstrap.sh`",
        "- [ ] Run every component test suite (bootstrap, cluster, commands, "
        "roles, daemon, sparkrun-hermes)",
        "- [ ] Verify live: daemon streams, multiplex served profiles, a slash "
        "command, and the `:4000` proxy",
        "- [ ] Note any incompatibilities fixed (e.g. profile/config migration)",
        "",
        "_Opened automatically by the check-runtime-deps workflow._",
    ]
    body_file = os.environ.get("DEP_PR_BODY_FILE", "pr_body.md")
    with open(body_file, "w") as f:
        f.write("\n".join(lines) + "\n")

    print("Updates:", summary)
    return 0


if __name__ == "__main__":
    if "--find-open-pr" in sys.argv[1:]:
        sys.exit(cli_find_open_pr())
    sys.exit(main())
