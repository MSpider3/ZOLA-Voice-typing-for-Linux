# ZOLA // VOICE TYPING FOR LINUX

[![Publish Zola Release](https://github.com/MSpider3/ZOLA-Voice-typing-for-Linux/actions/workflows/release.yml/badge.svg)](https://github.com/MSpider3/ZOLA-Voice-typing-for-Linux/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/MSpider3/ZOLA-Voice-typing-for-Linux/blob/main/LICENSE)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-orange.svg)]()

Zola is a high-performance, system-level Speech-to-Text (STT) voice-typing application specifically engineered for Linux desktops running Wayland (Fedora, Arch, Ubuntu). Designed with a distinct retro-military CRT terminal aesthetic, it provides four operation modes powered by a local Faster-Whisper engine and local Ollama model refinement.

## UI Previews

| Dashboard Console | Settings Menu |
|:---:|:---:|
| ![Zola Dashboard Console](docs/preview/zola_dashboard.png) | ![Zola Settings Menu](docs/preview/zola_settings.png) |

| Recent Logs | Transcription History |
|:---:|:---:|
| ![Zola Recent Logs](docs/preview/zola_recents.png) | ![Zola Transcription History](docs/preview/zola_transcription_history.png) |

---

## Technical Architecture: How It Works

Traditional voice-typing apps on Linux face two major hurdles:
1. **Wayland Security Isolation:** Wayland compositors block applications from injecting synthetic keystrokes or sniffing global key events of other windows.
2. **Resource Thrashing:** Running AI transcription models on consumer hardware can lead to high memory consumption, CPU spikes, and laggy UI rendering.

Zola resolves these problems with a decoupled **split architecture**:

```mermaid
graph TD
    subgraph Frontend [Tauri Frontend GUI]
        UI[HTML/JS/TS View] <--> |API Calls / Status| SSE[SSE Connection Handler]
    end

    subgraph Backend [Python Daemon Core]
        FastAPI[FastAPI Web Server] <--> state[State Manager]
        state <--> rec[Audio Recorder]
        state <--> stt[STT Engine: Faster-Whisper]
        state <--> llm[LLM Engine: Ollama / Local APIs]
    end

    subgraph OS [Linux Kernel & Environment]
        PW[PipeWire / Audio Input] --> |16kHz Mono Stream| rec
        uinput["/dev/uinput Device"]
        state --> |Key Injector| uinput
        uinput --> |Virtual Keystrokes| active[Active Target Window]
    end

    %% Communications
    UI -->|POST /trigger| FastAPI
    FastAPI -->|SSE Events / Amplitude| SSE
```

### Connection & Communication Architecture
* **Tauri HTTP REST API:** The frontend communicates with the backend daemon running on `http://127.0.0.1:5001`. For actions such as triggering recording, changing system settings, and updating active modes, the frontend issues standard HTTP `POST` requests.
* **Server-Sent Events (SSE):** Since transcription and recording states occur asynchronously on background threads, the backend pushes continuous status updates to the frontend using an SSE stream (`/events`). This includes realtime microphone amplitudes (to animate the CRT oscilloscope), active recording states, logs, and completed transcription text.
* **Exponential Backoff Connection Handler:** To ensure resilience, the frontend implements an exponential backoff reconnect handler with random jitter to automatically restore connection if the backend daemon restarts.

### Step-by-Step Program Lifecycle
1. **Shortcut Activation:** The user presses the configured global keybind (e.g., `Super + V`). The desktop compositor captures this shortcut and invokes a headless `POST` request to `http://127.0.0.1:5001/trigger`.
2. **Audio Capture (PipeWire):** The backend state manager transitions to the `RECORDING` state and initializes a PipeWire audio input pipeline. It captures raw audio frames at 16kHz mono, calculating amplitude levels and broadcasting them instantly via `/events` to drive the frontend oscilloscope waveform.
3. **Silence Detection (VAD):** The audio recorder runs continuous Voice Activity Detection (VAD) using Silero. If the user stops speaking for the configured threshold (e.g., 2.0 seconds), the backend automatically stops recording.
4. **Transcription & Local LLM Refinement:** The recorded audio buffer is passed to the local `faster-whisper` model. If LLM refinement mode is enabled, the transcribed text is sent locally to Ollama (`llama3` or any configured LLM) to clean up syntax, formatting, and grammar.
5. **Direct OS Keystroke Injection:** The final refined text is converted to virtual keyboard scancodes. The backend writes these codes directly to the `/dev/uinput` virtual keyboard device, injecting the keystrokes natively into the active foreground window.
6. **UI State Update:** A final event is pushed to the Tauri GUI via SSE to update the transcription logs and history.

### 1. Kernel-Level Keystroke Injection (`/dev/uinput`)
Instead of relying on buggy XWayland bridges or compositor-specific protocols (like Niri, Sway, or GNOME shell extensions), the Zola backend registers a virtual keyboard device directly with the Linux kernel `/dev/uinput` subsystem called `zola-virtual-keyboard`. When text is transcribed, the daemon translates characters to low-level scancodes and writes them directly to the input subsystem. This bypasses compositor isolation entirely, enabling input to flow to *any* active window (editors, browsers, chat tools).

### 2. High-Performance Audio Pipeline
- **Zero-Copy Buffers:** Audio frames are read at 16kHz mono (int16) from PipeWire. Redundant allocations are minimized by passing queue frames directly to the collection buffer without extra copy operations.
- **VAD (Voice Activity Detection):** Background thread silence detection avoids invoking the Whisper model during periods of silence.
- **CTranslate2 Backend:** Uses Faster-Whisper quantized to `int8` (optimal for Intel/AMD CPUs), achieving an RTF (Real-Time Factor) of ~0.10.
- **Explicit Garbage Collection:** Numpy buffers and Whisper segments are explicitly deallocated (`del` followed by `gc.collect()`) immediately after inference to prevent memory fragmentation and spikes.

### 3. Asynchronous Event Synchronization
The Tauri frontend communicates with the backend via two channels:
- **HTTP POST Request Triggers:** To trigger recordings or change settings.
- **Server-Sent Events (SSE):** The frontend listens to `/events` for state changes and raw audio amplitudes. To ensure resilience, the frontend implements an exponential backoff reconnect handler with random jitter.

### 4. Optimized Frontend Rendering
- **Canvas Visibility Throttling:** The CRT phosphor oscilloscope view uses the Page Visibility API. If the window is minimized or hidden, it draws a flat black frame (preventing frozen-frame compositor artifacts) and halts the animation loop, reducing GPU usage to 0%.
- **Targeted DOM Patching:** Rather than rebuilding the DOM on every event, Zola updates specific `.textContent` nodes for transcripts and statistics, avoiding layout thrashing.

---

## Key Features

- **Four Operation Modes:**
  1. **Batch STT:** Holds recording, transcribes on release, and types it out instantly.
  2. **Batch + LLM Refinement:** Transcribes audio, passes the text to a local Ollama model for grammar correction and formatting, copies to clipboard, and types it.
  3. **Realtime STT:** Streams audio and types words continuously as you speak.
  4. **Realtime + LLM Refinement:** Streams live transcription and performs final formatting cleanup when speaking stops.
- **CRT Phosphor Visualizer:** Genuine simulated green-glow analog oscilloscope showing mic amplitudes.
- **Automatic Legacy Migration:** Automatically migrates configurations from `~/.config/speaktype/` to `~/.config/zola/` on boot without crashing.
- **Hardened Error Fail-safes:** Employs a custom `release_all()` routine ensuring that if the daemon crashes mid-injection, modifier keys (like Shift or Ctrl) do not remain stuck.

---

## One-Line System Installation

Setting up Zola on Fedora, Arch, or Ubuntu requires only a single terminal command. This script downloads pre-compiled assets from GitHub, configures systemd, and registers uinput permissions.

```bash
curl -fsSL https://raw.githubusercontent.com/MSpider3/ZOLA-Voice-typing-for-Linux/main/install.sh | bash
```

> [!IMPORTANT]
> **Reboot or Relog Required:** The installer adds your user to the `input` group and configures a udev rule for `/dev/uinput`. You must **log out and log back in** (or reboot) for these group permissions to take effect.

---

## Desktop Environment & Compositor Integration

To dictate globally in any active text field, bind a global keyboard shortcut to Zola's local API trigger endpoint.

### GNOME (Fedora Default)
1. Open **Settings** -> **Keyboard** -> **Keyboard Shortcuts** -> **View and Customise Shortcuts** -> **Custom Shortcuts**.
2. Click **Add Shortcut** (`+`).
3. Set **Name** to `Zola Voice Trigger`.
4. Set **Command** to:
   ```bash
   curl -s -X POST http://127.0.0.1:5001/trigger
   ```
5. Assign your preferred keybinding (e.g., `Super + V`).

### KDE Plasma
1. Open **System Settings** -> **Shortcuts** -> **Add New** -> **Command**.
2. Set the command to:
   ```bash
   curl -s -X POST http://127.0.0.1:5001/trigger
   ```
3. Set your preferred shortcut (e.g., `Meta + V`).

### Hyprland
Add this to your configuration file (usually `~/.config/hypr/hyprland.conf`):
```ini
# Trigger active Zola transcription mode
bind = $mainMod, V, exec, curl -s -X POST http://127.0.0.1:5001/trigger
```

### Sway
Add this to your configuration file (usually `~/.config/sway/config`):
```sway
# Trigger active Zola transcription mode
bindsym Mod4+v exec curl -s -X POST http://127.0.0.1:5001/trigger
```

### Niri
Add this to your configuration file (usually `~/.config/niri/config.kdl`):
```kdl
// Float the Zola Voice Terminal window nicely in the center
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

binds {
    // Super+V: Toggle Active Voice Typing mode
    Mod+V { spawn "curl" "-s" "-X" "POST" "http://127.0.0.1:5001/trigger"; }
}
```

---

## Contributing

Contributions are welcome! Please follow these guidelines:

1. **Reporting Issues:** Search existing issues before opening a new one. Provide system specs (CPU, GPU, Wayland/X11, Compositor) and backend daemon logs using:
   ```bash
   journalctl --user -u zola-backend.service -n 100
   ```
2. **Pull Requests:** 
   - Fork the repository and create your branch from `main`.
   - Ensure changes build locally. Clean the build caches if you experience path errors:
     ```bash
     cd zola-frontend/src-tauri
     cargo clean
     ```
   - Commit messages should be clear and follow conventional commit styling (e.g. `feat: ...`, `fix: ...`, `docs: ...`).

---

## License

MIT License. See [LICENSE](LICENSE) for details.
