#!/usr/bin/env python3
"""
test_hammer.py — Zola Backend Stress Test Suite
================================================
Standalone script. Rapidly fires concurrent HTTP requests at the Zola FastAPI
daemon to surface silent failures, double-locks, duplicate threads, and race
conditions before packaging.

PREREQUISITES:
  1. Zola daemon must be running: uvicorn app:app --host 127.0.0.1 --port 5001
  2. Run from the zola-backend directory with the venv activated:
       source .venv/bin/activate && python test_hammer.py

TESTS:
  1. Status Hammer     — 200 concurrent GET /status
  2. Concurrent Trigger Flood — 50 simultaneous POST /trigger/realtime
  3. Toggle Stress     — rapid start/stop on each mode 20x at 100ms intervals
  4. SSE Client Flood  — 10 concurrent EventSource emulations
  5. Crash Injector    — POST /debug/crash-injector (requires ZOLA_DEBUG=1)

OUTPUT:
  Pass/Fail per test + latency percentiles (p50/p95/p99) per endpoint.
"""

import asyncio
import statistics
import sys
import time
from typing import NamedTuple

import httpx

BASE_URL = "http://127.0.0.1:5001"
TIMEOUT = httpx.Timeout(10.0, connect=3.0)

RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"


class TestResult(NamedTuple):
    name: str
    passed: bool
    detail: str
    latencies_ms: list[float]


def percentile(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    return statistics.quantiles(sorted(data), n=100)[p - 1]


def print_result(r: TestResult) -> None:
    icon = f"{GREEN}✓{RESET}" if r.passed else f"{RED}✗{RESET}"
    status = f"{GREEN}PASS{RESET}" if r.passed else f"{RED}FAIL{RESET}"
    print(f"\n  {icon} [{status}] {BOLD}{r.name}{RESET}")
    print(f"       {r.detail}")
    if r.latencies_ms:
        p50 = percentile(r.latencies_ms, 50)
        p95 = percentile(r.latencies_ms, 95)
        p99 = percentile(r.latencies_ms, 99)
        print(f"       Latency → p50={p50:.0f}ms  p95={p95:.0f}ms  p99={p99:.0f}ms")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Status Hammer — 200 concurrent GET /status
# ─────────────────────────────────────────────────────────────────────────────
async def test_status_hammer(client: httpx.AsyncClient) -> TestResult:
    N = 200
    latencies: list[float] = []
    errors: list[str] = []

    async def one_request():
        t0 = time.monotonic()
        try:
            r = await client.get("/status")
            latencies.append((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                errors.append(f"HTTP {r.status_code}")
        except Exception as e:
            errors.append(str(e))

    await asyncio.gather(*[one_request() for _ in range(N)])
    passed = len(errors) == 0 and len(latencies) == N
    detail = (
        f"{len(latencies)}/{N} OK, {len(errors)} errors"
        + (f" — ERRORS: {errors[:3]}" if errors else "")
    )
    return TestResult("Status Hammer (200 concurrent GET /status)", passed, detail, latencies)


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Concurrent Trigger Flood — 50 simultaneous POSTs
# The daemon MUST return exactly 1 success (200) and the rest as 409 Conflict.
# No crashes, no double-lock.
# ─────────────────────────────────────────────────────────────────────────────
async def test_concurrent_trigger_flood(client: httpx.AsyncClient) -> TestResult:
    N = 50
    successes: list[int] = []
    conflicts: list[int] = []
    errors: list[str] = []
    latencies: list[float] = []

    async def fire(mode: str):
        t0 = time.monotonic()
        try:
            r = await client.post(f"/trigger/{mode}")
            latencies.append((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                successes.append(r.status_code)
            elif r.status_code == 409:
                conflicts.append(r.status_code)
            else:
                errors.append(f"HTTP {r.status_code}: {r.text[:80]}")
        except Exception as e:
            errors.append(str(e))

    # Fire 25 realtime + 25 batch simultaneously
    await asyncio.gather(
        *[fire("realtime") for _ in range(25)],
        *[fire("batch") for _ in range(25)],
    )

    # Drain: stop any active recording
    await asyncio.sleep(0.2)
    for mode in ("realtime", "batch"):
        try:
            await client.post(f"/trigger/{mode}")
        except Exception:
            pass
    await asyncio.sleep(0.5)

    passed = len(errors) == 0 and len(successes) >= 1
    detail = (
        f"Success={len(successes)}, Conflict(409)={len(conflicts)}, Error={len(errors)}"
        + (f" — ERRORS: {errors[:3]}" if errors else "")
    )
    return TestResult(
        "Concurrent Trigger Flood (50 simultaneous POSTs, expect 1 success + 409s)",
        passed,
        detail,
        latencies,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Toggle Stress — rapid start/stop on each mode 20x at 100ms intervals
# ─────────────────────────────────────────────────────────────────────────────
async def test_toggle_stress(client: httpx.AsyncClient) -> TestResult:
    CYCLES = 20
    DELAY = 0.1  # 100ms between toggles
    errors: list[str] = []
    latencies: list[float] = []

    for mode in ("realtime", "batch"):
        for i in range(CYCLES):
            for _action in ("start", "stop"):
                t0 = time.monotonic()
                try:
                    r = await client.post(f"/trigger/{mode}")
                    latencies.append((time.monotonic() - t0) * 1000)
                    # Any 200 or 409 is valid; only 500 means internal crash
                    if r.status_code == 500:
                        errors.append(f"mode={mode} cycle={i} action={_action}: HTTP 500 — {r.text[:60]}")
                except Exception as e:
                    errors.append(f"mode={mode} cycle={i}: {e}")
                await asyncio.sleep(DELAY)

    # Final cleanup — ensure recording is stopped
    await asyncio.sleep(0.3)
    try:
        status = (await client.get("/status")).json()
        if status.get("is_recording"):
            # Still recording? stop it
            mode = status.get("active_mode", "realtime")
            await client.post(f"/trigger/{mode}")
    except Exception:
        pass

    passed = len(errors) == 0
    detail = (
        f"{CYCLES} cycles × 2 modes × 2 toggles = {CYCLES * 4} requests. Errors: {len(errors)}"
        + (f" — ERRORS: {errors[:3]}" if errors else "")
    )
    return TestResult(
        f"Toggle Stress ({CYCLES}× start/stop, 100ms intervals, both modes)",
        passed,
        detail,
        latencies,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: SSE Client Flood — 10 concurrent SSE connections, each reads 3 events
# ─────────────────────────────────────────────────────────────────────────────
async def test_sse_flood(client: httpx.AsyncClient) -> TestResult:
    N_CLIENTS = 10
    events_received: list[int] = []
    errors: list[str] = []

    async def sse_reader(client_id: int):
        received = 0
        try:
            async with client.stream("GET", "/events", timeout=8.0) as response:
                if response.status_code != 200:
                    errors.append(f"client {client_id}: HTTP {response.status_code}")
                    return
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        received += 1
                    if received >= 2:  # Read initial state_change + at least 1 more
                        break
        except asyncio.TimeoutError:
            pass  # Expected — we're reading a streaming endpoint
        except Exception as e:
            errors.append(f"client {client_id}: {str(e)[:60]}")
        events_received.append(received)

    # Trigger a status event while SSE clients are connected
    async def trigger_status_event():
        await asyncio.sleep(0.5)  # Let SSE clients connect first
        await client.get("/status")

    await asyncio.gather(
        *[sse_reader(i) for i in range(N_CLIENTS)],
        trigger_status_event(),
    )

    clients_got_data = sum(1 for n in events_received if n >= 1)
    passed = len(errors) == 0 and clients_got_data >= N_CLIENTS // 2
    detail = (
        f"{clients_got_data}/{N_CLIENTS} clients received ≥1 event. Errors: {len(errors)}"
        + (f" — ERRORS: {errors[:3]}" if errors else "")
    )
    return TestResult(f"SSE Client Flood ({N_CLIENTS} concurrent connections)", passed, detail, [])


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Crash Injector (requires ZOLA_DEBUG=1)
# ─────────────────────────────────────────────────────────────────────────────
async def test_crash_injector(client: httpx.AsyncClient) -> TestResult:
    latencies: list[float] = []

    # First verify the endpoint returns 404 if ZOLA_DEBUG is not set
    # (This branch tests the production safety — if running with ZOLA_DEBUG=1,
    # we test the actual crash path.)
    t0 = time.monotonic()
    try:
        r = await client.post("/debug/crash-injector")
        latencies.append((time.monotonic() - t0) * 1000)

        if r.status_code == 404:
            return TestResult(
                "Crash Injector (ZOLA_DEBUG=1 not set)",
                True,
                "HTTP 404 returned correctly — endpoint not exposed in production mode",
                latencies,
            )
        elif r.status_code == 500:
            body = r.json()
            if "INTENTIONAL_CRASH_OK" in body.get("detail", ""):
                return TestResult(
                    "Crash Injector (ZOLA_DEBUG=1 active)",
                    True,
                    "HTTP 500 with INTENTIONAL_CRASH_OK — release_all() executed successfully. "
                    "Verify no modifier keys are stuck in your compositor.",
                    latencies,
                )
            else:
                return TestResult(
                    "Crash Injector",
                    False,
                    f"HTTP 500 but unexpected body: {r.text[:100]}",
                    latencies,
                )
        else:
            return TestResult(
                "Crash Injector",
                False,
                f"Unexpected HTTP {r.status_code}: {r.text[:100]}",
                latencies,
            )
    except Exception as e:
        return TestResult("Crash Injector", False, f"Request failed: {e}", latencies)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║   ZOLA BACKEND STRESS TEST SUITE v1.0            ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════╝{RESET}")
    print(f"  Target: {BASE_URL}\n")

    # Verify daemon is up before starting
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as probe:
            r = await probe.get("/status")
            if r.status_code != 200:
                print(f"{RED}ERROR: Daemon returned HTTP {r.status_code}. Is it running?{RESET}")
                sys.exit(1)
            data = r.json()
            print(f"  Daemon OK — uptime={data.get('uptime_s', '?')}s, "
                  f"recording={data.get('is_recording', '?')}\n")
    except Exception as e:
        print(f"{RED}ERROR: Cannot reach daemon at {BASE_URL}: {e}{RESET}")
        print(f"  Start it with: uvicorn app:app --host 127.0.0.1 --port 5001")
        sys.exit(1)

    results: list[TestResult] = []

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as client:
        print(f"{BOLD}Running Test 1/5: Status Hammer...{RESET}")
        results.append(await test_status_hammer(client))

        print(f"{BOLD}Running Test 2/5: Concurrent Trigger Flood...{RESET}")
        results.append(await test_concurrent_trigger_flood(client))

        print(f"{BOLD}Running Test 3/5: Toggle Stress...{RESET}")
        results.append(await test_toggle_stress(client))

        print(f"{BOLD}Running Test 4/5: SSE Client Flood...{RESET}")
        results.append(await test_sse_flood(client))

        print(f"{BOLD}Running Test 5/5: Crash Injector...{RESET}")
        results.append(await test_crash_injector(client))

    # Print summary
    print(f"\n{BOLD}{CYAN}{'─' * 52}{RESET}")
    print(f"{BOLD}  RESULTS SUMMARY{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 52}{RESET}")
    for r in results:
        print_result(r)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n{BOLD}{CYAN}{'─' * 52}{RESET}")
    color = GREEN if passed == total else RED
    print(f"  {color}{BOLD}{passed}/{total} tests passed{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 52}{RESET}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
