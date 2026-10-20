"""
test_regressions.py — Config and trigger regression tests
==========================================================
No daemon, microphone or model needed. Run from the repo root:
  zola-backend/.venv/bin/python -m unittest tests.test_regressions -v
"""

import asyncio
import json
import os
import sys
import tempfile
import time

import numpy as np
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zola-backend"))

import config  # noqa: E402
import app as zapp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _TempConfig(unittest.TestCase):
    """Point config.py (and recording temp files) at a throwaway directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (config.CONFIG_DIR, config.CONFIG_PATH, config._LEGACY_CONFIG_PATH)
        config.CONFIG_DIR = Path(self._tmp.name)
        config.CONFIG_PATH = config.CONFIG_DIR / "settings.json"
        config._LEGACY_CONFIG_PATH = config.CONFIG_DIR / "no-legacy.json"
        # state.make_audio_tmp_path() would otherwise create files in the real
        # $XDG_RUNTIME_DIR/zola
        self._saved_runtime = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        config.CONFIG_DIR, config.CONFIG_PATH, config._LEGACY_CONFIG_PATH = self._saved
        if self._saved_runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._saved_runtime
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

    def test_one_invalid_setting_keeps_the_others(self) -> None:
        cfg = config.load_config()
        config.update_config(cfg, {"paste_method": "shift_insert", "replacements": {"STD": "STT"}})
        saved = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        saved["whisper_batch"] = "deleted-model-folder"
        config.CONFIG_PATH.write_text(json.dumps(saved), encoding="utf-8")
        reloaded = config.load_config()
        self.assertEqual(reloaded.whisper_batch, config._DEFAULTS["whisper_batch"])
        self.assertEqual(reloaded.paste_method, "shift_insert")
        self.assertEqual(reloaded.replacements, {"STD": "STT"})

    def test_invalid_file_is_backed_up_before_reset(self) -> None:
        config.CONFIG_PATH.write_text("{ not json", encoding="utf-8")
        config.load_config()
        backup = config.CONFIG_PATH.with_suffix(".json.bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), "{ not json")


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def start(self, audio_path, device=None) -> None:
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
        zapp.app.state.config.audio_feedback = False  # no tones from the test run
        zapp.app.state.app_state = zapp.AppState()
        zapp.app.state.recorder = _Recorder()
        zapp.app.state.llm = _LLM()
        zapp.app.state.stt = object()   # "models loaded"; only touched when a model setting changes
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

    def test_dictation_waits_for_models_to_load(self) -> None:
        zapp.app.state.stt = None  # still loading (first start may download models)
        self.assertFalse(self.client.get("/status").json()["models_ready"])
        self.assertEqual(self.client.post("/trigger/batch").status_code, 503)
        self.assertFalse(zapp.app.state.app_state.is_recording)
        r = self.client.post("/settings", json={"whisper_batch": "base"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.post("/settings", json={"paste_method": "shift_insert"}).status_code, 200)

    def test_cancel_discards_recording(self) -> None:
        self.assertEqual(self.client.post("/cancel").json(), {"status": "idle"})

        # TestClient runs each request on its own loop, so drive the handler directly
        async def scenario():
            st = zapp.AppState()
            await st.start_recording(zapp.RecordingMode.BATCH)
            task = asyncio.create_task(asyncio.sleep(3600))
            await st.set_realtime_task(task)
            zapp.app.state.app_state = st

            class _Req:
                app = zapp.app
            response = await zapp.cancel_recording(_Req())
            await asyncio.sleep(0)
            return response.body, task.cancelled()

        body, was_cancelled = asyncio.run(scenario())
        self.assertEqual(body, b'{"status":"cancelled"}')
        self.assertTrue(was_cancelled)

    def test_invalid_new_settings_rejected(self) -> None:
        for bad in ({"paste_method": "alt_v"}, {"replacements": {"a": 1}},
                    {"llm_api": "cloud"}, {"input_device": 3}):
            self.assertEqual(self.client.post("/settings", json=bad).status_code, 422, bad)
        r = self.client.post("/settings", json={"replacements": {"STD": "STT"}, "paste_method": "shift_insert"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_bad_setting_type_returns_422(self) -> None:
        r = self.client.post("/settings", json={"cpu_threads": "4"})
        self.assertEqual(r.status_code, 422)
        self.assertIsInstance(zapp.app.state.config.cpu_threads, int)


class TextAndHistoryTests(unittest.TestCase):
    def test_replacements_whole_word_case_insensitive(self) -> None:
        from core_stt import apply_replacements
        self.assertEqual(apply_replacements("an STD test, std.", {"std": "STT"}), "an STT test, STT.")
        self.assertEqual(apply_replacements("standard", {"std": "STT"}), "standard")
        self.assertEqual(apply_replacements("मेरा फोन", {"फोन": "phone"}), "मेरा phone")

    def test_repetition_loop_is_cut(self) -> None:
        from core_stt import _REPEAT_RE
        loop = "आवाज़ " * 8 + "end"
        self.assertEqual(_REPEAT_RE.sub(r"\1 \1", loop), "आवाज़ आवाज़ end")
        self.assertEqual(_REPEAT_RE.sub(r"\1 \1", "hello hello mic"), "hello hello mic")

    def test_pinned_model_language(self) -> None:
        import core_stt
        with tempfile.TemporaryDirectory() as tmp:
            saved = core_stt.MODELS_DIR
            core_stt.MODELS_DIR = Path(tmp)
            core_stt._model_meta.cache_clear()
            try:
                (Path(tmp) / "hinglish").mkdir()
                (Path(tmp) / "hinglish" / "zola.json").write_text('{"language": "en"}')
                self.assertEqual(core_stt._pinned_language("hinglish"), "en")
                self.assertIsNone(core_stt._pinned_language("small"))
            finally:
                core_stt.MODELS_DIR = saved
                core_stt._model_meta.cache_clear()

    def test_llm_answers_and_rewrites_are_rejected(self) -> None:
        from core_llm import _plausible_edit
        question = "can you tell me what is the capital of france i need it for the report"
        self.assertFalse(_plausible_edit(question, "The capital of France is Paris.", "english"))
        self.assertTrue(_plausible_edit(question, "Can you tell me what the capital of France is? I need it for the report.", "english"))
        hinglish = "main English aur hindi donon mein baar baar switch karta raha hoon yah dekhane ke lie"
        self.assertFalse(_plausible_edit(hinglish, "Main Hindi aur English, agar phirka aapki saaree kya ho jaata hai.", "preserve"))
        self.assertTrue(_plausible_edit(hinglish, "Main English aur Hindi donon mein baar-baar switch karta raha hoon, yah dekhane ke lie.", "preserve"))

    def test_history_survives_restart_and_clears(self) -> None:
        from state import AppState, RecordingMode
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            first = AppState(history_max=5, history_path=path)
            asyncio.run(first.add_history(RecordingMode.BATCH, "namaste", 1.0))
            second = AppState(history_max=5, history_path=path)
            self.assertEqual(second.history[-1]["transcript"], "namaste")
            asyncio.run(second.clear_history())
            self.assertEqual(AppState(history_max=5, history_path=path).history, [])


class ModelStoreTests(unittest.TestCase):
    """Catalog downloads, without network: the source fetch is replaced."""

    def setUp(self) -> None:
        import model_store
        self.store = model_store
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (model_store.MODELS_DIR, model_store._download_hf)
        model_store.MODELS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        self.store.MODELS_DIR, self.store._download_hf = self._saved
        self._tmp.cleanup()

    def test_download_installs_atomically(self) -> None:
        def fake_hf(repo, revision, dest, progress):
            (dest / "model.bin").write_bytes(b"x")
            progress(1, 1)
        self.store._download_hf = fake_hf
        self.store.download("hinglish-swift", lambda done, total: None)
        self.assertTrue(self.store.is_installed("hinglish-swift"))
        self.assertEqual(sorted(p.name for p in Path(self._tmp.name).iterdir()), ["hinglish-swift"])

    def test_failed_download_leaves_nothing(self) -> None:
        def broken_hf(repo, revision, dest, progress):
            (dest / "model.bin").write_bytes(b"partial")
            raise ConnectionError("network down")
        self.store._download_hf = broken_hf
        with self.assertRaises(ConnectionError):
            self.store.download("hinglish-swift", lambda done, total: None)
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_archive_with_wrong_checksum_is_rejected(self) -> None:
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
            for name in self.store.CATALOG["nemotron-3.5"]["source"]["files"]:
                info = tarfile.TarInfo(f"model/{name}")
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))

        def fake_stream(client, url, dest, done, total, progress):
            dest.write_bytes(buf.getvalue())
            return done

        class _Client:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def stream(self, method, url):
                class _Head:
                    headers = {}
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                return _Head()

        saved = (self.store._stream, self.store.httpx.Client)
        self.store._stream, self.store.httpx.Client = fake_stream, _Client
        try:
            with self.assertRaises(RuntimeError):
                self.store.download("nemotron-3.5", lambda done, total: None)
        finally:
            self.store._stream, self.store.httpx.Client = saved
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_endpoints_validate_names_and_usage(self) -> None:
        zapp.app.state.config = config.Config(**config._DEFAULTS)
        zapp.app.state.downloads = {}
        client = TestClient(zapp.app, base_url="http://127.0.0.1:5001")
        names = [m["name"] for m in client.get("/models/catalog").json()["models"]]
        self.assertIn("hinglish-swift", names)
        self.assertEqual(client.post("/models/download", json={"name": "evil"}).status_code, 404)
        self.assertEqual(client.post("/models/delete", json={"name": "hinglish-swift"}).status_code, 404)

        (Path(self._tmp.name) / "hinglish-swift").mkdir()
        (Path(self._tmp.name) / "hinglish-swift" / "zola.json").write_text("{}")
        self.assertEqual(client.post("/models/download", json={"name": "hinglish-swift"}).status_code, 409)
        zapp.app.state.config.whisper_batch = "hinglish-swift"
        self.assertEqual(client.post("/models/delete", json={"name": "hinglish-swift"}).status_code, 409)
        zapp.app.state.config.whisper_batch = "small"
        self.assertEqual(client.post("/models/delete", json={"name": "hinglish-swift"}).status_code, 200)
        self.assertFalse(self.store.is_installed("hinglish-swift"))


class RecorderCancelTests(unittest.TestCase):
    """POST /cancel cancels the pipeline task; the recorder must not swallow it,
    or the batch pipeline transcribes and types the cancelled recording."""

    def _cancel_while_running(self, start) -> None:
        import recorder as R

        async def scenario():
            rec = R.AudioRecorder()
            rec._is_recording = True
            rec._session_start = time.monotonic()
            for _ in range(5):  # some audio, so a swallowed cancel would have something to yield
                rec._queue.put_nowait(np.full((1600, 1), 0.3, dtype=np.float32))
            task = asyncio.create_task(start(rec))
            await asyncio.sleep(0.3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_batch_monitor_propagates_cancel(self) -> None:
        self._cancel_while_running(lambda rec: rec.monitor_until_silence(silence_timeout_s=3.0))

    def test_realtime_stream_propagates_cancel_without_yielding(self) -> None:
        async def consume(rec):
            async for _ in rec.stream_utterances(utterance_silence_s=0.7, force_commit_s=10.0):
                self.fail("an utterance was yielded after cancel")
        self._cancel_while_running(consume)


if __name__ == "__main__":
    unittest.main()
