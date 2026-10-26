# ZOLA // PRODUCTION DEPLOYMENT GUIDE
================================================================================
This document outlines the professional deployment workflow for **Zola — Voice Typing for Linux**.

By using GitHub Actions, the application components (Tauri frontend UI and Python backend daemon) are pre-compiled in the cloud and distributed as standalone assets. The host system requires no development toolchains, Node.js, Rust, or Python environments.

---

## 1. Automated CI/CD Pipeline

The project features a fully automated release pipeline in `.github/workflows/release.yml`.

### How Releases are Triggered:
1. Create a version tag and push it:
   ```bash
   git tag v1.3.0
   git push origin v1.3.0
   ```
2. GitHub Actions will spin up a builder container to:
   - Compile the **Python Daemon** into a single frozen binary executable (`zola-daemon`) using PyInstaller.
   - Copy it into `zola-frontend/src-tauri/binaries/` as a Tauri sidecar, so the `.rpm`, `.deb` and `.AppImage` each contain the app **and** the daemon (the app starts it when no daemon is running).
   - Attach the bundles, the standalone `zola-daemon` (used only by `install.sh` for its systemd service) and `checksums.txt` to the GitHub Release for that tag.

---

## 2. One-Line Client Installation

For end-users, setting up Zola on any system (e.g. Fedora Linux) takes only a single terminal command. 

This command fetches the master installer script directly from the repository, downloads the pre-compiled binary components, configures systemd, and registers the desktop launcher shortcut.

```bash
curl -fsSL https://raw.githubusercontent.com/MSpider3/ZOLA-Voice-typing-for-Linux/main/install.sh | bash
```

---

## 3. Manual Installation (Local Testing / Development)

If you are developing locally or testing without pushing to GitHub, you can build the binaries on your machine:

### Backend Local Setup
1. Navigate to the backend directory:
   ```bash
   cd zola-backend
   ```
2. Create and configure your environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Run the daemon locally (`python app.py`, not `uvicorn app:app`: app.py turns off request logging, which would record the `/events?token=`, and sets the shutdown grace period):
   ```bash
   python app.py
   ```

### Frontend Local Setup
1. Navigate to the frontend directory:
   ```bash
   cd zola-frontend
   ```
   Tauri requires the bundled daemon file to exist even for `npm run tauri dev`. Build it as in step 2, or for UI-only work create an empty placeholder (the app then uses a daemon you start yourself):
   ```bash
   mkdir -p src-tauri/binaries && touch "src-tauri/binaries/zola-daemon-$(rustc -vV | sed -n 's/^host: //p')"
   ```
2. Build the daemon first and place it as the sidecar, then compile the bundles:
   ```bash
   (cd ../zola-backend && pyinstaller zola-daemon.spec)
   mkdir -p src-tauri/binaries
   cp ../zola-backend/dist/zola-daemon "src-tauri/binaries/zola-daemon-$(rustc -vV | sed -n 's/^host: //p')"
   npm install
   npm run tauri build
   ```
3. Your compiled AppImage will be in `src-tauri/target/release/bundle/appimage/`.

---

## 4. Compositor Integration (Niri Wayland Example)

To integrate Zola seamlessly into the desktop environment, register global compositor hotkeys and window placement rules.

Add the following to your `~/.config/niri/config.kdl`:

```kdl
// ─────────────────────────────────────────────────────────────────────────────
// Window Rules: Position Zola floating with a retro CRT dimension
// ─────────────────────────────────────────────────────────────────────────────
window-rule {
    geometry-corner-radius 8
    clip-to-geometry

    match title="ZOLA // VOICE TERMINAL" {
        block-out-from "fullscreen"
        open-floating true
        default-floating-position x=100 y=100
        default-column-width { proportion 0.45; }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Keybinds: zola-trigger sends the trigger request with the API token
// ─────────────────────────────────────────────────────────────────────────────
spawn-at-startup "/usr/bin/zola-daemon"

binds {
    // Super+V: Toggle configured Active Voice Typing mode
    Mod+V { spawn "zola-trigger"; }
}
```

---

## Troubleshooting

### stuck modifier keys
If the daemon is killed during active typing injection, modifier keys (e.g. Shift) might remain pressed down at the kernel level. Removing the virtual keyboard releases them: stop and restart the daemon (a clean shutdown also runs its `release_all()` routine):
```bash
pkill -x zola-daemon   # or: systemctl --user restart zola-backend
```

### Clipboard Integration fails under systemd
If `wl-copy` cannot write to the clipboard when running inside the background service, import the graphical environment variables into systemd:
```bash
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR
systemctl --user restart zola-backend.service
```
This ensures the virtual keyboard backend knows which socket to talk to.

### WebKit2GTK Grey Screen / EGL_BAD_PARAMETER Abort
When running the pre-compiled AppImage on certain Wayland-based distributions (like Fedora or Arch Linux with Nvidia/Intel graphics), you may encounter a blank grey screen or an abort error:
`Could not create default EGL display: EGL_BAD_PARAMETER. Aborting...`

This occurs because the pre-packaged libraries inside the cloud-built AppImage mismatch with your system's graphics/Mesa driver configurations.

**Solution:** Compile the production binary natively on your machine so it links directly against your local host libraries:
1. Navigate to the frontend folder and run the release build:
   ```bash
   cd zola-frontend
   npm install
   npm run tauri build
   ```
2. Copy the natively compiled binary to replace the local installer's location:
   ```bash
   cp src-tauri/target/release/zola-frontend ~/.local/bin/Zola.AppImage
   chmod +x ~/.local/bin/Zola.AppImage
   ```
