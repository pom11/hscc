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
DAEMON="$HOME/.hermes/plugins/hscc-daemon/hscc.py"
if [ "$(uname -s)" = "Darwin" ]; then
  SERVICE="$HOME/Library/LaunchAgents/com.hermes.hscc-daemon.plist"
else
  SERVICE="$HOME/.config/systemd/user/hscc-daemon.service"
fi
if [ -f "$SERVICE" ]; then
  echo "daemon service present ($SERVICE)"
else
  python3 "$DAEMON" install >/dev/null 2>&1 && echo "daemon service installed ($SERVICE)" \
    || echo "WARNING: daemon install reported issues — run: python3 $DAEMON install"
fi
echo "bootstrap complete"
