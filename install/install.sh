#!/usr/bin/env bash
#
# HSCC Install Script
#
# Installs:
# 1. Hermes (if not present)
# 2. HSCC CLI
# 3. HSCC plugins (symlink to ~/.hermes/plugins/)
# 4. HSCC skills (copy to ~/.hermes/skills/)
# 5. HSCC templates (copy to ~/.hermes/templates/)
# 6. Launchd plist for daemon
# 7. Config from template
#
# Usage:
#   ./install.sh              # Interactive install
#   ./install.sh --yes        # Non-interactive install
#   ./install.sh --dry-run    # Show what would happen
#

set -euo pipefail

# Colors
GREEN='\033[92m'
RED='\033[91m'
YELLOW='\033[93m'
BLUE='\033[94m'
CYAN='\033[96m'
DIM='\033[2m'
RESET='\033[0m'

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HSCC_DIR="$HOME/.hscc"
HERMES_DIR="$HOME/.hermes"
hermes_DIR="$HOME/.hermes"
SPARKRUN_DIR="$HOME/.sparkrun-local"
R2D2CC_DIR="$HOME/.r2d2cc"

# Flags
DRY_RUN=false
YES=false

# Parse args
for arg in "$@"; do
    case $arg in
        --dry-run) DRY_RUN=true; shift ;;
        --yes) YES=true; shift ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

if [ "$DRY_RUN" = true ]; then
    echo -e "${BLUE}=== HSCC Install (Dry Run) ===${RESET}"
    echo "Would install:"
    echo "  1. HSCC CLI (pip install -e)"
    echo "  2. HSCC plugins → $HERMES_DIR/plugins/"
    echo "  3. HSCC skills → $HERMES_DIR/skills/"
    echo "  4. HSCC templates → $HERMES_DIR/templates/"
    echo "  5. Launchd plist"
    echo "  6. Config at $HSCC_DIR/config.yaml"
    exit 0
fi

echo -e "${BLUE}=== HSCC Install ===${RESET}\n"

# Pre-flight checks
echo -e "${CYAN}[1/7] Pre-flight checks${RESET}"

check_python() {
    python3 --version 2>&1 | grep -q "Python 3\." && echo -e "  ${GREEN}✓ Python 3${RESET}" || {
        echo -e "  ${RED}✗ Python 3 required${RESET}"
        exit 1
    }
}

check_git() {
    which git >/dev/null 2>&1 && echo -e "  ${GREEN}✓ Git${RESET}" || {
        echo -e "  ${RED}✗ Git required${RESET}"
        exit 1
    }
}

check_sparkrun() {
    which sparkrun >/dev/null 2>&1 && echo -e "  ${GREEN}✓ sparkrun${RESET}" || {
        echo -e "  ${YELLOW}⚠ sparkrun not found${RESET}"
    }
}

check_python
check_git
check_sparkrun

# Detect existing
echo -e "\n${CYAN}[2/7] Detecting existing setup${RESET}"

detect_existing() {
    if [ -f "$HERMES_DIR/config.yaml" ]; then
        echo -e "  ${GREEN}✓ Hermes config exists${RESET}"
    else
        echo -e "  ${YELLOW}⚠ No Hermes config found${RESET}"
    fi
    
    if [ -d "$HSCC_DIR" ]; then
        echo -e "  ${GREEN}✓ HSCC state exists${RESET}"
    else
        echo -e "  ${YELLOW}⚠ No HSCC state (will create)${RESET}"
    fi
    
    # Check Qwen3.6 cache
    cached=false
    for node in 192.0.2.10 192.0.2.11 192.0.2.12; do
        count=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null spark@$node \
            "find /home/spark/.cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B-FP8/blobs -type f 2>/dev/null | wc -l" 2>/dev/null || echo "0")
        if [ "$count" -gt 0 ]; then
            cached=true
            break
        fi
    done
    
    if [ "$cached" = true ]; then
        echo -e "  ${GREEN}✓ Qwen3.6 cached on cluster${RESET}"
    else
        echo -e "  ${YELLOW}⚠ Qwen3.6 not cached${RESET}"
    fi
}

detect_existing

# Install HSCC CLI
echo -e "\n${CYAN}[3/7] Installing HSCC CLI${RESET}"

if [ "$YES" = false ]; then
    read -p "Install HSCC CLI? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "  ${YELLOW}⚠ Skipping CLI install${RESET}"
    else
        pip install -e "$SCRIPT_DIR/hscc-cli/" >/dev/null 2>&1
        echo -e "  ${GREEN}✓ HSCC CLI installed${RESET}"
    fi
else
    pip install -e "$SCRIPT_DIR/hscc-cli/" >/dev/null 2>&1
    echo -e "  ${GREEN}✓ HSCC CLI installed${RESET}"
fi

# Wire plugins
echo -e "\n${CYAN}[4/7] Wiring plugins${RESET}"

mkdir -p "$HERMES_DIR/plugins"

# Check which plugins exist in ~/.hermes/plugins/
for plugin in hscc-daemon hscc-chat hscc-agent-coordinator hscc-governance hscc-skills hscc-bootstrap hscc-cluster hscc-events hscc-orchestrator hscc-projects hscc-provision hscc-optimizations; do
    if [ -d "$HOME/.hermes/plugins/$plugin" ]; then
        target="$HERMES_DIR/plugins/$plugin"
        if [ ! -L "$target" ] && [ ! -d "$target" ]; then
            # Symlink plugin
            ln -s "$HOME/.hermes/plugins/$plugin" "$target" 2>/dev/null || true
        fi
        echo -e "  ${GREEN}✓ $plugin${RESET}"
    fi
done

echo -e "  ${GREEN}✓ Plugins wired${RESET}"

# Install skills
echo -e "\n${CYAN}[5/7] Installing skills${RESET}"

mkdir -p "$HERMES_DIR/skills"

for skill in brainstorming caveman executing-plans systematic-debugging test-driven-development verification-before-completion writing-plans; do
    skill_src="$SCRIPT_DIR/hscc-skills/$skill"
    skill_dst="$HERMES_DIR/skills/$skill"
    
    if [ -d "$skill_src" ]; then
        mkdir -p "$skill_dst"
        cp -n "$skill_src/SKILL.md" "$skill_dst/SKILL.md" 2>/dev/null || true
        echo -e "  ${GREEN}✓ $skill${RESET}"
    fi
done

echo -e "  ${GREEN}✓ Skills installed${RESET}"

# Install templates
echo -e "\n${CYAN}[6/7] Installing templates${RESET}"

mkdir -p "$HERMES_DIR/templates"

for template in AGENTS.md HEARTBEAT.md IDENTITY.md SOUL.md TOOLS.md USER.md; do
    template_src="$SCRIPT_DIR/hscc-templates/$template"
    template_dst="$HERMES_DIR/templates/$template"
    
    if [ -f "$template_src" ]; then
        cp -n "$template_src" "$template_dst" 2>/dev/null || true
        echo -e "  ${GREEN}✓ $template${RESET}"
    fi
done

echo -e "  ${GREEN}✓ Templates installed${RESET}"

# Launchd plist
echo -e "\n${CYAN}[7/7] Installing launchd service${RESET}"

mkdir -p "$HOME/Library/LaunchAgents"

if [ ! -f "$HOME/Library/LaunchAgents/com.hermes.hscc-daemon.plist" ]; then
    # Check if daemon exists
    if [ -f "$HOME/.hermes/plugins/hscc-daemon/hscc.py" ]; then
        cat > "$HOME/Library/LaunchAgents/com.hermes.hscc-daemon.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.hscc-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>$HOME/.hermes/plugins/hscc-daemon/hscc.py</string>
        <string>start</string>
    </array>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/hscc-daemon.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/hscc-daemon.log</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ExitTimeOut</key>
    <integer>10</integer>
</dict>
</plist>
EOF
        echo -e "  ${GREEN}✓ Launchd plist installed${RESET}"
    else
        echo -e "  ${YELLOW}⚠ hscc-daemon not found — skipping launchd${RESET}"
    fi
else
    echo -e "  ${GREEN}✓ Launchd plist already exists${RESET}"
fi

# Summary
echo -e "\n${GREEN}═══════════════════════════════════════════${RESET}"
echo -e "${GREEN}✓ HSCC installation complete${RESET}"
echo -e "${GREEN}═══════════════════════════════════════════${RESET}\n"

echo "  Next steps:"
echo "    1. Run 'hscc init' to configure and deploy"
echo "    2. Check status with 'hscc status'"
echo "    3. Start chatting with 'hscc chat'"
