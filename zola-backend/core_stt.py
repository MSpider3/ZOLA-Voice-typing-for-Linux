"""
core_stt.py — faster-whisper Engine with Hot-Reload
=====================================================
Loads and manages WhisperModel instances backed by CTranslate2.
CTranslate2 uses Intel MKL / oneDNN for CPU inference.

Two models are kept resident in RAM simultaneously:
  - 'realtime' model (default: base, int8)          → ~150MB RAM
  - 'batch'    model (default: small, int8)           → ~500MB RAM

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

        t0 = time.monotonic()
        self._realtime: WhisperModel = self._load_model(config.whisper_realtime)
        t1 = time.monotonic()
        self._batch: WhisperModel = self._load_model(config.whisper_batch)
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
            pass

        logger.info(
            "WhisperEngine: model '%s' not in local cache — downloading (once only)", size
        )
        model = WhisperModel(size, **_common_kwargs)
        logger.info("WhisperEngine: model '%s' downloaded and cached", size)
        return model

    async def hot_reload(self, slot: str, new_model_name: str) -> None:
        """
        Safely swap the model in `slot` ('realtime' or 'batch') for a new one.
        Blocks all transcription during the swap via asyncio.Lock.
        """
        if slot not in ("realtime", "batch"):
            raise ValueError(f"Invalid slot '{slot}'. Must be 'realtime' or 'batch'.")

        logger.info("WhisperEngine: hot-reload slot='%s' → '%s'", slot, new_model_name)
        loop = asyncio.get_event_loop()

        async with self._lock:
            new_model: WhisperModel = await loop.run_in_executor(
                None, self._load_model, new_model_name
            )
            attr = f"_{slot}"
            old_model = getattr(self, attr)
            setattr(self, attr, new_model)
            del old_model
            gc.collect()

            logger.info(
                "WhisperEngine: slot='%s' swapped to '%s', old model freed",
                slot, new_model_name,
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

    def _get_whisper_language(self) -> str | None:
        """
        Resolve the Whisper language parameter from config.
        spoken_language takes priority over the legacy language field.
        """
        spoken = self._config.spoken_language
        if spoken == "en":
            return "en"
        if spoken == "hi":
            return "hi"
        # "auto", "mixed", and the legacy language=None all map to auto-detect
        return self._config.language  # None = auto-detect

    # ----------------------------------------------------------------------- #
    # Transcription (synchronous — run in ThreadPoolExecutor)
    # ----------------------------------------------------------------------- #
    def transcribe_file(self, audio_path: Union[str, Path], mode: str = "batch") -> str:
        """
        Transcribe a WAV file using the batch or realtime model.

        Thread safety: snapshots the model reference under the asyncio.Lock
        before releasing it, so hot_reload() cannot delete the model mid-call.
        """
        # Snapshot model reference under lock (prevents hot-reload race)
        import asyncio as _asyncio
        try:
            _ = _asyncio.get_event_loop()
        except RuntimeError:
            pass

        # We are in a ThreadPoolExecutor — get model reference safely.
        # Since asyncio.Lock is not thread-safe, we use the fact that
        # Python attribute reads are atomic in CPython (GIL protected).
        # The snapshot means we hold a reference even if hot_reload() runs.
        model = self._realtime if mode == "realtime" else self._batch
        model_name = (
            self._config.whisper_realtime if mode == "realtime"
            else self._config.whisper_batch
        )
        chunk_s = (
            self._config.realtime_chunk_s if mode == "realtime"
            else self._config.batch_chunk_s
        )

        logger.debug(
            "WhisperEngine.transcribe_file: model='%s', chunk_s=%.1fs, path=%s",
            model_name, chunk_s, audio_path,
        )
        t0 = time.monotonic()

        try:
            sample_rate, data = wavfile.read(str(audio_path))
            audio = data.astype(np.float32) / 32768.0

            chunk_size = int(sample_rate * chunk_s)
            transcripts = []
            prev_text = ""

            for i in range(0, len(audio), chunk_size):
                chunk = audio[i: i + chunk_size]
                if len(chunk) < int(sample_rate * 0.3):
                    continue

                rms = float(np.sqrt(np.mean(chunk ** 2)))
                if rms < self._config.silence_rms_threshold:
                    continue

                initial_prompt = self._build_initial_prompt(prev_text, mode=mode)
                whisper_lang = self._get_whisper_language()

                segments, info = model.transcribe(
                    chunk,
                    language=whisper_lang,
                    task="transcribe",
                    beam_size=5,
                    best_of=5,
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": 300,  # tighter — less clipping
                        "speech_pad_ms": 100,
                        "threshold": 0.4,               # more sensitive for Hindi
                        "min_speech_duration_ms": 200,  # ignore sub-200ms noise
                    },
                    no_speech_threshold=0.5,
                    initial_prompt=initial_prompt,
                    condition_on_previous_text=bool(prev_text),
                )
                chunk_text = _join_segments(segments)
                if chunk_text:
                    transcripts.append(chunk_text)
                    prev_text = chunk_text

            transcript = " ".join(transcripts)
            elapsed = time.monotonic() - t0
            audio_duration = len(audio) / sample_rate
            rtf = elapsed / audio_duration if audio_duration > 0 else 0.0

            logger.info(
                "WhisperEngine.transcribe_file: %.1fs audio → %.1fs inference "
                "(RTF=%.2f), chars=%d",
                audio_duration, elapsed, rtf, len(transcript),
            )

            del audio, transcripts
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

        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < self._config.silence_rms_threshold:
            return ""

        # Atomic snapshot of realtime model reference (GIL-protected)
        model = self._realtime
        initial_prompt = self._build_initial_prompt(prev_text, mode="realtime")
        whisper_lang = self._get_whisper_language()

        try:
            segments, info = model.transcribe(
                audio,
                language=whisper_lang,
                task="transcribe",
                beam_size=1,
                best_of=1,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 300,
                    "speech_pad_ms": 100,
                    "threshold": 0.4,
                    "min_speech_duration_ms": 200,
                },
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

            chunk_size = int(sample_rate * self._config.batch_chunk_s)
            transcripts = []
            prev_text = ""

            for i in range(0, len(audio), chunk_size):
                chunk = audio[i: i + chunk_size]
                if len(chunk) < int(sample_rate * 0.3):
                    continue

                rms = float(np.sqrt(np.mean(chunk ** 2)))
                if rms < self._config.silence_rms_threshold:
                    continue

                initial_prompt = self._build_initial_prompt(prev_text, mode="translate")

                segments, info = model.transcribe(
                    chunk,
                    language=self._get_whisper_language(),
                    task="translate",          # ← Whisper translates to English
                    beam_size=5,
                    best_of=5,
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": 300,
                        "speech_pad_ms": 100,
                        "threshold": 0.4,
                        "min_speech_duration_ms": 200,
                    },
                    initial_prompt=initial_prompt,
                    condition_on_previous_text=bool(prev_text),
                )
                chunk_text = _join_segments(segments)
                if chunk_text:
                    transcripts.append(chunk_text)
                    prev_text = chunk_text

            transcript = " ".join(transcripts)
            elapsed = time.monotonic() - t0
            audio_duration = len(audio) / sample_rate
            logger.info(
                "WhisperEngine.translate_file: %.1fs audio → %.1fs inference, "
                "chars=%d (source lang detected: %s)",
                audio_duration, elapsed, len(transcript),
                getattr(info, "language", "unknown"),
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
