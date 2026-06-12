#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# hscc_daemon Launchd Setup Helper
#
# Installs, loads, and verifies the hscc_daemon Launchd service.
#
# Usage:
#   ./launchd-setup.sh          # Load and verify (idempotent)
#   ./launchd-setup.sh force    # Force unload then load
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.hermes.hscc_daemon.plist"
PLIST_TEMPLATE="${SCRIPT_DIR}/${PLIST_NAME}.template"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LAUNCH_PLIST="${LAUNCH_DIR}/${PLIST_NAME}"
LABEL="com.hermes.hscc_daemon"
LOG_FILE="$HOME/Library/Logs/hscc_daemon.log"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Pre-flight checks ──────────────────────────────────────────────────────

if [[ ! -f "${PLIST_TEMPLATE}" ]]; then
    error "Plist template not found at ${PLIST_TEMPLATE}"
    exit 1
fi

if ! command -v launchctl &>/dev/null; then
    error "launchctl not found — this script requires macOS"
    exit 1
fi

# ── Ensure directories exist ───────────────────────────────────────────────

mkdir -p "${LAUNCH_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"

# ── Install plist ──────────────────────────────────────────────────────────

if [[ "${1:-}" == "force" ]]; then
    info "Force-reloading hscc_daemon..."
    launchctl bootout "gui/$UID/${LABEL}" 2>/dev/null || true
fi

# Resolve a REAL python interpreter for ProgramArguments[0]. The old template
# hardcoded /usr/local/bin/python3, which doesn't exist on Homebrew-only Macs or
# Spark nodes — launchd then fails to exec and KeepAlive respawn-loops it (C1).
# Prefer the Hermes venv python, then the env's python3, then known locations.
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
    error "No usable python3 found for the daemon plist (tried venv, PATH, /opt/homebrew, /usr/local, /usr/bin)"
    exit 1
fi
info "Daemon python: ${PYBIN}"

if true; then
    # Substitute __HOME__ + __PYBIN__ (launchctl does not expand ~/$HOME, and the
    # interpreter path must be a real absolute path), generating the real plist.
    sed -e "s|__HOME__|${HOME}|g" -e "s|__PYBIN__|${PYBIN}|g" \
        "${PLIST_TEMPLATE}" > "${LAUNCH_PLIST}"
    info "Installed plist to ${LAUNCH_PLIST}"
else
    info "Plist already up to date at ${LAUNCH_PLIST}"
fi

# ── Load service ───────────────────────────────────────────────────────────

if launchctl list "${LABEL}" &>/dev/null; then
    info "hscc_daemon is already loaded — skipping load"
else
    launchctl bootstrap "gui/$UID" "${LAUNCH_PLIST}"
    info "Loaded ${LABEL} via launchctl bootstrap"
fi

# ── Enable on login (already done by GUI bootstrap, but verify) ─────────────

if launchctl list "${LABEL}" &>/dev/null; then
    info "${LABEL} is active and will start on login"
else
    warn "${LABEL} did not start — check for errors"
fi

# ── Status check ───────────────────────────────────────────────────────────

echo ""
info "── hscc_daemon Service Status ────────────────────────────────────"

# Check launchd
if launchctl list "${LABEL}" &>/dev/null; then
    echo -e "  ${GREEN}●${NC} Launchd:    loaded and active"
    launchctl list "${LABEL}" 2>/dev/null | head -5 | sed 's/^/    /'
else
    echo -e "  ${RED}○${NC} Launchd:    not loaded"
fi

# Check daemon process
if pgrep -f "hscc.py.*start" &>/dev/null; then
    PID=$(pgrep -f "hscc.py.*start" | head -1)
    echo -e "  ${GREEN}●${NC} Process:    running (PID ${PID})"
else
    echo -e "  ${RED}○${NC} Process:    not running (expected to be started by Launchd)"
fi

# Check state dir
if [[ -d "$HOME/.hscc/state" ]]; then
    COUNT=$(find "$HOME/.hscc/state" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
    echo -e "  ${GREEN}●${NC} State:      ${COUNT} JSON files in ~/.hscc/state/"
else
    echo -e "  ${RED}○${NC} State:      ~/.hscc/state/ not found"
fi

# Check log
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
echo "  Start:   launchctl bootstrap gui/$UID ${LAUNCH_PLIST}"
echo "  Stop:    launchctl bootout  gui/$UID ${LABEL}"
echo "  Status:  launchctl list ${LABEL}"
echo "  Log:     tail -f ${LOG_FILE}"
echo "  Uninstall: ${SCRIPT_DIR}/hscc.py uninstall"
echo ""
