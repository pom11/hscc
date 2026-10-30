"""The ONE systemd --user unit name for the hscc_daemon service (t_d8f13693).

WHY THIS MODULE EXISTS
----------------------
Until this card the two Linux install paths wrote DIFFERENT unit names for the
SAME daemon: ``systemd-setup.sh`` wrote ``hscc_daemon.service``,
``install.py`` wrote ``com.hermes.hscc_daemon.service``, and
``hscc-cluster/bootstrap.sh`` probed the legacy path. Three consequences, all
silent:

  * a box installed by one path was invisible to anything hardcoded to the
    other name (status/doctor/restart logic, operator muscle memory);
  * ``systemctl --user disable <one>`` left the <other> enabled, so an
    operator could "stop" the daemon and have it return at next login;
  * a node could end up with BOTH units enabled — the twin class at the
    supervisor level, one level above ``daemon_singleton.py``.

THE RULING (this card)
----------------------
``com.hermes.hscc_daemon.service`` wins. It is the name that already matches
the launchd label (``com.hermes.hscc_daemon``), the plist file name
(``com.hermes.hscc_daemon.plist``) and the desktop-notification app id, so the
service is called ONE thing on both platforms instead of two.
``hscc_daemon.service`` is the LEGACY name: still probed, never written.

SINGLE SOURCE OF TRUTH
----------------------
``install.py`` imports the constants from here; ``systemd-setup.sh`` sources
them out of this file at run time (``sh``-sourced, POSIX, so any shell can
read it); ``restart_daemon.sh``'s candidate list and the migration script are
pinned against them by
``hscc-bootstrap/tests/test_restart_daemon.py`` and
``hscc-bootstrap/tests/test_migrate_systemd_unit_name.py``. A rename therefore
has exactly one edit site, and the drift pin turns any missed reader red in
CI rather than in production.

Address discipline: no real LAN/tailnet address appears here; fixtures use the
documented placeholder convention (10.0.0.x for a LAN node).
"""

from __future__ import annotations

# Keep this file importable with NO dependencies (it is `source`d by bash too).

#: The unit name every emitter writes and every reader should probe FIRST.
UNIT_NAME = "com.hermes.hscc_daemon.service"

#: The retired name written by ``systemd-setup.sh`` before t_d8f13693. Read-only
#: vocabulary: readers may recognise it, NO emitter may write it. Nodes still
#: carrying it are migrated by ``hscc-bootstrap/migrate_systemd_unit_name.py``.
LEGACY_UNIT_NAME = "hscc_daemon.service"

#: Everything a reader must probe, canonical first. ``restart_daemon.sh`` uses
#: this order; the migration script uses the two names individually.
UNIT_CANDIDATES = (UNIT_NAME, LEGACY_UNIT_NAME)

#: The launchd job label / plist base name — kept here so the "one service, one
#: name on both platforms" rule has a single home.
LAUNCHD_LABEL = "com.hermes.hscc_daemon"


def unit_candidates():
    """List copy of the probe order (canonical first)."""
    return list(UNIT_CANDIDATES)
