"""The alert store behind ``hscc notify`` / ``/v1/alerts`` (card N1).

These are the RULES as executable facts, not the transport: dedupe by key,
per-key and global rate limits, the bounded feed, monotonic ids and the
``since`` paging cursor. The route contract tests live in
``hscc-api/tests/test_routes_alerts.py`` and drive the same store over HTTP.

Every test runs on an explicit ``hscc_dir`` (tmp_path) and a pinned clock, so
nothing here reads or writes the operator's live ``~/.hscc/alerts.json`` and a
rate-limit test never sleeps.
"""

import json
from pathlib import Path

import pytest

from hscc_daemon import alerts


NOW = 1_795_000_000.0


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "hscc")


def post(store, now=NOW, **over):
    kwargs = {"title": "T", "body": "B", "level": "info", "key": None}
    kwargs.update(over)
    return alerts.post_alert(hscc_dir=store, now=now, **kwargs)


# --------------------------------------------------------------------------- #
# Create + persist
# --------------------------------------------------------------------------- #

def test_post_persists_and_ids_are_monotonic(store):
    first = post(store, title="One")
    second = post(store, title="Two", now=NOW + 1)

    assert first["created"] is True and first["deduped"] is False
    assert first["alert"]["id"] == 1
    assert second["alert"]["id"] == 2
    assert first["alert"]["ts"].endswith("Z")
    assert first["alert"]["acked"] is False
    assert first["alert"]["acked_at"] is None

    # It survives a reread, i.e. the API and a later CLI agree.
    doc = json.loads((Path(store) / alerts.ALERTS_FILE).read_text())
    assert [a["id"] for a in doc["alerts"]] == [1, 2]
    assert doc["next_id"] == 3


def test_alert_fields_are_the_public_contract(store):
    result = post(store, title="Release", body="shipped 2.16.0",
                  level="action", key="release-2.16.0")
    alert = result["alert"]
    assert alert == {
        "id": 1,
        "ts": alerts._iso(NOW),
        "title": "Release",
        "body": "shipped 2.16.0",
        "level": "action",
        "key": "release-2.16.0",
        "acked": False,
        "acked_at": None,
    }


# --------------------------------------------------------------------------- #
# Dedupe: one condition, not one call
# --------------------------------------------------------------------------- #

def test_same_key_same_content_does_not_re_alert(store):
    first = post(store, key="live-publish", title="Approve", body="BC LIVE")
    again = post(store, key="live-publish", title="Approve", body="BC LIVE",
                 now=NOW + 5)

    assert again["created"] is False
    assert again["deduped"] is True
    assert again["alert"]["id"] == first["alert"]["id"]
    assert alerts.list_alerts(hscc_dir=store)["count"] == 1


def test_changed_content_on_the_same_key_alerts_again(store):
    post(store, key="live-publish", title="Approve", body="BC LIVE")
    changed = post(store, key="live-publish", title="Approve",
                   body="BC LIVE, second project", now=NOW + 61)

    assert changed["created"] is True
    assert changed["alert"]["id"] == 2
    assert alerts.list_alerts(hscc_dir=store)["count"] == 2


def test_same_content_under_a_different_key_is_a_different_condition(store):
    post(store, key="a", title="Same", body="words")
    other = post(store, key="b", title="Same", body="words", now=NOW + 1)
    assert other["created"] is True
    assert other["alert"]["id"] == 2


def test_ack_clears_the_key_so_a_recurrence_can_alert(store):
    first = post(store, key="node-down", title="Node down", body="dgx-2")
    acked = alerts.ack_alert(first["alert"]["id"], hscc_dir=store,
                             now=NOW + 1)
    assert acked["acked"] is True
    assert acked["acked_at"] == alerts._iso(NOW + 1)

    # Inside the per-key rate window it is still rate-limited (that limit is
    # about volume, not identity), so clear the window honestly.
    suppressed = post(store, key="node-down", title="Node down", body="dgx-2",
                      now=NOW + 2)
    assert suppressed["rate_limited"] is True

    recurred = post(store, key="node-down", title="Node down", body="dgx-2",
                    now=NOW + 61)
    assert recurred["created"] is True
    assert recurred["alert"]["id"] == 2


def test_ack_is_idempotent_and_unknown_ids_return_none(store):
    created = post(store)["alert"]
    first = alerts.ack_alert(created["id"], hscc_dir=store, now=NOW + 1)
    again = alerts.ack_alert(created["id"], hscc_dir=store, now=NOW + 30)
    assert first["acked_at"] == again["acked_at"] == alerts._iso(NOW + 1)
    assert alerts.ack_alert(999, hscc_dir=store) is None


# --------------------------------------------------------------------------- #
# Rate limits
# --------------------------------------------------------------------------- #

def test_per_key_rate_limit_suppresses_churn(store):
    post(store, key="flapping", title="Flap", body="up")
    busy = post(store, key="flapping", title="Flap", body="down", now=NOW + 10)

    assert busy["created"] is False
    assert busy["rate_limited"] is True
    assert busy["reason"] == "rate_limited_key"
    assert 49.0 <= busy["retry_after_s"] <= 50.0
    assert alerts.list_alerts(hscc_dir=store)["count"] == 1

    freed = post(store, key="flapping", title="Flap", body="down", now=NOW + 61)
    assert freed["created"] is True


def test_global_rate_limit_caps_unkeyed_traffic(store):
    for i in range(alerts.GLOBAL_MAX_PER_WINDOW):
        assert post(store, title=f"alert {i}", now=NOW)["created"] is True

    refused = post(store, title="one too many", now=NOW + 1)
    assert refused["created"] is False
    assert refused["rate_limited"] is True
    assert refused["reason"] == "rate_limited_global"
    assert refused["retry_after_s"] > 0
    assert alerts.list_alerts(hscc_dir=store)["count"] == alerts.GLOBAL_MAX_PER_WINDOW

    later = post(store, title="allowed again", now=NOW + 61)
    assert later["created"] is True


# --------------------------------------------------------------------------- #
# Bounded feed
# --------------------------------------------------------------------------- #

def test_feed_keeps_only_the_last_max_alerts(store):
    total = alerts.MAX_ALERTS + 5
    for i in range(total):
        # Step past the global window so this test measures the BOUND, not the
        # rate limit; the clock is arithmetic, no sleeping.
        post(store, title=f"alert {i}", now=NOW + i * 61)

    page = alerts.list_alerts(limit=alerts.MAX_ALERTS, hscc_dir=store)
    ids = [a["id"] for a in page["alerts"]]
    assert len(ids) == alerts.MAX_ALERTS
    assert ids[0] == 6 and ids[-1] == total
    assert page["latest_id"] == total
    assert page["has_more"] is False

    nxt = post(store, title="after eviction", now=NOW + (total + 1) * 61)
    assert nxt["alert"]["id"] == total + 1


# --------------------------------------------------------------------------- #
# since paging
# --------------------------------------------------------------------------- #

def test_since_is_exclusive_ascending_and_paged(store):
    for i in range(5):
        post(store, title=f"alert {i}", now=NOW + i * 61)

    page = alerts.list_alerts(since=2, limit=2, hscc_dir=store)
    assert [a["id"] for a in page["alerts"]] == [3, 4]
    assert page["count"] == 2
    assert page["since"] == 2
    assert page["next_since"] == 4
    assert page["latest_id"] == 5
    assert page["has_more"] is True

    rest = alerts.list_alerts(since=page["next_since"], limit=2, hscc_dir=store)
    assert [a["id"] for a in rest["alerts"]] == [5]
    assert rest["has_more"] is False

    drained = alerts.list_alerts(since=5, hscc_dir=store)
    assert drained["alerts"] == []
    assert drained["count"] == 0
    assert drained["next_since"] == 5


def test_no_since_returns_the_newest_page(store):
    for i in range(4):
        post(store, title=f"alert {i}", now=NOW + i * 61)
    page = alerts.list_alerts(limit=2, hscc_dir=store)
    assert [a["id"] for a in page["alerts"]] == [3, 4]
    assert page["has_more"] is True


# --------------------------------------------------------------------------- #
# Validation / robustness
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kwargs", [
    {"title": ""},
    {"title": "x" * (alerts.MAX_TITLE_CHARS + 1)},
    {"body": "x" * (alerts.MAX_BODY_CHARS + 1)},
    {"level": "noisy"},
    {"key": "k" * (alerts.MAX_KEY_CHARS + 1)},
])
def test_invalid_fields_are_refused(store, kwargs):
    with pytest.raises(ValueError):
        post(store, **kwargs)


def test_corrupt_store_reads_as_empty_and_the_next_post_repairs_it(store):
    path = Path(store) / alerts.ALERTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")

    assert alerts.list_alerts(hscc_dir=store)["alerts"] == []
    created = post(store)
    assert created["created"] is True
    assert created["alert"]["id"] == 1
    assert json.loads(path.read_text())["alerts"][0]["id"] == 1


def test_push_sender_is_called_once_per_created_alert(store, monkeypatch):
    from hscc_daemon import alert_push

    seen = []
    monkeypatch.setattr(alert_push, "send",
                        lambda alert: seen.append(alert["id"])
                        or {"sent": True, "reason": "test"})

    created = post(store, key="k")
    post(store, key="k", now=NOW + 1)          # deduped -> no second push
    assert seen == [created["alert"]["id"]]
    assert created["push"] == {"sent": True, "reason": "test"}


def test_push_failure_never_fails_the_alert(store, monkeypatch):
    from hscc_daemon import alert_push

    def boom(alert):
        raise RuntimeError("transport down")

    monkeypatch.setattr(alert_push, "send", boom)
    result = post(store)
    assert result["created"] is True
    assert result["push"]["sent"] is False
    assert result["push"]["reason"] == "sender_error"
    assert alerts.list_alerts(hscc_dir=store)["count"] == 1
