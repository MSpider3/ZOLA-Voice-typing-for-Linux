"""
recorder.py — PipeWire Audio Capturer & Buffer Manager
=======================================================
Captures audio via sounddevice (PortAudio → PipeWire PulseAudio compat layer).

Architecture (VAD-segmented commit model — research-validated):
  Rather than sliding-window re-transcription (which causes constant rewriting),
  we use utterance-boundary detection:
    • Speech frames are accumulated in an utterance buffer
    • When silence >= utterance_silence_s, the utterance is committed (yielded)
    • Whisper transcribes the complete utterance → types only NEW text
    • No backspacing ever needed in the normal flow
    • force_commit_s prevents unbounded buffer growth during long speech

  This is the "VAD + Segment Commitment" architecture recommended by the
  faster-whisper streaming research community for reliable on-device STT.

Mic lifecycle:
  Stream is created in start() and destroyed in stop() so the mic indicator
  only shows as active during an actual recording session.
"""

import asyncio
import gc
import logging
import os
import queue
import threading
import time
import tracemalloc
from pathlib import Path
from typing import AsyncGenerator, Optional

import numpy as np
import sounddevice as sd
from scipy.io import wavfile

logger = logging.getLogger(__name__)

AUDIO_TMP_PATH = Path("/tmp/zola.wav")
_ZOLA_DEBUG = os.environ.get("ZOLA_DEBUG") == "1"


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self.channels = 1
        self.dtype = "float32"

        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._buffer: list[np.ndarray] = []       # full session buffer (for final WAV)
        self._buffer_lock = threading.Lock()

        self._is_recording = False
        self._session_start: float = 0.0
        self._stream: Optional[sd.InputStream] = None

        logger.info("AudioRecorder: ready (mic opens only during recording, rate=%dHz)", sample_rate)

    # ----------------------------------------------------------------------- #
    # PortAudio callback — runs in C thread, must never block
    # ----------------------------------------------------------------------- #
    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            logger.warning("AudioRecorder callback: %s", status)
        if self._is_recording:
            self._queue.put_nowait(indata.copy())

    # ----------------------------------------------------------------------- #
    # Session control
    # ----------------------------------------------------------------------- #
    def start(self) -> None:
        """Open mic and begin recording. Creates a fresh PortAudio stream."""
        if self._is_recording:
            logger.warning("AudioRecorder.start(): already recording — ignored")
            return

        # Drain stale queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        with self._buffer_lock:
            self._buffer.clear()

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype=self.dtype,
                blocksize=int(self.sample_rate * 0.1),  # 100ms blocks
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            logger.exception("AudioRecorder: failed to open mic: %s", exc)
            raise

        self._is_recording = True
        self._session_start = time.monotonic()
        logger.info("AudioRecorder: mic opened — session started")

        if _ZOLA_DEBUG:
            tracemalloc.start()
            self._mem_snapshot_start = tracemalloc.take_snapshot()
            logger.debug("[ZOLA_DEBUG] recorder: tracemalloc snapshot taken at session start")

    def request_stop(self) -> None:
        """Signal running generators to stop. Does NOT close mic; call stop() for that."""
        self._is_recording = False
        logger.debug("AudioRecorder: stop requested")

    def stop(self) -> tuple[Path, float]:
        """Stop and close mic stream; flush full session to WAV. Returns (path, duration_s)."""
        self._is_recording = False
        duration_s = max(time.monotonic() - self._session_start, 0.0)

        if self._stream is not None:
            try:
                if self._stream.active:
                    self._stream.stop()
                self._stream.close()
                logger.info("AudioRecorder: mic closed (%.1fs session)", duration_s)
            except Exception as exc:
                logger.error("AudioRecorder: stream close error: %s", exc)
            finally:
                self._stream = None

        # Short drain window for in-flight callback frames
        _end = time.monotonic() + 0.2
        while time.monotonic() < _end:
            try:
                chunk = self._queue.get_nowait()
                with self._buffer_lock:
                    self._buffer.append(chunk)
            except queue.Empty:
                break

        with self._buffer_lock:
            if not self._buffer:
                logger.warning("AudioRecorder.stop(): no audio — writing silence placeholder")
                _write_wav(AUDIO_TMP_PATH, self.sample_rate,
                           np.zeros(self.sample_rate, dtype=np.float32))
                return AUDIO_TMP_PATH, duration_s
            audio = np.concatenate(self._buffer, axis=0).flatten()

        _write_wav(AUDIO_TMP_PATH, self.sample_rate, audio)
        logger.info("AudioRecorder: %.1fs → %d samples → %s", duration_s, len(audio), AUDIO_TMP_PATH)

        if _ZOLA_DEBUG and hasattr(self, "_mem_snapshot_start"):
            snapshot_end = tracemalloc.take_snapshot()
            diff = snapshot_end.compare_to(self._mem_snapshot_start, "lineno")
            logger.debug("[ZOLA_DEBUG] recorder: memory diff top-5 after stop():")
            for stat in diff[:5]:
                logger.debug("  %s", stat)
            tracemalloc.stop()

        return AUDIO_TMP_PATH, duration_s

    def close(self) -> None:
        """Force-close on daemon shutdown."""
        self._is_recording = False
        if self._stream is not None:
            try:
                if self._stream.active:
                    self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.error("AudioRecorder.close(): %s", exc)
            finally:
                self._stream = None

    # ----------------------------------------------------------------------- #
    # Realtime: VAD-segmented utterance generator
    # ----------------------------------------------------------------------- #
    async def stream_utterances(
        self,
        utterance_silence_s: float = 0.8,
        force_commit_s: float = 10.0,
        silence_rms_threshold: float = 0.02,
        chunk_s: float = 2.0,
        realtime_silence_timeout_s: float = 3.0,
    ) -> AsyncGenerator[np.ndarray, None]:
        """
        Async generator that yields complete utterance audio arrays.

        Architecture (VAD-segmented commit):
          • Accumulate incoming audio frames into an utterance buffer
          • When RMS drops below threshold for utterance_silence_s → commit utterance
          • Also force-commit after force_commit_s even without silence
            (prevents unbounded buffer during long unbroken speech)
          • If utterance_silence_s is 0.0, VAD is disabled; we commit strictly
            on fixed chunk_s (e.g. 2.0s) intervals.
          • If silence duration reaches realtime_silence_timeout_s, recording is
            automatically stopped and the session is finalized.

        Each yielded array is a complete utterance — caller sends to Whisper and
        types the result. No backspacing needed because text is only appended.

        The full session audio is also accumulated in self._buffer for the final WAV.
        """
        utterance_buf: list[np.ndarray] = []
        utterance_samples = 0
        silence_since: Optional[float] = None
        utterance_start = time.monotonic()

        vad_enabled = utterance_silence_s > 0.0
        limit_s = force_commit_s if vad_enabled else chunk_s
        force_samples = int(self.sample_rate * limit_s) if limit_s > 0 else None

        logger.info(
            "AudioRecorder: utterance stream started (vad=%.1fs, limit_s=%.1fs, timeout=%.1fs, rms=%.3f)",
            utterance_silence_s if vad_enabled else 0.0,
            limit_s,
            realtime_silence_timeout_s,
            silence_rms_threshold,
        )

        try:
            while self._is_recording:
                await asyncio.sleep(0.05)  # 50ms poll — tight enough for 0.8s VAD

                # Drain queue into utterance buffer + session buffer
                frames_this_tick: list[np.ndarray] = []
                while True:
                    try:
                        frame = self._queue.get_nowait()
                        frames_this_tick.append(frame)
                        utterance_buf.append(frame.copy())   # own copy for utterance
                        utterance_samples += len(frame)
                        with self._buffer_lock:
                            self._buffer.append(frame)       # zero-copy: frame already owned by queue
                    except queue.Empty:
                        break

                if not frames_this_tick:
                    continue

                # RMS energy using a rolling 0.5-second window for robustness
                with self._buffer_lock:
                    recent_frames = self._buffer[-5:] if len(self._buffer) >= 5 else self._buffer
                    if recent_frames:
                        recent_audio = np.concatenate(recent_frames).flatten()
                        rms = float(np.sqrt(np.mean(recent_audio ** 2)))
                    else:
                        rms = 0.0

                # --- Silence monitoring (used for VAD and auto-stop) ---
                if rms < silence_rms_threshold:
                    if silence_since is None:
                        silence_since = time.monotonic()
                else:
                    silence_since = None

                # Check for entire session auto-stop on long silence
                if (silence_since is not None
                        and realtime_silence_timeout_s > 0.0
                        and (time.monotonic() - silence_since) >= realtime_silence_timeout_s):
                    logger.info("AudioRecorder: realtime auto-stop — %.1fs silence", realtime_silence_timeout_s)
                    self._is_recording = False
                    break

                # --- VAD: silence detection (utterance commit) ---
                if vad_enabled:
                    if (silence_since is not None
                            and (time.monotonic() - silence_since) >= utterance_silence_s
                            and utterance_samples >= int(self.sample_rate * 0.3)):
                        # Utterance boundary detected — commit
                        utterance = np.concatenate(utterance_buf, axis=0).flatten()
                        logger.debug(
                            "Utterance committed via VAD (%.2fs speech, %.2fs silence)",
                            (time.monotonic() - utterance_start),
                            time.monotonic() - silence_since,
                        )
                        utterance_buf.clear()
                        utterance_samples = 0
                        # Keep silence_since intact to track total continuous silence for auto-stop
                        utterance_start = time.monotonic()
                        yield utterance
                        continue

                # --- Force-commit: max utterance duration ---
                if force_samples and utterance_samples >= force_samples:
                    utterance = np.concatenate(utterance_buf, axis=0).flatten()
                    logger.debug("Utterance committed via force-commit (%.1fs)", force_commit_s)
                    utterance_buf.clear()
                    utterance_samples = 0
                    silence_since = None
                    utterance_start = time.monotonic()
                    yield utterance

        except asyncio.CancelledError:
            logger.debug("AudioRecorder: stream_utterances cancelled")
            self._is_recording = False

        # Yield any remaining partial utterance
        if utterance_buf and utterance_samples >= int(self.sample_rate * 0.3):
            yield np.concatenate(utterance_buf, axis=0).flatten()

        logger.debug("AudioRecorder: stream_utterances finished")

    # ----------------------------------------------------------------------- #
    # Batch: silence-triggered auto-stop monitor
    # ----------------------------------------------------------------------- #
    async def monitor_until_silence(
        self,
        silence_timeout_s: float = 3.0,
        silence_rms_threshold: float = 0.02,
        min_record_s: float = 0.5,
    ) -> None:
        """
        For batch mode: monitors audio and sets _is_recording=False when
        silence_timeout_s of continuous silence is detected.

        silence_timeout_s = 0 → disabled (runs until request_stop() is called).
        Caller must call stop() afterward to flush WAV.
        """
        if silence_timeout_s <= 0:
            logger.info("AudioRecorder: silence monitor disabled (manual stop only)")
            # Just drain queue into buffer while recording
            while self._is_recording:
                await asyncio.sleep(0.1)
                while True:
                    try:
                        chunk = self._queue.get_nowait()
                        with self._buffer_lock:
                            self._buffer.append(chunk.copy())
                    except queue.Empty:
                        break
            return

        silence_since: Optional[float] = None
        session_start = time.monotonic()

        logger.info(
            "AudioRecorder: batch silence monitor armed (timeout=%.1fs, rms=%.3f)",
            silence_timeout_s, silence_rms_threshold,
        )

        try:
            while self._is_recording:
                await asyncio.sleep(0.1)

                chunks: list[np.ndarray] = []
                while True:
                    try:
                        chunk = self._queue.get_nowait()
                        with self._buffer_lock:
                            self._buffer.append(chunk.copy())
                        chunks.append(chunk)
                    except queue.Empty:
                        break

                if not chunks:
                    continue

                if (time.monotonic() - session_start) < min_record_s:
                    continue

                # RMS energy using a rolling 0.5-second window for robustness
                with self._buffer_lock:
                    recent_frames = self._buffer[-5:] if len(self._buffer) >= 5 else self._buffer
                    if recent_frames:
                        recent_audio = np.concatenate(recent_frames).flatten()
                        rms = float(np.sqrt(np.mean(recent_audio ** 2)))
                    else:
                        rms = 0.0

                if rms < silence_rms_threshold:
                    if silence_since is None:
                        silence_since = time.monotonic()
                    elif (time.monotonic() - silence_since) >= silence_timeout_s:
                        logger.info("AudioRecorder: auto-stop — %.1fs silence", silence_timeout_s)
                        self._is_recording = False
                        break
                else:
                    silence_since = None

        except asyncio.CancelledError:
            self._is_recording = False

        logger.debug("AudioRecorder: monitor_until_silence exited")


# --------------------------------------------------------------------------- #
# Private helper
# --------------------------------------------------------------------------- #
def _write_wav(path: Path, sample_rate: int, audio: np.ndarray) -> None:
    try:
        pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        wavfile.write(str(path), sample_rate, pcm16)
        logger.debug("WAV: %s (%d samples @ %dHz)", path, len(pcm16), sample_rate)
    except Exception as exc:
        logger.exception("WAV write failed: %s", exc)
        raise
