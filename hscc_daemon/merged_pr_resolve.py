"""Resolve a card the dispatcher holds under ``active_pr`` whose PR is MERGED.

The defect (t_57d8ca28, reported by fleet-check 2026-11-23): Hermes' respawn
guard refuses to re-spawn a ready card whose recent comments cite a GitHub PR
URL — ``check_respawn_guard`` returns ``"active_pr"``
(``hermes_cli/kanban_db_dispatch.py:1207-1213``, window
``_RESPAWN_GUARD_PR_WINDOW`` = 86400 s at line 67) and the dispatcher appends a
``respawn_guarded`` event on EVERY tick it holds
(``hermes_cli/kanban_db_dispatch.py:1536-1537``). Nothing in that path ever
asks GitHub whether the PR is still open. So when the PR gets merged by someone
else — the merge queue, a reviewer, a squash by hand — the card keeps its hold,
the guard keeps firing every tick, and the card sits ``ready`` forever with a
worker never spawned.

Measured instance: card ``t_edc71fcb`` sat ``ready`` on board ``hscc`` from
2026-11-22 while PR #625 (``pom11/hscc``, MERGED at 2026-11-22T19:07:50Z, merge
commit ``134ec3d5``) was already on main. It burned **286 dispatcher ticks**
before the fleet-check cron completed it by hand. The gateway logged
``kanban dispatcher stuck`` the whole time (that is the same log line
``dispatcher_wedge`` reads), which is also why this module lives next to that
watchdog.

Why the watchdog cannot fix this by itself
------------------------------------------
``dispatcher_wedge`` deliberately treats ``active_pr`` as a BENIGN hold
(``BENIGN_HOLD_REASONS``, t_160d306f): a held card is the dispatcher working as
designed and restarting the gateway for it restarted the gateway 3× in 65
minutes on 2026-10-17. That invariant is untouched here and must stay — a
merged-PR hold is still not a wedge, so this module never restarts anything and
never changes the wedge classification. What it adds is the missing exit:
the hold is *resolved* instead of being re-guarded forever. Note the 286-tick
incident had a GREEN wedge stream precisely because of that exemption, so this
pass must NOT be gated on a declared stall — it runs on its own tick.

The fix shape (this module, read-then-act, all seams injectable)
----------------------------------------------------------------
1. READ (``held_active_pr_cards``): a card is a candidate only when the
   dispatcher is holding it RIGHT NOW — status ``ready``, no claim lock, an
   assignee, and a ``respawn_guarded`` event with payload
   ``reason == "active_pr"`` inside the hold window. The event, not the comment,
   is the trigger: a ready card that merely *mentions* a merged PR in prose is
   not being held by the guard and must never be auto-completed. The PRs it
   cites come from its comments, via ``merge_queue.PR_URL_RE`` (documented as
   the same URL shape Hermes' guard recognises).
2. VERIFY (``pr_view``): ask GitHub through ``gh pr view --repo <owner>/<repo>``
   for the real state. No PR read, no action — the pass fails CLOSED, so an
   unreachable gh, a non-zero exit or unparseable JSON ends that card's turn
   with zero mutations (the next tick retries, up to a per-card attempt cap).
3. PLAN (``plan_resolution``): a card is only COMPLETED when every PR it cites
   is MERGED *and* one of those merges demonstrably covers THIS card — the card
   id appears in the PR's head branch, title or body (the squash-merge commit
   message is built from title+body, so a hit there is the covering commit; the
   ``git log --grep <card-id> origin/main`` route the card suggested asks the
   same question of the local checkout and is intentionally not duplicated
   here). A merged merge that does NOT name the card is not proof the card
   shipped: t_edc71fcb's PR #625 was branched ``wt/t_5b560adf``, a sibling's
   branch, so branch identity alone can neither prove nor disprove coverage.
   Anything merged-but-unproven (or partially merged, or abandoned/CLOSED)
   BLOCKS the card with ``--kind needs_input`` naming the PR: a human decides.
   Either way the card leaves ``ready``, so the guard has nothing left to hold —
   never respawn past a merge.
4. ACT through the sanctioned ``hermes kanban`` CLI ONLY (``complete
   --metadata`` / ``block --kind needs_input``). Hermes owns completion
   semantics — the closing run, metadata, child promotion, notifications — and
   hscc does not reimplement board bookkeeping (``kanban_blocked`` writes a
   status flip directly only because ``reclaim_task`` provably cannot do it;
   there is no such excuse for a completion). CLI mutation is pin-safe: the
   board slug is always stated (``--board <slug>``) and the child environment
   is scrubbed of ``HERMES_KANBAN_DB`` when it would outrank that board — the
   plumbing is ``dispatcher_wedge``'s, reused rather than duplicated.

Every action is logged and written onto the card itself (the completion result/
metadata, or the block reason, which Hermes appends as a comment), so the audit
trail is on the card and in ``hermes kanban tail`` — never only in the daemon
log. Kill switches: ``HSCC_MERGED_PR_RESOLVE=off`` disables the pass entirely,
``HSCC_MERGED_PR_RESOLVE_DRY_RUN=1`` plans and reports without touching a board.
"""

import json
import os
import time

from . import autodown
from . import board_pin
# ``dispatcher_wedge`` owns the ONLY sanctioned ``hermes kanban`` CLI plumbing in
# this package (argv prefix, board-slug-always-stated argv, the child-env pin
# scrub, the default subprocess runner). Its helpers are named privately and are
# reached through the module on purpose: reusing the watchdog's transport is the
# fleet rule; a second implementation of "how hscc asks hermes about a board" is
# how a wedge detector ends up reading a board nobody named (t_b014c2b8,
# t_dad0da32). Nothing here changes the wedge's hold classification.
from . import dispatcher_wedge
from .daemon_ops import log
from .merge_queue import PR_URL_RE

#: How fresh a ``respawn_guarded active_pr`` event must be for the card to count
#: as "held RIGHT NOW". Mirrors Hermes' own PR guard window
#: (``_RESPAWN_GUARD_PR_WINDOW`` = 86400 s): the guard re-asserts itself every
#: tick while a citing comment is inside that window, so an event older than the
#: window means the dispatcher is no longer holding this card.
HOLD_WINDOW_SECONDS = int(
    os.environ.get("HSCC_MERGED_PR_HOLD_WINDOW_SECONDS", "86400"))

#: Minimum seconds between operator notifications for the SAME card (the pass
#: ticks every dispatcher-wedge interval; a card we keep declining to guess
#: about must not ping every minute).
NOTIFY_COOLDOWN_SECONDS = int(
    os.environ.get("HSCC_MERGED_PR_NOTIFY_SECONDS", "3600"))

#: Consecutive failed actions tolerated for one card before this process stops
#: trying it. In-memory on purpose — same lifetime as the wedge's streak
#: counters: a daemon restart is allowed one more honest try.
MAX_ATTEMPTS_PER_CARD = int(
    os.environ.get("HSCC_MERGED_PR_MAX_ATTEMPTS", "3"))

#: Seconds allowed for one ``gh pr view``.
GH_TIMEOUT_SECONDS = int(os.environ.get("HSCC_MERGED_PR_GH_TIMEOUT", "20"))

#: What we ask GitHub for. ``state``/``mergedAt``/``mergeCommit`` answer "is it
#: merged and what is the covering sha"; ``headRefName``/``title``/``body`` are
#: the three places a card id legitimately appears on its own PR.
PR_VIEW_FIELDS = ("number", "url", "state", "mergedAt", "mergeCommit",
                  "headRefName", "baseRefName", "title", "body")

#: In-memory bookkeeping for this process: failed-action attempts and last
#: notification time, both keyed ``"<board>|<card id>"``. A successful resolve
#: needs no entry — the card leaves ``ready`` and stops being a candidate.
_attempts = {}
_notified_at = {}


def _reset_resolution_state():
    """Forget per-card attempts/notifications (tests / daemon restart)."""
    _attempts.clear()
    _notified_at.clear()


def _env_flag(name, default=False):
    """Boolean env read at CALL time (so a test can flip it per-case)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "off", "false", "no", "")


def enabled():
    """Is the automatic merged-PR resolution pass on?"""
    return _env_flag("HSCC_MERGED_PR_RESOLVE", True)


def dry_run():
    """Plan and report without touching a board."""
    return _env_flag("HSCC_MERGED_PR_RESOLVE_DRY_RUN", False)


# ── 1. READ: which cards is the guard holding, and on which PRs? ────────────
def _held_event(conn, task_id, cutoff):
    """Latest ``respawn_guarded`` event for one card inside the window.

    Returns ``(epoch_seconds, reason)`` or ``None``. The reason is read from the
    event payload in Python rather than ``json_extract`` in SQL: the same
    payload Hermes writes (``{"reason": guard_reason}``), and no assumption
    about the sqlite build's JSON support.
    """
    try:
        rows = conn.execute(
            "SELECT payload, created_at FROM task_events "
            "WHERE task_id = ? AND kind = 'respawn_guarded' "
            "AND created_at >= ? ORDER BY created_at DESC",
            (task_id, cutoff)).fetchall()
    except Exception:  # noqa: BLE001 - an unreadable event table = no evidence
        return None
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        reason = payload.get("reason") if isinstance(payload, dict) else None
        if reason:
            return int(row["created_at"] or 0), reason
    return None


def _cited_prs(conn, task_id, cutoff):
    """Distinct GitHub PRs cited by this card's comments inside the window.

    ``merge_queue.PR_URL_RE`` is the URL shape (owner/repo/number), i.e. the
    same comment evidence Hermes' guard matched to say ``active_pr``.
    """
    found = {}
    try:
        rows = conn.execute(
            "SELECT body, created_at FROM task_comments "
            "WHERE task_id = ? AND created_at >= ? ORDER BY created_at DESC",
            (task_id, cutoff)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    for row in rows:
        body = row["body"] or ""
        for m in PR_URL_RE.finditer(body):
            number = int(m.group("number"))
            key = (m.group("owner"), m.group("repo"), number)
            found.setdefault(key, {
                "owner": m.group("owner"), "repo": m.group("repo"),
                "number": number,
                "url": ("https://github.com/%s/%s/pull/%d"
                        % key),
                "comment_at": int(row["created_at"] or 0),
            })
    return list(found.values())


def held_active_pr_cards(kanban_db=None, now=None, lookback_s=None,
                         boards=None):
    """READ-ONLY: cards currently held under ``active_pr``, with cited PRs.

    A candidate is ``ready`` + ``claim_lock IS NULL`` + assigned + a fresh
    ``respawn_guarded``/``active_pr`` event. Deliberately NOT "any ready card
    whose comments mention a merged PR": that would auto-complete cards citing a
    PR as background, which is a different card's work.

    Boards are enumerated through autodown (the single sanctioned enumeration)
    and every read goes through ``board_pin.open_board(mode="read")`` so a
    worker's ``HERMES_KANBAN_DB`` cannot make this pass describe a board nobody
    asked about. An unreadable board is recorded in ``errors`` and skipped,
    never fatal.

    Returns ``{"cards": [...], "errors": [...], "unreachable": str|None}``.
    """
    if now is None:
        now = time.time()
    if lookback_s is None:
        lookback_s = HOLD_WINDOW_SECONDS
    cutoff = int(now - lookback_s)
    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        return {"cards": [], "errors": [],
                "unreachable": autodown._KANBAN_LOAD.get(
                    "reason") or "kanban lib unreachable"}
    if boards is None:
        try:
            boards, multi = autodown._enum_board_names(kanban_db)
        except Exception as e:  # noqa: BLE001
            return {"cards": [], "errors": [f"board enumeration failed: {e}"],
                    "unreachable": None}
    else:
        _, multi = autodown._enum_board_names(kanban_db)
    cards, errors = [], []
    for board in boards:
        try:
            with board_pin.open_board(
                    kanban_db, board, multi=multi, mode="read") as conn:
                rows = conn.execute(
                    "SELECT id, assignee, title FROM tasks "
                    "WHERE status = 'ready' AND claim_lock IS NULL "
                    "AND assignee IS NOT NULL ORDER BY created_at ASC"
                ).fetchall()
                for row in rows:
                    held = _held_event(conn, row["id"], cutoff)
                    if not held or held[1] != "active_pr":
                        continue
                    prs = _cited_prs(conn, row["id"], cutoff)
                    if not prs:
                        # Held for a PR URL we can no longer find inside the
                        # window: nothing to verify, and guessing a PR number is
                        # exactly the kind of guess this pass must not make.
                        continue
                    cards.append({
                        "board": board if board else "default",
                        "id": row["id"],
                        "assignee": row["assignee"],
                        "title": row["title"] or "",
                        "hold_at": held[0],
                        "prs": prs,
                    })
        except board_pin.BoardPinConflict:
            # A pin that outranks the board we were told to read: refuse loudly
            # (the caller reports it) rather than resolve cards on the wrong
            # board. Ids repeat across boards, so this one is not negotiable.
            errors.append(f"{board or 'default'}: board pin conflict — "
                          "refusing to read/resolve on a board that was not "
                          "named")
            continue
        except Exception as e:  # noqa: BLE001 - one bad board must not end the pass
            errors.append(f"{board or 'default'}: {e}")
            continue
    return {"cards": cards, "errors": errors, "unreachable": None}


# ── 2. VERIFY: what does GitHub actually say about the PR? ──────────────────
def _default_gh_runner(argv):
    """Run one ``gh`` call. Returns the CompletedProcess, or None on any error."""
    import subprocess
    try:
        return subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=GH_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - unreachable gh is a None, not a crash
        return None


def pr_view(owner, repo, number, gh_runner=None):
    """``gh pr view <n> --repo owner/repo --json <fields>`` as a dict, or None.

    Fail closed: an exception, a non-zero exit or unparseable JSON all answer
    None, and the caller must then leave the card alone. An unverified merge is
    never a reason to complete somebody's card.
    """
    runner = gh_runner or _default_gh_runner
    argv = ["gh", "pr", "view", str(number), "--repo", f"{owner}/{repo}",
            "--json", ",".join(PR_VIEW_FIELDS)]
    try:
        proc = runner(argv)
    except Exception:  # noqa: BLE001 - a raising fake is still "unreadable"
        return None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def merge_sha(pr):
    """The commit that carried this PR onto its base branch, or None."""
    if not isinstance(pr, dict):
        return None
    commit = pr.get("mergeCommit")
    if isinstance(commit, dict) and commit.get("oid"):
        return commit["oid"]
    return None


def covers_card(card_id, pr):
    """``(covered, where)`` — does this merged PR demonstrably cover ``card_id``?

    Three places a card id legitimately appears on its own PR: the head branch
    (hscc branches are ``wt/<task-id>``), the title, and the body — and the
    body matters because a squash-merge commit message is built from
    title+body, so a body hit means the merge commit itself names the card.
    ``git log --grep <card-id> origin/main`` answers the same question from the
    local checkout; it is deliberately NOT consulted here, because this pass is
    repo-agnostic (one gh call, no fetch) and a second merge oracle would need
    its own disagreement-resolution rules.
    """
    if not isinstance(pr, dict) or not card_id:
        return False, ""
    for field in ("headRefName", "title", "body"):
        if card_id in (pr.get(field) or ""):
            return True, field
    return False, ""


# ── 3. PLAN: complete, block, or leave the hold alone ───────────────────────
def plan_resolution(card, views):
    """Decide one held card, given the GitHub views of every PR it cites.

    ``views`` is a list of ``(pr_ref, view|None)`` pairs. Returns a dict:
    ``{"action": "complete"|"block"|None, "reason": str, "sha": str|None,
       "prs": [...], "detail": str}``.

    Fail-closed rules, in order:
      * any cited PR we could not read ⇒ ``None`` (unverified — retry later).
      * no cited PR merged ⇒ ``None`` (the guard is holding a genuinely open
        PR; that is the benign case t_160d306f protects, nothing to resolve).
      * every cited PR merged AND one of them covers the card ⇒ ``complete``
        with that covering sha.
      * merged but no merge covers the card, or only some PRs merged (so part
        of what the card cites is still open/abandoned) ⇒ ``block``
        ``needs_input`` naming the PRs. The card stops respawning and a human
        decides, which is the "or block it with a needs_input reason naming the
        PR" branch of the fix shape.
    """
    prs = [ref for ref, _view in views]
    unread = [ref for ref, view in views if not isinstance(view, dict)]
    if unread:
        return {"action": None, "reason": "unverified", "sha": None,
                "prs": prs,
                "detail": "PR state unreadable via gh: "
                          + ", ".join(p["url"] for p in unread)}
    merged = [(ref, view) for ref, view in views
              if (view.get("state") or "").upper() == "MERGED"]
    if not merged:
        return {"action": None, "reason": "pr_open", "sha": None, "prs": prs,
                "detail": "cited PR not merged: "
                          + ", ".join(f"{ref['url']} "
                                      f"({view.get('state')})"
                                      for ref, view in views)}
    not_merged = [ref for ref, view in views
                  if (view.get("state") or "").upper() != "MERGED"]
    for ref, view in merged:
        covered, where = covers_card(card["id"], view)
        sha = merge_sha(view)
        if covered and sha:
            if not_merged:
                # Merged work for this card exists, but the card also cites
                # PR(s) that never merged: completing it would silently drop
                # that work. A human decides.
                return {"action": "block", "reason": "partially_merged",
                        "sha": sha, "prs": prs,
                        "detail": "covering merge "
                                  f"{sha[:12]} but unresolved citations: "
                                  + ", ".join(p["url"] for p in not_merged)}
            return {"action": "complete", "reason": "merged_covering",
                    "sha": sha, "prs": prs,
                    "detail": f"PR {ref['url']} merged {view.get('mergedAt')} "
                              f"as {sha[:12]}; card id in {where}"}
    return {"action": "block", "reason": "merged_uncovered", "sha": None,
            "prs": prs,
            "detail": "merged PR(s) do not name this card: "
                      + ", ".join(ref["url"] for ref, _v in merged)}


# ── 4. ACT: sanctioned hermes CLI only ──────────────────────────────────────
def _complete_argv(board, card_id, plan, view):
    result = (f"Work is on main: PR #{view['number']} merged "
              f"{view.get('mergedAt') or '(time unknown)'} "
              f"({plan['sha'][:12]}). Auto-resolved by hscc: the dispatcher "
              "held this card under active_pr for a PR that has already "
              "merged.")
    metadata = {
        "resolved_by": "hscc-merged-pr",
        "merged_pr": view["number"],
        "merged_pr_url": view["url"],
        "merge_sha": plan["sha"],
        "merged_at": view.get("mergedAt"),
        "card_id_match": covers_card(card_id, view)[1],
        "board": board,
    }
    return ["--board", str(board), "complete", str(card_id),
            "--result", result, "--summary", result,
            "--metadata", json.dumps(metadata, default=str)]


def _block_argv(board, card_id, plan):
    urls = ", ".join(p["url"] for p in plan["prs"])
    reason = (
        f"Dispatcher has this card held under active_pr for "
        f"{urls}, already merged on its base branch, but that merge does not "
        "demonstrably cover this card"
        + (f" ({plan['detail']})" if plan.get("detail") else "")
        + ". A human must decide: complete the card (name the covering commit) "
          "or release the hold and re-run it. hscc will not respawn past a "
          "merge and will not guess what shipped.")
    return ["--board", str(board), "block", str(card_id), reason,
            "--kind", "needs_input"]


def _merged_view(views, card_id):
    """The view that justified the action (used for the completion message)."""
    for ref, view in views:
        if (view or {}).get("state", "").upper() == "MERGED" \
                and merge_sha(view) and covers_card(card_id, view)[0]:
            return view
    for _ref, view in views:
        if (view or {}).get("state", "").upper() == "MERGED":
            return view
    return views[0][1] if views else None


def _notify(msg):
    """Best-effort operator notify. Never raises (same channel as the wedge)."""
    try:
        from .desktop import send_macos_notification
        send_macos_notification("HSCC: merged PR held a card ready", msg,
                                priority="normal")
    except Exception:  # noqa: BLE001
        pass


def _notify_throttled(key, msg, now, notify_fn=None):
    """One notify per card per ``NOTIFY_COOLDOWN_SECONDS`` (in-memory)."""
    last = _notified_at.get(key)
    if last is not None and (now - last) < NOTIFY_COOLDOWN_SECONDS:
        return False
    _notified_at[key] = now
    notify = notify_fn or _notify
    try:
        notify(msg)
    except Exception:  # noqa: BLE001 - notify is best-effort
        pass
    return True


def resolve_merged_pr_cards(kanban_db=None, now=None, gh_runner=None,
                            cli_runner=None, notify_fn=None, dry_run_flag=None,
                            lookback_s=None):
    """One pass: resolve every card held under ``active_pr`` whose PR merged.

    Injectable seams (all of them, so the suite runs with ZERO external calls):
    ``kanban_db`` (fake lib), ``gh_runner`` (argv -> CompletedProcess-like),
    ``cli_runner`` (argv[, env] -> CompletedProcess-like; production is the
    wedge's gate runner), ``notify_fn``, ``dry_run_flag`` (default from env),
    ``now``, ``lookback_s``.

    Returns ``{"resolved": [...], "blocked": [...], "held": [...],
    "errors": [...], "skipped": [...], "dry_run": bool}`` — every entry names
    board, card and PR(s). Never raises.
    """
    now = time.time() if now is None else now
    if dry_run_flag is None:
        dry_run_flag = dry_run()
    report = {"resolved": [], "blocked": [], "held": [], "errors": [],
              "skipped": [], "dry_run": bool(dry_run_flag)}

    snap = held_active_pr_cards(kanban_db=kanban_db, now=now,
                                lookback_s=lookback_s)
    report["errors"].extend(snap["errors"])
    if snap["unreachable"]:
        report["errors"].append(f"kanban unreadable: {snap['unreachable']}")
        return report
    cards = snap["cards"]
    if not cards:
        return report

    kanban_lib = kanban_db or autodown._load_kanban_db_or_default()
    base = dispatcher_wedge._hermes_cli_argv()
    runner = cli_runner or dispatcher_wedge._default_gate_cli_runner
    run_cli = dispatcher_wedge._GateCliCaller(runner, base) if base else None
    multi = dispatcher_wedge._gate_multi(kanban_lib) if kanban_lib else True

    for card in cards:
        key = f"{card['board']}|{card['id']}"
        attempts = _attempts.get(key, 0)
        if attempts >= MAX_ATTEMPTS_PER_CARD:
            report["skipped"].append({**{k: card[k] for k in
                                         ("board", "id", "assignee")},
                                      "reason": "attempt_cap",
                                      "attempts": attempts})
            continue
        views = [(ref, pr_view(ref["owner"], ref["repo"], ref["number"],
                               gh_runner=gh_runner)) for ref in card["prs"]]
        plan = plan_resolution(card, views)
        entry = {"board": card["board"], "card": card["id"],
                 "assignee": card["assignee"], "reason": plan["reason"],
                 "detail": plan["detail"], "sha": plan["sha"],
                 "prs": [p["url"] for p in plan["prs"]]}
        if plan["action"] is None:
            # Either genuinely open (the benign hold t_160d306f protects) or
            # unverifiable. Both mean: do not touch the card.
            report["held"].append(entry)
            if plan["reason"] == "unverified":
                _notify_throttled(
                    key, f"card {card['id']} on board {card['board']} is held "
                         f"by active_pr but its PR state is unreadable "
                         f"({plan['detail']}) — hscc did not resolve it",
                    now, notify_fn)
            continue

        view = _merged_view(views, card["id"]) or {}
        argv = (_complete_argv(card["board"], card["id"], plan, view)
                if plan["action"] == "complete"
                else _block_argv(card["board"], card["id"], plan))
        if dry_run_flag:
            entry["dry_run"] = True
            (report["resolved"] if plan["action"] == "complete"
             else report["blocked"]).append(entry)
            log(f"merged-PR resolve (dry run): would {plan['action']} card "
                f"{card['id']} on board {card['board']} — {plan['detail']}")
            continue
        if run_cli is None:
            report["errors"].append(f"hermes CLI not found — cannot resolve "
                                    f"{card['id']} on {card['board']}")
            continue
        child_env, blocked = None, ""
        if kanban_lib is not None:
            # The pin question is answered BY the lib (board_pin reads its
            # enumeration to know whether a slug names a real board); with no
            # lib in hand there is nothing to interrogate, and the argv states
            # its board explicitly either way.
            child_env, blocked = dispatcher_wedge._gate_child_env(
                kanban_lib, card["board"], run_cli)
        if blocked:
            report["errors"].append(blocked)
            continue
        try:
            proc = run_cli(argv, env=child_env)
        except Exception as e:  # noqa: BLE001 - a raising runner is a failed action
            proc = None
            entry["error"] = str(e)
        ok = proc is not None and getattr(proc, "returncode", 1) == 0
        if not ok:
            _attempts[key] = attempts + 1
            entry["attempts"] = attempts + 1
            entry["error"] = entry.get("error") or _proc_error(proc)
            report["errors"].append(
                f"{card['board']}/{card['id']}: {plan['action']} failed "
                f"({entry['error']}) — attempt {attempts + 1}/"
                f"{MAX_ATTEMPTS_PER_CARD}")
            _notify_throttled(
                key, f"could not resolve card {card['id']} on board "
                     f"{card['board']} after a merged PR "
                     f"({plan['detail']}): {entry['error']}", now, notify_fn)
            continue
        _attempts.pop(key, None)
        (report["resolved"] if plan["action"] == "complete"
         else report["blocked"]).append(entry)
        log(f"merged-PR resolve: {plan['action']}d card {card['id']} on board "
            f"{card['board']} — {plan['detail']}")
        _notify_throttled(
            key, f"card {card['id']} on board {card['board']} was held by "
                 f"active_pr with a merged PR; hscc {plan['action']}d it "
                 f"({plan['detail']})", now, notify_fn)
    return report


def _proc_error(proc):
    """A short, single-line error string from a failed CLI call."""
    if proc is None:
        return "no result from hermes CLI"
    text = ((getattr(proc, "stderr", "") or "")
            + (getattr(proc, "stdout", "") or "")).strip()
    first = text.splitlines()[0] if text else ""
    return (first or f"exit {getattr(proc, 'returncode', '?')}")[:300]


def tick(now=None):
    """Daemon entry point. Never raises; returns the report (or a stub)."""
    if not enabled():
        return {"skipped": [{"reason": "disabled"}], "resolved": [],
                "blocked": [], "held": [], "errors": [],
                "dry_run": dry_run()}
    try:
        report = resolve_merged_pr_cards(now=now)
    except Exception as e:  # noqa: BLE001 - a resolution pass must never
        # kill the loop that hosts it (same contract as the wedge recovery).
        log(f"merged-PR resolve: pass failed: {e}", "ERROR")
        return {"resolved": [], "blocked": [], "held": [],
                "errors": [f"pass failed: {e}"], "skipped": [],
                "dry_run": dry_run()}
    acted = len(report["resolved"]) + len(report["blocked"])
    if acted or report["errors"]:
        log(f"merged-PR resolve: {len(report['resolved'])} completed, "
            f"{len(report['blocked'])} blocked for input, "
            f"{len(report['held'])} still legitimately held, "
            f"{len(report['errors'])} error(s)"
            + (" (DRY RUN — no board was touched)" if report["dry_run"] else ""))
    return report
