# Changelog

All notable changes to the Zola Voice Typing project will be documented in this file.

## [v1.3.0] - 2026-10-20

### Added
- **One package with app and daemon**: the `.rpm`, `.deb` and `.AppImage` now contain the speech daemon (Tauri sidecar) and the app starts it when none is running; the `.rpm`/`.deb` also install the `/dev/uinput` access rule. The separate `zola-daemon` download is only needed by `install.sh`.
- **STT evaluation for weak CPUs** (`STT_EVALUATION.md`): 12 models tested offline on English, Hindi and Hinglish speech, clean and noisy, on a 2-core laptop CPU.
- **Extra model folders** in `~/.local/share/zola/models/`, listed in Settings with their labels; `zola.json` can pin a model's language token or select the sherpa-onnx engine.
- **In-app model downloads** (Settings → MODELS, `GET /models/catalog`, `POST /models/download`, `POST /models/delete`) with live progress; fixed sources only, installed atomically.
- **Hinglish Swift model** (`hinglish-swift`, published as `MSpider3/whisper-hindi2hinglish-swift-ct2`): Oriserve Whisper-Hindi2Hinglish-Swift as CTranslate2 int8. On a 2-core i3: Hindi 10% / Hinglish 12% WER at RTF 0.16, vs 44% / 51% at RTF 1.46 for `small` before this release.
- **Nemotron 3.5 engine** (`nemotron-3.5`, `sherpa-onnx` dependency): Devanagari Hindi and the most accurate English (8% WER).
- **English-only Whisper sizes** (`tiny.en`, `base.en`, `small.en`); first-run setup picks models by spoken language.
- **Paste method** (Ctrl+V / Ctrl+Shift+V / Shift+Insert), **clipboard restore**, **start/stop sound cues**, **microphone selection** (`GET /devices`), **word replacements**, vocabulary hint in Settings, **`POST /cancel`**.
- **History kept across restarts** (`save_history`), with copy and clear buttons.
- **Live mic level** (`level` SSE event) drives the oscilloscope and a dBFS readout that warns when the mic is too quiet.
- **OpenAI-compatible local LLM servers** (`llm_api: openai`).

### Fixed
- **Temperature-fallback retries**: Whisper re-decoded hard audio up to temperature 1.0, turning 35 s of Hindi into 70-200 s of processing and random-script output. Decoding is now greedy only, with a guard against repetition loops.
- **Language detection pass** now runs on the (smaller) realtime model, and is skipped for models with a pinned language.
- **Hinglish priming warning** was logged on every transcription; now once.
- **LLM refinement answered dictated questions or rewrote the text** with small models; the transcript is now marked as data, reasoning models run with thinking off, and outputs that are not a plausible correction are discarded.
- **`cpu_threads` default** follows the physical core count (more threads were 25-100% slower on a 2-core/4-thread CPU).
- **First-run setup for Hindi/Hinglish** downloads Hinglish Swift (or Nemotron 3.5 for Devanagari) and switches to it; standard Whisper sizes measured 84-97% WER on Hindi. `install.sh` asks for a model only for English.
- **One invalid setting no longer resets all settings**: only the invalid keys (e.g. a deleted model folder) fall back to their defaults.
- **`/dev/uinput` access check** replaces the misleading "not in the input group" warning, which fired even when the uaccess rule worked.
- **Model downloads are pinned**: Hinglish Swift to a fixed repository commit, Nemotron to the release archive's SHA-256.
- **The app does not start a second daemon** while one is still loading at login; the bundled daemon logs to `~/.local/state/zola/daemon.log`.
- **CI runs the regression tests** before building a release.
- Removed unused code: the template `greet` command, the opener plugin, `erase_chars()`, and the installer's stale Nemotron option.

## [1.2.0] - 2026-10-02

### Added
- **Language Selection**: Added support for Hindi and Code-Switched (Hinglish) dictation.
- **Hinglish Optimizations**: Native prompting engine configured to stop Hindi code-switching hallucinations on English words.
- **Nemotron Evaluation**: Evaluated NVIDIA Nemotron for monolingual Hindi/English dictation; deferred to v1.3.0 modular engine release due to 101% WER on Hinglish code-switching.
- **GUI Wizard**: Added an interactive setup wizard in the Zola app to select language and hardware profiles.

### Fixed
- **Hindi Clipboard Unicode Bug**: Switched from `pyautogui` to `xclip` / `wl-copy` for reliable UTF-8 pasting of Devanagari characters on Linux.
- **Security Hardening**: Closed open CORS vulnerabilities and prevented arbitrary URL loading in Ollama local endpoints.
- **Thermal Throttling Control**: Reverted the default batch model from `large-v3-turbo` to `small` to prevent sustained thermal throttling on laptops.
- **Realtime Latency Fix**: Corrected VAD thresholds preventing silence detection lockups.

## [1.1.1] - 2026-07-18

### Fixed
- **WebKit2GTK Wayland Fix**: Disabled DMA-BUF renderer programmatically on Linux target platforms to prevent Wayland/Nvidia/Intel graphics display crashes (grey/black screen).
- **PyInstaller Missing VAD Assets**: Updated backend compilation via `zola-daemon.spec` to pack the required `silero_vad_v6.onnx` models from the `faster_whisper` package, restoring speech transcription.
- **Installer Cleanup**: Modified `install.sh` to clean up and force close any lingering or frozen `Zola.AppImage` processes during system updates.

## [1.1.0] - 2026-07-18

### Fixed
- **Daemon PyInstaller Load Error**: Corrected frozen executable backend imports (calling uvicorn directly with the application instance rather than a module string).
- **Installer Update Lock**: Cleaned up systemd service teardown in `install.sh` to prevent `Text file busy` overwrite errors.

### Added
- **Global Keybinding Guides**: Added configuration files/instructions for custom shortcuts on GNOME, KDE, Hyprland, Sway, and Niri desktop environments to the README.

## [1.0.0] - 2026-07-07

### Added
- **Core STT System**: Integrated faster-whisper speech-to-text models with voice activity detection (VAD).
- **CRT Oscilloscope UI**: Retro-military green CRT vector oscilloscope visualizer displaying realtime wave forms.
- **Systemd Integration**: Daemon processes run seamlessly as persistent systemd user services.
