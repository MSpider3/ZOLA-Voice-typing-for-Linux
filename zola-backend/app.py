"""
app.py — FastAPI Application Entry Point & Task Orchestrator
=============================================================
Runs on 127.0.0.1:5001. Exposes trigger endpoints for the Tauri UI frontend
and orchestrates the full pipeline:

  Trigger → Recorder → STT (ThreadPoolExecutor) → [LLM] → Injector

Security notes:
  - CORS is locked to Tauri origins only (not wildcard).
  - Requests carrying a foreign Origin header are rejected outright (CORS alone
    only hides the response; the request would still run).
  - ollama_url is validated to loopback-only in config.py.
  - Transcript content is never logged at INFO level (privacy).
  - Trigger endpoints are rate-limited to 1 request per 500ms.
  - /tmp is not used for audio — XDG_RUNTIME_DIR/zola/ is used instead.

Start the server:
  uvicorn app:app --host 127.0.0.1 --port 5001 --log-level info
"""

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config import MODELS_DIR, Config, installed_models, load_config, update_config
from core_llm import OllamaClient
import model_store
from core_stt import WhisperEngine, _model_meta, apply_replacements
from injector import KeystrokeInjector
from recorder import AudioRecorder, list_input_devices, play_cue
from state import HISTORY_PATH, AppState, RecordingMode

# --------------------------------------------------------------------------- #
# Logging Setup
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zola")

_ZOLA_DEBUG = os.environ.get("ZOLA_DEBUG") == "1"

# Valid faster-whisper model sizes (kept in sync with config.VALID_WHISPER_SIZES)
WHISPER_MODEL_SIZES = [
    "tiny", "base", "small", "medium",
    "large-v2", "large-v3",
    "large-v3-turbo",
    "tiny.en", "base.en", "small.en",
]

# Whisper models that were not trained on the translate task
_NO_TRANSLATE_MODELS = {"large-v3-turbo", "distil-large-v3"}

# Rate limiting: minimum seconds between trigger requests
_TRIGGER_COOLDOWN_S = 0.5
_last_trigger_time: float = 0.0


# --------------------------------------------------------------------------- #
# Startup checks
# --------------------------------------------------------------------------- #
def _check_uinput_access() -> None:
    """
    Warn clearly if /dev/uinput is not writable, before the injector fails
    with a raw PermissionError. Access normally comes from the logged-in
    session (udev 'uaccess' rule from the rpm/deb or install.sh), not a group.
    """
    if not os.access("/dev/uinput", os.W_OK):
        logger.warning(
            "/dev/uinput is not writable — Zola cannot type text.\n"
            "Fix with these one-time commands, then log out and back in:\n"
            "  echo 'KERNEL==\"uinput\", TAG+=\"uaccess\", OPTIONS+=\"static_node=uinput\"' | "
            "sudo tee /etc/udev/rules.d/70-zola-uinput.rules\n"
            "  sudo udevadm control --reload-rules && sudo udevadm trigger"
        )


# --------------------------------------------------------------------------- #
# Lifespan — Startup / Shutdown
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("Zola daemon starting up…")
    logger.info("=" * 60)

    _check_uinput_access()

    cfg: Config = load_config()
    app.state.config = cfg

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zola-worker")
    app.state.executor = executor
    loop = asyncio.get_event_loop()
    loop.set_default_executor(executor)

    app.state.downloads = {}  # model name -> {"status", "done", "total"}
    app.state.app_state = AppState(
        history_max=cfg.history_max,
        history_path=HISTORY_PATH if cfg.save_history else None,
    )

    try:
        recorder = AudioRecorder(
            sample_rate=cfg.sample_rate,
            max_record_duration_s=cfg.max_record_duration_s,
        )
        app.state.recorder = recorder
    except Exception as exc:
        logger.critical("Failed to initialise AudioRecorder: %s", exc)
        raise

    # Models load in the background so the API (and the app's settings page)
    # answers at once; the first start may download models for minutes.
    app.state.stt = None

    async def load_models() -> None:
        try:
            app.state.stt = await loop.run_in_executor(None, WhisperEngine, cfg)
            broadcast_event("state_change", _status(app))
        except Exception as exc:
            logger.critical("Failed to initialise WhisperEngine: %s", exc)

    models_task = asyncio.create_task(load_models(), name="load-models")

    llm = OllamaClient(cfg)
    app.state.llm = llm

    ollama_ok = await llm.health_check()
    if ollama_ok:
        logger.info("Ollama is reachable at %s (model: %s)", cfg.ollama_url, cfg.ollama_model)
    else:
        logger.warning(
            "Ollama is NOT reachable at %s — batch-llm/realtime-llm will return raw transcripts. "
            "Start with: ollama serve",
            cfg.ollama_url,
        )

    try:
        injector = KeystrokeInjector()
        app.state.injector = injector
    except Exception as exc:
        logger.critical(
            "Failed to create UInput device: %s\n"
            "See injector.py header for one-time permission setup.",
            exc,
        )
        raise

    logger.info("=" * 60)
    logger.info("Zola daemon ready — listening on http://127.0.0.1:5001")
    logger.info("=" * 60)

    level_task = asyncio.create_task(_level_broadcaster(app), name="mic-level")

    try:
        yield
    finally:
        logger.info("Zola daemon shutting down…")
        level_task.cancel()
        models_task.cancel()

        if hasattr(app.state, "app_state"):
            try:
                await app.state.app_state.cancel_realtime_task()
            except Exception as exc:
                logger.error("Error cancelling realtime task: %s", exc)

        if hasattr(app.state, "recorder"):
            try:
                app.state.recorder.close()
            except Exception as exc:
                logger.error("Error closing recorder: %s", exc)

        if hasattr(app.state, "llm"):
            try:
                await app.state.llm.close()
            except Exception as exc:
                logger.error("Error closing LLM client: %s", exc)

        if hasattr(app.state, "injector"):
            try:
                app.state.injector.close()
            except Exception as exc:
                logger.error("Error closing injector: %s", exc)

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
    version="1.3.0",
    lifespan=lifespan,
)

# CORS: locked to Tauri's internal origin only.
# Previously allow_origins=["*"] — that allowed any browser page to control the daemon.
_ALLOWED_ORIGINS = ["tauri://localhost", "http://localhost:1420"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class _OriginGuard:
    """
    Reject any request whose Origin header is not the Tauri UI.
    Browsers always send Origin on cross-site POSTs, so this stops web pages
    from triggering the mic or changing settings. Requests without an Origin
    (curl from a compositor keybind) are allowed.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None and origin.decode("latin-1") not in _ALLOWED_ORIGINS:
                response = JSONResponse({"detail": "Origin not allowed"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(_OriginGuard)
# Host check defeats DNS-rebinding (a foreign hostname resolving to 127.0.0.1)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

# --------------------------------------------------------------------------- #
# Debug Endpoints (only registered when ZOLA_DEBUG=1)
# --------------------------------------------------------------------------- #
if _ZOLA_DEBUG:
    @app.post(
        "/debug/crash-injector",
        summary="[DEBUG] Crash mid-injection to verify release_all() failsafe",
        tags=["debug"],
    )
    async def debug_crash_injector(request: Request) -> JSONResponse:
        injector: KeystrokeInjector = request.app.state.injector
        logger.warning("[DEBUG] crash-injector triggered — intentional crash")
        try:
            injector.type_text("ZOLA_CRASH_TEST_", 12)
            raise RuntimeError("INTENTIONAL CRASH")
        except RuntimeError:
            injector.release_all()
            raise HTTPException(
                status_code=500,
                detail="INTENTIONAL_CRASH_OK — release_all() executed.",
            )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _state(request: Request) -> AppState:
    return request.app.state.app_state

def _cfg(request: Request) -> Config:
    return request.app.state.config


def _status(app: FastAPI) -> dict[str, Any]:
    """The full status the UI expects in /status and every state_change event."""
    status = app.state.app_state.get_status()
    status["ollama_model"] = app.state.llm.model_name
    status["whisper_realtime"] = app.state.config.whisper_realtime
    status["whisper_batch"] = app.state.config.whisper_batch
    status["models_ready"] = app.state.stt is not None
    return status

def _check_rate_limit() -> None:
    """Raise HTTP 429 if called more than once per 500ms."""
    global _last_trigger_time
    now = time.monotonic()
    if now - _last_trigger_time < _TRIGGER_COOLDOWN_S:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Wait {_TRIGGER_COOLDOWN_S*1000:.0f}ms between triggers.",
        )
    _last_trigger_time = now


async def _open_mic(request: Request, audio_path: Path) -> None:
    """Start the recorder; if the mic cannot be opened, undo the recording state."""
    cfg = _cfg(request)
    if request.app.state.stt is None:
        await _state(request).stop_recording()
        audio_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503,
            detail="Speech models are still loading (the first start downloads them). Try again shortly.",
        )
    if cfg.audio_feedback:
        # Played before the mic opens, so the tone is not recorded
        await asyncio.get_event_loop().run_in_executor(None, play_cue, "start")
    try:
        request.app.state.recorder.start(audio_path, cfg.input_device)
    except Exception as exc:
        await _state(request).stop_recording()
        audio_path.unlink(missing_ok=True)
        broadcast_event("state_change", _status(request.app))
        raise HTTPException(status_code=503, detail=f"Could not open microphone: {exc}")


# --------------------------------------------------------------------------- #
# SSE Clients Registry
# --------------------------------------------------------------------------- #
_sse_clients: set[asyncio.Queue] = set()

def broadcast_event(event_type: str, data: dict) -> None:
    """Broadcast an event to all connected SSE clients."""
    if not _sse_clients:
        return
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": event_type, "data": data})
        except Exception as exc:
            logger.error("Failed to push event to SSE client queue: %s", exc)


async def _level_broadcaster(app: FastAPI) -> None:
    """Send the mic peak level 10x per second while recording (UI level meter)."""
    while True:
        await asyncio.sleep(0.1)
        recorder = getattr(app.state, "recorder", None)
        if _sse_clients and recorder is not None and recorder._is_recording:
            broadcast_event("level", {"level": round(recorder.level, 4)})


def _stop_cue(cfg: Config) -> None:
    """Play the stop tone in the background (never delays transcription)."""
    if cfg.audio_feedback:
        asyncio.get_event_loop().run_in_executor(None, play_cue, "stop")


# --------------------------------------------------------------------------- #
# Trigger Routes
# --------------------------------------------------------------------------- #
@app.post("/trigger", summary="Toggle the configured active transcription mode")
async def trigger_active_mode(request: Request) -> JSONResponse:
    # Rate limiting is done by the mode handler this dispatches to.
    cfg = _cfg(request)
    mode = cfg.active_mode or "batch"
    if mode == "realtime":
        return await trigger_realtime(request)
    elif mode == "realtime-llm":
        return await trigger_realtime_llm(request)
    elif mode == "batch":
        return await trigger_batch(request)
    elif mode == "batch-llm":
        return await trigger_batch_llm(request)
    elif mode == "batch-translate":
        return await trigger_batch_translate(request)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid active mode: {mode}")


@app.post("/trigger/realtime", summary="Toggle real-time continuous transcription")
async def trigger_realtime(request: Request) -> JSONResponse:
    _check_rate_limit()
    app_state = _state(request)

    if app_state.is_recording and app_state.active_mode == RecordingMode.REALTIME:
        request.app.state.recorder.request_stop()
        return JSONResponse({"status": "stopping", "mode": "realtime"})

    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'.",
        )

    audio_path = await app_state.start_recording(RecordingMode.REALTIME)
    await _open_mic(request, audio_path)
    broadcast_event("state_change", _status(request.app))

    task = asyncio.create_task(_realtime_loop(request), name="realtime-stt-loop")
    await app_state.set_realtime_task(task)
    return JSONResponse({"status": "started", "mode": "realtime"})


@app.post("/trigger/realtime-llm", summary="Toggle real-time transcription + LLM refinement")
async def trigger_realtime_llm(request: Request) -> JSONResponse:
    _check_rate_limit()
    app_state = _state(request)

    if app_state.is_recording and app_state.active_mode == RecordingMode.REALTIME_LLM:
        request.app.state.recorder.request_stop()
        return JSONResponse({"status": "stopping", "mode": "realtime-llm"})

    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'.",
        )

    audio_path = await app_state.start_recording(RecordingMode.REALTIME_LLM)
    await _open_mic(request, audio_path)
    broadcast_event("state_change", _status(request.app))

    task = asyncio.create_task(_realtime_loop(request), name="realtime-llm-stt-loop")
    await app_state.set_realtime_task(task)
    return JSONResponse({"status": "started", "mode": "realtime-llm"})


@app.post("/trigger/batch", summary="Toggle batch transcription (full sentence)")
async def trigger_batch(request: Request) -> JSONResponse:
    _check_rate_limit()
    app_state = _state(request)

    if app_state.is_recording and app_state.active_mode == RecordingMode.BATCH:
        request.app.state.recorder.request_stop()
        return JSONResponse({"status": "stopping", "mode": "batch"})

    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'.",
        )

    audio_path = await app_state.start_recording(RecordingMode.BATCH)
    await _open_mic(request, audio_path)
    broadcast_event("state_change", _status(request.app))

    task = asyncio.create_task(
        _batch_pipeline_loop(request, RecordingMode.BATCH), name="batch-pipeline"
    )
    await app_state.set_realtime_task(task)
    return JSONResponse({"status": "started", "mode": "batch"})


@app.post("/trigger/batch-llm", summary="Toggle batch transcription + LLM formatting")
async def trigger_batch_llm(request: Request) -> JSONResponse:
    _check_rate_limit()
    app_state = _state(request)

    if app_state.is_recording and app_state.active_mode == RecordingMode.BATCH_LLM:
        request.app.state.recorder.request_stop()
        return JSONResponse({"status": "stopping", "mode": "batch-llm"})

    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'.",
        )

    audio_path = await app_state.start_recording(RecordingMode.BATCH_LLM)
    await _open_mic(request, audio_path)
    broadcast_event("state_change", _status(request.app))

    task = asyncio.create_task(
        _batch_pipeline_loop(request, RecordingMode.BATCH_LLM), name="batch-llm-pipeline"
    )
    await app_state.set_realtime_task(task)
    return JSONResponse({"status": "started", "mode": "batch-llm"})


@app.post(
    "/trigger/batch-translate",
    summary="Record Hindi/Hinglish speech → inject English text (no Ollama needed)",
)
async def trigger_batch_translate(request: Request) -> JSONResponse:
    """
    Batch-translate mode: records audio, then uses Whisper's built-in
    task="translate" to produce English text from Hindi/Hinglish speech.

    ⚠️ EXPERIMENTAL: Whisper's native translation is unreliable for Hinglish
    code-switching — it frequently outputs Hindi script instead of English.
    For reliable Hindi→English, use /trigger/batch-llm with output_language=english.

    No Ollama required — Whisper handles translation natively.
    Uses the batch model (small by default).
    """
    _check_rate_limit()
    app_state = _state(request)

    if _cfg(request).whisper_batch in _NO_TRANSLATE_MODELS:
        logger.warning(
            "batch-translate: model '%s' cannot translate and will output the "
            "spoken language. Use small/medium/large-v3, or batch-llm with "
            "output_language=english.",
            _cfg(request).whisper_batch,
        )

    if app_state.is_recording and app_state.active_mode == RecordingMode.BATCH_TRANSLATE:
        request.app.state.recorder.request_stop()
        return JSONResponse({"status": "stopping", "mode": "batch-translate"})

    if app_state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording in mode '{app_state.active_mode}'.",
        )

    audio_path = await app_state.start_recording(RecordingMode.BATCH_TRANSLATE)
    await _open_mic(request, audio_path)
    broadcast_event("state_change", _status(request.app))

    task = asyncio.create_task(
        _batch_pipeline_loop(request, RecordingMode.BATCH_TRANSLATE),
        name="batch-translate-pipeline",
    )
    await app_state.set_realtime_task(task)
    return JSONResponse({
        "status": "started",
        "mode": "batch-translate",
        "note": "Native Whisper translation is experimental on Hinglish. "
                "For reliable Hindi→English, use batch-llm with output_language=english."
    })


# --------------------------------------------------------------------------- #
# Status & History Routes
# --------------------------------------------------------------------------- #
@app.get("/status", summary="Daemon health and recording state")
async def get_status(request: Request) -> JSONResponse:
    return JSONResponse(_status(request.app))


@app.post("/cancel", summary="Stop recording and discard it (nothing is typed)")
async def cancel_recording(request: Request) -> JSONResponse:
    app_state = _state(request)
    if not app_state.is_recording:
        return JSONResponse({"status": "idle"})
    await app_state.cancel_realtime_task()
    return JSONResponse({"status": "cancelled"})


@app.get("/devices", summary="List microphones")
async def get_devices() -> JSONResponse:
    return JSONResponse({"devices": list_input_devices()})


@app.post("/history/clear", summary="Delete all transcription history")
async def clear_history(request: Request) -> JSONResponse:
    await _state(request).clear_history()
    return JSONResponse({"status": "ok"})


@app.get("/history", summary="Recent transcription history")
async def get_history(request: Request, limit: int = 20) -> JSONResponse:
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    return JSONResponse({"history": _state(request).get_history(limit)})


@app.get("/events", summary="Server-Sent Events stream")
async def sse_events(request: Request) -> StreamingResponse:
    """Streams real-time status and transcript updates to connected UI clients."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)  # bounded — prevents unbounded growth
    _sse_clients.add(queue)

    async def event_generator():
        try:
            yield f"event: state_change\ndata: {json.dumps(_status(request.app))}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {msg['type']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise   # re-raise so FastAPI can shut down cleanly (was swallowed before)
        finally:
            _sse_clients.discard(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Settings & Model Discovery Routes
# --------------------------------------------------------------------------- #
@app.get("/settings", summary="Read current daemon configuration")
async def get_settings(request: Request) -> JSONResponse:
    return JSONResponse(_cfg(request).to_dict())


@app.post("/settings", summary="Update daemon configuration at runtime")
async def post_settings(request: Request) -> JSONResponse:
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    cfg = _cfg(request)
    stt: WhisperEngine = request.app.state.stt
    engine_keys = ("whisper_realtime", "whisper_batch", "cpu_threads", "compute_type")
    previous = {k: getattr(cfg, k) for k in engine_keys}
    if stt is None and any(body.get(k, previous[k]) != previous[k] for k in engine_keys):
        raise HTTPException(
            status_code=409,
            detail="Speech models are still loading — change the model once they are ready.",
        )

    try:
        changed_keys = update_config(cfg, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    reloaded: list[str] = []

    # cpu_threads / compute_type only take effect on a freshly loaded model
    engine_changed = bool(changed_keys & {"cpu_threads", "compute_type"})
    try:
        if "whisper_realtime" in changed_keys or engine_changed:
            await stt.hot_reload("realtime", cfg.whisper_realtime, fresh=engine_changed)
            reloaded.append("whisper_realtime")
        if "whisper_batch" in changed_keys or engine_changed:
            await stt.hot_reload("batch", cfg.whisper_batch)
            reloaded.append("whisper_batch")
    except Exception as exc:
        logger.error("Model reload failed: %s — restoring previous models", exc)
        update_config(cfg, previous)
        await stt.hot_reload("realtime", cfg.whisper_realtime, fresh=True)
        await stt.hot_reload("batch", cfg.whisper_batch)
        raise HTTPException(status_code=500, detail=f"Model reload failed: {exc}")

    if "ollama_model" in changed_keys:
        request.app.state.llm.model_name = cfg.ollama_model
        reloaded.append("ollama_model")

    if "ollama_url" in changed_keys:
        request.app.state.llm.set_base_url(cfg.ollama_url)
        reloaded.append("ollama_url")

    if "llm_api" in changed_keys:
        request.app.state.llm.api = cfg.llm_api
        reloaded.append("llm_api")

    if "save_history" in changed_keys:
        _state(request).set_history_path(HISTORY_PATH if cfg.save_history else None)

    if "output_language" in changed_keys:
        request.app.state.llm.output_language = cfg.output_language
        reloaded.append("output_language")

    if "max_record_duration_s" in changed_keys:
        request.app.state.recorder._max_duration_s = cfg.max_record_duration_s

    broadcast_event("state_change", _status(request.app))

    return JSONResponse({
        "status": "ok",
        "changed": sorted(changed_keys),
        "reloaded": reloaded,
    })


@app.get("/models/ollama", summary="List locally installed Ollama models")
async def get_ollama_models(request: Request) -> JSONResponse:
    models = await request.app.state.llm.list_models()
    return JSONResponse({"models": models})


@app.get("/models/catalog", summary="Downloadable STT models and their install state")
async def get_model_catalog(request: Request) -> JSONResponse:
    downloads = request.app.state.downloads
    return JSONResponse({"models": [
        {
            "name": name,
            "label": entry["label"],
            "description": entry["description"],
            "size_mb": entry["size_mb"],
            "installed": model_store.is_installed(name),
            "download": downloads.get(name),
        }
        for name, entry in model_store.CATALOG.items()
    ]})


def _download_event(app: FastAPI, name: str, status: str, done: int = 0, total: int = 0,
                    error: str = "") -> None:
    """Record download progress and push it to the UI (event loop thread only)."""
    if status == "downloading":
        app.state.downloads[name] = {"status": status, "done": done, "total": total}
    else:
        app.state.downloads.pop(name, None)
    broadcast_event("model_download", {"name": name, "status": status, "done": done,
                                       "total": total, "error": error})


@app.post("/models/download", summary="Download and install a catalog model", status_code=202)
async def download_model(request: Request) -> JSONResponse:
    try:
        name = (await request.json()).get("name")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if name not in model_store.CATALOG:
        raise HTTPException(status_code=404, detail=f"Unknown model '{name}'")
    if model_store.is_installed(name):
        raise HTTPException(status_code=409, detail=f"'{name}' is already installed")
    if name in request.app.state.downloads:
        raise HTTPException(status_code=409, detail=f"'{name}' is already downloading")

    app_ = request.app
    loop = asyncio.get_running_loop()
    last_sent = [0.0]

    def progress(done: int, total: int) -> None:  # called from the download thread
        now = time.monotonic()
        if now - last_sent[0] >= 0.3 or done >= total:
            last_sent[0] = now
            loop.call_soon_threadsafe(_download_event, app_, name, "downloading", done, total)

    async def run() -> None:
        try:
            await loop.run_in_executor(None, model_store.download, name, progress)
            _model_meta.cache_clear()
            _download_event(app_, name, "done")
        except Exception as exc:
            logger.exception("Model download '%s' failed: %s", name, exc)
            _download_event(app_, name, "error", error=str(exc))

    _download_event(app_, name, "downloading")
    asyncio.create_task(run(), name=f"download-{name}")
    return JSONResponse({"status": "started", "name": name}, status_code=202)


@app.post("/models/delete", summary="Delete an installed catalog model")
async def delete_model(request: Request) -> JSONResponse:
    try:
        name = (await request.json()).get("name")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if name not in model_store.CATALOG or not model_store.is_installed(name):
        raise HTTPException(status_code=404, detail=f"'{name}' is not installed")
    cfg = _cfg(request)
    if name in (cfg.whisper_realtime, cfg.whisper_batch):
        raise HTTPException(status_code=409, detail=f"'{name}' is in use — pick another model first")
    model_store.delete(name)
    _model_meta.cache_clear()
    return JSONResponse({"status": "deleted", "name": name})


@app.get("/models/whisper", summary="List valid faster-whisper model sizes")
async def get_whisper_models() -> JSONResponse:
    """Standard sizes plus installed model folders; `labels` describes the folders."""
    custom = installed_models()
    labels = {}
    for name in custom:
        try:
            labels[name] = json.loads((MODELS_DIR / name / "zola.json").read_text(encoding="utf-8")).get("label", name)
        except (OSError, ValueError):
            labels[name] = name
    return JSONResponse({"models": custom + WHISPER_MODEL_SIZES, "labels": labels})


# --------------------------------------------------------------------------- #
# Background Tasks
# --------------------------------------------------------------------------- #
async def _batch_pipeline_loop(request: Request, mode: RecordingMode) -> None:
    """
    Background task for batch, batch-llm, and batch-translate modes.

    Flow:
      1. monitor_until_silence() — waits for silence or manual stop.
      2. recorder.stop() — flushes WAV, closes mic.
      3. Transcribe (or translate for batch-translate) in ThreadPoolExecutor.
      4. [batch-llm] Format via Ollama.
      5. inject_text() — clipboard paste primary, uinput fallback.
      6. copy_to_clipboard() — session-end clipboard update.
      7. Update state + history.
      8. Clean up temp audio file.
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

    audio_path = None
    stopped = False  # once True, the recorder may already belong to a newer session
    try:
        await recorder.monitor_until_silence(
            silence_timeout_s=cfg.batch_silence_timeout_s,
            min_record_s=cfg.batch_min_record_s,
            max_record_duration_s=cfg.max_record_duration_s,
        )

        audio_path, duration_s = recorder.stop()
        _stop_cue(cfg)
        await app_state.stop_recording()
        stopped = True
        broadcast_event("state_change", _status(request.app))

        if duration_s < 0.3:
            logger.warning("Batch pipeline: recording too short (%.2fs) — skipping", duration_s)
            return

        # Transcribe or translate
        if mode == RecordingMode.BATCH_TRANSLATE:
            raw_transcript: str = await loop.run_in_executor(
                None, stt.translate_file, audio_path
            )
            logger.info(
                "Batch-translate: %d chars in %.1fs",
                len(raw_transcript), time.monotonic() - t_start,
            )
        else:
            raw_transcript = await loop.run_in_executor(
                None, stt.transcribe_file, audio_path, "batch"
            )
            logger.info(
                "Batch transcription: %d chars in %.1fs",
                len(raw_transcript), time.monotonic() - t_start,
            )

        raw_transcript = apply_replacements(raw_transcript, cfg.replacements)
        final_transcript = raw_transcript
        if mode == RecordingMode.BATCH_LLM and raw_transcript:
            final_transcript = await llm.format_transcript(raw_transcript)

        if final_transcript:
            # Clipboard paste (Unicode-safe, works for Devanagari). The text
            # stays in the clipboard afterwards for manual paste if needed.
            await injector.inject_text(
                final_transcript, cfg.typing_delay_ms, cfg.paste_method, cfg.restore_clipboard,
            )

            await app_state.add_history(mode, final_transcript, duration_s)
            broadcast_event("new_transcript", {
                "transcript": final_transcript,
                "mode": mode.value,
                "duration_s": round(duration_s, 2),
                "session_transcript": final_transcript,
            })

        logger.info(
            "Batch pipeline complete — %.1fs audio, %.1fs total, chars=%d",
            duration_s, time.monotonic() - t_start, len(final_transcript),
        )

    except asyncio.CancelledError:
        logger.info("Batch pipeline cancelled")
        if not stopped:
            audio_path, _ = recorder.stop()
            try:
                await app_state.stop_recording()
                broadcast_event("state_change", _status(request.app))
            except Exception as b_err:
                logger.debug("Broadcast cancellation state change ignored: %s", b_err)
    except Exception as exc:
        logger.exception("Batch pipeline error: %s", exc)
        if not stopped:
            audio_path, _ = recorder.stop()
            try:
                await app_state.stop_recording()
                broadcast_event("state_change", _status(request.app))
            except Exception as b_err:
                logger.debug("Broadcast error state change ignored: %s", b_err)
    finally:
        # Clean up the per-session temp audio file
        if audio_path and Path(audio_path).exists():
            try:
                Path(audio_path).unlink()
                logger.debug("Cleaned up temp audio file: %s", audio_path)
            except Exception as exc:
                logger.warning("Could not delete temp audio: %s: %s", audio_path, exc)


async def _realtime_loop(request: Request) -> None:
    """
    Background task: VAD-segmented utterance commit model.

    Each yielded utterance is transcribed and injected via clipboard paste.
    Transcript content is NOT logged at INFO level (privacy).
    """
    recorder: AudioRecorder = request.app.state.recorder
    stt: WhisperEngine = request.app.state.stt
    injector: KeystrokeInjector = request.app.state.injector
    llm: OllamaClient = request.app.state.llm
    app_state: AppState = request.app.state.app_state
    cfg: Config = request.app.state.config
    loop = asyncio.get_event_loop()

    mode = app_state.active_mode or RecordingMode.REALTIME
    cancelled = False  # /cancel or shutdown: skip the final LLM refinement
    session_start = time.monotonic()
    session_transcript: list[str] = []
    prev_utterance: str = ""

    logger.info(
        "Realtime loop started (utterance_silence=%.1fs, force_commit=%.1fs, timeout=%.1fs)",
        cfg.realtime_utterance_silence_s, cfg.realtime_force_commit_s,
        cfg.realtime_silence_timeout_s,
    )

    try:
        async for utterance_audio in recorder.stream_utterances(
            utterance_silence_s=cfg.realtime_utterance_silence_s,
            force_commit_s=cfg.realtime_force_commit_s,
            chunk_s=cfg.realtime_chunk_s,
            realtime_silence_timeout_s=cfg.realtime_silence_timeout_s,
            max_record_duration_s=cfg.max_record_duration_s,
        ):
            text: str = await loop.run_in_executor(
                None, stt.transcribe_chunk, utterance_audio, cfg.sample_rate, prev_utterance,
            )

            text = apply_replacements(text.strip(), cfg.replacements) if text else ""
            if not text:
                continue

            # Inject via clipboard paste (Unicode-safe — handles Devanagari)
            prefix = " " if session_transcript else ""
            await injector.inject_text(
                prefix + text, cfg.typing_delay_ms, cfg.paste_method, cfg.restore_clipboard,
            )

            session_transcript.append(text)
            prev_utterance = text

            # Log only char count, NOT the transcript content (privacy)
            logger.info("Realtime: committed utterance (%d chars)", len(text))
            broadcast_event("new_transcript", {
                "transcript": text,
                "mode": mode.value,
                "duration_s": 0.0,
                "session_transcript": " ".join(session_transcript),
            })

    except asyncio.CancelledError:
        logger.info("Realtime loop cancelled")
        cancelled = True
    except Exception as exc:
        logger.exception("Realtime loop error: %s", exc)
    finally:
        audio_path_str = app_state.current_audio_path
        recorder.stop()
        _stop_cue(cfg)

        try:
            await app_state.stop_recording()
            broadcast_event("state_change", _status(request.app))
        except Exception as b_err:
            logger.debug("Broadcast final state change ignored: %s", b_err)

        duration_s = time.monotonic() - session_start
        full_text = " ".join(session_transcript)

        if full_text:
            if mode == RecordingMode.REALTIME_LLM and not cancelled:
                logger.info("Realtime LLM: refining %d chars via local LLM", len(full_text))
                refined_text = await llm.format_transcript(full_text)
                asyncio.create_task(injector.copy_to_clipboard(refined_text))
                await app_state.add_history(RecordingMode.REALTIME_LLM, refined_text, duration_s)
                asyncio.create_task(injector.send_notification(
                    "Zola", "Transcribed text has been refined."
                ))
                broadcast_event("new_transcript", {
                    "transcript": refined_text,
                    "mode": mode.value,
                    "duration_s": round(duration_s, 2),
                    "session_transcript": refined_text,
                })
            else:
                asyncio.create_task(injector.copy_to_clipboard(full_text))
                await app_state.add_history(RecordingMode.REALTIME, full_text, duration_s)
                broadcast_event("new_transcript", {
                    "transcript": full_text,
                    "mode": mode.value,
                    "duration_s": round(duration_s, 2),
                    "session_transcript": full_text,
                })

        logger.info(
            "Realtime loop done — %.1fs, %d utterances, %d chars",
            duration_s, len(session_transcript), len(full_text),
        )

        # Clean up per-session temp audio file
        if audio_path_str and Path(audio_path_str).exists():
            try:
                Path(audio_path_str).unlink()
                logger.debug("Cleaned up temp audio file: %s", audio_path_str)
            except Exception as exc:
                logger.warning("Could not delete temp audio: %s: %s", audio_path_str, exc)


# --------------------------------------------------------------------------- #
# Dev Entry Point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn
    # The UI's event stream never closes by itself; without a grace timeout,
    # stopping the daemon (logout, systemctl stop) waits forever for it.
    uvicorn.run(app, host="127.0.0.1", port=5001, log_level="info", reload=False,
                timeout_graceful_shutdown=3)
