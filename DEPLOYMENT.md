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
   git tag v1.1.1
   git push origin v1.1.1
   ```
2. GitHub Actions will spin up a builder container to:
   - Compile the **Python Daemon** into a single frozen binary executable (`zola-daemon`) using PyInstaller.
   - Build the **Tauri Frontend** AppImage (`Zola.AppImage`).
   - Automatically compile them and attach them to a new GitHub Release under the corresponding version tag.

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
3. Run the daemon locally:
   ```bash
   uvicorn app:app --host 127.0.0.1 --port 5001
   ```

### Frontend Local Setup
1. Navigate to the frontend directory:
   ```bash
   cd zola-frontend
   ```
2. Install dependencies and compile the AppImage:
   ```bash
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
// Keybinds: Map active trigger directly to background HTTP trigger
// ─────────────────────────────────────────────────────────────────────────────
binds {
    // Super+V: Toggle configured Active Voice Typing mode
    Mod+V { spawn "curl" "-s" "-X" "POST" "http://127.0.0.1:5001/trigger"; }
}
```

---

## Troubleshooting

### stuck modifier keys
If the daemon is killed during active typing injection, modifier keys (e.g. Shift) might remain pressed down at the kernel level. To release all keys instantly:
```bash
curl -X POST http://127.0.0.1:5001/trigger
```
This forces the backend state machine to run its `release_all()` routine.

### Clipboard Integration fails under systemd
If `wl-copy` cannot write to the clipboard when running inside the background service, import the graphical environment variables into systemd:
```bash
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR
systemctl --user restart zola-backend.service
```
This ensures the virtual keyboard backend knows which socket to talk to.
