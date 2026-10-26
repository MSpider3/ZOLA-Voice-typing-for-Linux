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
import functools
import gc
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from types import SimpleNamespace

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions

from config import MODELS_DIR, VALID_WHISPER_SIZES, Config

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

# Greedy decoding only. Whisper's default temperature fallback (0.2 … 1.0)
# re-decodes hard audio up to five more times; on a weak CPU that turned 35 s
# of Hindi into 70-150 s of processing and produced random-script garbage.
_TEMPERATURE = 0.0

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
        # Cleared while hot_reload() swaps a model: a transcription that starts
        # meanwhile waits for the new model instead of finding an empty slot.
        self._models_ready = threading.Event()
        self._models_ready.set()

        logger.info(
            "WhisperEngine: loading models — realtime='%s', batch='%s' "
            "(compute='%s', threads=%d)",
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
        num_workers=1: ZOLA never runs two transcriptions on one model at once,
        and a second worker cost +164 MB RAM even on the 78 MB Swift model.
        """
        _common_kwargs = dict(
            device="cpu",
            compute_type=self._config.compute_type,
            cpu_threads=self._config.cpu_threads,
            num_workers=1,
        )

        local_dir = MODELS_DIR / size
        if _model_meta(size).get("engine") == "sherpa-transducer":
            logger.info("WhisperEngine: loading sherpa-onnx model '%s' from %s", size, local_dir)
            return _SherpaTransducer(local_dir, self._config.cpu_threads)
        if (local_dir / "model.bin").is_file():
            logger.info("WhisperEngine: loading '%s' from %s", size, local_dir)
            return WhisperModel(str(local_dir), **_common_kwargs)

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
            self._models_ready.clear()
            try:
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
            finally:
                self._models_ready.set()

            logger.info(
                "WhisperEngine: slot='%s' swapped to '%s'", slot, new_model_name,
            )

    # ----------------------------------------------------------------------- #
    # Internal helpers
    # ----------------------------------------------------------------------- #
    def _model_for(self, slot: str) -> WhisperModel:
        """The model in `slot`, waiting out a hot_reload() in progress."""
        if not self._models_ready.wait(timeout=600):
            raise RuntimeError(f"timed out waiting for the {slot} model to load")
        model = getattr(self, f"_{slot}")
        if model is None:
            raise RuntimeError(f"no {slot} model loaded (the last model change failed)")
        return model

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
        # Fine-tuned models with a pinned language need no priming.
        global _priming_warned
        if (mode == "batch" and self._config.spoken_language in ("auto", "mixed")
                and not _pinned_language(self._config.whisper_batch)):
            if self._config.whisper_batch in _HINGLISH_SAFE_MODELS:
                parts.append(HINGLISH_PROMPT)
            elif not _priming_warned:
                _priming_warned = True
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

    def _resolve_language(self, model: WhisperModel, audio: np.ndarray, slot: str) -> str:
        """
        Resolve the Whisper language for this audio.
        A language pinned in the model's zola.json always wins (fine-tunes
        such as Oriserve Hindi2Hinglish need the 'en' token for any speech).
        spoken_language 'en'/'hi' (or the legacy language field) is used as-is.
        For 'auto'/'mixed' the choice is restricted to Hindi or English, so short
        Hinglish utterances are never transcribed as Urdu/Bengali/Punjabi.
        """
        name = self._names[slot]
        pinned = _pinned_language(name)
        if pinned:
            return pinned
        if name.endswith(".en"):
            return "en"
        spoken = self._config.spoken_language
        if isinstance(model, _SherpaTransducer):
            # Nemotron picks the language itself with "auto"; Hinglish is best
            # decoded as Hindi (English words come out in Devanagari).
            return spoken if spoken in ("en", "hi") else ("hi" if spoken == "mixed" else "auto")
        if spoken in ("en", "hi"):
            return spoken
        if self._config.language not in (None, "auto"):
            return self._config.language
        # Detection costs a full encoder pass. When the realtime model is a
        # (smaller) standard multilingual Whisper, detect with it instead.
        rt_name = self._names["realtime"]
        if rt_name in VALID_WHISPER_SIZES and not rt_name.endswith(".en") and self._realtime is not None:
            model = self._realtime
        _, _, all_probs = model.detect_language(
            audio, vad_filter=True, vad_parameters=_VAD_PARAMS,
        )
        probs = dict(all_probs)
        return "hi" if probs.get("hi", 0.0) > probs.get("en", 0.0) else "en"

    # ----------------------------------------------------------------------- #
    # Transcription (synchronous — run in ThreadPoolExecutor)
    # ----------------------------------------------------------------------- #
    def transcribe_audio(self, audio: np.ndarray, mode: str = "batch") -> str:
        """
        Transcribe a whole recording (16 kHz float32) with the batch or realtime model.

        Thread safety: holds its own reference to the model, so a later
        hot_reload() cannot free it mid-call; waits if a reload is under way.
        """
        t0 = time.monotonic()

        try:
            model = self._model_for("realtime" if mode == "realtime" else "batch")

            # One call for the whole recording: Whisper windows it internally,
            # so no word is split at an arbitrary chunk boundary.
            segments, info = model.transcribe(
                audio,
                language=self._resolve_language(model, audio, "realtime" if mode == "realtime" else "batch"),
                task="transcribe",
                beam_size=5,
                best_of=5,
                temperature=_TEMPERATURE,
                vad_filter=True,
                vad_parameters=_VAD_PARAMS,
                no_speech_threshold=0.5,
                initial_prompt=self._build_initial_prompt(mode=mode),
                condition_on_previous_text=False,
            )
            transcript = _join_segments(segments)
            elapsed = time.monotonic() - t0
            audio_duration = len(audio) / 16000
            rtf = elapsed / audio_duration if audio_duration > 0 else 0.0

            logger.info(
                "WhisperEngine.transcribe_audio: %.1fs audio → %.1fs inference "
                "(RTF=%.2f), chars=%d",
                audio_duration, elapsed, rtf, len(transcript),
            )

            del audio
            gc.collect()
            return transcript

        except Exception as exc:
            logger.exception("WhisperEngine.transcribe_audio failed: %s", exc)
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

        Thread safety: same model handling as transcribe_audio.
        """
        if len(audio) < sample_rate * 0.2:
            return ""

        initial_prompt = self._build_initial_prompt(prev_text, mode="realtime")

        try:
            model = self._model_for("realtime")
            segments, info = model.transcribe(
                audio,
                language=self._resolve_language(model, audio, "realtime"),
                task="transcribe",
                beam_size=1,
                best_of=1,
                temperature=_TEMPERATURE,
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

    def translate_audio(self, audio: np.ndarray) -> str:
        """
        Translate non-English audio (e.g. Hindi) directly to English text
        using Whisper's built-in translation task (task="translate").

        This is the backend for /trigger/batch-translate mode.
        No Ollama required — Whisper handles the translation natively.
        Uses the batch model for best accuracy.

        Thread safety: same model handling as transcribe_audio.
        """
        t0 = time.monotonic()

        try:
            model = self._model_for("batch")
            segments, info = model.transcribe(
                audio,
                language=self._resolve_language(model, audio, "batch"),
                task="translate",          # ← Whisper translates to English
                beam_size=5,
                best_of=5,
                temperature=_TEMPERATURE,
                vad_filter=True,
                vad_parameters=_VAD_PARAMS,
                initial_prompt=self._build_initial_prompt(mode="translate"),
                condition_on_previous_text=False,
            )
            transcript = _join_segments(segments)
            elapsed = time.monotonic() - t0
            audio_duration = len(audio) / 16000
            detected_lang = info.language
            logger.info(
                "WhisperEngine.translate_audio: %.1fs audio → %.1fs inference, "
                "chars=%d (source lang detected: %s)",
                audio_duration, elapsed, len(transcript),
                detected_lang,
            )
            return transcript

        except Exception as exc:
            logger.exception("WhisperEngine.translate_audio failed: %s", exc)
            return ""


# --------------------------------------------------------------------------- #
# Private Helpers
# --------------------------------------------------------------------------- #
_priming_warned = False

# A phrase of 1-4 words repeated 4+ times in a row: Whisper's decoding loop
_REPEAT_RE = re.compile(r"(?<!\S)(\S+(?:\s+\S+){0,3}?)(?:\s+\1(?!\S)){3,}")


@functools.lru_cache(maxsize=None)
def _model_meta(model_name: str | None) -> dict:
    """Contents of MODELS_DIR/<name>/zola.json ({} if absent)."""
    if not model_name:
        return {}
    try:
        return json.loads((MODELS_DIR / model_name / "zola.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pinned_language(model_name: str | None) -> str | None:
    """Language token pinned by MODELS_DIR/<name>/zola.json, if any."""
    return _model_meta(model_name).get("language")


class _SherpaTransducer:
    """
    A sherpa-onnx streaming transducer (NVIDIA Nemotron 3.5 ASR) with the
    small part of faster-whisper's WhisperModel interface that WhisperEngine
    uses, so the rest of the pipeline is unchanged. Whisper-only options
    (beam size, prompts, VAD filter, translate) are ignored.
    """

    def __init__(self, model_dir: Path, threads: int) -> None:
        import sherpa_onnx  # optional dependency, only needed for these models

        def pick(pattern: str) -> str:
            return str(sorted(model_dir.glob(pattern))[0])

        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=pick("encoder*.onnx"), decoder=pick("decoder*.onnx"),
            joiner=pick("joiner*.onnx"), tokens=str(model_dir / "tokens.txt"),
            num_threads=threads,
        )

    def transcribe(self, audio: np.ndarray, language: str | None = None, **_whisper_options):
        stream = self._recognizer.create_stream()
        stream.set_option("language", language or "auto")
        stream.accept_waveform(16000, audio)
        stream.accept_waveform(16000, np.zeros(8000, dtype=np.float32))  # flush the last chunk
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        text = self._recognizer.get_result(stream).strip()
        return [SimpleNamespace(text=text)], SimpleNamespace(language=language)


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    """Whole-word, case-insensitive find/replace (the 'replacements' setting)."""
    for wrong, right in replacements.items():
        if wrong.strip():
            text = re.sub(
                rf"(?<![\w\u0900-\u097F]){re.escape(wrong.strip())}(?![\w\u0900-\u097F])",
                lambda _m: right, text, flags=re.IGNORECASE,
            )
    return text


def _join_segments(segments) -> str:
    """Materialise the lazy segment generator, join the text, cut repetition loops."""
    texts = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            texts.append(text)
    return _REPEAT_RE.sub(r"\1 \1", " ".join(texts))
