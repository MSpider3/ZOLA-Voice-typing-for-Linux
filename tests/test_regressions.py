"""
test_regressions.py — Config and trigger regression tests
==========================================================
No daemon, microphone or model needed. Run from the repo root:
  zola-backend/.venv/bin/python -m unittest tests.test_regressions -v
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zola-backend"))

import config  # noqa: E402
import app as zapp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _TempConfig(unittest.TestCase):
    """Point config.py at a throwaway directory instead of ~/.config/zola."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (config.CONFIG_DIR, config.CONFIG_PATH, config._LEGACY_CONFIG_PATH)
        config.CONFIG_DIR = Path(self._tmp.name)
        config.CONFIG_PATH = config.CONFIG_DIR / "settings.json"
        config._LEGACY_CONFIG_PATH = config.CONFIG_DIR / "no-legacy.json"

    def tearDown(self) -> None:
        config.CONFIG_DIR, config.CONFIG_PATH, config._LEGACY_CONFIG_PATH = self._saved
        self._tmp.cleanup()


class ConfigTests(_TempConfig):
    def test_defaults_load_unchanged(self) -> None:
        cfg = config.load_config()
        self.assertEqual(cfg.realtime_force_commit_s, config._DEFAULTS["realtime_force_commit_s"])

    def test_user_choices_survive_restart(self) -> None:
        cfg = config.load_config()
        threads = min(4, os.cpu_count() or 1)
        config.update_config(cfg, {
            "whisper_batch": "large-v3-turbo",
            "cpu_threads": threads,
            "realtime_force_commit_s": 10.0,
        })
        reloaded = config.load_config()
        self.assertEqual(reloaded.whisper_batch, "large-v3-turbo")
        self.assertEqual(reloaded.cpu_threads, threads)
        self.assertEqual(reloaded.realtime_force_commit_s, 10.0)

    def test_wrong_types_rejected_and_rolled_back(self) -> None:
        cfg = config.load_config()
        for bad in (
            {"cpu_threads": "4"},
            {"initial_prompt": 123},
            {"realtime_chunk_s": "x"},
            {"typing_delay_ms": True},
            {"sample_rate": 48000},
            {"compute_type": "float16"},
            {"output_language": "hindi"},
        ):
            before = cfg.to_dict()
            with self.assertRaises(ValueError, msg=str(bad)):
                config.update_config(cfg, dict(bad))
            self.assertEqual(cfg.to_dict(), before, msg=str(bad))

    def test_invalid_file_is_backed_up_before_reset(self) -> None:
        config.CONFIG_PATH.write_text("{ not json", encoding="utf-8")
        config.load_config()
        backup = config.CONFIG_PATH.with_suffix(".json.bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), "{ not json")


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def start(self, audio_path) -> None:
        if self.fail:
            raise RuntimeError("no mic")

    def request_stop(self) -> None:
        pass

    def stop(self):
        return Path("/nonexistent"), 0.0

    async def monitor_until_silence(self, **kwargs) -> None:
        await asyncio.sleep(3600)


class _LLM:
    model_name = "x"
    output_language = "preserve"


class ApiTests(_TempConfig):
    def setUp(self) -> None:
        super().setUp()
        zapp.app.state.config = config.load_config()
        zapp.app.state.app_state = zapp.AppState()
        zapp.app.state.recorder = _Recorder()
        zapp.app.state.llm = _LLM()
        zapp.app.state.stt = None       # only touched when a model setting changes
        zapp.app.state.injector = None  # only touched once a transcript exists
        zapp._last_trigger_time = 0.0
        # No context manager: the lifespan (models, uinput) is not run.
        self.client = TestClient(zapp.app, base_url="http://127.0.0.1:5001")

    def test_trigger_dispatches_to_active_mode(self) -> None:
        r = self.client.post("/trigger")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"status": "started", "mode": "batch"})

    def test_trigger_is_still_rate_limited(self) -> None:
        self.client.post("/trigger")
        self.assertEqual(self.client.post("/trigger").status_code, 429)

    def test_mic_failure_does_not_leave_recording_state(self) -> None:
        zapp.app.state.recorder = _Recorder(fail=True)
        self.assertEqual(self.client.post("/trigger/batch").status_code, 503)
        self.assertFalse(zapp.app.state.app_state.is_recording)
        zapp.app.state.recorder = _Recorder()
        time.sleep(zapp._TRIGGER_COOLDOWN_S)
        self.assertEqual(self.client.post("/trigger/batch").json()["status"], "started")

    def test_foreign_origin_is_rejected(self) -> None:
        r = self.client.post(
            "/settings",
            content='{"active_mode":"realtime"}',
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(zapp.app.state.config.active_mode, "batch")
        self.assertEqual(
            self.client.post("/trigger", headers={"Origin": "https://evil.example"}).status_code,
            403,
        )

    def test_tauri_origin_and_no_origin_are_allowed(self) -> None:
        r = self.client.post(
            "/settings", json={"active_mode": "realtime"},
            headers={"Origin": "tauri://localhost"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/status").status_code, 200)

    def test_foreign_host_header_is_rejected(self) -> None:
        r = self.client.get("/status", headers={"Host": "evil.example:5001"})
        self.assertEqual(r.status_code, 400)

    def test_bad_setting_type_returns_422(self) -> None:
        r = self.client.post("/settings", json={"cpu_threads": "4"})
        self.assertEqual(r.status_code, 422)
        self.assertIsInstance(zapp.app.state.config.cpu_threads, int)


if __name__ == "__main__":
    unittest.main()
