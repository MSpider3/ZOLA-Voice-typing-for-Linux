"""
core_stt.py — faster-whisper Engine with Hot-Reload
=====================================================
Loads and manages WhisperModel instances backed by CTranslate2.
CTranslate2 uses Intel MKL / oneDNN for CPU inference — this is the
correct hardware acceleration path for Intel CPUs (not OpenVINO, which
is a separate stack incompatible with faster-whisper's CTranslate2 backend).

Two models are kept resident in RAM simultaneously:
  - 'realtime' model (default: base, int8)  → ~150MB RAM, ~3-5x realtime
  - 'batch'    model (default: small, int8) → ~500MB RAM, ~1-2x realtime

Hot-reload swaps one model at a time under an asyncio.Lock:
  del old → gc.collect() → load new
"""

import asyncio
import gc
import logging
import os
import time
from pathlib import Path
from typing import Optional, Union

import numpy as np
from faster_whisper import WhisperModel

from config import Config

logger = logging.getLogger(__name__)
_ZOLA_DEBUG = os.environ.get("ZOLA_DEBUG") == "1"


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
            "WhisperEngine: loading models — realtime='%s', batch='%s' (compute='%s', threads=%d)",
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
            t1 - t0,
            t2 - t1,
        )

    # ----------------------------------------------------------------------- #
    # Model Lifecycle
    # ----------------------------------------------------------------------- #
    def _load_model(self, size: str) -> WhisperModel:
        """
        Instantiate a WhisperModel with offline-first loading.

        Strategy:
          1. Try loading from local HuggingFace cache (local_files_only=True).
             → Zero network calls. Instant if model was previously downloaded.
          2. If the model is not cached yet, fall back to downloading it.
             → Only happens once per model size, ever.

        This eliminates the HEAD request spam to huggingface.co on every startup
        that you see when HF Hub validates cache freshness over the network.
        """
        _common_kwargs = dict(
            device="cpu",
            compute_type=self._config.compute_type,
            cpu_threads=self._config.cpu_threads,
            num_workers=1,
        )

        # --- Attempt 1: Load from local cache (no network) ---
        try:
            model = WhisperModel(size, local_files_only=True, **_common_kwargs)
            logger.info("WhisperEngine: loaded '%s' from local cache (offline)", size)
            return model
        except Exception:
            # Cache miss — model not downloaded yet
            pass

        # --- Attempt 2: Download from HuggingFace (first-time only) ---
        logger.info(
            "WhisperEngine: model '%s' not in local cache — downloading from HuggingFace "
            "(this only happens once)",
            size,
        )
        model = WhisperModel(size, **_common_kwargs)
        logger.info("WhisperEngine: model '%s' downloaded and cached", size)
        return model

    async def hot_reload(self, slot: str, new_model_name: str) -> None:
        """
        Safely swap the model in `slot` ('realtime' or 'batch') for a new one.
        Blocks transcription during the swap via asyncio.Lock.
        Frees old model memory with del + gc.collect().

        Must be called from the async event loop (e.g., triggered by POST /settings).
        The actual model loading runs in the default ThreadPoolExecutor so the
        event loop is not blocked during the potentially 3-5s load time.
        """
        if slot not in ("realtime", "batch"):
            raise ValueError(f"Invalid slot '{slot}'. Must be 'realtime' or 'batch'.")

        logger.info("WhisperEngine: hot-reload slot='%s' → '%s'", slot, new_model_name)
        loop = asyncio.get_event_loop()

        async with self._lock:
            # Load new model in thread pool (CPU-bound, blocks for several seconds)
            new_model: WhisperModel = await loop.run_in_executor(
                None, self._load_model, new_model_name
            )

            # Swap and free old model
            attr = f"_{slot}"
            old_model = getattr(self, attr)
            setattr(self, attr, new_model)
            del old_model
            gc.collect()

            logger.info(
                "WhisperEngine: slot='%s' swapped to '%s', old model freed", slot, new_model_name
            )

    # ----------------------------------------------------------------------- #
    # Transcription (synchronous — run in ThreadPoolExecutor)
    # ----------------------------------------------------------------------- #
    def transcribe_file(self, audio_path: Union[str, Path], mode: str = "batch") -> str:
        """
        Transcribe a WAV file using the batch or realtime model.
        Processes the audio in chunks of `batch_chunk_s` duration.
        """
        model = self._realtime if mode == "realtime" else self._batch
        model_name = self._config.whisper_realtime if mode == "realtime" else self._config.whisper_batch
        chunk_s = self._config.realtime_chunk_s if mode == "realtime" else self._config.batch_chunk_s

        logger.debug(
            "WhisperEngine.transcribe_file: model='%s', chunk_s=%.1fs, path=%s",
            model_name,
            chunk_s,
            audio_path,
        )
        t0 = time.monotonic()

        try:
            sample_rate, data = wavfile.read(str(audio_path))
            # Convert 16-bit PCM integer to float32 normal [-1.0, 1.0]
            audio = data.astype(np.float32) / 32768.0

            chunk_size = int(sample_rate * chunk_s)
            transcripts = []
            prev_text = ""

            for i in range(0, len(audio), chunk_size):
                chunk = audio[i : i + chunk_size]
                if len(chunk) < int(sample_rate * 0.3):
                    continue

                # Skip quiet chunks to prevent hallucinations
                rms = float(np.sqrt(np.mean(chunk ** 2)))
                if rms < self._config.silence_rms_threshold:
                    logger.debug("WhisperEngine.transcribe_file: skipping quiet chunk (RMS=%.4f < %.4f)",
                                 rms, self._config.silence_rms_threshold)
                    continue

                segments, info = model.transcribe(
                    chunk,
                    language=self._config.language,
                    task="transcribe",
                    beam_size=5,
                    best_of=5,
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": 500,
                        "speech_pad_ms": 200,
                        "threshold": 0.5,
                    },
                    no_speech_threshold=0.5,  # Reject quiet/noisy segments
                    initial_prompt=prev_text if prev_text else None,
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
                "WhisperEngine.transcribe_file: %.1fs audio → %.1fs inference (RTF=%.2f), chars=%d",
                audio_duration,
                elapsed,
                rtf,
                len(transcript),
            )

            # Explicitly free large objects to prevent RAM spikes between sessions
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

        Args:
            audio:       float32 numpy array, mono
            sample_rate: audio sample rate (must match model — 16000Hz)
            prev_text:   previous transcript for context continuity (initial_prompt)

        Returns:
            Transcribed text string for this chunk.
        """
        if len(audio) < sample_rate * 0.2:  # skip chunks shorter than 200ms
            return ""

        # Skip quiet chunks to prevent hallucinations
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < self._config.silence_rms_threshold:
            logger.debug("WhisperEngine.transcribe_chunk: skipping quiet chunk (RMS=%.4f < %.4f)",
                         rms, self._config.silence_rms_threshold)
            return ""

        try:
            segments, info = self._realtime.transcribe(
                audio,
                language=self._config.language,
                task="transcribe",
                beam_size=1,                          # Fastest — single beam for realtime
                best_of=1,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 300,   # More aggressive for realtime
                    "threshold": 0.5,                 # Less sensitive to background noise
                },
                no_speech_threshold=0.5,              # Reject quiet/noisy segments
                initial_prompt=prev_text if prev_text else None,
                condition_on_previous_text=bool(prev_text),
            )

            transcript = _join_segments(segments)
            if transcript:
                logger.debug(
                    "WhisperEngine.transcribe_chunk: lang=%s text='%.60s'",
                    info.language,
                    transcript,
                )

            # Explicitly free the audio buffer — numpy arrays from the recorder
            # can hold significant memory if Whisper processes them slowly
            del audio
            gc.collect()

            return transcript

        except Exception as exc:
            logger.exception("WhisperEngine.transcribe_chunk failed: %s", exc)
            return ""

    def translate_file(self, audio_path: Union[str, Path]) -> str:
        """
        Translate audio to English using the batch model.
        Processes in segments of `batch_chunk_s` duration.
        """
        logger.debug("WhisperEngine.translate_file: path=%s", audio_path)
        t0 = time.monotonic()

        try:
            sample_rate, data = wavfile.read(str(audio_path))
            audio = data.astype(np.float32) / 32768.0

            chunk_size = int(sample_rate * self._config.batch_chunk_s)
            transcripts = []
            prev_text = ""

            for i in range(0, len(audio), chunk_size):
                chunk = audio[i : i + chunk_size]
                if len(chunk) < int(sample_rate * 0.3):
                    continue

                segments, info = self._batch.transcribe(
                    chunk,
                    language=self._config.language,
                    task="translate",
                    beam_size=5,
                    best_of=5,
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": 500,
                        "speech_pad_ms": 200,
                        "threshold": 0.5,
                    },
                    initial_prompt=prev_text if prev_text else None,
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
                "WhisperEngine.translate_file: %.1fs audio → %.1fs inference, chars=%d",
                audio_duration,
                elapsed,
                len(transcript),
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
