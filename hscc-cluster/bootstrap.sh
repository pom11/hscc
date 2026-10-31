#!/usr/bin/env bash
# Idempotent bootstrap for native-Hermes cluster control.
# Re-running changes nothing once the system is in the desired state.
# Sets the kanban/toolset/plugin config (load -> mutate -> dump, never drops
# keys), verifies the toolset is present, warns if the default kanban assignee
# profile is missing, and installs the daemon auto-start service if absent
# (launchd on macOS, systemd --user on Linux).
set -euo pipefail
CFG="$HOME/.hermes/config.yaml"

# 1. kanban + toolset + plugin config (only writes when something is off)
python3 - "$CFG" <<'PY'
import sys, yaml
p = sys.argv[1]
d = yaml.safe_load(open(p))
k = d.setdefault('kanban', {})
changed = False
for key, val in (('dispatch_in_gateway', True), ('auto_decompose', True),
                 ('auto_decompose_per_tick', 3), ('failure_limit', 2),
                 ('default_assignee', 'worker-246')):
    if k.get(key) != val:
        k[key] = val; changed = True
ts = d.setdefault('toolsets', [])
if 'hscc-cluster' not in ts:
    ts.append('hscc-cluster'); changed = True
# user plugins in ~/.hermes/plugins/ do NOT auto-load — must be opt-in enabled.
pl = d.setdefault('plugins', {})
en = pl.setdefault('enabled', [])
if 'hscc-cluster' not in en:
    en.append('hscc-cluster'); changed = True
if changed:
    yaml.safe_dump(d, open(p, 'w'), sort_keys=False)
    print('config updated')
else:
    print('config already correct (no-op)')
PY

# 2. toolset present?
if [ -d "$HOME/.hermes/plugins/hscc-cluster" ]; then
  echo "toolset present"
else
  echo "MISSING toolset"; exit 1
fi

# 3. default kanban assignee profile present? (warn-only — don't set a phantom)
ASSIGNEE="$(python3 -c "import yaml;print((yaml.safe_load(open('$CFG')).get('kanban') or {}).get('default_assignee',''))")"
if [ -n "$ASSIGNEE" ] && [ ! -d "$HOME/.hermes/profiles/$ASSIGNEE" ]; then
  echo "WARNING: default_assignee '$ASSIGNEE' has no profile at ~/.hermes/profiles/$ASSIGNEE — kanban cards may not dispatch until it exists."
else
  echo "assignee profile present ($ASSIGNEE)"
fi

# 4. daemon auto-start service installed? (launchd on macOS, systemd on Linux)
#
# systemd: check BOTH unit names. com.hermes.hscc_daemon.service is canonical
# since t_d8f13693 (hscc_daemon/unit_name.py, matching the launchd label and the
# plist); hscc_daemon.service is the retired name that nodes installed by the
# pre-fix systemd-setup.sh still carry until
# hscc-bootstrap/migrate_systemd_unit_name.py runs. Missing the legacy name
# here would report "no service" on a healthy, supervised node and then run
# `hscc install` on top of a unit systemd already owns.
DAEMON="$HOME/.hermes/plugins/hscc_daemon/hscc.py"
if [ "$(uname -s)" = "Darwin" ]; then
  SERVICE_CANDIDATES="$HOME/Library/LaunchAgents/com.hermes.hscc_daemon.plist"
else
  SERVICE_CANDIDATES="$HOME/.config/systemd/user/com.hermes.hscc_daemon.service $HOME/.config/systemd/user/hscc_daemon.service"
fi
SERVICE=""
for _cand in $SERVICE_CANDIDATES; do
  if [ -f "$_cand" ]; then SERVICE="$_cand"; break; fi
done
if [ -n "$SERVICE" ]; then
  echo "daemon service present ($SERVICE)"
else
  python3 "$DAEMON" install >/dev/null 2>&1 && echo "daemon service installed" \
    || echo "WARNING: daemon install reported issues — run: python3 $DAEMON install"
fi
echo "bootstrap complete"
