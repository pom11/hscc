"""Tests for hscc_daemon.usage — per-bot / per-project token + cost accounting.

These tests build temp Hermes-profile-style directories with hand-made
``state.db`` ``sessions`` tables and assert the aggregation. They pass
``profiles_home`` and ``budget_file`` explicitly (the module's test seams), so
NO test touches the operator's real ``~/.hermes/profiles`` or ``~/.hscc``.
"""

import json
import sqlite3

import pytest

from hscc_daemon import usage


def _make_state_db(profile_dir, sessions):
    """Create profile_dir/state.db with a sessions table populated by sessions.

    ``sessions`` is a list of dicts with any subset of the token/cost keys;
    defaults fill the rest.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(profile_dir / "state.db"))
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0,
            actual_cost_usd REAL DEFAULT 0,
            cost_status TEXT,
            cost_source TEXT
        )"""
    )
    for i, s in enumerate(sessions):
        conn.execute(
            "INSERT INTO sessions (id, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "estimated_cost_usd, actual_cost_usd, cost_status, cost_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                s.get("id", f"s{i}"),
                s.get("input_tokens", 100),
                s.get("output_tokens", 50),
                s.get("cache_read_tokens", 10),
                s.get("cache_write_tokens", 5),
                s.get("reasoning_tokens", 0),
                s.get("estimated_cost_usd", 0.01),
                s.get("actual_cost_usd", 0.0),
                s.get("cost_status", "estimated"),
                s.get("cost_source", "default"),
            ),
        )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# per_bot + per_project attribution
# --------------------------------------------------------------------------- #

def test_per_bot_and_per_project_attribution(tmp_path):
    home = tmp_path / "profiles"
    # A project orchestrator -> belongs to project "pom".
    _make_state_db(home / "pom-orch", [
        {"id": "a", "input_tokens": 1000, "output_tokens": 200,
         "estimated_cost_usd": 0.12},
        {"id": "b", "input_tokens": 3000, "output_tokens": 300,
         "estimated_cost_usd": 0.34},
    ])
    # A plain bot -> project-less (appears in per_bot only).
    _make_state_db(home / "ios-engineer", [
        {"id": "c", "input_tokens": 500, "output_tokens": 50,
         "estimated_cost_usd": 0.05},
    ])

    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "no-budget.json"))

    # per_bot holds both profiles.
    assert set(result["per_bot"]) == {"pom-orch", "ios-engineer"}

    # per_project attributes the -orch profile to "pom".
    assert set(result["per_project"]) == {"pom"}
    pom = result["per_project"]["pom"]
    assert pom["sessions"] == 2
    assert pom["input_tokens"] == 4000
    assert pom["output_tokens"] == 500
    assert pom["total_tokens"] == 4000 + 500 + (10 + 5) * 2
    assert pom["cost_usd"] == pytest.approx(0.12 + 0.34)

    # totals span every tracked bot.
    total = result["total"]
    assert total["sessions"] == 3
    assert total["input_tokens"] == 4000 + 500


def test_project_orch_and_named_project_bots_both_count(tmp_path):
    """Only -orch declared projects are attributed per project; the rest stay
    per-bot. Non-orch project-ish names (e.g. ecofire-bc-engineer) are bots,
    not projects, under this convention."""
    home = tmp_path / "profiles"
    _make_state_db(home / "ecofire-bc-orch", [{"id": "x", "input_tokens": 10}])
    _make_state_db(home / "ecofire-bc-engineer", [{"id": "y", "input_tokens": 20}])

    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "b.json"))
    assert set(result["per_project"]) == {"ecofire-bc"}
    assert set(result["per_bot"]) == {"ecofire-bc-orch", "ecofire-bc-engineer"}
    # The bot's tokens count in the fleet total even though not project-attributed.
    assert result["total"]["input_tokens"] == 30


# --------------------------------------------------------------------------- #
# Cost honesty: cost_tracked only when a source actually priced usage
# --------------------------------------------------------------------------- #

def test_cost_not_tracked_when_source_none(tmp_path):
    home = tmp_path / "profiles"
    _make_state_db(home / "pom-orch", [
        {"id": "a", "estimated_cost_usd": 0.0, "cost_source": "none"},
    ])
    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "b.json"))
    assert result["cost_tracked"] is False
    # No fabrication: spent stays zero, budget never "exceeded".
    assert result["total"]["cost_usd"] == 0.0
    assert result["budget"]["exceeded"] is False


def test_cost_tracked_when_source_present(tmp_path):
    home = tmp_path / "profiles"
    _make_state_db(home / "pom-orch", [
        {"id": "a", "estimated_cost_usd": 3.5, "cost_status": "estimated",
         "cost_source": "openrouter"},
    ])
    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "b.json"))
    assert result["cost_tracked"] is True
    assert result["total"]["cost_usd"] == pytest.approx(3.5)


# --------------------------------------------------------------------------- #
# Budget warning
# --------------------------------------------------------------------------- #

def test_budget_warning_fires_on_real_tracked_spend(tmp_path):
    home = tmp_path / "profiles"
    _make_state_db(home / "pom-orch", [
        {"id": "a", "estimated_cost_usd": 800.0, "cost_source": "openrouter"},
    ])
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"budget_usd": 500.0}))

    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(budget_file))
    b = result["budget"]
    assert b["budget_usd"] == 500.0
    assert b["configured"] is True
    assert b["spent_usd"] == pytest.approx(800.0)
    assert b["exceeded"] is True
    assert b["pct"] == pytest.approx(160.0)
    assert b["remaining_usd"] == pytest.approx(-300.0)


def test_budget_default_when_file_missing(tmp_path):
    home = tmp_path / "profiles"
    _make_state_db(home / "pom-orch", [{"id": "a", "estimated_cost_usd": 10.0}])
    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "absent.json"),
                                 default_budget=250.0)
    b = result["budget"]
    assert b["budget_usd"] == 250.0
    assert b["configured"] is False
    assert b["pct"] == pytest.approx(4.0)


def test_budget_malformed_file_falls_back(tmp_path):
    home = tmp_path / "profiles"
    _make_state_db(home / "x-orch", [{"id": "a"}])
    budget_file = tmp_path / "budget.json"
    budget_file.write_text("not json")
    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(budget_file))
    assert result["budget"]["budget_usd"] == usage.DEFAULT_BUDGET_USD


# --------------------------------------------------------------------------- #
# Best-effort: missing / unreadable profile DBs never raise
# --------------------------------------------------------------------------- #

def test_missing_profiles_home_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = usage.compute_usage(profiles_home=str(missing),
                                 budget_file=str(tmp_path / "b.json"))
    assert result["per_bot"] == {}
    assert result["per_project"] == {}
    assert result["total"]["sessions"] == 0
    assert result["cost_tracked"] is False


def test_profiles_without_state_db_skipped(tmp_path):
    home = tmp_path / "profiles"
    (home / "architect").mkdir(parents=True)   # no state.db
    (home / "default").mkdir(parents=True)     # no state.db
    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "b.json"))
    assert result["per_bot"] == {}
    assert result["total"]["sessions"] == 0


def test_empty_state_db_counts_zero(tmp_path):
    home = tmp_path / "profiles"
    _make_state_db(home / "pom-orch", [])  # table but no rows
    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "b.json"))
    assert result["per_bot"]["pom-orch"]["sessions"] == 0
    assert result["total"]["sessions"] == 0


def test_corrupt_state_db_skipped(tmp_path):
    home = tmp_path / "profiles"
    d = home / "pom-orch"
    d.mkdir(parents=True)
    (d / "state.db").write_bytes(b"\x00\x01 not a real sqlite db")
    # A second, healthy profile ensures we still get results past the corrupt one.
    _make_state_db(home / "ios-engineer", [{"id": "a", "input_tokens": 7}])
    result = usage.compute_usage(profiles_home=str(home),
                                 budget_file=str(tmp_path / "b.json"))
    assert "pom-orch" not in result["per_bot"]
    assert "ios-engineer" in result["per_bot"]
    assert result["total"]["input_tokens"] == 7
