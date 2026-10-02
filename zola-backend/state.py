"""
state.py — Thread-Safe Global State Machine
============================================
Centralizes all mutable application state behind an asyncio.Lock.
Prevents race conditions from rapid duplicate trigger events.
"""

import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Secure runtime directory for audio temp files
# --------------------------------------------------------------------------- #
def _get_zola_runtime_dir() -> Path:
    """
    Return a per-user private runtime directory for Zola temp files.
    Uses $XDG_RUNTIME_DIR/zola (mode 0700) — not world-readable /tmp.
    This prevents /tmp symlink attacks where another user pre-creates
    /tmp/zola.wav pointing to a sensitive file.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and Path(xdg).is_dir():
        base = Path(xdg) / "zola"
    else:
        # Fallback: user-specific subdir inside /tmp
        uid = os.getuid()
        base = Path(f"/tmp/zola-{uid}")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base


def make_audio_tmp_path() -> Path:
    """
    Generate a unique, secure temp file path for WAV audio.
    Call once per session — delete the file after transcription finishes.
    """
    runtime_dir = _get_zola_runtime_dir()
    fd, path = tempfile.mkstemp(prefix="audio_", suffix=".wav", dir=runtime_dir)
    os.close(fd)
    return Path(path)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class RecordingMode(str, Enum):
    REALTIME = "realtime"
    REALTIME_LLM = "realtime-llm"
    BATCH = "batch"
    BATCH_LLM = "batch-llm"
    BATCH_TRANSLATE = "batch-translate"   # Records Hindi/Hinglish → injects English


# --------------------------------------------------------------------------- #
# State Machine
# --------------------------------------------------------------------------- #
class AppState:
    """
    Thread-safe state machine for the zola daemon.

    All public methods are async and acquire the internal lock before
    mutating state, ensuring atomic transitions even under concurrent
    HTTP trigger requests.
    """

    __slots__ = (
        '_lock', '_history_max', '_start_time',
        'is_recording', 'active_mode', 'current_audio_path',
        'realtime_task', 'history',
    )

    def __init__(self, history_max: int = 50) -> None:
        self._lock = asyncio.Lock()
        self._history_max = history_max

        self.is_recording: bool = False
        self.active_mode: RecordingMode | None = None
        # Per-session temp path — set in start_recording(), cleaned up after transcription
        self.current_audio_path: str = ""

        self.realtime_task: asyncio.Task | None = None
        self.history: list[dict[str, Any]] = []
        self._start_time: float = time.monotonic()

        logger.debug("AppState initialised")

    # ----------------------------------------------------------------------- #
    # Recording Lifecycle
    # ----------------------------------------------------------------------- #
    async def start_recording(self, mode: RecordingMode) -> Path:
        """
        Transition to recording state.
        Generates a unique temp audio path for this session.
        Returns the audio path so the caller can pass it to the recorder.
        Raises RuntimeError if already recording (caller should return 409).
        """
        async with self._lock:
            if self.is_recording:
                raise RuntimeError(
                    f"Already recording in mode '{self.active_mode}'. "
                    f"Stop the current session before starting a new one."
                )
            audio_path = make_audio_tmp_path()
            self.is_recording = True
            self.active_mode = mode
            self.current_audio_path = str(audio_path)
            logger.info("Recording STARTED — mode: %s, path: %s", mode.value, audio_path)
            return audio_path

    async def stop_recording(self) -> tuple[RecordingMode | None, str]:
        """
        Transition out of recording state.
        Returns (mode, audio_path) so the caller knows what to transcribe.
        Raises RuntimeError if not currently recording.
        """
        async with self._lock:
            if not self.is_recording:
                raise RuntimeError("No active recording session to stop.")
            mode = self.active_mode
            path = self.current_audio_path
            self.is_recording = False
            self.active_mode = None
            self.realtime_task = None
            logger.info("Recording STOPPED — mode: %s, audio: %s", mode, path)
            return mode, path

    async def set_realtime_task(self, task: asyncio.Task) -> None:
        """Store a reference to the active realtime streaming background task."""
        async with self._lock:
            self.realtime_task = task

    async def cancel_realtime_task(self) -> None:
        """Cancel the realtime task if one is running, then clear the reference."""
        async with self._lock:
            if self.realtime_task and not self.realtime_task.done():
                self.realtime_task.cancel()
                logger.debug("Realtime background task cancelled")
            self.realtime_task = None

    # ----------------------------------------------------------------------- #
    # History
    # ----------------------------------------------------------------------- #
    async def add_history(
        self,
        mode: RecordingMode,
        transcript: str,
        duration_s: float,
    ) -> None:
        """Append a completed transcription to history, capped at history_max."""
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": mode.value if mode else "unknown",
            "transcript": transcript,
            "duration_s": round(duration_s, 2),
        }
        async with self._lock:
            self.history.append(entry)
            if len(self.history) > self._history_max:
                self.history = self.history[-self._history_max:]
            logger.debug(
                "History entry added (total: %d): %d chars",
                len(self.history),
                len(transcript),
            )

    # ----------------------------------------------------------------------- #
    # Status Snapshot
    # ----------------------------------------------------------------------- #
    def get_status(self) -> dict[str, Any]:
        """Return a non-locking snapshot of current state for GET /status."""
        return {
            "is_recording": self.is_recording,
            "active_mode": self.active_mode.value if self.active_mode else None,
            "current_audio_path": self.current_audio_path,
            "uptime_s": round(time.monotonic() - self._start_time, 1),
        }

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent `limit` history entries (newest last)."""
        return self.history[-limit:]
