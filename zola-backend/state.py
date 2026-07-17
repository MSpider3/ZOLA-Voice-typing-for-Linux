"""
state.py — Thread-Safe Global State Machine
============================================
Centralizes all mutable application state behind an asyncio.Lock.
Prevents race conditions from rapid duplicate trigger events.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class RecordingMode(str, Enum):
    REALTIME = "realtime"
    REALTIME_LLM = "realtime-llm"
    BATCH = "batch"
    BATCH_LLM = "batch-llm"


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

    # __slots__ prevents dynamic dict allocation, locking down the memory
    # footprint of every AppState instance and preventing accidental attr creation.
    __slots__ = (
        '_lock', '_history_max', '_start_time',
        'is_recording', 'active_mode', 'current_audio_path',
        'realtime_task', 'history',
    )

    def __init__(self, history_max: int = 50) -> None:
        self._lock = asyncio.Lock()
        self._history_max = history_max

        # Core recording state
        self.is_recording: bool = False
        self.active_mode: Optional[RecordingMode] = None
        self.current_audio_path: str = "/tmp/zola.wav"

        # Background asyncio.Task handle for realtime streaming loop
        # Stored so it can be cancelled on stop-trigger
        self.realtime_task: Optional[asyncio.Task] = None

        # History of completed transcriptions
        self.history: list[dict[str, Any]] = []

        # Daemon start time (for /status uptime reporting)
        self._start_time: float = time.monotonic()

        logger.debug("AppState initialised")

    # ----------------------------------------------------------------------- #
    # Recording Lifecycle
    # ----------------------------------------------------------------------- #
    async def start_recording(self, mode: RecordingMode) -> None:
        """
        Transition to recording state.
        Raises RuntimeError if already recording (caller should return 409).
        """
        async with self._lock:
            if self.is_recording:
                raise RuntimeError(
                    f"Already recording in mode '{self.active_mode}'. "
                    f"Stop the current session before starting a new one."
                )
            self.is_recording = True
            self.active_mode = mode
            logger.info("Recording STARTED — mode: %s", mode.value)

    async def stop_recording(self) -> tuple[Optional[RecordingMode], str]:
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
            # Trim to max — remove oldest entries
            if len(self.history) > self._history_max:
                self.history = self.history[-self._history_max :]
            logger.debug(
                "History entry added (total: %d): %.60s…",
                len(self.history),
                transcript,
            )

    # ----------------------------------------------------------------------- #
    # Status Snapshot (no lock needed — reads are atomic in CPython)
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
