"""
model_store.py — Downloadable STT models
=========================================
A fixed catalog of extra models that ZOLA can install into MODELS_DIR from
the app (Settings → MODELS) or with POST /models/download. Only these sources
are ever fetched; the API never accepts a URL.

Standard Whisper sizes (tiny … large-v3-turbo, *.en) are not listed here:
faster-whisper downloads them automatically the first time they are selected.

Downloads go to MODELS_DIR/.partial-<name>/ and are renamed into place only
when complete, so a cancelled or failed download never leaves a half model.
"""

import json
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Callable

import httpx

from config import MODELS_DIR

logger = logging.getLogger(__name__)

# Recommendations come from STT_EVALUATION.md (2-core i3, CPU only).
CATALOG: dict[str, dict] = {
    "hinglish-swift": {
        "label": "Hinglish Swift (Oriserve Whisper-Hindi2Hinglish-Swift, int8) — romanized Hindi/Hinglish",
        "description": "Recommended for Hindi and Hinglish. Writes Roman script. "
                       "Hindi 10% / Hinglish 12% WER, ~1 s per sentence on a 2-core CPU.",
        "size_mb": 78,
        "source": {"type": "huggingface", "repo": "MSpider3/whisper-hindi2hinglish-swift-ct2"},
    },
    "nemotron-3.5": {
        "label": "Nemotron 3.5 (NVIDIA, int8) — Devanagari Hindi + English, best English accuracy",
        "description": "For Hindi in Devanagari or the most accurate English (8% WER). "
                       "Set the spoken language to Hindi or English. ~1.7 s per sentence.",
        "size_mb": 450,
        "source": {
            "type": "tar",
            "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
                   "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11.tar.bz2",
            "files": ["encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"],
        },
        "zola": {
            "engine": "sherpa-transducer",
            "source": "https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b (sherpa-onnx int8 export, 560 ms chunks)",
            "license": "OpenMDW-1.1 (per the NVIDIA model card)",
        },
    },
}

Progress = Callable[[int, int], None]  # (bytes done, bytes total)


def is_installed(name: str) -> bool:
    folder = MODELS_DIR / name
    return (folder / "model.bin").is_file() or (folder / "zola.json").is_file()


def download(name: str, progress: Progress) -> None:
    """Install catalog model `name` (blocking; run in an executor)."""
    entry = CATALOG[name]
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    partial = MODELS_DIR / f".partial-{name}"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir()
    try:
        source = entry["source"]
        if source["type"] == "huggingface":
            _download_hf(source["repo"], partial, progress)
        else:
            _download_tar(source["url"], source["files"], partial, progress)
        if "zola" in entry:
            meta = {"label": entry["label"], **entry["zola"]}
            (partial / "zola.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        if not (partial / "zola.json").is_file() and not (partial / "model.bin").is_file():
            raise RuntimeError("downloaded files do not look like a model")
        partial.rename(MODELS_DIR / name)
        logger.info("model_store: installed '%s' in %s", name, MODELS_DIR / name)
    finally:
        shutil.rmtree(partial, ignore_errors=True)


def delete(name: str) -> None:
    """Remove an installed model folder (catalog models only)."""
    shutil.rmtree(MODELS_DIR / name)
    logger.info("model_store: deleted '%s'", name)


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #
def _stream(client: httpx.Client, url: str, dest: Path, done: int, total: int, progress: Progress) -> int:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in response.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                progress(done, total)
    return done


def _download_hf(repo: str, dest: Path, progress: Progress) -> None:
    from huggingface_hub import HfApi  # installed with faster-whisper

    info = HfApi().model_info(repo, files_metadata=True)
    files = [s for s in info.siblings if not s.rfilename.startswith(".") and "/" not in s.rfilename]
    total, done = sum(s.size or 0 for s in files), 0
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
        for s in files:
            url = f"https://huggingface.co/{repo}/resolve/{info.sha}/{s.rfilename}"
            done = _stream(client, url, dest / s.rfilename, done, total, progress)


def _download_tar(url: str, members: list[str], dest: Path, progress: Progress) -> None:
    with tempfile.TemporaryDirectory(dir=MODELS_DIR) as tmp:
        archive = Path(tmp) / "model.tar.bz2"
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
            with client.stream("HEAD", url) as head:
                total = int(head.headers.get("content-length", 0))
            _stream(client, url, archive, 0, total, progress)
        # Copy only the expected files by basename (no path from the archive is used)
        wanted = set(members)
        with tarfile.open(archive, "r:bz2") as tar:
            for member in tar.getmembers():
                base = Path(member.name).name
                if member.isfile() and base in wanted:
                    with tar.extractfile(member) as src, open(dest / base, "wb") as out:
                        shutil.copyfileobj(src, out)
                    wanted.discard(base)
        if wanted:
            raise RuntimeError(f"archive is missing {sorted(wanted)}")
