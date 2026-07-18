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

# Stop active service if running to avoid "Text file busy" error when overwriting the binary
if systemctl --user is-active --quiet zola-backend.service; then
    log_info "Stopping active zola-backend.service for update..."
    systemctl --user stop zola-backend.service
fi

# 1. Download pre-compiled assets
log_info "Downloading standalone backend daemon binary..."
curl -L -o "$BIN_DIR/zola-daemon" "https://github.com/$REPO/releases/download/$LATEST_RELEASE/zola-daemon"

log_info "Downloading Tauri frontend AppImage..."
VERSION_NO="${LATEST_RELEASE#v}"
curl -L -o "$BIN_DIR/Zola.AppImage" "https://github.com/$REPO/releases/download/$LATEST_RELEASE/zola_${VERSION_NO}_amd64.AppImage"

# 2. Set executable permissions
log_info "Applying execution permissions to binaries..."
chmod +x "$BIN_DIR/zola-daemon"
chmod +x "$BIN_DIR/Zola.AppImage"

# 3. Configure uinput permissions
log_info "Verifying kernel uinput device permissions..."
UDEV_RULE_PATH="/etc/udev/rules.d/99-uinput.rules"
NEEDS_RELOG=false

# Check if user is in input group
if ! groups "$USER" | grep -q "\binput\b"; then
    log_warn "Your user is not in the 'input' group (needed to write keystrokes via evdev)."
    log_info "Adding $USER to 'input' group (requires sudo)..."
    sudo usermod -aG input "$USER"
    NEEDS_RELOG=true
fi

# Check if udev rule is set up
if [ ! -f "$UDEV_RULE_PATH" ] || ! grep -q "uinput" "$UDEV_RULE_PATH"; then
    log_warn "Udev permission rule for /dev/uinput is missing or incorrect."
    log_info "Writing udev rule to $UDEV_RULE_PATH (requires sudo)..."
    echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee "$UDEV_RULE_PATH" > /dev/null
    log_info "Reloading udev rules..."
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

# 4. Write Systemd user service
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

[Install]
WantedBy=default.target
EOF

# 5. Activate Systemd service
log_info "Reloading systemd user configuration..."
systemctl --user daemon-reload

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
