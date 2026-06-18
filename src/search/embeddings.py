from __future__ import annotations

import logging
import time

from config import Config

logger = logging.getLogger(__name__)


def embed_text(text: str) -> list[float]:
    """Generate one Azure OpenAI embedding vector."""
    if not text.strip():
        raise ValueError("Cannot embed empty text")
    if not Config.AZURE_OPENAI_ENDPOINT or not Config.AZURE_OPENAI_API_KEY:
        raise RuntimeError("Azure OpenAI embedding configuration is missing")

    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=Config.AZURE_OPENAI_API_KEY,
        azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
        api_version="2024-02-01",
    )
    started_at = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            attempt_started_at = time.perf_counter()
            response = client.embeddings.create(
                model=Config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
                input=text[:8000],
            )
            vector = list(response.data[0].embedding)
            if len(vector) != Config.EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"Embedding dimension mismatch: got {len(vector)}, expected {Config.EMBEDDING_DIMENSIONS}"
                )
            elapsed = time.perf_counter() - started_at
            attempt_elapsed = time.perf_counter() - attempt_started_at
            logger.info(
                "EMBEDDING OK | model=%s | chars=%s | attempt=%s | attempt_seconds=%.2f | total_seconds=%.2f",
                Config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
                len(text[:8000]),
                attempt + 1,
                attempt_elapsed,
                elapsed,
            )
            return vector
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(min(2**attempt, 8))
    logger.warning("Embedding request failed after retries: %s", type(last_error).__name__)
    logger.info(
        "EMBEDDING FAILED | model=%s | chars=%s | total_seconds=%.2f",
        Config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        len(text[:8000]),
        time.perf_counter() - started_at,
    )
    raise last_error or RuntimeError("Embedding request failed")
