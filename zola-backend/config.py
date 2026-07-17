"""
config.py — Dynamic Configuration Manager
==========================================
Loads and persists settings from ~/.config/zola/settings.json.
Auto-creates the file with production defaults on first run.
Supports runtime hot-reload via update_config() without daemon restart.
If a legacy ~/.config/speaktype/settings.json is found on first boot,
it is automatically migrated to the new path.

SINGLE SOURCE OF TRUTH FOR DEFAULTS:
  All default values live ONLY in the _DEFAULTS dict below.
  The Config dataclass defines types and validation — not values.
  This means no default is ever buried in Python code; every tunable
  value is visible in and sourced from settings.json.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CONFIG_DIR = Path.home() / ".config" / "zola"
CONFIG_PATH = CONFIG_DIR / "settings.json"

# Legacy path — migrated automatically on first boot
_LEGACY_CONFIG_PATH = Path.home() / ".config" / "speaktype" / "settings.json"

VALID_WHISPER_SIZES = {"tiny", "base", "small", "medium", "large-v2", "large-v3"}
VALID_COMPUTE_TYPES = {"int8", "int8_float16", "int16", "float16", "float32"}


# --------------------------------------------------------------------------- #
# Single Source of Truth — All Default Values
# --------------------------------------------------------------------------- #
# Edit this dict (or settings.json) to change any default.
# These are written to settings.json on first run; after that, settings.json
# is the authoritative source. Python code never overrides user JSON values.
_DEFAULTS: dict[str, Any] = {
    # ── LLM (Ollama) ──────────────────────────────────────────────────────────
    # Local Ollama model tag. Run `ollama list` to see installed models.
    "ollama_model": "llama3.2",
    "ollama_url": "http://127.0.0.1:11434",

    # ── Speech-to-Text (faster-whisper) ───────────────────────────────────────
    # Model size for real-time mode (low latency): tiny | base | small
    "whisper_realtime": "base",
    # Model size for batch mode (higher accuracy): small | medium
    "whisper_batch": "small",
    # CTranslate2 quantization. int8 = optimal for Intel CPUs (MKL/oneDNN).
    "compute_type": "int8",
    # Number of CPU threads for inference. Match to physical core count.
    "cpu_threads": 4,
    # Transcription language. null = auto-detect (supports Hinglish/Hindi/English).
    "language": None,

    # ── Audio Capture ─────────────────────────────────────────────────────────
    # Sample rate in Hz. Whisper requires 16000. Do not change unless you know
    # what you are doing.
    "sample_rate": 16000,

    # ── Keystroke Injection (evdev) ───────────────────────────────────────────
    # Inter-key delay in milliseconds. Minimum for Niri/Wayland is ~12ms.
    # Increase to 20-30ms if characters are dropped in slow applications.
    "typing_delay_ms": 12,

    # ── Realtime Transcription ────────────────────────────────────────────────
    # Seconds of silence that mark the end of one utterance.
    # When this much silence is heard, the buffered audio is sent to Whisper
    # and the result is typed. Text is appended — never rewritten.
    # Set to 0 to disable VAD splitting (uses force_commit only).
    "realtime_utterance_silence_s": 0.5,
    # Force-commit utterance after this many seconds even if no silence detected.
    # Prevents unbounded buffer growth during continuous long speech.
    "realtime_force_commit_s": 10.0,
    # Max chunk size when utterance_silence_s = 0 (fixed-interval mode).
    "realtime_chunk_s": 1.0,
    # Auto-stop recording when silence exceeds this many seconds in realtime mode.
    # Set to 0 to disable auto-stop (recording continues until manual toggle).
    "realtime_silence_timeout_s": 3.0,

    # ── Batch Transcription ───────────────────────────────────────────────────
    # Auto-stop recording when silence exceeds this many seconds.
    # Set to 0 to keep recording until second trigger press (manual stop only).
    "batch_silence_timeout_s": 3.0,
    # Minimum recording duration before silence-stop can activate.
    "batch_min_record_s": 0.5,
    # Chunk size for processing long batch recordings in segments.
    "batch_chunk_s": 6.0,

    # ── Silence Detection ─────────────────────────────────────────────────────
    # RMS energy below this value = silence (range 0.0–1.0).
    # Raise if background noise causes false positives.
    # Lower if your microphone is very quiet and silence isn't detected.
    "silence_rms_threshold": 0.035,

    # ── History ───────────────────────────────────────────────────────────────
    # Maximum number of transcription entries kept in memory.
    "history_max": 50,

    # ── Active Mode & Keybinding ──────────────────────────────────────────────
    # Active transcription mode: batch | batch-llm | realtime | realtime-llm
    "active_mode": "batch",
    # Global trigger shortcut string
    "keybinding": "ctrl+shift+v",
}


# --------------------------------------------------------------------------- #
# Config Dataclass — Types & Validation Only, No Hardcoded Values
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """
    Typed view of the settings loaded from settings.json.
    All fields are set by load_config() from _DEFAULTS + user JSON.
    No field has a Python-level default — values always come from the JSON file.
    """
    ollama_model: str
    ollama_url: str
    whisper_realtime: str
    whisper_batch: str
    compute_type: str
    cpu_threads: int
    language: Optional[str]
    sample_rate: int
    typing_delay_ms: int
    realtime_utterance_silence_s: float
    realtime_force_commit_s: float
    realtime_chunk_s: float
    realtime_silence_timeout_s: float
    batch_silence_timeout_s: float
    batch_min_record_s: float
    batch_chunk_s: float
    silence_rms_threshold: float
    history_max: int
    active_mode: str
    keybinding: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (for JSON persistence)."""
        return {
            "ollama_model": self.ollama_model,
            "ollama_url": self.ollama_url,
            "whisper_realtime": self.whisper_realtime,
            "whisper_batch": self.whisper_batch,
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "typing_delay_ms": self.typing_delay_ms,
            "realtime_utterance_silence_s": self.realtime_utterance_silence_s,
            "realtime_force_commit_s": self.realtime_force_commit_s,
            "realtime_chunk_s": self.realtime_chunk_s,
            "realtime_silence_timeout_s": self.realtime_silence_timeout_s,
            "batch_silence_timeout_s": self.batch_silence_timeout_s,
            "batch_min_record_s": self.batch_min_record_s,
            "batch_chunk_s": self.batch_chunk_s,
            "silence_rms_threshold": self.silence_rms_threshold,
            "history_max": self.history_max,
            "active_mode": self.active_mode,
            "keybinding": self.keybinding,
        }

    def __post_init__(self) -> None:
        """Validate all fields. Raises ValueError on bad values."""
        if self.active_mode not in {"batch", "batch-llm", "realtime", "realtime-llm"}:
            raise ValueError(
                f"active_mode '{self.active_mode}' is invalid. "
                f"Valid: {sorted({'batch', 'batch-llm', 'realtime', 'realtime-llm'})}"
            )
        if self.whisper_realtime not in VALID_WHISPER_SIZES:
            raise ValueError(
                f"whisper_realtime '{self.whisper_realtime}' is invalid. "
                f"Valid: {sorted(VALID_WHISPER_SIZES)}"
            )
        if self.whisper_batch not in VALID_WHISPER_SIZES:
            raise ValueError(
                f"whisper_batch '{self.whisper_batch}' is invalid. "
                f"Valid: {sorted(VALID_WHISPER_SIZES)}"
            )
        if self.compute_type not in VALID_COMPUTE_TYPES:
            raise ValueError(
                f"compute_type '{self.compute_type}' is invalid. "
                f"Valid: {sorted(VALID_COMPUTE_TYPES)}"
            )
        if self.sample_rate not in (8000, 16000, 22050, 44100, 48000):
            raise ValueError(f"sample_rate {self.sample_rate} is not a standard rate.")
        if not (0 < self.typing_delay_ms <= 200):
            raise ValueError("typing_delay_ms must be between 1 and 200.")
        if not (1 <= self.cpu_threads <= (os.cpu_count() or 1)):
            raise ValueError(
                f"cpu_threads must be between 1 and {os.cpu_count()} on this machine."
            )
        if self.silence_rms_threshold < 0 or self.silence_rms_threshold > 1.0:
            raise ValueError("silence_rms_threshold must be between 0.0 and 1.0.")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def load_config() -> Config:
    """
    Load configuration from settings.json.

    Strategy:
      1. If no file exists → write _DEFAULTS to JSON, then load it.
      2. Merge: user JSON values override _DEFAULTS for known keys.
         Unknown keys in the JSON are ignored (forward compatibility).
      3. Validate merged values. On failure → revert to _DEFAULTS.

    This guarantees: all values always come from the JSON file.
    _DEFAULTS is only used to generate the initial file and fill in
    any keys the user may have deleted.
    """
    # --- One-time migration from legacy ~/.config/speaktype/ path ---
    try:
        if _LEGACY_CONFIG_PATH.exists() and not CONFIG_PATH.exists():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_LEGACY_CONFIG_PATH, CONFIG_PATH)
            logger.info(
                "Migrated legacy config: %s → %s",
                _LEGACY_CONFIG_PATH, CONFIG_PATH
            )
    except Exception as exc:
        logger.error(
            "Failed to migrate legacy config (continuing with defaults): %s", exc
        )

    if not CONFIG_PATH.exists():
        logger.info("No config file found — generating defaults at %s", CONFIG_PATH)
        _write_defaults()

    migrated = False
    try:
        raw: dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        
        # Migration: if user has old defaults, upgrade them for better experience
        if raw.get("realtime_force_commit_s") == 10.0:
            raw["realtime_force_commit_s"] = 2.5
            migrated = True
            logger.info("Migrated realtime_force_commit_s to 2.5s for lower latency")
        if raw.get("realtime_utterance_silence_s") == 0.8:
            raw["realtime_utterance_silence_s"] = 0.5
            migrated = True
            logger.info("Migrated realtime_utterance_silence_s to 0.5s for faster commits")
        if raw.get("realtime_chunk_s") == 2.0:
            raw["realtime_chunk_s"] = 1.0
            migrated = True
            logger.info("Migrated realtime_chunk_s to 1.0s for faster streaming")
        if raw.get("silence_rms_threshold") == 0.02:
            raw["silence_rms_threshold"] = 0.035
            migrated = True
            logger.info("Migrated silence_rms_threshold to 0.035 for robust noise suppression")
            
    except json.JSONDecodeError as exc:
        logger.error("Malformed JSON in config: %s — resetting to defaults", exc)
        _write_defaults()
        raw = {}

    # Overlay user values on top of _DEFAULTS (unknown user keys silently dropped)
    merged = {k: raw.get(k, v) for k, v in _DEFAULTS.items()}

    try:
        cfg = Config(**merged)
        logger.info("Configuration loaded from %s", CONFIG_PATH)
        
        # If new default keys were merged or migrated, save the file to update user's settings.json
        if migrated or (set(_DEFAULTS.keys()) - set(raw.keys())):
            logger.info("Updating %s with new default keys/migrations", CONFIG_PATH)
            save_config(cfg)
            
        return cfg
    except (TypeError, ValueError) as exc:
        logger.error("Config validation failed: %s — reverting to defaults", exc)
        _write_defaults()
        return Config(**_DEFAULTS)


def save_config(cfg: Config) -> None:
    """Persist the current Config state to settings.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.debug("Configuration saved to %s", CONFIG_PATH)


def update_config(cfg: Config, partial: dict[str, Any]) -> Set[str]:
    """
    Apply a partial dict of settings to an existing Config in-place.
    Persists changes to settings.json immediately.
    Returns the set of keys that actually changed.
    """
    changed: Set[str] = set()
    current = cfg.to_dict()

    for key, new_value in partial.items():
        if key not in current:
            logger.warning("Unknown config key '%s' — skipping", key)
            continue
        if current[key] != new_value:
            setattr(cfg, key, new_value)
            changed.add(key)
            logger.info("Config update: %s = %r (was %r)", key, new_value, current[key])

    if changed:
        try:
            cfg.__post_init__()
        except ValueError as exc:
            logger.error("Config update rejected (validation): %s — rolling back", exc)
            for key in changed:
                setattr(cfg, key, current[key])
            raise
        save_config(cfg)
        logger.info("Config persisted — %d change(s): %s", len(changed), changed)
    else:
        logger.debug("Config update: no values changed")

    return changed


# --------------------------------------------------------------------------- #
# Private Helpers
# --------------------------------------------------------------------------- #
def _discover_ollama_model() -> str:
    """Try to discover an installed Ollama model dynamically on the local machine."""
    import urllib.request
    import json
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/tags",
            headers={"User-Agent": "Zola-Daemon"}
        )
        with urllib.request.urlopen(req, timeout=1.0) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                models = data.get("models", [])
                if models:
                    # Return the first available model name
                    return models[0]["name"]
    except Exception:
        pass
    return "llama3.2"


def _write_defaults() -> None:
    """Write _DEFAULTS dict to settings.json with pretty formatting."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Dynamically inject the first discovered model name as default if possible
    defaults = _DEFAULTS.copy()
    defaults["ollama_model"] = _discover_ollama_model()

    CONFIG_PATH.write_text(
        json.dumps(defaults, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Default settings written to %s", CONFIG_PATH)
