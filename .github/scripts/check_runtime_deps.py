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

Exit code is always 0 — a transient API failure for one dep is skipped, not
fatal, so a single flaky upstream never fails the workflow.
"""

import json
import os
import sys
import urllib.error
import urllib.request

LOCK_PATH = os.environ.get(
    "RUNTIME_VERSIONS_FILE", "hscc-bootstrap/runtime-versions.json")
BRANCH = "deps/runtime-bump"


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


def main():
    with open(LOCK_PATH) as f:
        lock = json.load(f)
    deps = lock.get("dependencies", {})

    changes = []  # (name, repo, old, new)
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
            continue
        if latest != cur:
            changes.append((name, repo, cur, latest))
            meta["tag"] = latest  # stage the bump

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
    sys.exit(main())
