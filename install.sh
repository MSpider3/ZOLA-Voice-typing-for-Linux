#!/usr/bin/env bash
# ==============================================================================
# ZOLA ONE-LINE PRODUCTION INSTALLER
# ==============================================================================
set -euo pipefail

# ANSI Escape Sequences for CRT Retro-Green Aesthetic
GREEN='\033[0;32m'
BRIGHT_GREEN='\033[1;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[ZOLA] $1${NC}"
}

log_warn() {
    echo -e "${YELLOW}[ZOLA] WARNING: $1${NC}"
}

log_success() {
    echo -e "${BRIGHT_GREEN}[ZOLA] $1${NC}"
}

log_error() {
    echo -e "${RED}[ZOLA] ERROR: $1${NC}" >&2
}

REPO="MSpider3/ZOLA-Voice-typing-for-Linux"

echo -e "${BRIGHT_GREEN}"
echo "   ╔═════════════════════════════════════════════════════════════════╗"
echo "   ║           ZOLA VOICE TYPING ONE-LINE SYSTEM DEPLOYMENT          ║"
echo "   ╚═════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Fetch latest release tag dynamically
log_info "Fetching latest release details from GitHub..."
LATEST_RELEASE=$(curl -s "https://api.github.com/repos/$REPO/releases/latest" | grep "tag_name" | cut -d '"' -f 4 || true)

if [ -z "$LATEST_RELEASE" ]; then
    log_error "Could not fetch the latest release tag. Please ensure the repository is public and has releases."
    exit 1
fi

log_info "Latest Release Found: ${LATEST_RELEASE}"

BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="$HOME/.config/systemd/user"
APP_DIR="$HOME/.local/share/zola"

# Create layout directories
mkdir -p "$BIN_DIR"
mkdir -p "$SYSTEMD_DIR"
mkdir -p "$APP_DIR"

# Stop only the ZOLA service; never kill generic process names.
systemctl --user stop zola-backend.service 2>/dev/null || true

# 1. Download pre-compiled assets
log_info "Downloading standalone backend daemon binary..."
curl --fail --show-error -L -o "$BIN_DIR/zola-daemon" "https://github.com/$REPO/releases/download/$LATEST_RELEASE/zola-daemon"

log_info "Downloading Tauri frontend AppImage..."
VERSION_NO="${LATEST_RELEASE#v}"
curl --fail --show-error -L -o "$BIN_DIR/Zola.AppImage" "https://github.com/$REPO/releases/download/$LATEST_RELEASE/zola_${VERSION_NO}_amd64.AppImage"

log_info "Verifying checksums..."
CHECKSUMS_URL="https://github.com/$REPO/releases/download/$LATEST_RELEASE/checksums.txt"
if curl --output /dev/null --silent --head --fail "$CHECKSUMS_URL"; then
    EXPECTED_DAEMON_HASH=$(curl --fail --show-error -sL "$CHECKSUMS_URL" | grep "zola-daemon" | cut -d' ' -f1)
    ACTUAL_DAEMON_HASH=$(sha256sum "$BIN_DIR/zola-daemon" | cut -d' ' -f1)
    
    if [ -n "$EXPECTED_DAEMON_HASH" ] && [ "$EXPECTED_DAEMON_HASH" != "$ACTUAL_DAEMON_HASH" ]; then
        log_error "Checksum mismatch for zola-daemon. Aborting."
        rm -f "$BIN_DIR/zola-daemon"
        exit 1
    fi
    
    APPIMAGE_NAME="zola_${VERSION_NO}_amd64.AppImage"
    EXPECTED_APP_HASH=$(curl --fail --show-error -sL "$CHECKSUMS_URL" | grep "$APPIMAGE_NAME" | cut -d' ' -f1)
    ACTUAL_APP_HASH=$(sha256sum "$BIN_DIR/Zola.AppImage" | cut -d' ' -f1)
    
    if [ -n "$EXPECTED_APP_HASH" ] && [ "$EXPECTED_APP_HASH" != "$ACTUAL_APP_HASH" ]; then
        log_error "Checksum mismatch for AppImage. Aborting."
        rm -f "$BIN_DIR/Zola.AppImage"
        exit 1
    fi
    log_info "Checksums verified successfully."
else
    log_warn "No checksums.txt found for release. Skipping verification."
fi

# 2. Set executable permissions
log_info "Applying execution permissions to binaries..."
chmod +x "$BIN_DIR/zola-daemon"
chmod +x "$BIN_DIR/Zola.AppImage"

# 4. Configure uinput permissions
log_info "Verifying kernel uinput device permissions..."
UDEV_RULE_PATH="/etc/udev/rules.d/99-uinput.rules"
NEEDS_RELOG=false

# Prefer seat-aware uaccess over the broad input group. This grants the active
# desktop user access to /dev/uinput without granting access to every input device.
if [ ! -f "$UDEV_RULE_PATH" ] || ! grep -q 'TAG+="uaccess"' "$UDEV_RULE_PATH"; then
    log_info "Writing seat-aware udev rule for /dev/uinput (requires sudo)..."
    echo 'KERNEL=="uinput", TAG+="uaccess", OPTIONS+="static_node=uinput"' | sudo tee "$UDEV_RULE_PATH" > /dev/null
    log_info "Reloading udev rules..."
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

if [ ! -r /dev/uinput ] || [ ! -w /dev/uinput ]; then
    log_warn "/dev/uinput is still not writable by the current session."
    log_warn "Log out/in once so systemd-logind can apply the uaccess ACL."
    NEEDS_RELOG=true
fi

# 4. Interactive First-Run Configuration Wizard
if [ -t 0 ]; then
    log_info "Starting interactive first-run configuration..."
    ZOLA_CONF_DIR="$HOME/.config/zola"
    mkdir -p "$ZOLA_CONF_DIR"
    ZOLA_CONF_FILE="$ZOLA_CONF_DIR/settings.json"
    
    echo -e "\n${YELLOW}=== 1. Hardware Profile ===${NC}"
    echo "1) Light (Low-spec laptops, fast but less accurate)"
    echo "2) Balanced (Most modern laptops)"
    echo "3) Accurate (High-end CPU/M-series, downloads ~1.5GB model)"
    read -r -p "Select profile [2]: " hw_choice
    case "$hw_choice" in
        1) w_real="tiny"; w_batch="base" ;;
        3) w_real="small"; w_batch="large-v3-turbo" ;;
        4) 
            log_warn "Nemotron modular engine support is slated for v1.3.0. Defaulting to Balanced profile."
            w_real="base"; w_batch="small"
            ;;
        *) w_real="base"; w_batch="small" ;;
    esac

    echo -e "\n${YELLOW}=== 2. Primary Language ===${NC}"
    echo "1) English"
    echo "2) Hindi"
    echo "3) Hinglish (Mixed)"
    echo "4) Auto-detect"
    read -r -p "Select language [4]: " lang_choice
    case "$lang_choice" in
        1) s_lang="en" ;;
        2) s_lang="hi" ;;
        3) s_lang="mixed" ;;
        *) s_lang="auto" ;;
    esac

    echo -e "\n${YELLOW}=== 3. Checking Ollama Integration ===${NC}"
    if curl -s -m 2 http://127.0.0.1:11434/api/tags | grep -q "name"; then
        log_success "Found local Ollama instance."
    else
        log_warn "Ollama not running locally on port 11434. LLM features may fail until installed."
    fi

    cat > "$ZOLA_CONF_FILE" << JSON_CONF
{
  "whisper_realtime": "$w_real",
  "whisper_batch": "$w_batch",
  "spoken_language": "$s_lang",
  "first_run_completed": true
}
JSON_CONF
    log_success "Saved configuration to $ZOLA_CONF_FILE"
    echo -e "\n"
fi

# 5. Write Systemd user service
log_info "Writing systemd user service definition..."
cat > "$SYSTEMD_DIR/zola-backend.service" << EOF
[Unit]
Description=Zola Voice Typing Daemon Core
After=graphical-session.target pipewire.service
Wants=pipewire.service

[Service]
Type=simple
ExecStart=$BIN_DIR/zola-daemon
Environment=PYTHONUNBUFFERED=1
PassEnvironment=WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=default.target
EOF

# 5. Activate Systemd service
log_info "Reloading systemd user configuration..."
systemctl --user daemon-reload

log_info "Enabling systemd linger for background persistence..."
loginctl enable-linger "$USER"

log_info "Enabling and starting zola-backend.service..."
systemctl --user enable --now zola-backend.service

# 6. Integrate Desktop environment shortcut
log_info "Generating desktop integration shortcut..."
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/zola.desktop" << EOF
[Desktop Entry]
Name=Zola Voice Terminal
Comment=Retro-military analog CRT engine for desktop speech-to-text dictation.
Exec=$BIN_DIR/Zola.AppImage
Icon=terminal
Terminal=false
Type=Application
Categories=Utility;Development;
StartupWMClass=zola
EOF

# 7. Success message banner
echo -e "${BRIGHT_GREEN}"
echo "   ┌─────────────────────────────────────────────────────────────────┐"
echo "   │ [ZOLA] SYSTEM DEPLOYMENT SUCCESSFUL                             │"
echo "   │ -> Backend daemon: Enabled & running via systemd core           │"
echo "   │ -> Frontend GUI: Accessible via desktop launchers (zola.desktop)│"
echo "   └─────────────────────────────────────────────────────────────────┘"
echo -e "${NC}"

if [ "$NEEDS_RELOG" = true ]; then
    log_warn "You were added to the 'input' group. You MUST log out and log back in (or reboot)"
    log_warn "for uinput keystroke injection permissions to take effect on your user session."
else
    log_success "Deployment complete. Monitor daemon with: journalctl --user -u zola-backend.service -f"
fi
