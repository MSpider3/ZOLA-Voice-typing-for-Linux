"""
core_llm.py — Async Local Ollama API Client
============================================
Wraps the Ollama /api/generate endpoint using a persistent httpx.AsyncClient.
The client is created once at startup and reused across all requests.

System prompt is selected based on config.output_language:
  preserve → maintain code-switching as spoken (Hinglish output)
  english  → normalize all output to English
"""

import logging

import httpx

from config import Config

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# System Prompts — selected by output_language config field
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPTS: dict[str, str] = {
    # Default: maintain whatever language mix was spoken.
    # Best for Hinglish users who want to preserve their natural speech style.
    "preserve": (
        "You are an invisible text editor. Clean up grammatical errors, add logical "
        "structural punctuation, and correct messy sentence structures within the "
        "following Speech-to-Text transcript. Maintain the original linguistic "
        "vocabulary and all code-switching characteristics exactly as spoken "
        "(Hindi words stay Hindi, English words stay English, Hinglish phrasing "
        "is preserved). Return ONLY the corrected transcript text without "
        "introductory statements, meta-commentary, or stylistic quotes."
    ),
    # Normalize everything to English.
    # Best when you spoke Hindi/Hinglish but want clean English text injected.
    "english": (
        "You are an invisible text editor. Clean up the following Speech-to-Text "
        "transcript and output it in English only. Translate any Hindi or Hinglish "
        "phrases to natural English. Fix grammatical errors and add appropriate "
        "punctuation. Return ONLY the corrected English text without introductory "
        "statements, meta-commentary, or stylistic quotes."
    ),
}

# Fallback if output_language is somehow unrecognised
_DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPTS["preserve"]


class OllamaClient:
    """
    Async client for the local Ollama API.

    The httpx.AsyncClient is long-lived (created once, reused across all calls).
    model_name and output_language are mutable — updating them via POST /settings
    takes effect on the next API call with no restart required.
    """

    def __init__(self, config: Config) -> None:
        self.model_name: str = config.ollama_model
        self.output_language: str = config.output_language
        self._base_url: str = config.ollama_url.rstrip("/")

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=5.0,
                read=180.0,
                write=10.0,
                pool=5.0,
            ),
            headers={"Content-Type": "application/json"},
        )
        logger.info(
            "OllamaClient: initialised — url='%s', model='%s', output_language='%s'",
            self._base_url, self.model_name, self.output_language,
        )

    # ----------------------------------------------------------------------- #
    # Core LLM Operations
    # ----------------------------------------------------------------------- #
    async def format_transcript(self, transcript: str) -> str:
        """
        Send a raw STT transcript to Ollama for grammar correction and
        punctuation formatting. System prompt is chosen by output_language.

        Returns the cleaned text, or the original transcript on any failure.
        """
        if not transcript or not transcript.strip():
            return transcript

        system_prompt = _SYSTEM_PROMPTS.get(self.output_language, _DEFAULT_SYSTEM_PROMPT)

        payload = {
            "model": self.model_name,
            "system": system_prompt,
            "prompt": transcript.strip(),
            "stream": False,
            "options": {
                "temperature": 0.1,
                "top_p": 0.9,
                # Scale with the input so long dictations are not cut off
                # (Devanagari can need more than one token per character).
                "num_predict": max(512, 2 * len(transcript)),
            },
        }

        logger.debug(
            "OllamaClient.format_transcript: model='%s', output_language='%s', input_chars=%d",
            self.model_name, self.output_language, len(transcript),
        )

        try:
            response = await self._client.post("/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            cleaned = data.get("response", "").strip()

            if data.get("done_reason") == "length":
                logger.warning(
                    "OllamaClient.format_transcript: output hit the token limit — "
                    "returning original transcript instead of a truncated one"
                )
                return transcript

            if not cleaned:
                logger.warning(
                    "OllamaClient.format_transcript: empty response from Ollama — "
                    "returning original transcript"
                )
                return transcript

            logger.info(
                "OllamaClient.format_transcript: %d → %d chars (model='%s')",
                len(transcript), len(cleaned), self.model_name,
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
                "returning raw transcript. Consider a smaller model.",
                self.model_name,
            )
            return transcript

        except httpx.HTTPStatusError as exc:
            logger.error(
                "OllamaClient: HTTP %d from Ollama: %s — returning raw transcript",
                exc.response.status_code, exc.response.text[:200],
            )
            return transcript

        except Exception as exc:
            logger.exception("OllamaClient.format_transcript: unexpected error: %s", exc)
            return transcript

    def set_base_url(self, url: str) -> None:
        """Point the client at a new Ollama URL (POST /settings, no restart)."""
        self._base_url = url.rstrip("/")
        self._client.base_url = self._base_url

    # ----------------------------------------------------------------------- #
    # Model Discovery
    # ----------------------------------------------------------------------- #
    async def list_models(self) -> list[str]:
        """Query Ollama for locally installed model names."""
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            models.sort()
            return models
        except httpx.ConnectError:
            logger.warning("OllamaClient.list_models: Ollama unreachable at %s", self._base_url)
            return []
        except Exception as exc:
            logger.error("OllamaClient.list_models: error: %s", exc)
            return []

    async def health_check(self) -> bool:
        """Returns True if Ollama is running and reachable."""
        try:
            response = await self._client.get("/api/tags", timeout=3.0)
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the persistent httpx client. Called during daemon shutdown."""
        await self._client.aclose()
        logger.info("OllamaClient: HTTP client closed")
