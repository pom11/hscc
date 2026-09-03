"""Unit tests for hscc-api Phase A3 — project/kanban READ endpoints.

The suite is hermetic: every flightdeck backing call is replaced with a fake
namespace (SimpleNamespace) via monkeypatch, so NO test ever reads the real
live kanban DB, a real git repo, or a real registry file. Handlers are driven
over real loopback HTTP (loopback port 0) exactly like A1's suite, so auth and
the route dispatcher are exercised end-to-end.

Coverage required by the card:
  * each of the 6 project/kanban endpoints -> 200 + expected shape + non-empty
    ``speak``;
  * 404 for an unknown / unresolvable card id;
  * auth enforced (401 without a token) on these routes too;
  * graceful degradation on a backing error (200 with an honest ``speak``,
    never a crash, never fabricated values);
  * ``/v1/review/{id}`` is a DRY-RUN: no merge / no close-card (no mutation).
"""

import json
import types

import pytest

import api_server
import routes_project


# --------------------------------------------------------------------------- #
# Fakes for the flightdeck backing modules
# --------------------------------------------------------------------------- #
#
# routes_project holds the flightdeck modules as module-level names
# (`_kanban`, `_review_cmd`, `_qa_cmd`, `_standup_cmd`, `_review_core`,
# `_registry`). Each test replaces those names with a SimpleNamespace fake so
# the real library (which would read the live board/git) never runs.

def _fake_module(**attrs):
    return types.SimpleNamespace(**attrs)


def _card(cid="t_abc123", title="Do a thing", status="running", board="default",
          branch="wt/t_abc123", body="", created_at=1000):
    d = {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch,
        "body": body,
        "created_at": created_at,
        "workspace_path": "/tmp/repo",
    }
    return d


# --- standup ---

def _standup_data():
    return {
        "needs_you": [_card(cid="t_rev1", status="review")],
        "running": [_card(cid="t_run1", status="running")],
        "stale": [],
        "failing": [_card(cid="t_fail1", status="failing")],
        "drift": [],
        "unreadable": [],
    }


# --- review ---

def _review_facts(exists=True):
    return {
        "exists": exists,
        "subject": "Add the thing",
        "files": 2,
        "insertions": 10,
        "deletions": 1,
        "conflicts": 0,
    }


def _diff_patch():
    """A small canned unified diff over two files (one added, one modified).

    Mirrors what ``git diff --no-color base...branch`` emits. Used by the
    hermetic diff-route tests (through the faked ``_branch_diff``).
    """
    return (
        "diff --git a/newfile.txt b/newfile.txt\n"
        "new file mode 100644\n"
        "index 0000000..abc1234\n"
        "--- /dev/null\n"
        "+++ b/newfile.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+hello\n"
        "+world\n"
        "diff --git a/app.py b/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def setup():\n"
        "-    old = True\n"
        "+    new = True\n"
        "     return 0\n"
    )


def _project(name="hscc", repo="/tmp/repo", board="default"):
    return types.SimpleNamespace(name=name, repo=repo, board=board)


def _why_story(card_id="t_abc123"):
    return {
        "id": card_id, "title": "Do a thing", "status": "running",
        "assignee": "node", "board": "default", "project": "hscc",
        "milestone": None, "created_at": 1000, "started_at": 1200,
        "last_heartbeat_at": 1300, "status_duration_s": 300,
        "branch": "wt/t_abc123", "branch_exists": True,
        "commits": ["Add the thing"], "landed": False, "uncommitted": [],
        "workspace_path": "/tmp/repo", "is_worktree": False,
        "verdict": "in progress — keep going", "boards_searched": ["default"],
    }


def _roadmap_result(present=True):
    def _milestone(name):
        return _fake_module(
            items=[_fake_module(text="Do it", checked=True),
                   _fake_module(text="Later thing", checked=False)],
            done_count=1, total=2, name=name, id=name.lower(),
        )

    return _fake_module(present=present, milestone=_milestone)


def _metrics_dict():
    return {
        "window": {"since": 1000.0, "now": 1000.0 + 86400, "days": 1.0},
        "reviewed": 3, "merged_count": 2,
        "first_time_pass": {"n": 3, "count": 2, "rate": 0.67},
        "started": 4, "stalled": {"n": 3, "count": 1, "rate": 0.33},
        "review_latency": {"n": 3, "median": 60.0, "p90": 120.0},
        "throughput": {"n": 2, "per_day": 2.0},
        "rework": {"n": 3, "count": 1, "share": 0.33},
    }


# --------------------------------------------------------------------------- #
# Fixtures: a running server + full set of fakes
# --------------------------------------------------------------------------- #

@pytest.fixture
def fakes(monkeypatch):
    """Install hermetic fakes for every flightdeck backing module.

    Returns a dict of the fakes keyed by the routes_project attribute name so
    tests can mutate a single fake (e.g. make it raise) per case.
    """
    fd = {
        "_kanban": _fake_module(
            list_cards=lambda board=None, include_archived=False: [_card()],
            find_card=lambda cid: _card(cid=cid) if cid == "t_abc123" else None,
            project_for_card=lambda card, projects: _project(),
            UNATTRIBUTED=object(),
        ),
        "_registry": _fake_module(
            load_registry=lambda path=None: [_project()],
            get_project=lambda name, path=None: _project(name=name),
            ProjectNotFoundError=type("ProjectNotFoundError", (Exception,), {}),
        ),
        "_git_state": _fake_module(
            is_repo=lambda repo: True,
            current_branch=lambda repo: "main",
            is_dirty=lambda repo: False,
            uncommitted_files=lambda repo: [],
            last_commit_age_seconds=lambda repo: 120,
            head_sha=lambda repo: "abc123",
            ahead_of_upstream=lambda repo, branch: 3,
            behind_of_upstream=lambda repo, branch: 1,
        ),
        "_standup_cmd": _fake_module(
            gather_data=lambda registry_path: _standup_data(),
        ),
        "_review_cmd": _fake_module(
            ReviewError=type("ReviewError", (Exception,), {}),
            git_state=_fake_module(is_merged=lambda repo, branch, base: False),
            _enrich_project_cards=lambda projects, _run=None: [
                dict(_card(cid="t_r1", status="review", branch="wt/t_r1"))
            ],
            _resolve=lambda cards, projects, card_id: (
                _card(cid=card_id), _project(), "wt/" + card_id
            ),
            _branch_facts=lambda repo, branch, base="main": _review_facts(),
            _branch_diff=lambda repo, branch, base="main": _diff_patch(),
            _parse_patch=lambda patch_text: [
                {"path": "newfile.txt", "status": "A", "additions": 2,
                 "deletions": 0,
                 "hunks": [{"header": "@@ -0,0 +1,2 @@", "lines": [
                     {"type": "+", "text": "hello"},
                     {"type": "+", "text": "world"},
                 ]}]},
                {"path": "app.py", "status": "M", "additions": 1,
                 "deletions": 1,
                 "hunks": [{"header": "@@ -1,3 +1,3 @@", "lines": [
                     {"type": "context", "text": "def setup():"},
                     {"type": "-", "text": "    old = True"},
                     {"type": "+", "text": "    new = True"},
                     {"type": "context", "text": "     return 0"},
                 ]}]},
            ],
            _verify_line=lambda body: (True, "pytest"),
            _render_json=lambda *a, **k: {
                "id": "t_abc123", "title": "Add the thing", "board": "default",
                "project": "hscc", "repo": "/tmp/repo", "branch": "wt/t_abc123",
                "base": "main", "subject": "Add the thing", "files_changed": 2,
                "insertions": 10, "deletions": 1, "conflicts": 0, "landed": False,
                "verify_present": True, "verify": "pytest", "apply_outcome": None,
                "dependents": None,
            },
        ),
        "_review_core": _fake_module(
            review_queue=lambda cards, now=None: [
                {"project": (d.get("project") or "hscc"),
                 "card_id": d.get("id"), "branch": d.get("branch"),
                 "age_seconds": 300, "title": d.get("title")}
                for d in (cards or [])
            ],
        ),
        "_qa_cmd": _fake_module(
            _collect=lambda cards, projects, _run=None, _run_verify=None: [
                {"project": "hscc", "repo": "/tmp/repo", "id": "t_q1",
                 "title": "QA me", "status": "review", "branch": "wt/t_q1",
                 "unattributed": False, "verify_present": True, "verify": "pytest",
                 "files": 1, "verify_configured": True, "verify_run": True,
                 "verify_passed": True, "created_at": 1000},
            ],
            _load_manual=lambda _path=None: [
                {"id": "mqa-1", "project": "hscc", "description": "check it",
                 "card_id": None, "added_at": "2026-08-20T00:00:00",
                 "checked": False, "checked_at": None},
            ],
            _render_json=lambda rows, manual: {
                "queue": [{"project": "hscc", "card_id": "t_q1", "title": "QA me",
                           "status": "review", "branch": "wt/t_q1",
                           "unverifiable": False, "verify": "pytest",
                           "files_changed": 1, "verify_configured": True,
                           "verify_run": True, "verify_passed": True,
                           "created_at": 1000}],
                "manual_qa": [{"id": "mqa-1", "project": "hscc",
                               "description": "check it", "card_id": None,
                               "added_at": "2026-08-20T00:00:00", "checked": False,
                               "checked_at": None}],
            },
        ),
        "_why_cmd": _fake_module(
            gather=lambda card_id, projects: _why_story(card_id),
            render_json=lambda story: story,
            UnknownCardError=type("UnknownCardError", (Exception,), {}),
        ),
        "_roadmap_cmd": _fake_module(
            _definite_path=lambda proj: "/tmp/repo/ROADMAP.md",
            _SECTION_HEADING={"now": "Now", "next": "Next", "later": "Later"},
        ),
        "_roadmap_core": _fake_module(
            parse_roadmap=lambda path: _roadmap_result(),
        ),
        "_release_core": _fake_module(
            preconditions=lambda project, version: [],
        ),
        "_metrics_cmd": _fake_module(
            DEFAULT_SINCE_SECONDS=86400,
            gather=lambda projects, since_ts=None, now=None, project=None:
                _metrics_dict(),
            render_json=lambda d: d,
        ),
        "_hygiene_cmd": _fake_module(
            _collect_worktrees=lambda projects: [],
            hygiene=_fake_module(
                CLOSED_STATUSES={"done", "archived", "cancelled"},
                TRIAGE_STATUS="triage",
                DEFAULT_SIMILARITY=0.88,
                build_plan=lambda active, git_facts, worktrees, closed_ids,
                                threshold=0.88: {
                    "duplicates": [], "triage": [], "stale_worktrees": [],
                },
            ),
            _git_facts_for_cards=lambda all_cards, projects, card_ids=None: {},
        ),
    }
    for name, fake in fd.items():
        monkeypatch.setattr(routes_project, name, fake)
    return fd


@pytest.fixture
def running(tmp_path, fakes):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path), addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    import threading

    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _request(running, token, method="GET", path="/v1/standup"):
    import http.client

    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload: dict = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"raw": raw}
    return resp.status, payload


# --------------------------------------------------------------------------- #
# Each endpoint: 200 + expected shape + non-empty speak
# --------------------------------------------------------------------------- #

def test_standup_200_shape(running, token, fakes):
    status, payload = _request(running, token, path="/v1/standup")
    assert status == 200
    assert "needs_you" in payload and "running" in payload and "failing" in payload
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_cards_200_shape(running, token, fakes):
    status, payload = _request(running, token, path="/v1/cards")
    assert status == 200
    assert isinstance(payload["cards"], list) and len(payload["cards"]) == 1
    assert payload["count"] == 1
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_cards_status_filter(running, token, fakes):
    # Real cards have status running; filtering for 'review' yields zero.
    status, payload = _request(running, token, path="/v1/cards?status=review")
    assert status == 200
    assert payload["count"] == 0
    assert payload["cards"] == []
    assert "0" in payload["speak"]


def test_cards_board_param_passed(running, token, fakes, monkeypatch):
    seen = {}

    def fake_list(board=None, include_archived=False):
        seen["board"] = board
        return [_card()]
    monkeypatch.setattr(fakes["_kanban"], "list_cards", fake_list)
    status, payload = _request(running, token, path="/v1/cards?board=myboard")
    assert status == 200
    assert seen.get("board") == "myboard"


def test_card_detail_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/cards/t_abc123")
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["title"] == "Do a thing"
    assert isinstance(payload["speak"], str) and payload["speak"]
    assert "t_abc123" in payload["speak"]


def test_card_detail_404_unknown(running, token, fakes):
    status, payload = _request(running, token, path="/v1/cards/nope")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_review_queue_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/review/queue")
    assert status == 200
    assert isinstance(payload["queue"], list) and len(payload["queue"]) == 1
    assert payload["count"] == 1
    # Row shape from the design.
    row = payload["queue"][0]
    for key in ("project", "card_id", "branch", "age_seconds", "title"):
        assert key in row
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_review_queue_empty_speak(running, token, fakes, monkeypatch):
    monkeypatch.setattr(fakes["_review_cmd"], "_enrich_project_cards",
                        lambda projects, _run=None: [])
    status, payload = _request(running, token, path="/v1/review/queue")
    assert status == 200
    assert payload["count"] == 0
    assert payload["speak"] == "Nothing awaiting review."


def test_review_detail_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/review/t_abc123")
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["conflicts"] == 0
    assert "merges cleanly" in payload["speak"]
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_review_detail_404_unresolvable(running, token, fakes, monkeypatch):
    def raises(cards, projects, card_id):
        raise fakes["_review_cmd"].ReviewError("no card")
    monkeypatch.setattr(fakes["_review_cmd"], "_resolve", raises)
    status, payload = _request(running, token, path="/v1/review/ghost")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_review_detail_is_dry_run_no_mutation(running, token, fakes, monkeypatch):
    """Proof the read path never merges or closes a card.

    We replace the review backing module with one whose _do_apply /
    _real_close_card are BOMBS that raise if ever invoked. A successful 200
    proves the handler never reached them. (The real handler has no call site
    for either; this guards against a future regression that wires merge into
    the GET.)
    """
    calls = []

    def bomb_apply(*a, **k):
        calls.append("_do_apply")
        raise AssertionError("MUTATION: review GET called _do_apply")

    def bomb_close(*a, **k):
        calls.append("_real_close_card")
        raise AssertionError("MUTATION: review GET called _real_close_card")

    base = fakes["_review_cmd"]
    monkeypatch.setattr(
        routes_project, "_review_cmd",
        _fake_module(
            ReviewError=base.ReviewError,
            git_state=base.git_state,
            _enrich_project_cards=base._enrich_project_cards,
            _resolve=base._resolve,
            _branch_facts=base._branch_facts,
            _verify_line=base._verify_line,
            _render_json=base._render_json,
            _do_apply=bomb_apply,
            _real_close_card=bomb_close,
        ),
    )

    status, payload = _request(running, token, path="/v1/review/t_abc123")
    assert status == 200
    assert calls == []  # neither mutation seam was invoked


# --------------------------------------------------------------------------- #
# /v1/review/{id}/diff — per-file diff for a reviewable card
# --------------------------------------------------------------------------- #

def test_review_diff_200_shape(running, token, fakes):
    status, payload = _request(running, token, path="/v1/review/t_abc123/diff")
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["branch"] == "wt/t_abc123"
    assert payload["file_count"] == 2
    assert payload["truncated"] is False
    assert payload["total_lines_served"] == 6
    files = payload["files"]
    assert [f["path"] for f in files] == ["newfile.txt", "app.py"]
    # Shape per file: path/status/counts/hunks, with typed lines.
    nf = files[0]
    assert nf["status"] == "A" and nf["additions"] == 2 and nf["deletions"] == 0
    assert nf["hunks"][0]["header"] == "@@ -0,0 +1,2 @@"
    assert nf["hunks"][0]["lines"] == [
        {"type": "+", "text": "hello"},
        {"type": "+", "text": "world"},
    ]
    ap = files[1]
    assert ap["status"] == "M" and ap["additions"] == 1 and ap["deletions"] == 1
    assert ap["hunks"][0]["lines"][1] == {"type": "-", "text": "    old = True"}
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_review_diff_404_unresolvable(running, token, fakes, monkeypatch):
    def raises(cards, projects, card_id):
        raise fakes["_review_cmd"].ReviewError("no card")
    monkeypatch.setattr(fakes["_review_cmd"], "_resolve", raises)
    status, payload = _request(running, token, path="/v1/review/ghost/diff")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_review_diff_404_branch_missing(running, token, fakes, monkeypatch):
    monkeypatch.setattr(fakes["_review_cmd"], "_branch_diff",
                        lambda repo, branch, base="main": None)
    status, payload = _request(running, token, path="/v1/review/t_abc123/diff")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_review_diff_pagination(running, token, fakes):
    # offset=1&limit=1 returns only the second file; the whole remainder was
    # consumed (offset+limit == file_count) so there is nothing more to fetch.
    status, payload = _request(
        running, token, path="/v1/review/t_abc123/diff?offset=1&limit=1")
    assert status == 200
    assert [f["path"] for f in payload["files"]] == ["app.py"]
    assert payload["offset"] == 1 and payload["limit"] == 1
    assert payload["file_count"] == 2
    assert payload["truncated"] is False
    # limit that leaves more files after the window -> truncated True.
    status, payload = _request(
        running, token, path="/v1/review/t_abc123/diff?offset=0&limit=1")
    assert status == 200
    assert [f["path"] for f in payload["files"]] == ["newfile.txt"]
    assert payload["truncated"] is True  # file index 1 remains to be paged


def test_review_diff_pagination_all_files_ranges(running, token, fakes):
    # limit large enough to cover every file -> not truncated.
    status, payload = _request(
        running, token, path="/v1/review/t_abc123/diff?offset=0&limit=10")
    assert status == 200
    assert len(payload["files"]) == 2
    assert payload["truncated"] is False
    # offset beyond the end -> empty files, still not truncated.
    status, payload = _request(
        running, token, path="/v1/review/t_abc123/diff?offset=99")
    assert status == 200
    assert payload["files"] == []
    assert payload["truncated"] is False


def test_review_diff_line_cap_truncation(running, token, fakes):
    # max_lines=4: the first file (2 lines) fits, the second (4 lines) would
    # push past 4 -> the response stops, truncated=True.
    status, payload = _request(
        running, token, path="/v1/review/t_abc123/diff?max_lines=4")
    assert status == 200
    assert [f["path"] for f in payload["files"]] == ["newfile.txt"]
    assert payload["total_lines_served"] == 2
    assert payload["truncated"] is True


def test_review_diff_line_cap_not_reached(running, token, fakes):
    status, payload = _request(
        running, token, path="/v1/review/t_abc123/diff?max_lines=1000")
    assert status == 200
    assert len(payload["files"]) == 2
    assert payload["truncated"] is False


def test_review_diff_invalid_query_params_degrade(running, token, fakes):
    status, payload = _request(
        running, token, path="/v1/review/t_abc123/diff?limit=abc&offset=-5")
    assert status == 200
    assert payload["offset"] == 0
    assert payload["limit"] == 20  # default when the value is non-numeric
    assert payload["truncated"] is False


def test_review_diff_is_read_only_no_mutation(running, token, fakes, monkeypatch):
    """Proof the diff path never merges or closes a card.

    The handler must only resolve + read git. Any merge/close seam would be a
    mutation on a GET. We swap the whole _review_cmd for one whose mutation
    functions are BOOMS; a successful 200 proves they were never called.
    """
    calls = []

    def bomb_apply(*a, **k):
        calls.append("_do_apply")
        raise AssertionError("MUTATION: diff GET called _do_apply")

    def bomb_close(*a, **k):
        calls.append("_real_close_card")
        raise AssertionError("MUTATION: diff GET called _real_close_card")

    base = fakes["_review_cmd"]
    monkeypatch.setattr(
        routes_project, "_review_cmd",
        _fake_module(
            ReviewError=base.ReviewError,
            _resolve=base._resolve,
            _branch_diff=base._branch_diff,
            _parse_patch=base._parse_patch,
            _do_apply=bomb_apply,
            _real_close_card=bomb_close,
        ),
    )

    status, payload = _request(running, token, path="/v1/review/t_abc123/diff")
    assert status == 200
    assert calls == []  # neither mutation seam was invoked


def test_qa_queue_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/qa/queue")
    assert status == 200
    assert isinstance(payload["queue"], list) and len(payload["queue"]) == 1
    assert isinstance(payload["manual_qa"], list) and len(payload["manual_qa"]) == 1
    assert isinstance(payload["speak"], str) and payload["speak"]


# --------------------------------------------------------------------------- #
# Auth enforced on these routes too
# --------------------------------------------------------------------------- #

def test_auth_enforced_401(running, fakes):
    status, payload = _request(running, token=None, path="/v1/cards")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_auth_enforced_wrong_token(running, fakes):
    status, payload = _request(running, token="bad", path="/v1/qa/queue")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# Graceful degradation on backing error
# --------------------------------------------------------------------------- #

def test_standup_degrades_on_backing_error(running, token, fakes, monkeypatch):
    def boom(registry_path):
        raise RuntimeError("boom")
    monkeypatch.setattr(fakes["_standup_cmd"], "gather_data", boom)
    status, payload = _request(running, token, path="/v1/standup")
    assert status == 200
    assert "error" in payload  # honest degraded marker, never fabricated counts
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_cards_degrades_on_backing_error(running, token, fakes, monkeypatch):
    def boom(board=None, include_archived=False):
        raise RuntimeError("boom")
    monkeypatch.setattr(fakes["_kanban"], "list_cards", boom)
    status, payload = _request(running, token, path="/v1/cards")
    assert status == 200
    assert payload["cards"] == [] and payload["count"] == 0
    assert "unavailable" in payload["speak"]


def test_review_queue_degrades_on_backing_error(running, token, fakes, monkeypatch):
    def boom(projects, _run=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(fakes["_review_cmd"], "_enrich_project_cards", boom)
    status, payload = _request(running, token, path="/v1/review/queue")
    assert status == 200
    assert payload["queue"] == [] and payload["count"] == 0
    assert "unavailable" in payload["speak"]


def test_qa_queue_degrades_on_backing_error(running, token, fakes, monkeypatch):
    def boom(cards, projects, _run=None, _run_verify=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(fakes["_qa_cmd"], "_collect", boom)
    status, payload = _request(running, token, path="/v1/qa/queue")
    assert status == 200
    assert payload["queue"] == [] and payload["manual_qa"] == []
    assert "unavailable" in payload["speak"]


# --------------------------------------------------------------------------- #
# speak helpers are pure / unit-testable with no I/O
# --------------------------------------------------------------------------- #

def test_speak_pure_helpers():
    assert "Nothing needs attention." == routes_project._speak_standup(
        {"needs_you": [], "running": [], "failing": []}
    )
    s = routes_project._speak_standup(
        {"needs_you": [1], "running": [1, 2], "failing": [1]}
    )
    assert "1 card" in s and "2 are running" in s and "1 failing" in s
    assert routes_project._speak_review_queue({"count": 0}) == "Nothing awaiting review."
    assert "3 cards await review" in routes_project._speak_review_queue({"count": 3})

# --------------------------------------------------------------------------- #
# /v1/projects (registry list) + /v1/projects/{name} detail + scoped standup
# --------------------------------------------------------------------------- #

def test_projects_list_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/projects")
    assert status == 200
    assert payload["count"] == 1
    row = payload["projects"][0]
    assert row["name"] == "hscc"
    assert row["repo"] == "/tmp/repo"
    assert row["board"] == "default"
    assert row["topic"] == "unknown"
    assert "1 project registered." in payload["speak"]


def test_projects_list_auth_401(running):
    status, payload = _request(running, None, path="/v1/projects")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_project_detail_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/projects/hscc")
    assert status == 200
    assert payload["name"] == "hscc"
    assert payload["board"] == "default"
    # board_counts: the fake list_cards returns one running card by default.
    assert payload["board_counts"].get("running") == 1
    assert payload["board_counts"].get("total") == 1
    assert payload["git"]["is_repo"] is True
    assert payload["git"]["branch"] == "main"
    assert payload["git"]["last_activity_seconds_ago"] == 120
    assert payload["git"]["ahead"] == 3
    assert payload["git"]["behind"] == 1
    assert payload["speak"]


def test_project_detail_unknown_404(running, token, fakes, monkeypatch):
    def raise_notfound(name, path=None):
        raise fakes["_registry"].ProjectNotFoundError
    monkeypatch.setattr(fakes["_registry"], "get_project", raise_notfound)
    status, payload = _request(running, token, path="/v1/projects/ghost")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_project_detail_auth_401(running):
    status, payload = _request(running, None, path="/v1/projects/hscc")
    assert status == 401


def test_project_standup_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/projects/hscc/standup")
    assert status == 200
    assert status == 200
    assert isinstance(payload.get("speak"), str) and payload["speak"]
    assert isinstance(payload.get("running"), list)


def test_project_standup_unknown_404(running, token, fakes, monkeypatch):
    def raise_notfound(name, path=None):
        raise fakes["_registry"].ProjectNotFoundError
    monkeypatch.setattr(fakes["_registry"], "get_project", raise_notfound)
    status, payload = _request(running, token, path="/v1/projects/ghost/standup")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_project_standup_auth_401(running):
    status, payload = _request(running, None, path="/v1/projects/hscc/standup")
    assert status == 401


# --------------------------------------------------------------------------- #
# speak helpers for the projects surface
# --------------------------------------------------------------------------- #

def test_projects_speak_pure_helpers():
    assert routes_project._speak_projects_list({"projects": [], "count": 0}) == "0 projects registered."
    assert routes_project._speak_projects_list({"projects": [1, 2], "count": 2}) == "2 projects registered."
    d = routes_project._speak_project_detail({
        "name": "hscc", "board": "hscc",
        "board_counts": {"running": 2, "ready": 1, "done": 5},
    })
    assert "2 running" in d and "3 open cards" in d
    s = routes_project._speak_project_standup({"needs_you": [], "running": [], "failing": []})
    assert s == "Nothing needs attention."


# --------------------------------------------------------------------------- #
# Portfolio: why / roadmap / incidents / release / metrics / hygiene
# --------------------------------------------------------------------------- #

def test_why_200_shape(running, token, fakes):
    status, payload = _request(running, token, path="/v1/why/t_abc123")
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["title"] == "Do a thing"
    assert payload["verdict"]
    assert isinstance(payload["speak"], str) and payload["speak"]
    assert "in progress" in payload["speak"]


def test_why_404_unknown_card(running, token, fakes, monkeypatch):
    def raise_unknown(card_id, projects):
        raise fakes["_why_cmd"].UnknownCardError("no card")
    monkeypatch.setattr(fakes["_why_cmd"], "gather", raise_unknown)
    status, payload = _request(running, token, path="/v1/why/ghost")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_why_route_requires_card_id(running, token, fakes):
    # A bare /v1/why/ (empty card id) matches no route -> 404 not_found.
    status, payload = _request(running, token, path="/v1/why/")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_why_degrades_on_backing_error(running, token, fakes, monkeypatch):
    def boom(card_id, projects):
        raise RuntimeError("boom")
    monkeypatch.setattr(fakes["_why_cmd"], "gather", boom)
    status, payload = _request(running, token, path="/v1/why/t_abc123")
    assert status == 200
    assert "unavailable" in payload["speak"]


def test_why_auth_401(running):
    status, payload = _request(running, None, path="/v1/why/t_abc123")
    assert status == 401


def test_roadmap_200_shape(running, token, fakes):
    status, payload = _request(running, token, path="/v1/projects/hscc/roadmap")
    assert status == 200
    assert payload["present"] is True
    assert payload["name"] == "hscc"
    ms = payload["milestones"]
    assert "Now" in ms and "Next" in ms and "Later" in ms
    assert ms["Now"]["done"] == 1 and ms["Now"]["total"] == 2
    assert ms["Now"]["items"][0]["checked"] is True
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_roadmap_missing_roadmap_present_false(running, token, fakes, monkeypatch):
    monkeypatch.setattr(fakes["_roadmap_core"], "parse_roadmap",
                        lambda path: _roadmap_result(present=False))
    status, payload = _request(running, token, path="/v1/projects/hscc/roadmap")
    assert status == 200
    assert payload["present"] is False
    assert "no roadmap" in payload["speak"]


def test_roadmap_unknown_project_404(running, token, fakes, monkeypatch):
    def raise_notfound(name, path=None):
        raise fakes["_registry"].ProjectNotFoundError
    monkeypatch.setattr(fakes["_registry"], "get_project", raise_notfound)
    status, payload = _request(running, token, path="/v1/projects/ghost/roadmap")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_incidents_200_shape(running, token, fakes, tmp_path, monkeypatch):
    repo = str(tmp_path / "repo")
    import os
    os.makedirs(os.path.join(repo, "docs"), exist_ok=True)
    with open(os.path.join(repo, "docs", "INCIDENTS.md"), "w") as fh:
        fh.write(
            "# Incidents\n\n"
            "## 2026-08-20 — topic not mapped\n"
            "**Project:** hscc\n"
            "**Symptom:** out of memory\n"
            "**Cause:** unbounded cache\n"
            "**Fix:** ran topics bind\n"
            "**Lesson:** always bind topics\n"
        )
    proj = types.SimpleNamespace(name="hscc", repo=repo, board="d")
    monkeypatch.setattr(fakes["_registry"], "load_registry",
                        lambda path=None: [proj])
    monkeypatch.setattr(fakes["_registry"], "get_project",
                        lambda name, path=None: proj)
    status, payload = _request(running, token, path="/v1/projects/hscc/incidents")
    assert status == 200
    assert payload["present"] is True
    entries = payload["incidents"]
    assert len(entries) == 1
    e = entries[0]
    assert e["date"] == "2026-08-20"
    assert e["heading"] == "topic not mapped"
    assert e["project"] == "hscc"
    assert e["cause"] == "unbounded cache"
    assert e["lesson"] == "always bind topics"
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_incidents_parse_pure():
    text = (
        "# Incidents\n\n"
        "## 2026-08-20 — thing broke\n"
        "**Project:** hscc\n"
        "**Symptom:** crash\n"
        "**Cause:** x\n"
        "**Fix:** y\n"
        "**Lesson:** z\n"
    )
    entries = routes_project._parse_incidents(text)
    assert len(entries) == 1
    assert entries[0]["date"] == "2026-08-20"
    assert entries[0]["heading"] == "thing broke"
    assert entries[0]["lesson"] == "z"


def test_incidents_missing_log_present_false(running, token, fakes, tmp_path):
    import os
    proj = types.SimpleNamespace(name="hscc", repo=str(tmp_path / "norepo"),
                                 board="d")
    # default fakes load_registry returns [_project()] with repo /tmp/repo (no file) -> present False
    status, payload = _request(running, token, path="/v1/projects/hscc/incidents")
    assert status == 200
    assert payload["present"] is False
    assert payload["incidents"] == []
    assert "no incident" in payload["speak"] or "0" in payload["speak"]


def test_release_ready_200(running, token, fakes):
    status, payload = _request(running, token,
                               path="/v1/projects/hscc/release?version=1.5.0")
    assert status == 200
    assert payload["ready"] is True
    assert payload["version"] == "1.5.0"
    assert len(payload["plan"]) == 7
    assert isinstance(payload["speak"], str) and payload["speak"]
    assert "release-ready" in payload["speak"]


def test_release_blocked_200(running, token, fakes, monkeypatch):
    monkeypatch.setattr(
        fakes["_release_core"], "preconditions",
        lambda project, version: [
            types.SimpleNamespace(code="dirty", message="tree is dirty"),
            types.SimpleNamespace(code="wrong-branch", message="on feature"),
        ],
    )
    status, payload = _request(running, token,
                               path="/v1/projects/hscc/release?version=1.5.0")
    assert status == 200
    assert payload["ready"] is False
    assert [p["code"] for p in payload["problems"]] == ["dirty", "wrong-branch"]
    assert isinstance(payload["speak"], str) and "NOT release-ready" in payload["speak"]


def test_release_missing_version_400(running, token, fakes):
    status, payload = _request(running, token, path="/v1/projects/hscc/release")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_release_unknown_project_404(running, token, fakes, monkeypatch):
    def raise_notfound(name, path=None):
        raise fakes["_registry"].ProjectNotFoundError
    monkeypatch.setattr(fakes["_registry"], "get_project", raise_notfound)
    status, payload = _request(running, token,
                               path="/v1/projects/ghost/release?version=1.0")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_metrics_200_shape(running, token, fakes):
    status, payload = _request(running, token, path="/v1/projects/hscc/metrics")
    assert status == 200
    assert payload["name"] == "hscc"
    m = payload["metrics"]
    assert m["merged_count"] == 2
    assert m["reviewed"] == 3
    assert m["first_time_pass"]["rate"] == 0.67
    assert m["window"]["days"] == 1.0
    assert isinstance(payload["speak"], str) and "2 merged" in payload["speak"]


def test_metrics_degrades_on_backing_error(running, token, fakes, monkeypatch):
    def boom(projects, since_ts=None, now=None, project=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(fakes["_metrics_cmd"], "gather", boom)
    status, payload = _request(running, token, path="/v1/projects/hscc/metrics")
    assert status == 200
    assert payload["metrics"] is None
    assert "unavailable" in payload["speak"]


def test_metrics_unknown_project_404(running, token, fakes, monkeypatch):
    def raise_notfound(name, path=None):
        raise fakes["_registry"].ProjectNotFoundError
    monkeypatch.setattr(fakes["_registry"], "get_project", raise_notfound)
    status, payload = _request(running, token, path="/v1/projects/ghost/metrics")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_hygiene_clean_200(running, token, fakes):
    status, payload = _request(running, token, path="/v1/projects/hscc/hygiene")
    assert status == 200
    assert payload["name"] == "hscc"
    assert payload["duplicates"] == [] and payload["triage"] == []
    assert payload["stale_worktrees"] == []
    assert payload["issue_count"] == 0
    assert "clean" in payload["speak"]


def test_hygiene_with_issues_filters_to_project(running, token, fakes,
                                                monkeypatch):
    # Duplicate: keep card belongs to hscc -> included; a keep card that is
    # unattributed (project_for_card returns None) -> dropped.
    dups = [
        {"keep": {"id": "t_keep1"}, "board": "default", "title": "Dup thing",
         "archive": [{"id": "t_dup2"}]},
        {"keep": {"id": "t_loner"}, "board": "x", "title": "Other",
         "archive": []},
    ]
    triage = [
        {"card": {"id": "t_tr1", "board": "default", "title": "Trap"},
         "branch": "wt/t_tr1", "branch_has_work": True, "commits_ahead": 2},
    ]
    stale = [
        {"card_id": "t_st1", "board": "default",
         "worktree": "/tmp/repo/.worktrees/t_st1"},
        {"card_id": "t_st2", "board": "x",
         "worktree": "/tmp/other/.worktrees/t_st2"},
    ]
    monkeypatch.setattr(
        fakes["_hygiene_cmd"].hygiene, "build_plan",
        lambda active, git_facts, worktrees, closed_ids, threshold=0.88: {
            "duplicates": dups, "triage": triage, "stale_worktrees": stale,
        },
    )
    # project_for_card returns hscc for the cards we care about, but None for
    # the "loner" so its duplicate is filtered out.
    def pfc(card, projects):
        if card.get("id") in ("t_keep1", "t_tr1"):
            return _project()
        return None  # unattributed
    monkeypatch.setattr(fakes["_kanban"], "project_for_card", pfc)
    # list_cards must surface the cards the handler attributes back to a project.
    def lc(board=None, include_archived=False):
        return [
            {"id": "t_keep1", "board": "default", "status": "running"},
            {"id": "t_tr1", "board": "default", "status": "triage"},
            {"id": "t_st1", "board": "default", "status": "done"},
            {"id": "t_st2", "board": "x", "status": "done"},
            {"id": "t_loner", "board": "x", "status": "running"},
        ]
    monkeypatch.setattr(fakes["_kanban"], "list_cards", lc)
    monkeypatch.setattr(fakes["_hygiene_cmd"], "_collect_worktrees",
                        lambda projects: stale)
    status, payload = _request(running, token, path="/v1/projects/hscc/hygiene")
    assert status == 200
    assert len(payload["duplicates"]) == 1
    assert payload["duplicates"][0]["keep"] == "t_keep1"
    assert len(payload["triage"]) == 1
    assert payload["triage"][0]["card_id"] == "t_tr1"
    # stale worktree under /tmp/repo (hscc) included; under /tmp/other excluded
    assert len(payload["stale_worktrees"]) == 1
    assert payload["stale_worktrees"][0]["card_id"] == "t_st1"
    assert payload["issue_count"] == 3
    assert isinstance(payload["speak"], str) and "3" in payload["speak"]


def test_hygiene_degrades_on_backing_error(running, token, fakes, monkeypatch):
    def boom(board=None, include_archived=True):
        raise RuntimeError("boom")
    monkeypatch.setattr(fakes["_kanban"], "list_cards", boom)
    status, payload = _request(running, token, path="/v1/projects/hscc/hygiene")
    assert status == 200
    assert payload["issue_count"] == 0
    assert "unavailable" in payload["speak"]


def test_portfolio_auth_401(running):
    for path in ("/v1/projects/hscc/roadmap", "/v1/projects/hscc/incidents",
                 "/v1/projects/hscc/release?version=1.0",
                 "/v1/projects/hscc/metrics", "/v1/projects/hscc/hygiene"):
        status, payload = _request(running, None, path=path)
        assert status == 401

