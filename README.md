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
* **Server-Sent Events (SSE):** Since transcription and recording states occur asynchronously on background threads, the backend pushes continuous status updates to the frontend using an SSE stream (`/events`). This includes active recording states and completed transcription text.
* **Exponential Backoff Connection Handler:** To ensure resilience, the frontend implements an exponential backoff reconnect handler with random jitter to automatically restore connection if the backend daemon restarts.

### Step-by-Step Program Lifecycle
1. **Shortcut Activation:** The user presses the configured global keybind (e.g., `Super + V`). The desktop compositor captures this shortcut and invokes a headless `POST` request to `http://127.0.0.1:5001/trigger`.
2. **Audio Capture (PipeWire):** The backend state manager transitions to the `RECORDING` state and initializes a PipeWire audio input pipeline. It captures raw audio frames at 16kHz mono.
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
- **Server-Sent Events (SSE):** The frontend listens to `/events` for state changes and transcripts. To ensure resilience, the frontend implements an exponential backoff reconnect handler with random jitter.

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
- **CRT Phosphor Visualizer:** Green-glow analog oscilloscope driven by the live microphone level, with a dBFS readout that warns when the mic is too quiet.
- **Output options:** paste with Ctrl+V, Ctrl+Shift+V (terminals) or Shift+Insert; optionally restore your previous clipboard after pasting; start/stop sound cues; microphone selection.
- **Corrections:** a vocabulary hint (names and terms you use) and whole-word replacements (`STD => STT`) applied to every transcript.
- **History:** kept across restarts in `~/.local/share/zola/history.json` (can be turned off), with copy and clear buttons. `POST /cancel` discards a recording without typing anything.
- **Automatic Legacy Migration:** Automatically migrates configurations from `~/.config/speaktype/` to `~/.config/zola/` on boot without crashing.
- **Hardened Error Fail-safes:** Employs a custom `release_all()` routine ensuring that if the daemon crashes mid-injection, modifier keys (like Shift or Ctrl) do not remain stuck.

---

## Installation

**Fedora / openSUSE (.rpm) or Ubuntu / Debian (.deb):** download the package from the [latest release](https://github.com/MSpider3/ZOLA-Voice-typing-for-Linux/releases/latest) and install it (`sudo dnf install ./zola-*.rpm` or `sudo apt install ./zola_*.deb`). The package contains both the app and its speech daemon, plus the `/dev/uinput` access rule; log out and back in once, then open Zola — it starts the daemon itself (its log: `~/.local/state/zola/daemon.log`).

The `.AppImage` also contains the daemon and works the same way. To have the daemon start at login (so the dictation hotkey works without opening the window), use the one-line installer below instead.

## One-Line System Installation (AppImage + systemd)

Setting up Zola on Fedora, Arch, or Ubuntu requires only a single terminal command. This script downloads pre-compiled assets from GitHub, configures systemd, and registers uinput permissions.

```bash
curl -fsSL https://raw.githubusercontent.com/MSpider3/ZOLA-Voice-typing-for-Linux/main/install.sh | bash
```

> [!IMPORTANT]
> **Reboot or Relog Required:** The installer adds your user to the `input` group and configures a udev rule for `/dev/uinput`. You must **log out and log back in** (or reboot) for these group permissions to take effect.

---

## Configuration & Settings (`~/.config/zola/settings.json`)

| Setting | Description |
|---|---|
| `spoken_language` | Sets language dictation mode: `en` (English), `hi` (Hindi), `mixed` (Hinglish), or `auto`. |
| `whisper_batch` | Model used for batch transcription (default: `small`). |
| `whisper_realtime` | Model used for realtime streaming (default: `base`). |
| `paste_method` | `ctrl_v` (default), `ctrl_shift_v` (terminals) or `shift_insert` (apps and terminals). |
| `restore_clipboard` | Put your previous clipboard text back after pasting (default: off). |
| `audio_feedback` | Short tones when recording starts and stops (default: on). |
| `input_device` | Microphone name from `GET /devices`; `null` = system default. |
| `replacements` | Whole-word fixes, e.g. `{"STD": "STT"}`. |
| `save_history` | Keep history across restarts (default: on). |
| `llm_api` | `ollama` (default) or `openai` for an OpenAI-compatible local server (llama.cpp server, LM Studio) at `ollama_url`. |

## Models

Zola runs local CTranslate2 `faster-whisper` models, plus extra model folders in `~/.local/share/zola/models/`. Measured on a 2-core Intel i3-1005G1 (word error rate; Hindi and Hinglish scored after romanizing so the output script does not count; RTF = processing time ÷ audio length):

| Model | English | Hindi | Hinglish | Your-voice Hindi / Hinglish | RTF | RAM | Output |
|---|---|---|---|---|---|---|---|
| **`hinglish-swift`** (Oriserve Hindi2Hinglish-Swift, int8, 78 MB) | 18% | **10%** | **12%** | 18% / 17% | **0.15** | 0.9 GB | Romanized Hinglish |
| **`nemotron-3.5`** (NVIDIA, sherpa-onnx int8) | **8%** | 17% | 36% | 13% / 45% | 0.46 | 1.0 GB | Devanagari + English |
| `base.en` / `small.en` | 11% / 12% | – | – | – | 0.12 / 0.31 | 0.9 / 1.5 GB | English only |
| `small` (old default) | 16% | 44% | 51% | 100% / 101% | 1.46 | 2.3 GB | Devanagari |

- **Download from the app:** Settings → MODELS → DOWNLOAD, then pick the model for batch and realtime separately. Downloads come from fixed sources (`hinglish-swift` from [MSpider3/whisper-hindi2hinglish-swift-ct2](https://huggingface.co/MSpider3/whisper-hindi2hinglish-swift-ct2), `nemotron-3.5` from the sherpa-onnx release) and the models then run offline. Headless: `curl -X POST http://127.0.0.1:5001/models/download -H 'Content-Type: application/json' -d '{"name":"hinglish-swift"}'`.
- **Hindi / Hinglish speakers:** `hinglish-swift` for both batch and realtime (~10x faster than `small` and far more accurate). It writes Roman script, like most people type Hinglish.
- **Devanagari Hindi or best English:** `nemotron-3.5` with `spoken_language` set to `hi` or `en`.
- **English only:** `base.en` (fastest) or `small.en`.
- Installed models appear in Settings without restarting the daemon. Full evaluation: [STT_EVALUATION.md](STT_EVALUATION.md).

## Requirements

- **OS:** Linux (Wayland / X11)
- **Audio:** PipeWire or PulseAudio, and PortAudio (`portaudio` / `libportaudio2`; the .rpm/.deb install it automatically)
- **Dependencies (Backend):** Python 3.11+, `faster-whisper`, `sounddevice`, `evdev` (for `uinput` virtual keystrokes)
- **Dependencies (Frontend):** Tauri, WebKit2GTK

## Known Limitations

- **Ollama Translators**: The local HTTP endpoint `/trigger/batch-translate` is currently marked experimental. Native Whisper translation to English is heavily unreliable for Hinglish input.
- **Code-Switching Support**: `hinglish-swift` handles Hinglish in Roman script; no model tested writes mixed script (Hindi in Devanagari, English in Latin) reliably on a weak CPU.
- **LLM refinement on weak CPUs**: small models (1–2B) sometimes answer a dictated question or change the meaning; Zola discards such outputs, but refinement is slow (10–70 s on a 2-core CPU) and best left off there.

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
