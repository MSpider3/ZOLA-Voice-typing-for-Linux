# Zola — Technical Walkthrough & Change Reference

**Project:** Zola — Voice Typing for Linux (formerly SpeakType)
**Session date:** 2026-07-17
**Scope:** Full rebranding + backend hardening + frontend resilience + stress-test tooling

---

## Table of Contents

1. [Overview](#overview)
2. [Folder Structure After Rename](#folder-structure)
3. [Phase 0 — Branding Overhaul](#phase-0)
4. [Phase 1 — Backend Daemon Hardening](#phase-1)
5. [Phase 1.5 — Test & Profiling Scripts](#phase-15)
6. [Phase 2 — Frontend Resilience](#phase-2)
7. [Verification Results](#verification)
8. [Systemd Service Template](#systemd)
9. [Usage Guide — New Scripts](#usage)

---

## 1. Overview

This session completed a three-phase full-stack QA and stabilization pass before packaging Zola as a Tauri AppImage + systemd daemon. Every user-facing string, internal class name, file path, configuration directory, kernel device name, and log message was unified under the **Zola** identity. Backend hardening addressed memory safety (zero-copy buffers, explicit GC, `__slots__`), kernel-level safety (crash-injector test path, `release_all()` validation), and environment safety (Wayland socket fallback probing). Frontend resilience eliminated layout-thrashing DOM rebuilds, added exponential backoff SSE reconnection, and prevented GPU waste when the window is hidden.

---

## 2. Folder Structure After Rename

```
speak-to_write/
├── zola-backend/          ← renamed from speaktype-backend/
│   ├── app.py
│   ├── config.py
│   ├── core_llm.py
│   ├── core_stt.py
│   ├── injector.py
│   ├── recorder.py
│   ├── state.py
│   ├── requirements.txt
│   ├── test_hammer.py     ← NEW: 5-phase async stress test suite
│   ├── profile_backend.py ← NEW: cProfile/pyinstrument profiler
│   └── walkthrough.md     ← superseded by this document
│
└── zola-frontend/         ← renamed from speaktype-frontend/
    ├── index.html
    ├── package.json
    ├── src/
    │   ├── api.ts
    │   ├── main.ts
    │   └── views/
    │       ├── dashboard.ts
    │       ├── history.ts
    │       ├── recent.ts
    │       └── settings.ts
    └── src-tauri/
        ├── tauri.conf.json
        ├── Cargo.toml
        └── src/
            ├── lib.rs
            └── main.rs
```

---

## 3. Phase 0 — Branding Overhaul (SpeakType → Zola)

Complete elimination of all `SpeakType` / `SPEAKTYPE` / `speaktype-*` strings from every source file. **Verified clean** by post-run grep — zero legacy references remain.

### 3.1 Folder Rename

```bash
mv speaktype-backend  zola-backend
mv speaktype-frontend zola-frontend
```

### 3.2 Tauri Shell (Rust / Config)

| File | Before | After |
|---|---|---|
| `tauri.conf.json` | `productName: "speaktype-frontend"` | `productName: "zola"` |
| `tauri.conf.json` | `identifier: "dev.speaktype.app"` | `identifier: "dev.zola.app"` |
| `tauri.conf.json` | `title: "SPEAKTYPE // ZOLA TERMINAL"` | `title: "ZOLA // VOICE TERMINAL"` |
| `Cargo.toml` | `name = "speaktype-frontend"` | `name = "zola-frontend"` |
| `Cargo.toml` | `name = "speaktype_frontend_lib"` | `name = "zola_frontend_lib"` |
| `main.rs` | `speaktype_frontend_lib::run()` | `zola_frontend_lib::run()` |
| `package.json` | `"name": "speaktype-frontend"` | `"name": "zola-frontend"` |

### 3.3 Frontend HTML & TypeScript

| File | What Changed |
|---|---|
| `index.html` | `<title>` → `ZOLA // VOICE TERMINAL`, `<meta description>` → `"Zola — Voice Typing for Linux"`, nav drawer `.glow-title` div → `ZOLA` |
| `api.ts` | Class `SpeakTypeAPI` → `ZolaAPI` (primary API class) |
| `main.ts` | Import, instantiation `new ZolaAPI()`, `ViewModule` interface type → `ZolaAPI` |
| `dashboard.ts` | Import + `render()` parameter type → `ZolaAPI` |
| `recent.ts` | Import + `render()` parameter type → `ZolaAPI` |
| `history.ts` | Import + module variable + `render()` parameter type → `ZolaAPI` |
| `settings.ts` | Import + module variable + `render()` parameter type → `ZolaAPI` |

### 3.4 Backend Python

| File | What Changed |
|---|---|
| `app.py` | Logger `"speaktype"` → `"zola"`, thread prefix `"speaktype-worker"` → `"zola-worker"`, FastAPI title `"SpeakType Backend"` → `"Zola Backend"`, all 4 startup/shutdown log messages, notification title → `"Zola"` |
| `config.py` | `CONFIG_DIR` → `~/.config/zola/`, `User-Agent` → `"Zola-Daemon"`, docstring, `shutil` import added |
| `injector.py` | UInput device name → `"zola-virtual-keyboard"` |
| `recorder.py` | `AUDIO_TMP_PATH` → `Path("/tmp/zola.wav")` |
| `state.py` | Default `current_audio_path` → `"/tmp/zola.wav"`, docstring |
| `requirements.txt` | Header comment → `"Zola Backend"` |

---

## 4. Phase 1 — Backend Daemon Hardening

### 4.1 Config Path Migration (`config.py`)

**Problem:** Config was at `~/.config/speaktype/settings.json`. Renaming breaks existing user settings.

**Solution:** One-time automatic migration wrapped in `try/except` so a corrupted legacy file or permission denial cannot crash the daemon on its very first boot under the new name.

```python
# New constants:
CONFIG_DIR = Path.home() / ".config" / "zola"
CONFIG_PATH = CONFIG_DIR / "settings.json"
_LEGACY_CONFIG_PATH = Path.home() / ".config" / "speaktype" / "settings.json"

# Added to load_config() before reading CONFIG_PATH:
try:
    if _LEGACY_CONFIG_PATH.exists() and not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_CONFIG_PATH, CONFIG_PATH)
        logger.info("Migrated legacy config: %s → %s", _LEGACY_CONFIG_PATH, CONFIG_PATH)
except Exception as exc:
    logger.error("Failed to migrate legacy config (continuing with defaults): %s", exc)
```

The `try/except Exception` catches `OSError` (permissions), `shutil.Error` (disk full), and any other failure, always falling through to load defaults instead of crashing.

---

### 4.2 Debug Crash-Injector Endpoint (`app.py`)

**Problem:** No reproducible way to verify that `release_all()` fires correctly during an exception, preventing modifier keys (Shift, Ctrl, etc.) from getting stuck at the kernel level.

**Solution:** A conditional `POST /debug/crash-injector` endpoint that intentionally crashes mid-`type_text()`.

**Safety:** The route is registered **only when `ZOLA_DEBUG=1`** at startup time. Without this variable, the route does not exist in FastAPI's router at all — a scanner hitting it receives a standard `HTTP 404 Not Found` with no information leakage.

```python
import os as _os
if _os.environ.get("ZOLA_DEBUG") == "1":
    @app.post("/debug/crash-injector", tags=["debug"])
    async def debug_crash_injector(request: Request) -> JSONResponse:
        injector: KeystrokeInjector = request.app.state.injector
        try:
            injector.type_text("ZOLA_CRASH_TEST_", 12)  # partial injection
            raise RuntimeError("INTENTIONAL CRASH — verifying release_all()")
        except RuntimeError:
            injector.release_all()  # must clear all stuck modifier keys
            raise HTTPException(status_code=500,
                detail="INTENTIONAL_CRASH_OK — release_all() executed.")
```

**Verification:**
```bash
# Production (no ZOLA_DEBUG):
curl -X POST http://127.0.0.1:5001/debug/crash-injector
# → HTTP 404 Not Found

# Debug mode:
ZOLA_DEBUG=1 uvicorn app:app --host 127.0.0.1 --port 5001
curl -X POST http://127.0.0.1:5001/debug/crash-injector
# → HTTP 500: {"detail": "INTENTIONAL_CRASH_OK — release_all() executed."}
# → Type normally in any window to confirm no stuck modifier keys
```

---

### 4.3 Wayland Environment Fallback Probe (`injector.py`)

**Problem:** When running as a `systemd --user` service, `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR` may be absent if `systemctl --user import-environment` was not called, causing `wl-copy` to silently fail.

**Solution:** Two-stage fallback in `_capture_wayland_env()`:

**Stage 1 — `XDG_RUNTIME_DIR`:** If missing, probe `/run/user/<uid>/` (the standard XDG location on all systemd-based distros).

**Stage 2 — `WAYLAND_DISPLAY`:** If still missing after stage 1, glob `$XDG_RUNTIME_DIR/wayland-*` and use the first valid socket file found (typically `wayland-0` or `wayland-1`). Iterates via `sorted()` so `wayland-0` is always tried before `wayland-1`.

```python
if "XDG_RUNTIME_DIR" not in env:
    uid = os.getuid()
    xdg_fallback = f"/run/user/{uid}"
    if os.path.isdir(xdg_fallback):
        env["XDG_RUNTIME_DIR"] = xdg_fallback

if "WAYLAND_DISPLAY" not in env:
    runtime = env.get("XDG_RUNTIME_DIR", "")
    for sock in sorted(Path(runtime).glob("wayland-*")):
        try:
            if sock.is_socket():
                env["WAYLAND_DISPLAY"] = sock.name  # e.g. "wayland-0"
                break
        except OSError:
            continue
```

Both successful probes log at `INFO`. Failed probes log a `WARNING` with the exact `systemctl --user import-environment` command needed.

---

### 4.4 `__slots__` on AppState (`state.py`)

**Problem:** Python classes without `__slots__` allocate a `__dict__` per instance, enabling accidental attribute creation (silent bugs) and consuming unnecessary heap memory.

**Solution:**
```python
class AppState:
    __slots__ = (
        '_lock', '_history_max', '_start_time',
        'is_recording', 'active_mode', 'current_audio_path',
        'realtime_task', 'history',
    )
```

**Benefits:**
- Eliminates per-instance `__dict__` allocation
- `state.is_recoding = True` (typo) now raises `AttributeError` immediately instead of silently going nowhere
- Locks down the state machine's memory footprint permanently

---

### 4.5 Zero-Copy Audio Session Buffer (`recorder.py`)

**Problem:** In `stream_utterances()`, every audio frame was `.copy()`'d twice — once into `utterance_buf` (needed) and once into `self._buffer` (redundant, queue already released ownership).

**Before:**
```python
utterance_buf.append(frame.copy())  # copy #1 — needed
self._buffer.append(frame.copy())   # copy #2 — redundant allocation
```

**After:**
```python
utterance_buf.append(frame.copy())  # copy #1 — needed (concatenated + freed later)
self._buffer.append(frame)          # zero-copy — frame already owned by this scope
```

At 16kHz mono int16, each 100ms frame ≈ 3.2 KB. Over a 60s session, eliminates ~192 KB of redundant allocation plus the GC overhead of collecting those objects.

---

### 4.6 Tracemalloc Profiling in recorder.py (`ZOLA_DEBUG=1`)

When `ZOLA_DEBUG=1`, the recorder takes Python `tracemalloc` memory snapshots:

- **`start()`** — baseline snapshot before mic opens
- **`stop()`** — diff snapshot, logs top-5 memory-growing allocations with source line numbers and size

```bash
ZOLA_DEBUG=1 uvicorn app:app --host 127.0.0.1 --port 5001 --log-level debug 2>&1 | \
  grep -E "ZOLA_DEBUG|memory diff"
```

---

### 4.7 Explicit GC After Whisper Inference (`core_stt.py`)

**Problem:** Whisper produces large intermediate numpy arrays that Python's cyclic GC may not reclaim promptly, causing RSS memory spikes between sessions.

**Solution:** Explicit `del` + `gc.collect()` at the end of both transcription methods:

```python
# transcribe_file():
del audio, transcripts
gc.collect()

# transcribe_chunk():
del audio
gc.collect()
```

> `gc.collect()` is a no-op when there are no reference cycles — zero performance cost when nothing needs collecting. It is a proactive signal to check now rather than wait for the next scheduled pass.

---

## 5. Phase 1.5 — Test & Profiling Scripts

### 5.1 `test_hammer.py` — Backend Stress Test Suite

**Location:** `zola-backend/test_hammer.py`
**Dependencies:** `httpx` (already in `requirements.txt`)

| # | Test | Assertion |
|---|---|---|
| 1 | **Status Hammer** | 200 concurrent `GET /status` — all return 200, no deadlocks |
| 2 | **Concurrent Trigger Flood** | 50 simultaneous POSTs — exactly ≥1 success, rest = 409, no 500s |
| 3 | **Toggle Stress** | 20 rapid start/stop cycles per mode at 100ms — no HTTP 500s |
| 4 | **SSE Client Flood** | 10 concurrent SSE connections — ≥5 receive at least 1 event |
| 5 | **Crash Injector** | 404 in prod, 500+`INTENTIONAL_CRASH_OK` in debug mode |

Reports p50/p95/p99 latency percentiles per test. Exit code 0 = all pass, 1 = any failure.

```bash
cd zola-backend && source .venv/bin/activate
# Start daemon in one terminal, then:
python test_hammer.py
```

---

### 5.2 `profile_backend.py` — Bottleneck Profiler

**Location:** `zola-backend/profile_backend.py`

Dual-mode profiler with a synthetic full-cycle workload:

```bash
# cProfile mode (default):
python profile_backend.py
# → top 30 functions by cumtime + tottime
# → saves /tmp/zola_profile.prof

# pyinstrument mode (async-aware, cleaner call graph):
PROFILER=pyinstrument python profile_backend.py
# → saves /tmp/zola_profile.html

# Interactive visualization:
pip install snakeviz && snakeviz /tmp/zola_profile.prof
```

> **cProfile + asyncio note:** cProfile correctly captures CPU-bound bottlenecks (numpy, Whisper CTranslate2, evdev). However, it attributes time to the event loop machinery rather than specific awaitables. If async route handlers look suspicious in the output, switch to `pyinstrument` which natively understands coroutine context switching.

---

## 6. Phase 2 — Frontend Resilience

### 6.1 SSE Exponential Backoff (`api.ts`)

**Before:** Fixed 3s reconnect delay, regardless of consecutive failures.

**After:** Exponential backoff with ±1s random jitter, capped at 15s.

| Attempt | Base | With jitter |
|---|---|---|
| 1 | 1s | 1–2s |
| 2 | 2s | 2–3s |
| 3 | 4s | 4–5s |
| 4 | 8s | 8–9s |
| 5+ | 15s (cap) | 15–16s |

**Key detail:** `OFFLINE` fires **immediately** on `onerror` before the backoff timer starts — the UI badge turns red with zero artificial delay. `RECONNECTING` fires just before the next connection attempt. Counter resets to 0 on `onopen`.

---

### 6.2 Canvas Visibility Throttling (`dashboard.ts`)

**Problem:** `requestAnimationFrame` loop ran unconditionally — wasting GPU compositor time when the window was minimized.

**Solution:** Page Visibility API with blank-before-pause:

1. `visibilitychange` listener: draw **solid black frame** before cancelling `rAF` (prevents frozen-frame artifact in Wayland compositor thumbnails)
2. `document.hidden` guard at top of `loop()` (double-safety if a queued frame fires during transition)
3. Listener removed by reference in `destroy()` to prevent orphaned handlers on view navigation

```typescript
visibilityHandler = () => {
  if (document.hidden) {
    blankCanvas(ctx, canvas.width, canvas.height); // solid black
    cancelAnimationFrame(animFrameId!);
    animFrameId = null;
  } else {
    if (animFrameId === null && canvas.parentElement) loop();
  }
};
```

---

### 6.3 Targeted DOM Updates (`recent.ts`)

**Problem:** Every SSE event during live streaming triggered a full `innerHTML` rebuild — layout recalculation on every character.

**Solution:** Two-path rendering:

| Condition | Method | Cost |
|---|---|---|
| First render or live↔static mode transition | `buildFullDOM()` — full `innerHTML` | One-time layout |
| Same mode (text/metrics update) | `patchDOM()` — `.textContent` only | Zero layout cost |

Targets updated in `patchDOM()`: `#recent-live-text`, `#recent-mode`, `#recent-metrics`, `#recent-duration`.

**Display cap:** DOM shows last 50,000 characters when text exceeds cap. **Full text always kept in memory** and used for clipboard copy (so clipboard is never truncated).

---

### 6.4 Debounced History Refresh (`history.ts`)

**Problem:** `loadAndRenderHistory()` (a network fetch) called on every SSE event — unnecessary during live streaming.

**Solution:** 500ms debounce. Only the last event in each burst triggers a fetch.

```typescript
updateState(_state: AppState) {
  if (historyDebounceTimer) clearTimeout(historyDebounceTimer);
  historyDebounceTimer = window.setTimeout(() => {
    loadAndRenderHistory();
    historyDebounceTimer = null;
  }, 500);
},
destroy() {
  if (historyDebounceTimer) clearTimeout(historyDebounceTimer); // prevent post-destroy fetch
}
```

DOM cap: table rows capped at 100 (`slice(0, 100)` after sorting).

---

## 7. Verification Results

| Check | Result |
|---|---|
| `npx tsc --noEmit` | ✓ **ZERO TypeScript errors** |
| Branding grep (source files) | ✓ **ZERO legacy SpeakType references** |
| Internal path grep (source files) | ✓ **ZERO legacy speaktype-backend/frontend paths** |

---

## 8. Systemd Service Template

`~/.config/systemd/user/zola-backend.service`:

```ini
[Unit]
Description=Zola Voice Typing Daemon
After=graphical-session.target pipewire.service
Wants=pipewire.service

[Service]
Type=simple
WorkingDirectory=%h/Projects/speak-to_write/zola-backend
ExecStart=%h/Projects/speak-to_write/zola-backend/.venv/bin/uvicorn \
    app:app --host 127.0.0.1 --port 5001

# Inherit Wayland compositor env (critical for wl-copy clipboard)
Environment=PYTHONUNBUFFERED=1
PassEnvironment=WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS

Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now zola-backend.service
journalctl --user -u zola-backend.service -f

# If wl-copy clipboard doesn't work from the service:
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR
systemctl --user restart zola-backend.service

# Debug mode (enables tracemalloc + crash-injector):
# Add to [Service]: Environment=ZOLA_DEBUG=1
```

---

## 9. Usage Guide — New Scripts

### Stress Test

```bash
cd ~/Projects/speak-to_write/zola-backend
source .venv/bin/activate

# Terminal 1:
uvicorn app:app --host 127.0.0.1 --port 5001

# Terminal 2:
python test_hammer.py

# With crash-injector enabled:
ZOLA_DEBUG=1 uvicorn app:app --host 127.0.0.1 --port 5001  # T1
python test_hammer.py                                         # T2
```

### Profiler

```bash
cd ~/Projects/speak-to_write/zola-backend && source .venv/bin/activate

# Ensure daemon is running, then:
python profile_backend.py                      # cProfile
PROFILER=pyinstrument python profile_backend.py  # async-aware
snakeviz /tmp/zola_profile.prof                # interactive viz
```

### SSE Reconnection Manual Test

1. Start daemon + Zola UI → status = **STANDBY** (green)
2. `pkill -f "uvicorn app:app"` → UI = **OFFLINE** (red, immediate)
3. Wait 2–3s → UI = **RECONNECTING** (amber)
4. Restart daemon → UI = **STANDBY** (green)
5. Console: `[Zola SSE] Reconnect attempt #1 in 1234ms` → resets to 0

### Canvas Visibility Test

1. Open dashboard → oscilloscope animating
2. Minimize Tauri window → `htop` shows WebView CPU ≈ 0%
3. Restore → animation resumes, no frozen-frame artifact
4. Window thumbnail (taskbar hover) shows clean black, not a frozen waveform
