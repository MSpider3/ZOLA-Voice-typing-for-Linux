"""
core_llm.py — Async Local Ollama API Client
============================================
Wraps the Ollama /api/generate endpoint using a persistent httpx.AsyncClient.
The client is created once at startup and reused across all requests —
this avoids TCP connection overhead on every transcription.

Target model: thirdeyeai/qwen2.5-1.5b-instruct-uncensored:Q4_0
(configured in settings.json, hot-swappable at runtime via POST /settings)
"""

import logging
from typing import Optional

import httpx

from config import Config

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# System Prompt — Zero-Shot Text Formatting Instruction
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are an invisible text editor. Clean up grammatical errors, add logical "
    "structural punctuation, and correct messy sentence structures within the "
    "following Speech-to-Text transcript. Maintain the original linguistic "
    "vocabulary and code-switching characteristics (such as Hindi/English mixed "
    "phrasing). Return ONLY the corrected transcript text without introductory "
    "statements, meta-commentary, or stylistic quotes."
)


class OllamaClient:
    """
    Async client for the local Ollama API.

    The httpx.AsyncClient is long-lived (created once, reused across all calls)
    to amortise TCP and TLS connection overhead. Ollama runs locally so TLS
    is irrelevant, but persistent connections still avoid OS socket overhead.

    The model name attribute is mutable — updating it (from POST /settings)
    takes effect on the next API call with no restart required.
    """

    def __init__(self, config: Config) -> None:
        self.model_name: str = config.ollama_model
        self._base_url: str = config.ollama_url.rstrip("/")

        # Persistent client — do NOT create a new one per request
        # Timeout is generous because local LLM inference is CPU-bound and slow
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=5.0,      # Ollama should connect instantly (localhost)
                read=180.0,       # Allow 3 minutes for generation on slow CPU
                write=10.0,
                pool=5.0,
            ),
            headers={"Content-Type": "application/json"},
        )
        logger.info(
            "OllamaClient: initialised — url='%s', model='%s'",
            self._base_url,
            self.model_name,
        )

    # ----------------------------------------------------------------------- #
    # Core LLM Operations
    # ----------------------------------------------------------------------- #
    async def format_transcript(self, transcript: str) -> str:
        """
        Send a raw STT transcript to Ollama for grammar correction and
        punctuation formatting. Returns the cleaned text.

        Uses stream=false so we wait for the full generation before returning.
        This is intentional — partial LLM output would produce incomplete
        sentences that make no sense when injected character-by-character.

        Args:
            transcript: Raw STT output (may contain Hinglish, filler words, etc.)

        Returns:
            Formatted transcript string, or the original transcript on failure.
        """
        if not transcript or not transcript.strip():
            logger.debug("OllamaClient.format_transcript: empty input, skipping LLM call")
            return transcript

        payload = {
            "model": self.model_name,
            "system": SYSTEM_PROMPT,
            "prompt": transcript.strip(),
            "stream": False,
            "options": {
                # Keep inference deterministic for text editing tasks
                "temperature": 0.1,
                "top_p": 0.9,
                "num_predict": 512,   # Max tokens — transcripts are rarely longer
            },
        }

        logger.debug(
            "OllamaClient.format_transcript: model='%s', input_chars=%d",
            self.model_name,
            len(transcript),
        )

        try:
            response = await self._client.post("/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            cleaned = data.get("response", "").strip()

            if not cleaned:
                logger.warning(
                    "OllamaClient.format_transcript: empty response from Ollama — "
                    "returning original transcript"
                )
                return transcript

            logger.info(
                "OllamaClient.format_transcript: %d → %d chars (model='%s')",
                len(transcript),
                len(cleaned),
                self.model_name,
            )
            return cleaned

        except httpx.ConnectError:
            logger.error(
                "OllamaClient: cannot connect to Ollama at %s — "
                "is 'ollama serve' running? Returning raw transcript.",
                self._base_url,
            )
            return transcript

        except httpx.TimeoutException:
            logger.error(
                "OllamaClient: Ollama request timed out after 180s (model='%s') — "
                "returning raw transcript. Consider switching to a smaller model.",
                self.model_name,
            )
            return transcript

        except httpx.HTTPStatusError as exc:
            logger.error(
                "OllamaClient: HTTP %d from Ollama: %s — returning raw transcript",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return transcript

        except Exception as exc:
            logger.exception("OllamaClient.format_transcript: unexpected error: %s", exc)
            return transcript

    # ----------------------------------------------------------------------- #
    # Model Discovery
    # ----------------------------------------------------------------------- #
    async def list_models(self) -> list[str]:
        """
        Query Ollama for locally installed model names.
        Returns a sorted list of name strings (e.g. ['llama3.2:1b', 'qwen2.5:1.5b']).
        Returns empty list if Ollama is unreachable.
        """
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            models.sort()
            logger.debug("OllamaClient.list_models: found %d models", len(models))
            return models

        except httpx.ConnectError:
            logger.warning("OllamaClient.list_models: Ollama unreachable at %s", self._base_url)
            return []

        except Exception as exc:
            logger.error("OllamaClient.list_models: error fetching model list: %s", exc)
            return []

    async def health_check(self) -> bool:
        """
        Returns True if Ollama is running and reachable, False otherwise.
        Used at startup to warn the user if the LLM service is unavailable.
        """
        try:
            response = await self._client.get("/api/tags", timeout=3.0)
            return response.status_code == 200
        except Exception:
            return False

    # ----------------------------------------------------------------------- #
    # Cleanup
    # ----------------------------------------------------------------------- #
    async def close(self) -> None:
        """Close the persistent httpx client. Called during daemon shutdown."""
        await self._client.aclose()
        logger.info("OllamaClient: HTTP client closed")
