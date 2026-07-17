#!/usr/bin/env python3
"""
profile_backend.py — Zola Backend Bottleneck Analysis
======================================================
Profiles the Zola daemon's HTTP pipeline using cProfile to identify CPU-bound
bottlenecks. Saves a .prof file for interactive analysis with snakeviz.

NOTE on cProfile + asyncio
--------------------------
cProfile measures cumulative wall-clock time per function call, which means
it correctly captures heavy CPU-bound work (numpy ops, Whisper inference,
evdev writes). However, it can attribute time to the asyncio event loop
machinery rather than to specific awaitables. For a cleaner async call graph,
swap to pyinstrument:

    pip install pyinstrument
    pyinstrument -m profile_backend  (or use the PROFILER env var below)

Set PROFILER=pyinstrument to switch automatically (requires: pip install pyinstrument).

USAGE:
    # Ensure daemon is NOT running (this script starts its own synthetic requests)
    source .venv/bin/activate

    # cProfile mode (default):
    python profile_backend.py

    # pyinstrument mode (cleaner async call graph):
    PROFILER=pyinstrument python profile_backend.py

    # Interactive snakeviz visualization:
    pip install snakeviz
    snakeviz /tmp/zola_profile.prof

OUTPUT:
    - Top 30 functions by cumtime (total call tree cost)
    - Top 30 functions by tottime (self cost only, excludes callees)
    - Saved to /tmp/zola_profile.prof (cProfile format)
"""

import asyncio
import cProfile
import io
import os
import pstats
import sys
import time

import httpx

BASE_URL = "http://127.0.0.1:5001"
PROFILE_OUTPUT = "/tmp/zola_profile.prof"
PROFILER_MODE = os.environ.get("PROFILER", "cprofile").lower()

BOLD  = "\033[1m"
CYAN  = "\033[96m"
GREEN = "\033[92m"
RED   = "\033[91m"
RESET = "\033[0m"


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic request workload — simulates a complete usage cycle
# ─────────────────────────────────────────────────────────────────────────────
async def run_workload():
    """
    Executes a representative request cycle to exercise all hot paths:
      1. GET /status             — state machine read
      2. GET /history?limit=50   — history list read
      3. POST /trigger/batch     — recording start
      4. Wait 0.5s               — simulate brief speech
      5. POST /trigger/batch     — recording stop + transcription pipeline
      6. GET /history?limit=50   — verify new entry
      7. POST /settings          — config update path
      8. GET /events             — SSE connection + keepalive
    """
    timeout = httpx.Timeout(30.0, connect=5.0)
    print(f"\n  {CYAN}Running synthetic workload against {BASE_URL}...{RESET}")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        steps = [
            ("GET", "/status", None),
            ("GET", "/history?limit=50", None),
            ("POST", "/trigger/batch", None),
        ]
        for method, path, body in steps:
            t0 = time.monotonic()
            try:
                if method == "GET":
                    r = await client.get(path)
                else:
                    r = await client.post(path, json=body or {})
                elapsed = (time.monotonic() - t0) * 1000
                print(f"  {method:4} {path:<30} → HTTP {r.status_code}  ({elapsed:.0f}ms)")
            except Exception as e:
                print(f"  {RED}{method} {path} FAILED: {e}{RESET}")

        # Let batch recording run briefly
        print(f"  {'---':4} {'<waiting 1.5s for batch silence>':30}")
        await asyncio.sleep(1.5)

        # Stop batch + trigger transcription pipeline
        stop_steps = [
            ("POST", "/trigger/batch", None),
            ("GET", "/history?limit=50", None),
            ("POST", "/settings", {"typing_delay_ms": 12}),
        ]
        for method, path, body in stop_steps:
            t0 = time.monotonic()
            try:
                if method == "GET":
                    r = await client.get(path)
                else:
                    r = await client.post(path, json=body or {})
                elapsed = (time.monotonic() - t0) * 1000
                print(f"  {method:4} {path:<30} → HTTP {r.status_code}  ({elapsed:.0f}ms)")
            except Exception as e:
                print(f"  {RED}{method} {path} FAILED: {e}{RESET}")

        # Brief SSE connection to profile event loop
        print(f"  {'GET':4} {'(SSE /events, 2s sample)':30}")
        t0 = time.monotonic()
        try:
            async with client.stream("GET", "/events", timeout=5.0) as stream:
                async for line in stream.aiter_lines():
                    if (time.monotonic() - t0) > 2.0:
                        break
        except Exception:
            pass
        elapsed = (time.monotonic() - t0) * 1000
        print(f"  {'   ':4} {'<SSE sample complete>':30}   ({elapsed:.0f}ms)")

    print(f"\n  {GREEN}Workload complete.{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# cProfile mode
# ─────────────────────────────────────────────────────────────────────────────
def run_with_cprofile():
    profiler = cProfile.Profile()

    print(f"\n{BOLD}[cProfile]{RESET} Profiling workload...")
    profiler.enable()
    asyncio.run(run_workload())
    profiler.disable()

    # Save binary profile
    profiler.dump_stats(PROFILE_OUTPUT)
    print(f"\n  Profile saved to: {BOLD}{PROFILE_OUTPUT}{RESET}")
    print(f"  Visualize with: {CYAN}snakeviz {PROFILE_OUTPUT}{RESET}")

    # Print text reports
    stream = io.StringIO()

    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}  TOP 30 by cumtime (total call tree cost){RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}")
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats("cumulative")
    stats.print_stats(30)
    print(stream.getvalue())

    stream.truncate(0)
    stream.seek(0)

    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}  TOP 30 by tottime (self cost only){RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}")
    stats2 = pstats.Stats(profiler, stream=stream)
    stats2.sort_stats("tottime")
    stats2.print_stats(30)
    print(stream.getvalue())

    print(f"\n{BOLD}Tip:{RESET} cProfile measures wall-clock time including asyncio machinery.")
    print(f"If async routes look suspicious, switch to pyinstrument for a cleaner graph:")
    print(f"  {CYAN}pip install pyinstrument && PROFILER=pyinstrument python profile_backend.py{RESET}\n")


# ─────────────────────────────────────────────────────────────────────────────
# pyinstrument mode (cleaner async call graph)
# ─────────────────────────────────────────────────────────────────────────────
def run_with_pyinstrument():
    try:
        from pyinstrument import Profiler
    except ImportError:
        print(f"{RED}pyinstrument not installed. Run: pip install pyinstrument{RESET}")
        sys.exit(1)

    profiler = Profiler(async_mode="enabled")
    print(f"\n{BOLD}[pyinstrument]{RESET} Profiling workload with async-aware profiler...")

    with profiler:
        asyncio.run(run_workload())

    print(profiler.output_text(unicode=True, color=True, timeline=False))

    html_path = "/tmp/zola_profile.html"
    with open(html_path, "w") as f:
        f.write(profiler.output_html())
    print(f"\n  HTML report saved to: {BOLD}{html_path}{RESET}")
    print(f"  Open with: {CYAN}xdg-open {html_path}{RESET}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║   ZOLA BACKEND PROFILER                          ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════╝{RESET}")
    print(f"  Mode: {BOLD}{PROFILER_MODE.upper()}{RESET}  |  Target: {BASE_URL}")

    # Verify daemon is reachable
    import urllib.request
    try:
        with urllib.request.urlopen(f"{BASE_URL}/status", timeout=3.0) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
        print(f"  Daemon: {GREEN}reachable{RESET}\n")
    except Exception as e:
        print(f"\n  {RED}ERROR: Daemon not reachable at {BASE_URL}: {e}{RESET}")
        print(f"  Start it first with:")
        print(f"    uvicorn app:app --host 127.0.0.1 --port 5001")
        sys.exit(1)

    if PROFILER_MODE == "pyinstrument":
        run_with_pyinstrument()
    else:
        run_with_cprofile()


if __name__ == "__main__":
    main()
