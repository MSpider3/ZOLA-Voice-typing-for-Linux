"""
core_stt.py — faster-whisper Engine with Hot-Reload
=====================================================
Loads and manages WhisperModel instances backed by CTranslate2.
CTranslate2 uses oneDNN / OpenBLAS for CPU inference.

Two models are kept resident in RAM simultaneously:
  - 'realtime' model (default: base, int8)          → ~150MB RAM
  - 'batch'    model (default: small, int8)           → ~500MB RAM

If both slots name the same model, one instance is shared.

Hot-reload swaps one model at a time under an asyncio.Lock.
Transcription methods snapshot the model reference under the lock
to prevent a race condition where hot_reload() deletes the old
model while a ThreadPoolExecutor call is still using it.

Hinglish priming:
  When spoken_language is 'auto' or 'mixed', HINGLISH_PROMPT is
  prepended to initial_prompt to bias Whisper toward Hindi/English
  code-switching and reduce mis-detection of short utterances.
"""

import asyncio
import gc
import logging
import os
import time
from pathlib import Path
from typing import Union

import numpy as np
from scipy.io import wavfile
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions

from config import Config

logger = logging.getLogger(__name__)
_ZOLA_DEBUG = os.environ.get("ZOLA_DEBUG") == "1"

# Prepended to initial_prompt when spoken_language is 'auto' or 'mixed'.
# Biases Whisper toward Hindi/English code-switching, significantly reducing
# mis-detection of short Hinglish utterances as Urdu/Bengali/Punjabi.
HINGLISH_PROMPT = (
    "Hindi-English code-switching transcription. "
    "मैं हिंदी और English दोनों में बोलता हूँ।"
)

# Models large enough to handle the Hinglish priming prompt without
# hallucinating.  Smaller models (tiny, base, small) produce >100% WER
# when prompted with mixed-script text.
_HINGLISH_SAFE_MODELS = {"large-v3-turbo", "large-v3", "large-v2", "medium"}

_VAD_PARAMS = VadOptions(
    min_silence_duration_ms=300,  # tighter — less clipping
    speech_pad_ms=100,
    threshold=0.4,                # more sensitive for Hindi
    min_speech_duration_ms=200,   # ignore sub-200ms noise
)

class WhisperEngine:
    """
    Manages two persistent WhisperModel instances for real-time and batch use.
    All public transcription methods are synchronous (CPU-bound) and must be
    called via loop.run_in_executor() from async code.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = asyncio.Lock()

        logger.info(
            "WhisperEngine: loading models — realtime='%s', batch='%s' "
            "(compute='%s', threads=%d, workers=2)",
            config.whisper_realtime,
            config.whisper_batch,
            config.compute_type,
            config.cpu_threads,
        )

        self._names = {"realtime": config.whisper_realtime, "batch": config.whisper_batch}

        t0 = time.monotonic()
        self._realtime: WhisperModel = self._load_model(config.whisper_realtime)
        t1 = time.monotonic()
        self._batch: WhisperModel = (
            self._realtime if config.whisper_batch == config.whisper_realtime
            else self._load_model(config.whisper_batch)
        )
        t2 = time.monotonic()

        logger.info(
            "WhisperEngine: models loaded — realtime in %.1fs, batch in %.1fs",
            t1 - t0, t2 - t1,
        )

    # ----------------------------------------------------------------------- #
    # Model Lifecycle
    # ----------------------------------------------------------------------- #
    def _load_model(self, size: str) -> WhisperModel:
        """
        Instantiate a WhisperModel with offline-first loading.
        num_workers=2: allows CTranslate2 to preprocess the next audio chunk
        while the current one is being inferred — hides preprocessing latency.
        """
        _common_kwargs = dict(
            device="cpu",
            compute_type=self._config.compute_type,
            cpu_threads=self._config.cpu_threads,
            num_workers=2,   # Up from 1: preprocessing overlaps with inference
        )

        try:
            model = WhisperModel(size, local_files_only=True, **_common_kwargs)
            logger.info("WhisperEngine: loaded '%s' from local cache (offline)", size)
            return model
        except Exception:
            pass  # nosec B110

        logger.info(
            "WhisperEngine: model '%s' not in local cache — downloading (once only)", size
        )
        model = WhisperModel(size, **_common_kwargs)
        logger.info("WhisperEngine: model '%s' downloaded and cached", size)
        return model

    async def hot_reload(self, slot: str, new_model_name: str, fresh: bool = False) -> None:
        """
        Safely swap the model in `slot` ('realtime' or 'batch') for a new one.
        The old model is released before the new one is loaded so two large
        models are never resident at once; the slot is None during the load
        and stays None if the load fails (the caller reloads the old model).
        fresh=True always loads a new instance (cpu_threads/compute_type changed)
        instead of sharing the other slot's model.
        """
        if slot not in ("realtime", "batch"):
            raise ValueError(f"Invalid slot '{slot}'. Must be 'realtime' or 'batch'.")

        logger.info("WhisperEngine: hot-reload slot='%s' → '%s'", slot, new_model_name)
        loop = asyncio.get_event_loop()
        other = "batch" if slot == "realtime" else "realtime"

        async with self._lock:
            attr = f"_{slot}"
            setattr(self, attr, None)
            self._names[slot] = None
            gc.collect()

            if not fresh and new_model_name == self._names[other]:
                new_model = getattr(self, f"_{other}")
            else:
                new_model = await loop.run_in_executor(
                    None, self._load_model, new_model_name
                )
            setattr(self, attr, new_model)
            self._names[slot] = new_model_name

            logger.info(
                "WhisperEngine: slot='%s' swapped to '%s'", slot, new_model_name,
            )

    # ----------------------------------------------------------------------- #
    # Internal helpers
    # ----------------------------------------------------------------------- #
    def _build_initial_prompt(self, prev_text: str = "", mode: str = "batch") -> str | None:
        """
        Build the initial_prompt string for a Whisper call.

        Priority order:
          1. User-configured initial_prompt (from settings.json)
          2. Hinglish priming prompt (batch mode + auto/mixed language + safe model)
          3. Previous utterance text (for cross-utterance context continuity)

        Returns None if there is nothing to pass (no unnecessary empty strings).
        """
        parts: list[str] = []

        # User-configured static prompt (domain vocabulary, language bias)
        if self._config.initial_prompt:
            parts.append(self._config.initial_prompt.strip())

        # Hinglish priming — biases Whisper toward Hindi/English code-switching.
        # Only applied when: batch mode + mixed/auto language + model is large enough.
        # Small models hallucinate catastrophically with this prompt (WER >100%).
        if mode == "batch" and self._config.spoken_language in ("auto", "mixed"):
            if self._config.whisper_batch in _HINGLISH_SAFE_MODELS:
                parts.append(HINGLISH_PROMPT)
            else:
                logger.warning(
                    "WhisperEngine: Hinglish priming prompt suppressed — "
                    "model '%s' hallucinates with this prompt. "
                    "Switch to large-v3-turbo for Hinglish accuracy, "
                    "or set spoken_language to 'en' or 'hi'.",
                    self._config.whisper_batch,
                )

        # Cross-utterance context
        if prev_text:
            parts.append(prev_text.strip())

        combined = " ".join(parts).strip()
        return combined if combined else None

    def _resolve_language(self, model: WhisperModel, audio: np.ndarray) -> str:
        """
        Resolve the Whisper language for this audio.
        spoken_language 'en'/'hi' (or the legacy language field) is used as-is.
        For 'auto'/'mixed' the choice is restricted to Hindi or English, so short
        Hinglish utterances are never transcribed as Urdu/Bengali/Punjabi.
        """
        spoken = self._config.spoken_language
        if spoken in ("en", "hi"):
            return spoken
        if self._config.language not in (None, "auto"):
            return self._config.language
        _, _, all_probs = model.detect_language(
            audio, vad_filter=True, vad_parameters=_VAD_PARAMS,
        )
        probs = dict(all_probs)
        return "hi" if probs.get("hi", 0.0) > probs.get("en", 0.0) else "en"

    # ----------------------------------------------------------------------- #
    # Transcription (synchronous — run in ThreadPoolExecutor)
    # ----------------------------------------------------------------------- #
    def transcribe_file(self, audio_path: Union[str, Path], mode: str = "batch") -> str:
        """
        Transcribe a WAV file using the batch or realtime model.

        Thread safety: snapshots the model reference under the asyncio.Lock
        before releasing it, so hot_reload() cannot delete the model mid-call.
        """
        # We are in a ThreadPoolExecutor — get model reference safely.
        # Since asyncio.Lock is not thread-safe, we use the fact that
        # Python attribute reads are atomic in CPython (GIL protected).
        # The snapshot means we hold a reference even if hot_reload() runs.
        model = self._realtime if mode == "realtime" else self._batch
        model_name = (
            self._config.whisper_realtime if mode == "realtime"
            else self._config.whisper_batch
        )

        logger.debug(
            "WhisperEngine.transcribe_file: model='%s', path=%s",
            model_name, audio_path,
        )
        t0 = time.monotonic()

        try:
            sample_rate, data = wavfile.read(str(audio_path))
            audio = data.astype(np.float32) / 32768.0

            # One call for the whole recording: Whisper windows it internally,
            # so no word is split at an arbitrary chunk boundary.
            segments, info = model.transcribe(
                audio,
                language=self._resolve_language(model, audio),
                task="transcribe",
                beam_size=5,
                best_of=5,
                vad_filter=True,
                vad_parameters=_VAD_PARAMS,
                no_speech_threshold=0.5,
                initial_prompt=self._build_initial_prompt(mode=mode),
                condition_on_previous_text=False,
            )
            transcript = _join_segments(segments)
            elapsed = time.monotonic() - t0
            audio_duration = len(audio) / sample_rate
            rtf = elapsed / audio_duration if audio_duration > 0 else 0.0

            logger.info(
                "WhisperEngine.transcribe_file: %.1fs audio → %.1fs inference "
                "(RTF=%.2f), chars=%d",
                audio_duration, elapsed, rtf, len(transcript),
            )

            del audio
            gc.collect()
            return transcript

        except Exception as exc:
            logger.exception("WhisperEngine.transcribe_file failed: %s", exc)
            return ""

    def transcribe_chunk(
        self,
        audio: np.ndarray,
        sample_rate: int,
        prev_text: str = "",
    ) -> str:
        """
        Transcribe a live audio chunk (numpy float32 array) using the realtime model.
        Optimised for speed: beam_size=1, VAD enabled to skip silence frames.

        Thread safety: same GIL-atomic attribute snapshot as transcribe_file.
        """
        if len(audio) < sample_rate * 0.2:
            return ""

        # Atomic snapshot of realtime model reference (GIL-protected)
        model = self._realtime
        initial_prompt = self._build_initial_prompt(prev_text, mode="realtime")

        try:
            segments, info = model.transcribe(
                audio,
                language=self._resolve_language(model, audio),
                task="transcribe",
                beam_size=1,
                best_of=1,
                vad_filter=True,
                vad_parameters=_VAD_PARAMS,
                no_speech_threshold=0.5,
                initial_prompt=initial_prompt,
                condition_on_previous_text=bool(prev_text),
            )

            transcript = _join_segments(segments)
            if transcript and _ZOLA_DEBUG:
                logger.debug(
                    "WhisperEngine.transcribe_chunk: lang=%s text='%.60s'",
                    info.language, transcript,
                )

            del audio
            gc.collect()
            return transcript

        except Exception as exc:
            logger.exception("WhisperEngine.transcribe_chunk failed: %s", exc)
            return ""

    def translate_file(self, audio_path: Union[str, Path]) -> str:
        """
        Translate non-English audio (e.g. Hindi) directly to English text
        using Whisper's built-in translation task (task="translate").

        This is the backend for /trigger/batch-translate mode.
        No Ollama required — Whisper handles the translation natively.
        Uses the batch model for best accuracy.

        Thread safety: GIL-atomic batch model snapshot.
        """
        model = self._batch  # atomic snapshot
        logger.debug("WhisperEngine.translate_file: path=%s", audio_path)
        t0 = time.monotonic()

        try:
            sample_rate, data = wavfile.read(str(audio_path))
            audio = data.astype(np.float32) / 32768.0

            segments, info = model.transcribe(
                audio,
                language=self._resolve_language(model, audio),
                task="translate",          # ← Whisper translates to English
                beam_size=5,
                best_of=5,
                vad_filter=True,
                vad_parameters=_VAD_PARAMS,
                initial_prompt=self._build_initial_prompt(mode="translate"),
                condition_on_previous_text=False,
            )
            transcript = _join_segments(segments)
            elapsed = time.monotonic() - t0
            audio_duration = len(audio) / sample_rate
            detected_lang = info.language
            logger.info(
                "WhisperEngine.translate_file: %.1fs audio → %.1fs inference, "
                "chars=%d (source lang detected: %s)",
                audio_duration, elapsed, len(transcript),
                detected_lang,
            )
            return transcript

        except Exception as exc:
            logger.exception("WhisperEngine.translate_file failed: %s", exc)
            return ""


# --------------------------------------------------------------------------- #
# Private Helpers
# --------------------------------------------------------------------------- #
def _join_segments(segments) -> str:
    """Materialise the lazy segment generator and join all text."""
    texts = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            texts.append(text)
    return " ".join(texts)
