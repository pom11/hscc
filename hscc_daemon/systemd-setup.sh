#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# hscc_daemon systemd --user Setup Helper (Linux)
#
# Installs, enables, and verifies the hscc_daemon systemd --user service.
# This is the Linux counterpart of launchd-setup.sh.
#
# THE UNIT NAME (t_d8f13693). This helper used to write `hscc_daemon.service`
# while `hscc install` (install.py) wrote `com.hermes.hscc_daemon.service` —
# two names for one daemon, so disabling one left the other enabled and each
# installer was blind to the other's box. The canonical name is now
# `com.hermes.hscc_daemon.service` (it matches the launchd label and the plist
# name); `hscc_daemon.service` is the LEGACY name, retired and purged here.
# Both names, and the unit content, come from hscc_daemon/unit_name.py +
# install.emit_systemd_unit() — this script does not re-type either.
#
# THE UNIT FILE (same card). The unit is no longer produced by
# `hscc.py plist > UNIT_FILE`. That redirected a rendered Rich console view —
# panel borders plus soft-WRAPPED long assignments — into a unit file, and
# systemd's parser silently dropped the pieces: `ExecStart=… -m hscc_daemon`
# with `start-daemon` on a skipped continuation line (so the supervised daemon
# ran the module with no subcommand), and a `PATH=` truncated at the wrap. Use
# `unit-emit`, which writes the raw template.
#
# TWIN RULE: this script never leaves zero or two *enabled* units. The canonical
# unit is enabled + started FIRST and only then is the legacy name purged, so an
# interrupted run can leave a brief overlap (harmless — daemon_singleton.py
# refuses a second loop and systemd retries the supervised start) but never a
# window with no supervisor. For the fully verified version (dry-run capable,
# read-back confirms one enabled + one active unit on every node) use
# hscc-bootstrap/migrate_systemd_unit_name.py.
#
# Usage:
#   ./systemd-setup.sh          # Install, enable + start, verify (idempotent)
#   ./systemd-setup.sh force    # Re-install the unit, then restart
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="${SCRIPT_DIR}/hscc.py"
UNIT_DIR="$HOME/.config/systemd/user"
LOG_FILE="$HOME/.hscc/daemon.log"

# Resolve a REAL python interpreter (same candidate order as install.py's
# _resolve_python and launchd-setup.sh): the unit must exec an interpreter that
# exists and has the daemon's deps, or systemd respawn-loops it (C1).
PYBIN=""
for cand in \
    "${HSCC_PYBIN:-}" \
    "${HOME}/.hermes/hermes-agent/venv/bin/python" \
    "$(command -v python3 2>/dev/null || true)" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "/usr/bin/python3"; do
    if [[ -n "${cand}" && -x "${cand}" ]]; then PYBIN="${cand}"; break; fi
done
if [[ -z "${PYBIN}" ]]; then
    echo "[ERROR] No usable python3 for the daemon unit (tried HSCC_PYBIN, venv, PATH, /opt/homebrew, /usr/local, /usr/bin)" >&2
    exit 1
fi

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Pre-flight ──────────────────────────────────────────────────────────────
if ! command -v systemctl &>/dev/null; then
    error "systemctl not found — this script requires a systemd-based Linux"
    exit 1
fi

mkdir -p "${UNIT_DIR}" "$(dirname "${LOG_FILE}")"

# ── Install unit through the ONE emitter ────────────────────────────────────
# `unit-emit` writes the canonical unit file and prints the names as JSON, so
# this script never hardcodes either name. NO --purge-legacy here: purging the
# old name before the canonical one is enabled + verified would leave this box
# with no supervisor if `enable --now` then failed (see the TWIN RULE header).
EMIT_RC=0
EMIT_JSON="$(PYTHONPATH="${SCRIPT_DIR}/.." "${PYBIN}" "${DAEMON}" unit-emit --unit-dir "${UNIT_DIR}" 2>&1)" || EMIT_RC=$?
if [[ "${EMIT_RC}" -ne 0 ]]; then
    error "unit-emit failed (rc ${EMIT_RC}): ${EMIT_JSON}"
    exit 1
fi

UNIT_NAME="$(printf '%s' "${EMIT_JSON}" | sed -n 's/.*"unit_name": "\([^"]*\)".*/\1/p')"
LEGACY_NAME="$(printf '%s' "${EMIT_JSON}" | sed -n 's/.*"legacy_name": "\([^"]*\)".*/\1/p')"
if [[ -z "${UNIT_NAME}" ]]; then
    error "unit-emit did not report the unit name it wrote — refusing to guess. Output was: ${EMIT_JSON}"
    exit 1
fi
info "Installed unit ${UNIT_NAME} in ${UNIT_DIR}"

systemctl --user daemon-reload

# ── Enable + start ──────────────────────────────────────────────────────────
if [[ "${1:-}" == "force" ]]; then
    systemctl --user restart "${UNIT_NAME}" || true
fi
systemctl --user enable --now "${UNIT_NAME}"
info "Enabled + started ${UNIT_NAME}"

# ── Retire the legacy name, now that supervision is proven ──────────────────
# Second call to the SAME emitter (writing is idempotent) with --purge-legacy:
# still one emitter and one name, but only after the canonical unit is up, which
# is what keeps "never zero active units" true even if enable had failed above.
if [[ -n "${LEGACY_NAME}" ]]; then
    PURGE_RC=0
    PURGE_JSON="$(PYTHONPATH="${SCRIPT_DIR}/.." "${PYBIN}" "${DAEMON}" unit-emit --unit-dir "${UNIT_DIR}" --purge-legacy --json 2>&1)" || PURGE_RC=$?
    if [[ "${PURGE_RC}" -ne 0 ]]; then
        warn "could not retire ${LEGACY_NAME} (rc ${PURGE_RC}): ${PURGE_JSON}"
        warn "  until it is gone, 'systemctl --user disable ${UNIT_NAME}' will leave the daemon running: ${LEGACY_NAME}"
    elif printf '%s' "${PURGE_JSON}" | grep -q '"found": true'; then
        info "Retired legacy unit ${LEGACY_NAME} (it would have kept the daemon enabled under the old name)"
    fi
fi

# Survive logout (no-op if already enabled or not permitted).
loginctl enable-linger "$USER" 2>/dev/null \
    && info "Lingering enabled — daemon survives logout" \
    || warn "Could not enable lingering (daemon stops on logout unless 'loginctl enable-linger $USER' is run as needed)"

# ── Status ──────────────────────────────────────────────────────────────────
echo ""
info "── hscc_daemon Service Status ────────────────────────────────────"
if systemctl --user is-active --quiet "${UNIT_NAME}"; then
    echo -e "  ${GREEN}●${NC} systemd:    active (${UNIT_NAME})"
else
    echo -e "  ${RED}○${NC} systemd:    not active (${UNIT_NAME})"
fi

# The twin check: a second unit for the same daemon still enabled means this box
# has two supervisors for one loop — the class daemon_singleton.py exists for,
# one level up. Say so; never present a half-migrated box as installed.
if [[ -n "${LEGACY_NAME}" ]] && systemctl --user is-enabled "${LEGACY_NAME}" &>/dev/null; then
    warn "legacy unit ${LEGACY_NAME} is still known to systemd — retire it: systemctl --user disable --now ${LEGACY_NAME} && rm -f ${UNIT_DIR}/${LEGACY_NAME} && systemctl --user daemon-reload"
fi

if pgrep -f "hscc.py.*start-daemon" &>/dev/null; then
    PID=$(pgrep -f "hscc.py.*start-daemon" | head -1)
    echo -e "  ${GREEN}●${NC} Process:    running (PID ${PID})"
else
    echo -e "  ${RED}○${NC} Process:    not running (expected to be started by systemd)"
fi

if [[ -d "$HOME/.hscc/state" ]]; then
    COUNT=$(find "$HOME/.hscc/state" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
    echo -e "  ${GREEN}●${NC} State:      ${COUNT} JSON files in ~/.hscc/state/"
else
    echo -e "  ${RED}○${NC} State:      ~/.hscc/state/ not found"
fi

if [[ -f "${LOG_FILE}" ]]; then
    LINES=$(wc -l < "${LOG_FILE}" | tr -d ' ')
    echo -e "  ${GREEN}●${NC} Log:        ${LOG_FILE} (${LINES} lines)"
    echo ""
    echo -e "  ${YELLOW}── Tail last 10 log lines ────────────────────────────────────${NC}"
    tail -10 "${LOG_FILE}" | sed 's/^/    /'
else
    echo -e "  ${YELLOW}○${NC} Log:        ${LOG_FILE} (will be created on first run)"
fi

echo ""
info "── Management Commands ──────────────────────────────────────────"
echo "  Start:   systemctl --user start ${UNIT_NAME}"
echo "  Stop:    systemctl --user stop ${UNIT_NAME}"
echo "  Status:  systemctl --user status ${UNIT_NAME}"
echo "  Log:     tail -f ${LOG_FILE}"
echo "  Uninstall: ${DAEMON} uninstall"
echo ""
