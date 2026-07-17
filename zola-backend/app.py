"""
app.py — FastAPI Application Entry Point & Task Orchestrator
=============================================================
Runs on 127.0.0.1:5001. Exposes trigger endpoints for the Tauri UI frontend
and orchestrates the full pipeline:

  Trigger → Recorder → STT (ThreadPoolExecutor) → [LLM] → Injector

Uses the modern FastAPI lifespan context manager (replaces deprecated
@app.on_event). All models are pre-loaded at startup and kept in RAM.

Start the server:
  uvicorn app:app --host 127.0.0.1 --port 5001 --log-level info

For development with auto-reload:
  uvicorn app:app --host 127.0.0.1 --port 5001 --reload
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from config import Config, load_config, update_config
from core_llm import OllamaClient
from core_stt import WhisperEngine
from injector import KeystrokeInjector
from recorder import AudioRecorder
from state import AppState, RecordingMode

# --------------------------------------------------------------------------- #
# Logging Setup
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zola")

# Available Whisper model sizes (static — faster-whisper's supported set)
WHISPER_MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]


# --------------------------------------------------------------------------- #
# Lifespan — Startup / Shutdown
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Everything before `yield` runs at startup; everything after at shutdown.
    Resources are stored on app.state for access inside route handlers.
    """
    logger.info("=" * 60)
    logger.info("Zola daemon starting up…")
    logger.info("=" * 60)

    # --- Load Config ---
    cfg: Config = load_config()
    app.state.config = cfg

    # --- Thread Pool (CPU-bound: Whisper inference + keystroke injection) ---
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zola-worker")
    app.state.executor = executor
    loop = asyncio.get_event_loop()
    loop.set_default_executor(executor)

    # --- State Machine ---
    app.state.app_state = AppState(history_max=cfg.history_max)

    # --- Audio Recorder (PipeWire via PortAudio) ---
    try:
        recorder = AudioRecorder(sample_rate=cfg.sample_rate)
        app.state.recorder = recorder
    except Exception as exc:
        logger.critical("Failed to initialise AudioRecorder: %s", exc)
        raise

    # --- Whisper STT Engine (loads both models into RAM) ---
    try:
        stt = WhisperEngine(cfg)
        app.state.stt = stt
    except Exception as exc:
        logger.critical("Failed to initialise WhisperEngine: %s", exc)
        raise

    # --- Ollama LLM Client ---
    llm = OllamaClient(cfg)
    app.state.llm = llm

    ollama_ok = await llm.health_check()
    if ollama_ok:
        logger.info("Ollama is reachable at %s (model: %s)", cfg.ollama_url, cfg.ollama_model)
    else:
        logger.warning(
            "Ollama is NOT reachable at %s — /trigger/batch-llm will return raw transcripts. "
            "Start Ollama with: ollama serve",
            cfg.ollama_url,
        )

    # --- Keystroke Injector (evdev UInput) ---
    try:
        injector = KeystrokeInjector()
        app.state.injector = injector
    except Exception as exc:
        logger.critical(
            "Failed to create UInput device: %s\n"
            "See injector.py header for one-time permission setup instructions.",
            exc,
        )
        raise

    logger.info("=" * 60)
    logger.info("Zola daemon ready — listening on http://127.0.0.1:5001")
    logger.info("=" * 60)

    try:
        yield  # ← Application runs here
    finally:
        # --- Shutdown ---
        logger.info("Zola daemon shutting down…")

        # Cancel any active realtime task
        if hasattr(app.state, "app_state"):
            try:
                await app.state.app_state.cancel_realtime_task()
            except Exception as exc:
                logger.error("Error cancelling realtime task on shutdown: %s", exc)

        # Close mic stream if still open
        if hasattr(app.state, "recorder"):
            try:
                app.state.recorder.close()
            except Exception as exc:
                logger.error("Error closing recorder on shutdown: %s", exc)

        # Close LLM client
        if hasattr(app.state, "llm"):
            try:
                await app.state.llm.close()
            except Exception as exc:
                logger.error("Error closing LLM client on shutdown: %s", exc)

        # Release virtual keyboard
        if hasattr(app.state, "injector"):
            try:
                app.state.injector.close()
            except Exception as exc:
                logger.error("Error closing injector on shutdown: %s", exc)

        # Shut down thread pool
        try:
            executor.shutdown(wait=False)
        except Exception as exc:
            logger.error("Error shutting down executor: %s", exc)

        logger.info("Zola daemon stopped cleanly.")


# --------------------------------------------------------------------------- #
# FastAPI App Instance
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="Zola Backend",
    description="Linux-native Speech-to-Text daemon for Fedora/Niri Wayland",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow Tauri UI to call the API (allows all origins, methods, and headers for local Tauri app access)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# Debug Endpoints (only registered when ZOLA_DEBUG=1)                          #
# Routes that do not exist here simply 404 in production — zero API surface.   #
# --------------------------------------------------------------------------- #
import os as _os
if _os.environ.get("ZOLA_DEBUG") == "1":
    @app.post(
        "/debug/crash-injector",
        summary="[DEBUG] Crash mid-injection to verify release_all() failsafe",
        tags=["debug"],
    )
    async def debug_crash_injector(request: Request) -> JSONResponse:
        """
        Intentionally throws a RuntimeError in the middle of a type_text loop
        to verify that release_all() prevents KEY_LEFTSHIFT or other modifier
        keys from remaining stuck at the evdev level after an exception.

        Only registered when ZOLA_DEBUG=1 is set. Returns HTTP 404 in production
        because the route does not exist in the router at all.
        """
        injector: KeystrokeInjector = request.app.state.injector
        logger.warning("[DEBUG] crash-injector triggered — this will throw intentionally")
        try:
            # Type partial text then raise before finishing
            injector.type_text("ZOLA_CRASH_TEST_", 12)
            raise RuntimeError(
                "INTENTIONAL CRASH — verifying release_all() in finally block. "
                "If you see this, the exception path executed correctly."
            )
        except RuntimeError:
            injector.release_all()  # Must prevent stuck modifier keys
            logger.warning("[DEBUG] crash-injector: release_all() called successfully")
            raise HTTPException(
                status_code=500,
                detail="INTENTIONAL_CRASH_OK — release_all() executed. Check evdev state.",
            )


# --------------------------------------------------------------------------- #
# Helper — Convenience Accessor
# --------------------------------------------------------------------------- #
def _state(request: Request) -> AppState:
    return request.app.state.app_state

def _cfg(request: Request) -> Config:
    return request.app.state.config


# --------------------------------------------------------------------------- #
# SSE Clients Registry & Event Broadcast Helper
# --------------------------------------------------------------------------- #
_sse_clients: set[asyncio.Queue] = set()

def broadcast_event(event_type: str, data: dict) -> None:
    """Broadcast an event to all connected SSE clients."""
    if not _sse_clients:
        return
    logger.debug("Broadcasting SSE event '%s' to %d client(s)", event_type, len(_sse_clients))
    for queue in list(_sse_clients):
        try:
            queue.put_nowait({"type": event_type, "data": data})
        except Exception as exc:
            logger.error("Failed to push event to SSE client queue: %s", exc)


# --------------------------------------------------------------------------- #
# Trigger Routes
# --------------------------------------------------------------------------- #
@app.post("/trigger/realtime", summary="Toggle real-time continuous transcription")
async def trigger_realtime(request: Request) -> JSONResponse:
    """
    Toggle real-time transcription mode.

    First call:  Starts recording + VAD utterance stream. Each time you
                 pause speaking (default 0.8s), the utterance is transcribed
                 and appended — no rewriting, no backspacing.
                 If realtime_utterance_silence_s = 0, commits on fixed 2s chunks.
    Second call: Signals stop. Background task finishes current utterance,
                 commits full session to clipboard, then stops mic.
    """
    app_state = _state(request)

    # --- STOP (second trigger) ---
    if app_state.is_recording and app_state.active_mode == RecordingMode.REALTIME:
        request.app.state.recorder.request_stop()
        logger.info("/trigger/realtime: stop requested — finishing current utterance")
        return JSONResponse({"status": "stopping", "mode": "realtime"})

    # --- CONFLICT ---
    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'. Stop that session first.",
        )

    # --- START ---
    await app_state.start_recording(RecordingMode.REALTIME)
    request.app.state.recorder.start()
    broadcast_event("state_change", app_state.get_status())

    task = asyncio.create_task(
        _realtime_loop(request),
        name="realtime-stt-loop",
    )
    await app_state.set_realtime_task(task)
    return JSONResponse({"status": "started", "mode": "realtime"})


@app.post("/trigger/realtime-llm", summary="Toggle real-time continuous transcription + LLM refinement")
async def trigger_realtime_llm(request: Request) -> JSONResponse:
    """
    Toggle real-time transcription mode with LLM refinement.

    First call:  Starts recording + VAD utterance stream. Each time you
                 pause speaking, the utterance is transcribed and typed live.
    Second call: Signals stop. Background task finishes current utterance,
                 refines full transcript via Ollama, copies to clipboard,
                 sends a desktop notification, then stops mic.
    """
    app_state = _state(request)

    # --- STOP (second trigger) ---
    if app_state.is_recording and app_state.active_mode == RecordingMode.REALTIME_LLM:
        request.app.state.recorder.request_stop()
        logger.info("/trigger/realtime-llm: stop requested — finishing current utterance")
        return JSONResponse({"status": "stopping", "mode": "realtime-llm"})

    # --- CONFLICT ---
    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'. Stop that session first.",
        )

    # --- START ---
    await app_state.start_recording(RecordingMode.REALTIME_LLM)
    request.app.state.recorder.start()
    broadcast_event("state_change", app_state.get_status())

    task = asyncio.create_task(
        _realtime_loop(request),
        name="realtime-llm-stt-loop",
    )
    await app_state.set_realtime_task(task)
    return JSONResponse({"status": "started", "mode": "realtime-llm"})


@app.post("/trigger/batch", summary="Toggle batch transcription (full sentence)")
async def trigger_batch(request: Request) -> JSONResponse:
    """
    Toggle batch transcription mode with automatic silence-triggered stop.

    First call:  Starts recording + silence monitor. Auto-stops after
                 batch_silence_timeout_s of silence (default 3s), then
                 transcribes and injects text.
    Second call: Early manual stop — signals the background task to stop
                 immediately and proceed to transcription.
    """
    app_state = _state(request)

    # --- EARLY STOP (second press while background task is running) ---
    if app_state.is_recording and app_state.active_mode == RecordingMode.BATCH:
        request.app.state.recorder.request_stop()
        logger.info("/trigger/batch: early stop requested — transcription will follow")
        return JSONResponse({"status": "stopping", "mode": "batch"})

    # --- CONFLICT ---
    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'.",
        )

    # --- START ---
    await app_state.start_recording(RecordingMode.BATCH)
    request.app.state.recorder.start()
    broadcast_event("state_change", app_state.get_status())

    task = asyncio.create_task(
        _batch_pipeline_loop(request, RecordingMode.BATCH),
        name="batch-pipeline",
    )
    await app_state.set_realtime_task(task)  # reuse task slot for cancellation
    return JSONResponse({"status": "started", "mode": "batch"})


@app.post("/trigger/batch-llm", summary="Toggle batch transcription + LLM formatting")
async def trigger_batch_llm(request: Request) -> JSONResponse:
    """
    Same as /trigger/batch but passes the transcript through Ollama for
    grammar correction and punctuation before injection.
    Falls back to raw transcript if Ollama is unreachable.
    """
    app_state = _state(request)

    # --- EARLY STOP ---
    if app_state.is_recording and app_state.active_mode == RecordingMode.BATCH_LLM:
        request.app.state.recorder.request_stop()
        logger.info("/trigger/batch-llm: early stop requested — transcription will follow")
        return JSONResponse({"status": "stopping", "mode": "batch-llm"})

    # --- CONFLICT ---
    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'.",
        )

    # --- START ---
    await app_state.start_recording(RecordingMode.BATCH_LLM)
    request.app.state.recorder.start()
    broadcast_event("state_change", app_state.get_status())

    task = asyncio.create_task(
        _batch_pipeline_loop(request, RecordingMode.BATCH_LLM),
        name="batch-llm-pipeline",
    )
    await app_state.set_realtime_task(task)
    return JSONResponse({"status": "started", "mode": "batch-llm"})


# --------------------------------------------------------------------------- #
# Status & History Routes
# --------------------------------------------------------------------------- #
@app.get("/status", summary="Daemon health and recording state")
async def get_status(request: Request) -> JSONResponse:
    """Returns current recording state, active mode, and daemon uptime."""
    status = _state(request).get_status()
    status["ollama_model"] = request.app.state.llm.model_name
    status["whisper_realtime"] = request.app.state.config.whisper_realtime
    status["whisper_batch"] = request.app.state.config.whisper_batch
    return JSONResponse(status)


@app.get("/history", summary="Recent transcription history")
async def get_history(request: Request, limit: int = 20) -> JSONResponse:
    """Returns the last `limit` transcription entries (newest last)."""
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    return JSONResponse({"history": _state(request).get_history(limit)})


@app.get("/events", summary="Server-Sent Events stream")
async def sse_events(request: Request) -> StreamingResponse:
    """Streams real-time status and transcript updates to connected UI clients."""
    queue: asyncio.Queue = asyncio.Queue()
    _sse_clients.add(queue)

    async def event_generator():
        try:
            status = _state(request).get_status()
            status["ollama_model"] = request.app.state.llm.model_name
            status["whisper_realtime"] = request.app.state.config.whisper_realtime
            status["whisper_batch"] = request.app.state.config.whisper_batch
            yield f"event: state_change\ndata: {json.dumps(status)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {msg['type']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _sse_clients.discard(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Settings & Model Discovery Routes
# --------------------------------------------------------------------------- #
@app.get("/settings", summary="Read current daemon configuration")
async def get_settings(request: Request) -> JSONResponse:
    """Returns the full parsed configuration settings."""
    return JSONResponse(_cfg(request).to_dict())


@app.post("/settings", summary="Update daemon configuration at runtime")
async def post_settings(request: Request) -> JSONResponse:
    """
    Apply a partial settings update.
    Only the provided keys are changed; all others are preserved.

    If whisper_realtime or whisper_batch change, the corresponding Whisper
    model is hot-reloaded (old model freed, new model loaded without restart).
    If ollama_model changes, the new model name takes effect on the next call.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    cfg = _cfg(request)

    try:
        changed_keys = update_config(cfg, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    reloaded: list[str] = []

    # Hot-reload Whisper realtime model if its size changed
    if "whisper_realtime" in changed_keys:
        try:
            await request.app.state.stt.hot_reload("realtime", cfg.whisper_realtime)
            reloaded.append("whisper_realtime")
        except Exception as exc:
            logger.error("Failed to hot-reload realtime model: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Model reload failed for whisper_realtime: {exc}",
            )

    # Hot-reload Whisper batch model if its size changed
    if "whisper_batch" in changed_keys:
        try:
            await request.app.state.stt.hot_reload("batch", cfg.whisper_batch)
            reloaded.append("whisper_batch")
        except Exception as exc:
            logger.error("Failed to hot-reload batch model: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Model reload failed for whisper_batch: {exc}",
            )

    # Ollama model name update is instant (just update the string)
    if "ollama_model" in changed_keys:
        request.app.state.llm.model_name = cfg.ollama_model
        reloaded.append("ollama_model")
        logger.info("OllamaClient: model updated to '%s'", cfg.ollama_model)

    # Broadcast settings update to UI
    status = _state(request).get_status()
    status["ollama_model"] = request.app.state.llm.model_name
    status["whisper_realtime"] = request.app.state.config.whisper_realtime
    status["whisper_batch"] = request.app.state.config.whisper_batch
    broadcast_event("state_change", status)

    return JSONResponse({
        "status": "ok",
        "changed": sorted(changed_keys),
        "reloaded": reloaded,
    })


@app.get("/models/ollama", summary="List locally installed Ollama models")
async def get_ollama_models(request: Request) -> JSONResponse:
    """
    Queries Ollama for installed models and returns a clean list of names.
    Returns empty list if Ollama is unreachable (does not raise an error).
    """
    models = await request.app.state.llm.list_models()
    return JSONResponse({"models": models})


@app.get("/models/whisper", summary="List valid faster-whisper model sizes")
async def get_whisper_models() -> JSONResponse:
    """Returns the static list of valid faster-whisper model size identifiers."""
    return JSONResponse({"models": WHISPER_MODEL_SIZES})


# --------------------------------------------------------------------------- #
# Background Task — Realtime STT Loop
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Background Task — Batch Pipeline (silence-triggered auto-stop)
# --------------------------------------------------------------------------- #
async def _batch_pipeline_loop(request: Request, mode: RecordingMode) -> None:
    """
    Background task for batch and batch-llm modes.

    Flow:
      1. monitor_until_silence() — waits for batch_silence_timeout_s of silence
         OR for request_stop() to be called (early manual stop via 2nd trigger).
      2. recorder.stop() — flushes WAV, closes mic (removes 'in use' indicator).
      3. Transcribe in ThreadPoolExecutor.
      4. [batch-llm only] Format via Ollama.
      5. Inject keystrokes + copy to clipboard.
      6. Update state history.
    """
    recorder: AudioRecorder = request.app.state.recorder
    stt: WhisperEngine = request.app.state.stt
    injector: KeystrokeInjector = request.app.state.injector
    llm: OllamaClient = request.app.state.llm
    app_state: AppState = request.app.state.app_state
    cfg: Config = request.app.state.config
    loop = asyncio.get_event_loop()
    t_start = time.monotonic()

    logger.info("Batch pipeline started (mode=%s)", mode.value)

    try:
        # Step 1: Record until silence or manual stop
        await recorder.monitor_until_silence(
            silence_timeout_s=cfg.batch_silence_timeout_s,
            silence_rms_threshold=cfg.silence_rms_threshold,
            min_record_s=cfg.batch_min_record_s,
        )

        # Step 2: Flush WAV + close mic
        audio_path, duration_s = recorder.stop()

        # Step 3: Update state
        await app_state.stop_recording()
        broadcast_event("state_change", app_state.get_status())

        if duration_s < 0.3:
            logger.warning("Batch pipeline: recording too short (%.2fs) — skipping transcription", duration_s)
            return

        # Step 4: Transcribe
        raw_transcript: str = await loop.run_in_executor(
            None, stt.transcribe_file, audio_path, "batch"
        )
        logger.info("Batch transcription: %d chars in %.1fs", len(raw_transcript), time.monotonic() - t_start)

        # Step 5: [batch-llm] LLM formatting
        final_transcript = raw_transcript
        if mode == RecordingMode.BATCH_LLM and raw_transcript:
            final_transcript = await llm.format_transcript(raw_transcript)

        # Step 6: Inject + clipboard
        if final_transcript:
            await loop.run_in_executor(
                None, injector.type_text, final_transcript, cfg.typing_delay_ms
            )
            asyncio.create_task(injector.copy_to_clipboard(final_transcript))

        await app_state.add_history(mode, final_transcript, duration_s)
        broadcast_event("new_transcript", {
            "transcript": final_transcript,
            "mode": mode.value,
            "duration_s": round(duration_s, 2),
            "session_transcript": final_transcript
        })

        logger.info(
            "Batch pipeline complete — %.1fs audio, %.1fs total, chars=%d",
            duration_s, time.monotonic() - t_start, len(final_transcript),
        )

    except asyncio.CancelledError:
        logger.info("Batch pipeline cancelled — flushing partial audio")
        recorder.stop()  # ensure mic is released even on cancellation
        try:
            await app_state.stop_recording()
            broadcast_event("state_change", app_state.get_status())
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Batch pipeline error: %s", exc)
        recorder.stop()
        try:
            await app_state.stop_recording()
            broadcast_event("state_change", app_state.get_status())
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Background Task — Realtime STT Loop
# --------------------------------------------------------------------------- #
async def _realtime_loop(request: Request) -> None:
    """
    Background task: VAD-segmented utterance commit model.

    Architecture (no backspace, append-only):
      • stream_utterances() yields one numpy array per detected utterance
        (speech followed by utterance_silence_s of silence)
      • Each utterance is transcribed independently, appended to the screen
      • initial_prompt chains the last utterance for cross-utterance context
      • force_commit_s prevents unbounded buffer during long unbroken speech
      • 0 for utterance_silence_s → uses fixed realtime_chunk_s chunks only

    Stop modes:
      • request_stop() called (second trigger) → finishes current utterance
      • Both auto-silence AND manual stop are supported via the same path
    """
    recorder: AudioRecorder = request.app.state.recorder
    stt: WhisperEngine = request.app.state.stt
    injector: KeystrokeInjector = request.app.state.injector
    llm: OllamaClient = request.app.state.llm
    app_state: AppState = request.app.state.app_state
    cfg: Config = request.app.state.config
    loop = asyncio.get_event_loop()

    # Capture the active mode at startup before state shifts
    mode = app_state.active_mode or RecordingMode.REALTIME

    session_start = time.monotonic()
    session_transcript: list[str] = []  # all committed utterances this session
    prev_utterance: str = ""            # passed as initial_prompt for context

    logger.info("Realtime loop started (utterance_silence=%.1fs, force_commit=%.1fs, timeout=%.1fs)",
                cfg.realtime_utterance_silence_s, cfg.realtime_force_commit_s, cfg.realtime_silence_timeout_s)

    try:
        async for utterance_audio in recorder.stream_utterances(
            utterance_silence_s=cfg.realtime_utterance_silence_s,
            force_commit_s=cfg.realtime_force_commit_s,
            silence_rms_threshold=cfg.silence_rms_threshold,
            chunk_s=cfg.realtime_chunk_s,
            realtime_silence_timeout_s=cfg.realtime_silence_timeout_s,
        ):
            # Transcribe in thread pool (CPU-bound)
            text: str = await loop.run_in_executor(
                None,
                stt.transcribe_chunk,
                utterance_audio,
                cfg.sample_rate,
                prev_utterance,   # context from previous utterance
            )

            text = text.strip() if text else ""
            if not text:
                logger.debug("Realtime: empty utterance (silence/noise) — skipped")
                continue

            # Append-only: type a space separator if not first word, then new text
            prefix = " " if session_transcript else ""
            await loop.run_in_executor(
                None, injector.type_text, prefix + text, cfg.typing_delay_ms
            )

            session_transcript.append(text)
            prev_utterance = text
            logger.info("Realtime: committed utterance '%s'", text[:80])
            broadcast_event("new_transcript", {
                "transcript": text,
                "mode": mode.value,
                "duration_s": 0.0,
                "session_transcript": " ".join(session_transcript)
            })

    except asyncio.CancelledError:
        logger.info("Realtime loop cancelled")
    except Exception as exc:
        logger.exception("Realtime loop error: %s", exc)
    finally:
        # Flush WAV + close mic
        recorder.stop()

        # Update state
        try:
            await app_state.stop_recording()
            broadcast_event("state_change", app_state.get_status())
        except Exception:
            pass

        duration_s = time.monotonic() - session_start
        full_text = " ".join(session_transcript)

        # Handle clipboard, LLM refinement, and notification based on mode
        if full_text:
            if mode == RecordingMode.REALTIME_LLM:
                logger.info("Realtime LLM: refining %d chars via local LLM", len(full_text))
                refined_text = await llm.format_transcript(full_text)
                asyncio.create_task(injector.copy_to_clipboard(refined_text))
                await app_state.add_history(RecordingMode.REALTIME_LLM, refined_text, duration_s)
                asyncio.create_task(injector.send_notification("Zola", "Transcribed text has been refined."))
                broadcast_event("new_transcript", {
                    "transcript": refined_text,
                    "mode": mode.value,
                    "duration_s": round(duration_s, 2),
                    "session_transcript": refined_text
                })
            else:
                asyncio.create_task(injector.copy_to_clipboard(full_text))
                await app_state.add_history(RecordingMode.REALTIME, full_text, duration_s)
                broadcast_event("new_transcript", {
                    "transcript": full_text,
                    "mode": mode.value,
                    "duration_s": round(duration_s, 2),
                    "session_transcript": full_text
                })

        logger.info("Realtime loop done — %.1fs, %d utterances, %d chars",
                    duration_s, len(session_transcript), len(full_text))


# --------------------------------------------------------------------------- #
# Dev Entry Point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=5001,
        log_level="info",
        reload=False,
    )
