# Changelog

All notable changes to the Zola Voice Typing project will be documented in this file.

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
