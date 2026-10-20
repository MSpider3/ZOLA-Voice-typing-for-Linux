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

import ipaddress
import json
import logging
import os
import shutil
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Set
from urllib.parse import urlparse

import httpx
import psutil

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CONFIG_DIR = Path.home() / ".config" / "zola"
CONFIG_PATH = CONFIG_DIR / "settings.json"

_LEGACY_CONFIG_PATH = Path.home() / ".config" / "speaktype" / "settings.json"

# Extra models live in one folder each under ~/.local/share/zola/models/<name>/:
#   - CTranslate2 Whisper models (e.g. fine-tunes): model.bin + tokenizer.json
#   - sherpa-onnx transducers (e.g. NVIDIA Nemotron 3.5): encoder/decoder/joiner
#     *.onnx + tokens.txt, with {"engine": "sherpa-transducer"} in zola.json
# zola.json can also pin the language token a model needs,
# e.g. {"language": "en"} for Oriserve Hindi2Hinglish (romanized output).
MODELS_DIR = Path.home() / ".local" / "share" / "zola" / "models"

VALID_WHISPER_SIZES = {
    "tiny", "base", "small", "medium",
    "large-v2", "large-v3",
    "large-v3-turbo",   # 6x faster than large-v3, negligible accuracy loss
    "tiny.en", "base.en", "small.en",  # English-only: faster and more accurate for English
    "distil-large-v3",  # DEPRECATED for multilingual — 89% WER on Hindi, auto-translates to English
}
# CPU-only: float16 variants need a GPU and make CTranslate2 refuse to load.
VALID_COMPUTE_TYPES = {"int8", "int8_float32", "int16", "float32"}

# Valid Whisper language codes relevant to Zola's target languages.
# None = auto-detect (recommended for Hinglish / code-switching sessions).
VALID_LANGUAGES: set[str | None] = {
    None, "en", "hi", "auto",
    # Additional common codes — expand as needed
    "fr", "de", "es", "zh", "ja", "ar", "pt", "ru",
}


# --------------------------------------------------------------------------- #
# Single Source of Truth — All Default Values
# --------------------------------------------------------------------------- #
_DEFAULTS: dict[str, Any] = {
    # ── LLM (Ollama) ──────────────────────────────────────────────────────────
    "ollama_model": "llama3.2",
    # ollama_url MUST be a loopback address. Validated in __post_init__.
    "ollama_url": "http://127.0.0.1:11434",
    # ollama = Ollama /api/generate; openai = any OpenAI-compatible local
    # server (llama.cpp server, LM Studio, vLLM) at ollama_url /v1/chat/completions
    "llm_api": "ollama",

    # ── Speech-to-Text (faster-whisper) ───────────────────────────────────────
    # Realtime model: keep small for low latency on CPU.
    "whisper_realtime": "base",
    # Batch model: large-v3-turbo gives much better Hindi/Hinglish accuracy
    # vs. 'small' with only moderate extra RAM (~1.6 GB vs ~500 MB).
    "whisper_batch": "small",
    "compute_type": "int8",
    # Physical core count, NOT logical thread count: on a 2-core/4-thread
    # i3 both engines ran ~25-100% slower with 4 threads than with 2
    # (STT_EVALUATION.md). Capped at 4 to leave cores for the desktop.
    "cpu_threads": max(1, min(4, psutil.cpu_count(logical=False) or 2)),

    # ── Language & Output ─────────────────────────────────────────────────────
    # spoken_language: what you speak into the mic.
    #   auto  = Whisper auto-detects per utterance (best for Hinglish)
    #   en    = force English
    #   hi    = force Hindi
    #   mixed = Hinglish code-switching (enables priming prompt)
    "spoken_language": "auto",
    # output_language: what gets typed / injected.
    #   preserve = output whatever Whisper returns (Devanagari or Latin)
    #   english  = translate Hindi/Hinglish to English (uses Whisper translate task)
    "output_language": "preserve",
    # Legacy single field kept for backwards compat — maps to spoken_language.
    # null = auto-detect.
    "language": None,
    # Optional prompt prepended to every Whisper call.
    # Use for domain vocabulary and language bias.
    # Example: "IIIT Ranchi, IIT Patna, Python, Rust, PyO3"
    "initial_prompt": None,

    # ── Audio Capture ─────────────────────────────────────────────────────────
    "sample_rate": 16000,
    # Hard cap on session length (seconds). Prevents runaway buffer growth.
    # 0 = no cap (not recommended).
    "max_record_duration_s": 300,

    # Microphone name (as listed by GET /devices); null = system default
    "input_device": None,
    # Short start/stop tones, so hotkey use gives feedback without the UI
    "audio_feedback": True,

    # ── Keystroke Injection (evdev / clipboard) ───────────────────────────────
    "typing_delay_ms": 12,
    # Key combo sent after copying the text:
    #   ctrl_v       = most apps
    #   ctrl_shift_v = terminal emulators
    #   shift_insert = works in both GTK/Qt apps and most terminals
    "paste_method": "ctrl_v",
    # Put the previous clipboard text back after pasting (text only: a copied
    # image or file is replaced by the transcript)
    "restore_clipboard": False,

    # ── Corrections ───────────────────────────────────────────────────────────
    # Whole-word, case-insensitive fixes applied to every transcript,
    # e.g. {"STD": "STT", "zola": "ZOLA"}
    "replacements": {},

    # ── Realtime Transcription ────────────────────────────────────────────────
    # Raised to 0.7s (from 0.5s) for better Hindi/Hinglish utterance segmentation.
    # Hindi speech has longer natural pauses between clauses than English.
    "realtime_utterance_silence_s": 0.7,
    "realtime_force_commit_s": 10.0,
    "realtime_chunk_s": 1.0,
    "realtime_silence_timeout_s": 3.0,

    # ── Batch Transcription ───────────────────────────────────────────────────
    "batch_silence_timeout_s": 3.0,
    "batch_min_record_s": 0.5,

    # ── History ───────────────────────────────────────────────────────────────
    "history_max": 50,
    # Keep history across restarts in ~/.local/share/zola/history.json
    "save_history": True,

    # ── Active Mode & Keybinding ──────────────────────────────────────────────
    # batch | batch-llm | batch-translate | realtime | realtime-llm
    "active_mode": "batch",
    "keybinding": "ctrl+shift+v",
    "first_run_completed": False,
}


# --------------------------------------------------------------------------- #
# Config Dataclass — Types & Validation Only, No Hardcoded Values
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """
    Typed view of settings loaded from settings.json.
    All fields are set by load_config() from _DEFAULTS + user JSON.
    No field has a Python-level default — values always come from the JSON file.
    """
    ollama_model: str
    ollama_url: str
    llm_api: str
    whisper_realtime: str
    whisper_batch: str
    compute_type: str
    cpu_threads: int
    spoken_language: str
    output_language: str
    language: str | None
    initial_prompt: str | None
    sample_rate: int
    max_record_duration_s: float
    input_device: str | None
    audio_feedback: bool
    typing_delay_ms: int
    paste_method: str
    restore_clipboard: bool
    replacements: dict
    realtime_utterance_silence_s: float
    realtime_force_commit_s: float
    realtime_chunk_s: float
    realtime_silence_timeout_s: float
    batch_silence_timeout_s: float
    batch_min_record_s: float
    history_max: int
    save_history: bool
    active_mode: str
    keybinding: str
    first_run_completed: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (for JSON persistence)."""
        return {
            "ollama_model": self.ollama_model,
            "ollama_url": self.ollama_url,
            "llm_api": self.llm_api,
            "whisper_realtime": self.whisper_realtime,
            "whisper_batch": self.whisper_batch,
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "spoken_language": self.spoken_language,
            "output_language": self.output_language,
            "language": self.language,
            "initial_prompt": self.initial_prompt,
            "sample_rate": self.sample_rate,
            "max_record_duration_s": self.max_record_duration_s,
            "input_device": self.input_device,
            "audio_feedback": self.audio_feedback,
            "typing_delay_ms": self.typing_delay_ms,
            "paste_method": self.paste_method,
            "restore_clipboard": self.restore_clipboard,
            "replacements": dict(self.replacements),
            "realtime_utterance_silence_s": self.realtime_utterance_silence_s,
            "realtime_force_commit_s": self.realtime_force_commit_s,
            "realtime_chunk_s": self.realtime_chunk_s,
            "realtime_silence_timeout_s": self.realtime_silence_timeout_s,
            "batch_silence_timeout_s": self.batch_silence_timeout_s,
            "batch_min_record_s": self.batch_min_record_s,
            "history_max": self.history_max,
            "save_history": self.save_history,
            "active_mode": self.active_mode,
            "keybinding": self.keybinding,
            "first_run_completed": self.first_run_completed,
        }

    def __post_init__(self) -> None:
        """Validate all fields. Raises ValueError on bad values."""

        # ── Types (POST /settings passes raw JSON values) ──
        for field in fields(self):
            name, expected = field.name, field.type
            value = getattr(self, name)
            if expected is float:
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            elif expected is int:
                ok = isinstance(value, int) and not isinstance(value, bool)
            elif expected in (str, bool):
                ok = isinstance(value, expected)
            elif expected is dict:
                ok = isinstance(value, dict) and all(
                    isinstance(k, str) and isinstance(v, str) for k, v in value.items()
                )
            else:  # str | None
                ok = value is None or isinstance(value, str)
            if not ok:
                raise ValueError(f"{name} has the wrong type ({type(value).__name__}).")

        # ── Active mode ──
        valid_modes = {"batch", "batch-llm", "batch-translate", "realtime", "realtime-llm"}
        if self.active_mode not in valid_modes:
            raise ValueError(
                f"active_mode '{self.active_mode}' is invalid. Valid: {sorted(valid_modes)}"
            )

        # ── Whisper model sizes ──
        valid_models = VALID_WHISPER_SIZES | set(installed_models())
        if self.whisper_realtime not in valid_models:
            raise ValueError(
                f"whisper_realtime '{self.whisper_realtime}' is invalid. "
                f"Valid: {sorted(valid_models)}"
            )
        if self.whisper_batch not in valid_models:
            raise ValueError(
                f"whisper_batch '{self.whisper_batch}' is invalid. "
                f"Valid: {sorted(valid_models)}"
            )

        # ── Compute type ──
        if self.compute_type not in VALID_COMPUTE_TYPES:
            raise ValueError(
                f"compute_type '{self.compute_type}' is invalid. "
                f"Valid: {sorted(VALID_COMPUTE_TYPES)}"
            )

        # ── Language codes ──
        if self.language is not None:
            if not isinstance(self.language, str):
                raise ValueError(
                    f"language must be a string language code or null, got {type(self.language).__name__}"
                )
            if self.language not in VALID_LANGUAGES:
                raise ValueError(
                    f"language '{self.language}' is not a recognised Whisper language code. "
                    f"Use null for auto-detect, 'en' for English, 'hi' for Hindi."
                )

        valid_spoken = {"auto", "en", "hi", "mixed"}
        if self.spoken_language not in valid_spoken:
            raise ValueError(
                f"spoken_language '{self.spoken_language}' is invalid. Valid: {sorted(valid_spoken)}"
            )

        if self.llm_api not in ("ollama", "openai"):
            raise ValueError("llm_api must be 'ollama' or 'openai'.")
        if self.paste_method not in ("ctrl_v", "ctrl_shift_v", "shift_insert"):
            raise ValueError("paste_method must be 'ctrl_v', 'ctrl_shift_v' or 'shift_insert'.")

        valid_output = {"preserve", "english"}
        if self.output_language not in valid_output:
            raise ValueError(
                f"output_language '{self.output_language}' is invalid. Valid: {sorted(valid_output)}"
            )

        # ── ollama_url: MUST be loopback (SSRF prevention) ──
        _validate_ollama_url(self.ollama_url)

        # ── Numeric ranges ──
        if self.sample_rate != 16000:
            raise ValueError("sample_rate must be 16000 (Whisper input rate; no resampling).")
        if not (0 < self.typing_delay_ms <= 200):
            raise ValueError("typing_delay_ms must be between 1 and 200.")
        if not (1 <= self.cpu_threads <= (os.cpu_count() or 1)):
            raise ValueError(
                f"cpu_threads must be between 1 and {os.cpu_count()} on this machine."
            )

        # ── Duration fields must be positive ──
        if self.realtime_utterance_silence_s < 0:
            raise ValueError("realtime_utterance_silence_s must be >= 0.")
        if self.realtime_force_commit_s <= 0:
            raise ValueError("realtime_force_commit_s must be positive.")
        if self.realtime_chunk_s <= 0:
            raise ValueError("realtime_chunk_s must be positive.")
        if self.history_max < 1:
            raise ValueError("history_max must be at least 1.")
        if self.max_record_duration_s < 0:
            raise ValueError("max_record_duration_s must be >= 0 (0 = no cap).")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def installed_models() -> list[str]:
    """Names of the model folders found in MODELS_DIR."""
    if not MODELS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in MODELS_DIR.iterdir()
        if not p.name.startswith(".")  # .partial-* = download in progress
        and ((p / "model.bin").is_file() or (p / "zola.json").is_file())
    )


def load_config() -> Config:
    """
    Load configuration from settings.json.

    Strategy:
      1. If no file exists → write _DEFAULTS to JSON, then load it.
      2. Merge: user JSON values override _DEFAULTS for known keys.
         Unknown keys in the JSON are ignored (forward compatibility).
      3. Validate merged values. On failure → revert to _DEFAULTS.
    """
    # One-time migration from legacy ~/.config/speaktype/ path
    try:
        if _LEGACY_CONFIG_PATH.exists() and not CONFIG_PATH.exists():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_LEGACY_CONFIG_PATH, CONFIG_PATH)
            logger.info(
                "Migrated legacy config: %s → %s",
                _LEGACY_CONFIG_PATH, CONFIG_PATH
            )
    except Exception as exc:
        logger.error("Failed to migrate legacy config (continuing with defaults): %s", exc)

    if not CONFIG_PATH.exists():
        logger.info("No config file found — generating defaults at %s", CONFIG_PATH)
        _write_defaults()

    try:
        raw: dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    except json.JSONDecodeError as exc:
        logger.error("Malformed JSON in config: %s — resetting to defaults", exc)
        _backup_config()
        _write_defaults()
        raw = {}

    # Overlay user values on top of _DEFAULTS
    merged = {k: raw.get(k, v) for k, v in _DEFAULTS.items()}

    try:
        cfg = Config(**merged)
        logger.info("Configuration loaded from %s", CONFIG_PATH)
        if set(_DEFAULTS.keys()) - set(raw.keys()):
            logger.info("Updating %s with new default keys", CONFIG_PATH)
            save_config(cfg)
        return cfg
    except (TypeError, ValueError) as exc:
        # Reset only the invalid settings (e.g. a deleted model folder), keeping
        # the rest. Keys are tried one at a time on top of the defaults.
        logger.error("Config validation failed: %s — resetting the invalid settings", exc)
        _backup_config()
        good = dict(_DEFAULTS)
        for key, value in merged.items():
            try:
                Config(**{**good, key: value})
                good[key] = value
            except (TypeError, ValueError) as key_exc:
                logger.error("Config: '%s' reset to default %r (%s)", key, _DEFAULTS[key], key_exc)
        cfg = Config(**good)
        save_config(cfg)
        return cfg


def save_config(cfg: Config) -> None:
    """Persist the current Config state to settings.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a crash mid-save cannot leave a truncated file.
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp_path, CONFIG_PATH)
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
        except (TypeError, ValueError) as exc:
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
def _validate_ollama_url(url: str) -> None:
    """
    Ensure ollama_url points to a loopback address only.
    Prevents SSRF: a malicious POST /settings cannot redirect transcripts
    to an external server.
    Raises ValueError on any non-loopback URL.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError(f"ollama_url is not a valid URL: {exc}") from exc

    if parsed.scheme != "http":
        raise ValueError(
            f"ollama_url must use http scheme, got '{parsed.scheme}'."
        )
        
    if parsed.username or parsed.password:
        raise ValueError("ollama_url cannot contain credentials.")

    host = parsed.hostname or ""
    if host in ("localhost",):
        return  # localhost always resolves to loopback — allowed

    try:
        addr = ipaddress.ip_address(host)
        if not addr.is_loopback:
            raise ValueError(
                f"ollama_url host '{host}' is not a loopback address. "
                f"Only 127.x.x.x or ::1 are allowed to prevent transcript leakage."
            )
    except ValueError as exc:
        # ip_address() raises ValueError for non-IP hostnames
        if "loopback" in str(exc):
            raise
        raise ValueError(
            f"ollama_url host '{host}' must be 'localhost' or a loopback IP "
            f"(127.x.x.x / ::1). Hostnames other than 'localhost' are not allowed."
        ) from exc


def _backup_config() -> None:
    """Keep the rejected settings.json as settings.json.bak before it is overwritten."""
    try:
        shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))
    except OSError as exc:
        logger.error("Could not back up %s: %s", CONFIG_PATH, exc)


def _discover_ollama_model() -> str:
    """
    Try to discover an installed Ollama model via its local API.
    Uses httpx (already a project dependency) instead of urllib.
    Falls back to 'llama3.2' if Ollama is unreachable.
    """
    try:
        response = httpx.get(
            "http://127.0.0.1:11434/api/tags",
            timeout=1.0,
            headers={"User-Agent": "Zola-Daemon"},
        )
        if response.status_code == 200:
            data = response.json()
            models = data.get("models", [])
            if models:
                return models[0]["name"]
    except Exception:
        pass  # nosec B110
    return "llama3.2"


def _write_defaults() -> None:
    """Write _DEFAULTS dict to settings.json with pretty formatting."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    defaults = _DEFAULTS.copy()
    defaults["ollama_model"] = _discover_ollama_model()
    CONFIG_PATH.write_text(
        json.dumps(defaults, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Default settings written to %s", CONFIG_PATH)
