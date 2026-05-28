#!/bin/bash
#
# HSCC Bootstrap Command
#
# Single entry-point that runs the full initialization sequence:
#   1. Skill install (hscc-skills install)
#   2. Skill status verification (hscc-skills status)
#   3. State validation (verify all ~/.hscc/ state files)
#   4. Daemon health check (HSCC daemon via pgrep)
#   5. Cluster health check (sparkrun status)
#
# Usage:
#   hscc-bootstrap                  # Run all checks
#   hscc-bootstrap --skip-skills    # Skip skill install
#   hscc-bootstrap --skip-gateway   # Skip gateway check
#   hscc-bootstrap --skip-cluster   # Skip cluster check
#   hscc-bootstrap --verbose        # Show command output
#   hscc-bootstrap --json           # Machine-readable JSON output
#
# Constraints:
#   - NO SSH to remote hosts
#   - NO external network requests (localhost only)
#   - ONLY local file operations and Python subprocesses
#

set -uo pipefail

# ── Configuration ──────────────────────────────────────────────────────────

HSCC_DIR="${HSCC_DIR:-$HOME/.hscc}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

SKILLS_PLUGIN="$HERMES_HOME/plugins/hscc-skills/hscc.py"

GATEWAY_HOST="localhost"
GATEWAY_PORT="18789"
GATEWAY_URL="http://$GATEWAY_HOST:$GATEWAY_PORT"

# ── Color / Format helpers ─────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── Flags ──────────────────────────────────────────────────────────────────

SKIP_SKILLS=false
SKIP_GATEWAY=false
SKIP_CLUSTER=false
VERBOSE=false
JSON_OUTPUT=false
EXIT_CODE=0

# ── Temp files for JSON accumulation ───────────────────────────────────────

TMPDIR_BOOT=$(mktemp -d 2>/dev/null || mktemp -d -t 'hscc-bootstrap')
STAGE_RESULT_FILE="$TMPDIR_BOOT/stages.json"
SKILLS_STATUS_FILE="$TMPDIR_BOOT/skills_status.txt"
touch "$STAGE_RESULT_FILE"

cleanup() {
    rm -rf "$TMPDIR_BOOT" 2>/dev/null
}
trap cleanup EXIT

# ── Parse arguments ────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-skills)   SKIP_SKILLS=true; shift ;;
        --skip-skill)    SKIP_SKILLS=true; shift ;;   # backward-compatible alias
        --skip-gateway)  SKIP_GATEWAY=true; shift ;;
        --skip-cluster)  SKIP_CLUSTER=true; shift ;;
        --verbose|-v)    VERBOSE=true; shift ;;
        --json)          JSON_OUTPUT=true; shift ;;
        --help|-h)
            echo "Usage: hscc-bootstrap [--skip-skills] [--skip-gateway] [--skip-cluster] [--verbose] [--json]"
            echo ""
            echo "Run the full HSCC initialization sequence:"
            echo "  1. Skill install          — hscc-skills install"
            echo "  2. Skill status         — hscc-skills status (verification)"
            echo "  3. State validation     — verify ~/.hscc/ state files"
            echo "  4. Daemon health check  — HSCC daemon (pgrep)"
            echo "  5. Cluster health check — sparkrun status"
            echo ""
            echo "Flags:"
            echo "  --skip-skills   Skip skill/template installation"
            echo "  --skip-daemon    Skip daemon health check"
            echo "  --skip-cluster  Skip cluster health check"
            echo "  --verbose       Show command output"
            echo "  --json          Machine-readable JSON output"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# ── Logging helpers ────────────────────────────────────────────────────────

log_pass() {
    if $JSON_OUTPUT; then
        echo -n "PASS"
    else
        echo -e "  ${GREEN}✓${NC} $1"
    fi
}

log_fail() {
    EXIT_CODE=1
    if $JSON_OUTPUT; then
        echo -n "FAIL"
    else
        echo -e "  ${RED}✗${NC} $1"
    fi
}

log_warn() {
    if $JSON_OUTPUT; then
        echo -n "WARN"
    else
        echo -e "  ${YELLOW}⚠${NC} $1"
    fi
}

log_info() {
    if $JSON_OUTPUT; then
        echo -n "INFO"
    else
        echo -e "  ${CYAN}ℹ${NC} $1"
    fi
}

log_dim() {
    if ! $JSON_OUTPUT && $VERBOSE; then
        echo -e "  ${DIM}$1${NC}"
    fi
}

# ── Helpers ────────────────────────────────────────────────────────────────

# Check if skills are already fully installed and up-to-date.
# Returns 0 if all skills+templates are UP-TO-DATE, 1 otherwise.
skills_are_current() {
    if [[ ! -f "$SKILLS_PLUGIN" ]]; then
        return 1
    fi
    local status_output
    status_output=$(python3 "$SKILLS_PLUGIN" status 2>&1) || return 1

    # hscc-skills status prints "✓ OK" for current items
    # and "⚠" prefix for any issues
    if echo "$status_output" | grep -q "⚠"; then
        return 1
    fi
    return 0
}

# Run hscc-skills install and return its exit code.
# Prints output to stdout.
run_skills_install() {
    python3 "$SKILLS_PLUGIN" install 2>&1
    return $?
}

# Run hscc-skills status and save output, return combined status line.
# Returns: "all_ok", "warnings", "errors", "not_available"
run_skills_status() {
    if [[ ! -f "$SKILLS_PLUGIN" ]]; then
        echo "not_available" > "$SKILLS_STATUS_FILE"
        return 1
    fi

    local status_output
    status_output=$(python3 "$SKILLS_PLUGIN" status 2>&1) || {
        echo "not_available" > "$SKILLS_STATUS_FILE"
        return 1
    }

    echo "$status_output" > "$SKILLS_STATUS_FILE"

    # Check for warning/error indicators in status output
    # hscc-skills uses "⚠ MISSING", "⚠ NOT INSTALLED", "⚠ OUT-OF-DATE", "⚠ error"
    if echo "$status_output" | grep -q "⚠"; then
        if echo "$status_output" | grep -q "⚠ MISSING\|⚠ NOT INSTALLED"; then
            echo "errors" > "$SKILLS_STATUS_FILE"
            return 1
        else
            echo "warnings" > "$SKILLS_STATUS_FILE"
            return 0
        fi
    fi

    echo "all_ok" > "$SKILLS_STATUS_FILE"
    return 0
}

# ── Stage 1: Skill Install ─────────────────────────────────────────────────

run_skill_install() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  STAGE 1: Skill & Template Installation${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    if $SKIP_SKILLS; then
        log_info "Skipped (flag --skip-skills)"
        echo "skill_install=skipped" >> "$STAGE_RESULT_FILE"
        return 0
    fi

    if [[ ! -f "$SKILLS_PLUGIN" ]]; then
        log_fail "hscc-skills plugin not found at: $SKILLS_PLUGIN"
        echo "skill_install=not_found" >> "$STAGE_RESULT_FILE"
        return 1
    fi

    # Check if skills are already up-to-date (idempotent check)
    local idempotent_skip=false
    if skills_are_current; then
        idempotent_skip=true
    fi

    if $idempotent_skip; then
        log_info "Skills already installed and up-to-date"
        echo "skill_install=up_to_date" >> "$STAGE_RESULT_FILE"
    else
        # Run install
        echo ""
        local install_output
        if install_output=$(run_skills_install); then
            log_pass "Skills and templates installed/verified"
            echo "skill_install=installed" >> "$STAGE_RESULT_FILE"
        else
            echo "$install_output"
            log_warn "Skills installation exited with non-zero code"
            echo "skill_install=errors" >> "$STAGE_RESULT_FILE"
            return 1
        fi
    fi

    # ── Skill Status Verification ──────────────────────────────────────
    echo ""
    echo -e "  ${MAGENTA}${DIM}─── Skills Status Verification ───${NC}"

    if run_skills_status; then
        local status_val
        status_val=$(cat "$SKILLS_STATUS_FILE")
        case "$status_val" in
            all_ok)
                log_pass "All skills and templates up-to-date"
                ;;
            warnings)
                log_warn "Some skills have warnings (non-critical)"
                ;;
            *)
                log_warn "Could not determine skills status"
                ;;
        esac
    else
        local status_val
        status_val=$(cat "$SKILLS_STATUS_FILE" 2>/dev/null || echo "unknown")
        case "$status_val" in
            errors)
                log_warn "Some skills not installed or missing (non-critical)"
                ;;
            not_available)
                log_info "hscc-skills status not available"
                ;;
            *)
                log_info "Skills status: $status_val"
                ;;
        esac
    fi

    # Show status details in verbose mode
    if $VERBOSE; then
        local status_file_content
        status_file_content=$(cat "$SKILLS_STATUS_FILE" 2>/dev/null || echo "(no status output)")
        if [[ -n "$status_file_content" ]]; then
            echo "$status_file_content" | while IFS= read -r line; do
                [[ -n "$line" ]] && echo "    $line"
            done
        fi
    fi

    return 0
}

# ── Stage 2: State Validation ──────────────────────────────────────────────

run_state_validation() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  STAGE 2: State File Validation${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    # Create HSCC dir if needed
    if [[ ! -d "$HSCC_DIR" ]]; then
        mkdir -p "$HSCC_DIR"
        log_info "Created $HSCC_DIR"
    fi

    local checks=0
    local errors=0
    local warnings=0

    # Validate each expected JSON state file using Python (single invocation)
    local validation_result
    validation_result=$(python3 << 'PYEOF'
import json
import os

hscc_dir = os.environ.get("HSCC_DIR", os.path.expanduser("~/.hscc"))

expected = {
    "lifecycle.json": ["agents", "history"],
    "provision.json": ["mappings", "history"],
    "projects.json": ["projects", "activeProjectId"],
    "notifications.json": ["notifications"],
    "policy.json": ["rules"],
    "triggers.json": ["rules"],
    "recovery.json": ["history"],
    "cooldowns.json": None,
    "watchdog_block.json": None,
}

results = []
checks = 0
errors = 0
warnings = 0

for fname, required_keys in expected.items():
    path = os.path.join(hscc_dir, fname)
    checks += 1
    if not os.path.exists(path):
        results.append(f"WARN|missing|{fname}: not found (created on first use)")
        warnings += 1
        continue
    try:
        with open(path) as f:
            data = json.load(f)
        if required_keys:
            missing = [k for k in required_keys if k not in data]
            if missing:
                results.append(f"WARN|keys|{fname}: missing keys: {','.join(missing)}")
                warnings += 1
            else:
                results.append(f"OK|valid|{fname}")
        else:
            results.append(f"OK|valid|{fname}")
    except (json.JSONDecodeError, IOError) as e:
        results.append(f"ERROR|json|{fname}: {e}")
        errors += 1

print(f"CHECKS={checks}")
print(f"ERRORS={errors}")
print(f"WARNINGS={warnings}")
for r in results:
    print(r)
PYEOF
)

    local stage_errors=0
    while IFS= read -r line; do
        local status_field
        status_field=$(echo "$line" | cut -d'|' -f1)
        local category
        category=$(echo "$line" | cut -d'|' -f2)
        local message
        message=$(echo "$line" | cut -d'|' -f3-)
        case "$status_field" in
            OK)    log_pass "$message" ;;
            WARN)  log_warn "$message" ;;
            ERROR) log_fail "$message"; ((stage_errors++)) ;;
            INFO)  log_info "$message" ;;
        esac
    done <<< "$validation_result"

    # Extract summary counts
    local total_checks total_errors total_warnings
    total_checks=$(echo "$validation_result" | grep "^CHECKS=" | cut -d= -f2)
    total_errors=$(echo "$validation_result" | grep "^ERRORS=" | cut -d= -f2)
    total_warnings=$(echo "$validation_result" | grep "^WARNINGS=" | cut -d= -f2)

    if [[ ${total_errors:-0} -gt 0 ]]; then
        echo ""
        log_fail "State validation: $total_errors error(s)"
        echo "state_validation=errors" >> "$STAGE_RESULT_FILE"
        return 1
    fi

    log_pass "State validation complete ($total_checks checked, ${total_warnings:-0} warnings)"
    echo "state_validation=passed" >> "$STAGE_RESULT_FILE"
    return 0
}

# ── Stage 3: Gateway Health Check ──────────────────────────────────────────

run_gateway_check() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  STAGE 3: Gateway Health Check${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    if $SKIP_GATEWAY; then
        log_info "Skipped (flag --skip-gateway)"
        echo "gateway_check=skipped" >> "$STAGE_RESULT_FILE"
        return 0
    fi

    local result
    result=$(python3 << PYEOF
import socket
import urllib.request
import json

host = "$GATEWAY_HOST"
port = $GATEWAY_PORT

# TCP check
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
tcp_ok = False
try:
    s.connect((host, port))
    s.close()
    tcp_ok = True
except Exception:
    pass

if tcp_ok:
    # HTTP health check
    http_ok = False
    http_status = ""
    http_body = ""
    try:
        req = urllib.request.Request(f"http://{host}:{port}/health", method="GET")
        req.add_header("User-Agent", "hscc-bootstrap/1.0")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            http_ok = True
            http_status = str(resp.status)
            http_body = json.dumps(data)[:200]
    except Exception as e:
        http_status = f"error: {e}"

    print(f"TCP:UP")
    print(f"HTTP:{http_status}")
    if http_body and not http_body.startswith("error"):
        print(f"BODY:{http_body}")
    exit(0)
else:
    print("TCP:DOWN")
    exit(1)
PYEOF
) || true

    local tcp_status
    tcp_status=$(echo "$result" | grep "^TCP:" | cut -d: -f2)

    if [[ "$tcp_status" == "UP" ]]; then
        log_pass "Gateway TCP: $GATEWAY_HOST:$GATEWAY_PORT — UP"
        local http_line
        http_line=$(echo "$result" | grep "^HTTP:" | cut -d: -f2-)
        if [[ -n "$http_line" ]]; then
            if [[ "$http_line" != error* ]]; then
                log_pass "Gateway HTTP: $http_line"
                if $VERBOSE && echo "$result" | grep -q "^BODY:"; then
                    echo "$result" | grep "^BODY:" | sed 's/^BODY: //' | sed 's/^/      /'
                fi
                echo "gateway_check=healthy" >> "$STAGE_RESULT_FILE"
                return 0
            else
                log_warn "Gateway TCP: UP, HTTP error: $http_line"
                echo "gateway_check=partial" >> "$STAGE_RESULT_FILE"
                return 1
            fi
        fi
        echo "gateway_check=healthy" >> "$STAGE_RESULT_FILE"
        return 0
    else
        log_fail "Gateway TCP: $GATEWAY_HOST:$GATEWAY_PORT — DOWN"
        if $VERBOSE; then
            echo "$result" | grep -v "^TCP:" | sed 's/^/      /'
        fi
        log_info "Gateway not listening (may need to be started)"
        echo "gateway_check=unreachable" >> "$STAGE_RESULT_FILE"
        return 1
    fi
}

# ── Stage 4: Cluster Health Check ──────────────────────────────────────────

run_cluster_check() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  STAGE 4: Cluster Health Check${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    if $SKIP_CLUSTER; then
        log_info "Skipped (flag --skip-cluster)"
        echo "cluster_check=skipped" >> "$STAGE_RESULT_FILE"
        return 0
    fi

    # Check sparkrun command availability
    if command -v sparkrun &>/dev/null; then
        log_pass "sparkrun command: available"

        # Run sparkrun status (local only, no SSH)
        local status_output
        status_output=$(sparkrun status 2>&1) || true

        local workloads idle
        workloads=$(echo "$status_output" | grep -c "^Job:" 2>/dev/null || echo "0")
        workloads=$(echo "$workloads" | tr -d ' ')
        idle=$(echo "$status_output" | grep -c "solo.*Up" 2>/dev/null || echo "0")
        idle=$(echo "$idle" | tr -d ' ')

        log_pass "sparkrun status: $workloads workload(s) running, $idle idle host(s)"

        if [[ "$workloads" -gt 0 ]] && $VERBOSE; then
            echo "$status_output" | grep "^Job:" | while IFS= read -r line; do
                echo "  - $line"
            done
        fi
    else
        log_warn "sparkrun command: not found in PATH"
        log_info "Install from: pip install sparkrun (or local package)"
    fi

    # Check gateway.json (HSCC-managed)
    local gateway_file="$HSCC_DIR/gateway.json"
    if [[ -f "$gateway_file" ]]; then
        local gw_info
        gw_info=$(python3 -c "
import json
d = json.load(open('$gateway_file'))
url = d.get('url', 'unknown')
status = d.get('status', 'unknown')
print(f'url={url}, status={status}')
" 2>/dev/null) || true
        if [[ -n "$gw_info" ]]; then
            log_pass "Gateway config: valid"
            if $VERBOSE; then
                echo "$gw_info" | sed 's/^/      /'
            fi
        else
            log_warn "Gateway config: parse error"
        fi
    else
        log_warn "Gateway config: not found at $gateway_file"
    fi

    # Check NAS mount (local only)
    local nas_mount="/mnt/nas"
    if [[ -d "$nas_mount" ]]; then
        local nas_size
        nas_size=$(du -sh "$nas_mount" 2>/dev/null | cut -f1 || echo "?")
        log_pass "NAS mount: $nas_mount ($nas_size)"
    else
        log_info "NAS mount: $nas_mount not locally mounted"
    fi

    echo "cluster_check=checked" >> "$STAGE_RESULT_FILE"
    return 0
}

# ── Main execution ─────────────────────────────────────────────────────────

main() {
    local start_time
    start_time=$(date +%s 2>/dev/null || echo "0")

    # Header
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║          HSCC Bootstrap — Full Initialization               ║${NC}"
    echo -e "${BOLD}║          $(date -u +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null || date)           ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${CYAN}HSCC_DIR:      ${HSCC_DIR}${NC}"
    echo -e "  ${CYAN}HERMES_HOME:   ${HERMES_HOME}${NC}"
    echo -e "  ${CYAN}GATEWAY:      ${GATEWAY_HOST}:${GATEWAY_PORT}${NC}"
    echo -e "  ${CYAN}SKILLS_PLUGIN: ${SKILLS_PLUGIN}${NC}"
    echo ""

    # Run stages sequentially
    run_skill_install || true

    run_state_validation || true

    if ! $SKIP_GATEWAY; then
        run_gateway_check || true
    fi

    if ! $SKIP_CLUSTER; then
        run_cluster_check || true
    fi

    # ── Summary ────────────────────────────────────────────────────────
    local end_time elapsed
    end_time=$(date +%s 2>/dev/null || echo "0")
    elapsed=$((end_time - start_time))

    if $JSON_OUTPUT; then
        echo ""
        echo "{"
        echo "  \"timestamp\": \"$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date)\","
        echo "  \"elapsed_seconds\": $elapsed,"
        echo "  \"stages\": {"

        local first=true
        while IFS= read -r line; do
            local key val
            key=$(echo "$line" | cut -d= -f1)
            val=$(echo "$line" | cut -d= -f2)
            if ! $first; then
                echo ","
            fi
            printf "    \"%s\": \"%s\"" "$key" "$val"
            first=false
        done < "$STAGE_RESULT_FILE"

        echo ""
        echo "  },"
        echo "  \"exit_code\": $EXIT_CODE"
        echo "}"
    else
        echo ""
        echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${BOLD}  BOOTSTRAP COMPLETE${NC}"
        echo -e "  Elapsed: ${elapsed}s  |  Exit code: ${EXIT_CODE}"
        if [[ $EXIT_CODE -eq 0 ]]; then
            echo -e "  ${GREEN}All checks passed.${NC}"
        else
            echo -e "  ${YELLOW}Some checks had warnings or errors (see above).${NC}"
            echo -e "  ${CYAN}Non-critical issues do not block usage.${NC}"
        fi
        echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
    fi

    return $EXIT_CODE
}

main "$@"
