"""The Needs-me feed must not be produced for a parked node (t_81de1863).

WHY THIS EXISTS
---------------
``~/.hscc/alerts.json`` — this store — is the ONLY source of the operator's
"Needs me" rows (``hscc notify list``, ``GET /v1/alerts``, the iOS
``NeedsMeWidget`` and the macOS dropdown all read it). The daemon has no other
writer, so "no Needs-me item is produced for a paused node's absence/down" can
only be honoured here, at the post. The desktop-page half of the same
contradiction is closed in ``trigger.py``; a rule cannot express it there and a
feed cannot express it at read time — an alert that exists but is filtered at
render time still occupies an id, a dedupe slot and the operator's ack queue,
and it would be a second source of truth about what the operator was told.

NOT A RATE LIMIT, AND THAT IS THE POINT
--------------------------------------
A rate limit is a delay with a real slot behind it (``retry_after_s`` says when
to ask again). A parked node is not a delay. So the refusal runs BEFORE the
store lock and before any store read: nothing is written, no id is consumed, no
dedupe slot is taken and no per-key rate stamp is laid down. The strongest form
of that is pinned directly — the post that follows the resume is created at the
SAME pinned clock the refused post used, which a consumed rate slot would have
turned into ``rate_limited_key``.

BOTH HALVES, ALWAYS
-------------------
Every property is pinned from both sides: the parked node's alert is refused,
and an unscoped post, an unrelated node, or a broken overlay posts exactly as
before. A feed that quietly drops alerts is worse than one that pages too much.

ASK ``covering``, NEVER ``paused``
---------------------------------
A tensor-parallel unit stops as one thing, so the sibling node named only in the
record's ``span_nodes`` is just as deliberately down. The pin asserts the two
APIs answer DIFFERENTLY and that the behaviour follows ``covering``.

THE OVERLAY'S ROOT IS ``node_pauses``' OWN
------------------------------------------
Deliberately NOT ``hscc_dir``/``$HSCC_HOME`` (the feed's root), and pinned both
ways: a pause written at the overlay's own root suppresses a post whose feed
lives elsewhere, and a decoy overlay inside the feed's root does NOT suppress
anything. That is the writer/reader root split this package keeps removing —
the pause is written by the CLI through ``node_pauses``, so it must be read
through ``node_pauses``.

SAFETY
------
Every host is TEST-NET-1 (RFC 5737 — documentation space, never this fleet),
every feed runs on an explicit ``hscc_dir`` under tmp_path with a pinned clock,
and every overlay is written through ``node_pauses``' own ``directory=`` seam.
The two default-root tests guard-assert that ``node_pauses.HSCC_DIR`` is the
conftest-sealed tmp root before writing to it. No live ``~/.hscc`` is touched.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

from hscc_daemon import alerts, node_pauses, notify_cli

# TEST-NET-1 (RFC 5737) — documentation space, never routable, never this fleet.
PAUSED_NODE = "192.0.2.10"
SPAN_SIBLING = "192.0.2.11"
UNRELATED_NODE = "192.0.2.30"

NOW = 1_795_000_000.0


def past_the_deadline():
    """An ISO instant strictly past any pause deadline this file creates.

    DERIVED, not pinned. ``pause(until="1h")`` resolves its deadline against
    ``node_pauses``' OWN clock, so a hard-coded far-future witness (this file passed
    ``"2099-01-01T00:00:00+00:00"``) is only "past due" while the real clock sits below
    it. t_0389093a measured ``test_the_expiry_path_restores_the_post`` red at frozen
    2200-01-01 and at ``HSCC_WALL_SHIFT_DAYS=+63225`` — the pause was then genuinely
    not yet expired and the product was right to keep suppressing. ±400 days (the
    shipped ``test_single_clock_rule.py`` travel) never reaches it, and this file owns
    no ``datetime(20xx,`` literal, so the rule never ran it at any shift.
    """
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


@pytest.fixture
def store(tmp_path):
    """An explicit feed root (never the operator's ~/.hscc/alerts.json)."""
    return str(tmp_path / "feed")


@pytest.fixture
def overlay(tmp_path):
    """A temp overlay root for this test's pauses (node_pauses' only seam)."""
    d = tmp_path / "overlay"
    d.mkdir()
    return str(d)


def post(store, now=NOW, **over):
    kwargs = {"title": "T", "body": "B", "level": "info", "key": None}
    kwargs.update(over)
    return alerts.post_alert(hscc_dir=store, now=now, **kwargs)


def pause(host, directory, **kw):
    """Record a pause through the real producer, not hand-planted JSON."""
    return node_pauses.pause(host, directory=directory, **kw)


def feed_rows(store):
    path = os.path.join(store, alerts.ALERTS_FILE)
    if not os.path.exists(path):
        return []
    return json.loads(open(path).read()).get("alerts") or []


# ── the refusal ────────────────────────────────────────────────────────────


class TestParkedNodeAlertRefused:
    def test_scoped_post_is_refused_and_nothing_is_produced(self, store,
                                                            overlay):
        pause(PAUSED_NODE, overlay, reason="training run")
        result = post(store, title="node down", body="worker not answering",
                      level="critical", key="node/down",
                      node=PAUSED_NODE, pauses_directory=overlay)
        assert result == {
            "created": False,
            "deduped": False,
            "rate_limited": False,
            "paused": True,
            "reason": alerts.PAUSED_REASON,
            "alert": None,
            "retry_after_s": None,
            "paused_node": PAUSED_NODE,
            "paused_reason": "training run",
        }
        assert feed_rows(store) == []
        assert not os.path.exists(os.path.join(store, alerts.ALERTS_FILE)), (
            "a refused post must not even create the feed document")

    def test_span_sibling_alert_is_refused_too(self, store, overlay):
        """covering() vs paused(): the pause is keyed to the sibling and names
        PAUSED_NODE only in span_nodes, so the two APIs DISAGREE here and the
        behaviour must follow covering()."""
        pause(SPAN_SIBLING, overlay, reason="span parked as one unit",
              span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        assert node_pauses.paused(PAUSED_NODE, directory=overlay) is None
        assert node_pauses.covering(PAUSED_NODE,
                                    directory=overlay) is not None
        result = post(store, node=PAUSED_NODE, pauses_directory=overlay)
        assert result["paused"] is True
        assert result["paused_reason"] == "span parked as one unit"
        assert feed_rows(store) == []

    def test_whitespace_on_the_scope_is_trimmed_not_rejected(self, store,
                                                             overlay):
        pause(PAUSED_NODE, overlay)
        result = post(store, node="  " + PAUSED_NODE + "  ",
                      pauses_directory=overlay)
        assert result["paused"] is True and result["created"] is False

    def test_a_pause_with_no_reason_still_refuses_and_says_no_reason(
            self, store, overlay):
        pause(PAUSED_NODE, overlay)
        result = post(store, node=PAUSED_NODE, pauses_directory=overlay)
        assert result["paused"] is True
        assert result["paused_reason"] is None

    def test_pause_recorded_past_its_deadline_still_holds(self, store, overlay):
        """``paused()`` is truth-accurate: an expired pause is still a pause
        until the daemon's own resume path ends it, so the feed stays quiet
        across the deadline instead of producing an item minutes early."""
        pause(PAUSED_NODE, overlay, until="1h", reason="training run")
        later = post(store, node=PAUSED_NODE, now=NOW + 7200,
                     pauses_directory=overlay)
        assert later["paused"] is True
        assert feed_rows(store) == []


# ── nothing was consumed: the resume posts at once ─────────────────────────


class TestRefusalConsumesNothing:
    def test_the_post_after_the_resume_is_created_at_the_same_clock(
            self, store, overlay):
        """The strongest no-stale-suppression pin: a consumed per-key rate slot
        would answer this with rate_limited_key for 60 s, so this test goes red
        the moment the refusal starts touching the store's rate map."""
        key = "node/down"
        pause(PAUSED_NODE, overlay, reason="training run")
        refused = post(store, title="worker not answering", level="critical",
                       key=key, node=PAUSED_NODE, pauses_directory=overlay)
        assert refused["paused"] is True

        assert node_pauses.clear(PAUSED_NODE, directory=overlay) is not None
        after = post(store, title="worker not answering", level="critical",
                     key=key, pauses_directory=overlay)
        assert after["created"] is True, after
        assert after["rate_limited"] is False and after["deduped"] is False
        assert after["alert"]["id"] == 1, (
            "the refusal consumed an id — the feed's ordering promise would "
            "say an alert the operator never saw existed")

    def test_the_expiry_path_restores_the_post(self, store, overlay):
        pause(PAUSED_NODE, overlay, until="1h", reason="training run")
        assert post(store, node=PAUSED_NODE, pauses_directory=overlay)["paused"]
        node_pauses.expiry_pass(now=past_the_deadline(),
                                directory=overlay)
        assert post(store, node=PAUSED_NODE,
                    pauses_directory=overlay)["created"] is True

    def test_a_live_dedupe_slot_survives_a_refused_post(self, store, overlay):
        """The refusal is not a clear: an alert the operator has not seen keeps
        its slot, so the next identical post still dedupes."""
        key = "disk/filling"
        first = post(store, title="Disk filling", body="92%", key=key)
        assert first["created"] is True
        pause(PAUSED_NODE, overlay)
        refused = post(store, title="Disk filling", body="92%", key=key,
                       node=PAUSED_NODE, pauses_directory=overlay)
        assert refused["paused"] is True
        again = post(store, title="Disk filling", body="92%", key=key)
        assert again["deduped"] is True and again["alert"]["id"] == 1

    def test_the_feed_document_is_byte_identical_across_a_refusal(self, store,
                                                                  overlay):
        """Not one byte moves: same content, same mtime, same nothing."""
        post(store, title="Before", key="before")
        path = os.path.join(store, alerts.ALERTS_FILE)
        before = (open(path, "rb").read(), os.path.getmtime(path))
        pause(PAUSED_NODE, overlay, reason="training run")
        post(store, title="Parked", node=PAUSED_NODE,
             pauses_directory=overlay)
        assert (open(path, "rb").read(), os.path.getmtime(path)) == before


# ── the premise of the expiry case, checked executably ─────────────────────


class TestThePastDueWitnessIsDerived:
    """Why ``expiry_pass(now=...)`` must be DERIVED here (t_0389093a).

    The shipped clock rule travels ±400 days and cannot see a premise that only breaks
    in 2099, so the bite is a source guard: a pinned instant may not be handed to the
    product as its clock. Re-pinning the expiry call to
    ``2099-01-01T00:00:00+00:00`` reddens this class on any host, immediately.
    """

    def test_no_pinned_instant_is_handed_to_the_product_as_now(self):
        with open(__file__) as fh:
            pinned = [ln.strip() for ln in fh
                      if re.search(r'(now|as_of)\s*=\s*"2\d{3}-', ln)]
        assert not pinned, (
            "a hard-coded far-future instant is being used as the product's clock — "
            "the case is then green only while the real clock sits below it "
            f"(derive it, see past_the_deadline): {pinned}")

    def test_the_witness_is_far_past_any_deadline_this_file_writes(self):
        # `until="1h"` is resolved by the producer against its own clock, so the
        # MARGIN is what makes a pause expired — never the calendar year.
        assert node_pauses._utc(past_the_deadline()) > (
            node_pauses._utc(None) + timedelta(hours=12))


# ── what must still be produced ────────────────────────────────────────────


class TestNothingElseIsRefused:
    def test_unscoped_post_is_exactly_todays_behaviour(self, store, overlay):
        pause(PAUSED_NODE, overlay, reason="training run")
        result = post(store, title="Release", body="shipped", level="action",
                      key="release")
        assert result["created"] is True
        assert result["paused"] is False
        assert result["reason"] == "created"

    def test_a_pause_on_an_unrelated_node_does_not_refuse(self, store, overlay):
        pause(UNRELATED_NODE, overlay, reason="training run")
        assert post(store, node=PAUSED_NODE,
                    pauses_directory=overlay)["created"] is True

    def test_blank_scope_is_unscoped_not_an_error(self, store, overlay):
        """``--node "$MAYBE_EMPTY"`` from a script must behave like the flag
        being absent — and posting is the safe direction for a false page."""
        pause(PAUSED_NODE, overlay)
        for blank in ("", "   "):
            assert post(store, node=blank,
                        pauses_directory=overlay)["created"] is True

    @pytest.mark.parametrize("junk", [[PAUSED_NODE], 1234, {"h": PAUSED_NODE},
                                      True])
    def test_a_non_string_scope_is_a_client_error(self, store, overlay, junk):
        """A list of hosts is not a host; picking one is the string-matching
        this seam refuses. 400 at the route, loudly, never a guess."""
        with pytest.raises(ValueError):
            post(store, node=junk, pauses_directory=overlay)

    def test_malformed_overlay_fails_toward_alerting(self, store, tmp_path):
        root = tmp_path / "broken"
        root.mkdir()
        (root / node_pauses.OVERLAY_NAME).write_text("{not json at all")
        assert post(store, node=PAUSED_NODE,
                    pauses_directory=str(root))["created"] is True

    def test_missing_overlay_is_the_normal_case(self, store, tmp_path):
        root = tmp_path / "no-overlay"
        root.mkdir()
        assert post(store, node=PAUSED_NODE,
                    pauses_directory=str(root))["created"] is True

    def test_validation_still_wins_over_the_pause(self, store, overlay):
        """A malformed request is a malformed request whether or not the node
        is parked — the pause must not swallow a 400."""
        pause(PAUSED_NODE, overlay)
        with pytest.raises(ValueError):
            post(store, title="", node=PAUSED_NODE, pauses_directory=overlay)
        with pytest.raises(ValueError):
            post(store, title="ok", level="loud", node=PAUSED_NODE,
                 pauses_directory=overlay)

    def test_the_envelope_shape_is_stable_across_every_reason(self, store,
                                                              overlay):
        """Clients read ``paused`` the same way they read ``created``: a key
        that sometimes exists is a lie of omission."""
        pause(PAUSED_NODE, overlay)
        shapes = []
        shapes.append(post(store, node=PAUSED_NODE, pauses_directory=overlay))
        created = post(store, title="A", key="k1")
        shapes.append(created)
        shapes.append(post(store, title="A", key="k1"))          # deduped
        shapes.append(post(store, title="A", key="k1", now=NOW + 5))
        for result in shapes:
            assert {"created", "deduped", "rate_limited", "paused", "reason",
                    "alert", "retry_after_s"} <= set(result)
        assert created["created"] is True


# ── the two roots stay two (the split this package keeps removing) ─────────


class TestOverlayRootIsNotTheFeedRoot:
    def test_a_pause_at_the_overlays_own_root_suppresses_a_faraway_feed(
            self, tmp_path):
        """The CLI writes the pause through node_pauses; the API/CLI read it
        through node_pauses. The feed root is a different directory and must
        not be able to un-scope the suppression."""
        root = node_pauses.HSCC_DIR
        # Guard first: this test writes at the DEFAULT root, so refuse to run
        # unless conftest has sealed that constant into this test's tmp tree.
        assert os.path.realpath(root) == os.path.realpath(str(tmp_path / "hscc")), (
            "conftest's node_pauses.HSCC_DIR seal moved; refusing to write a "
            "pause anywhere else")
        node_pauses.pause(PAUSED_NODE, reason="training run")  # default root
        result = post(str(tmp_path / "elsewhere-feed"), node=PAUSED_NODE)
        assert result["paused"] is True
        assert result["paused_reason"] == "training run"

    def test_a_decoy_overlay_inside_the_feed_root_suppresses_nothing(
            self, tmp_path):
        """The mirror half: the overlay root is never derived from
        ``hscc_dir``/``$HSCC_HOME``. An unrelated document sitting in the
        feed's directory cannot mute the feed."""
        feed = str(tmp_path / "feed-with-decoy")
        node_pauses.save({"pauses": {PAUSED_NODE: {"reason": "decoy"}}},
                         directory=feed)
        result = alerts.post_alert(title="T", body="B", level="info",
                                   key=None, node=PAUSED_NODE,
                                   hscc_dir=feed, now=NOW)
        assert result["created"] is True


# ── the verb: --node reaches the feed, and says why a post was refused ────


class TestNotifyVerbScope:
    def test_node_flag_parses_both_spellings(self):
        for argv in (["T", "B", "--node", PAUSED_NODE],
                     ["T", "B", f"--node={PAUSED_NODE}"]):
            opts, error = notify_cli.parse_notify_args(argv)
            assert error is None
            assert opts["node"] == PAUSED_NODE

    def test_no_flag_leaves_the_payload_unscoped(self):
        opts, error = notify_cli.parse_notify_args(["T", "B"])
        assert error is None and opts["node"] is None

    def test_the_payload_carries_the_scope_only_when_given(self, monkeypatch):
        captured = {}

        def fake_call(opts, hscc_dir, path, payload=None, method="POST",
                      label=None):
            captured["payload"] = payload
            return 201, {"created": True}

        monkeypatch.setattr(notify_cli, "_call", fake_call)
        opts, _ = notify_cli.parse_notify_args(["T", "B", "--node",
                                               PAUSED_NODE])
        notify_cli.post_alert(opts)
        assert captured["payload"]["node"] == PAUSED_NODE

        opts, _ = notify_cli.parse_notify_args(["T", "B"])
        notify_cli.post_alert(opts)
        assert "node" not in captured["payload"]

    @pytest.mark.parametrize("argv", [
        ["list", "--node", PAUSED_NODE],
        ["ack", "1", "--node", PAUSED_NODE],
    ])
    def test_scope_is_refused_where_it_means_nothing(self, argv):
        opts, error = notify_cli.parse_notify_args(argv)
        assert error is not None and "--node" in error

    def test_a_flag_that_takes_no_value_is_still_a_usage_error(self):
        opts, error = notify_cli.parse_notify_args(["T", "B", "--nod", "x"])
        assert opts is None and "--nod" in error

    def test_summary_names_the_node_instead_of_a_rate_limit(self):
        payload = {"created": False, "paused": True, "reason": "paused_node",
                   "paused_node": PAUSED_NODE, "paused_reason": "training run"}
        assert notify_cli._summary(payload) == (
            f"alert not posted (node {PAUSED_NODE} is paused: training run)")
        payload_no_reason = {"created": False, "paused": True,
                             "paused_node": PAUSED_NODE}
        assert notify_cli._summary(payload_no_reason) == (
            f"alert not posted (node {PAUSED_NODE} is paused)")

    def test_a_suppression_is_still_not_an_error_exit(self, monkeypatch,
                                                       capsys):
        monkeypatch.setattr(
            notify_cli, "_call",
            lambda *a, **kw: (200, {"created": False, "paused": True,
                                    "reason": "paused_node",
                                    "paused_node": PAUSED_NODE}))
        code = notify_cli.main(["T", "B", "--node", PAUSED_NODE])
        assert code == 0
        assert "is paused" in capsys.readouterr().out


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
