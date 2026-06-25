from __future__ import annotations

import logging
import time

from config import Config

logger = logging.getLogger(__name__)

# Cache the AzureOpenAI client at module level so the HTTP connection pool /
# TLS session is reused across calls. Recreating the client on every embedding
# request added significant per-query latency (fresh TLS handshake each time).
_embedding_client = None

# Per-request timeout (seconds). After a long idle the cached client's keep-alive
# TCP socket can go stale; without a timeout the first post-idle request blocks on
# the dead socket for ~60s (OS TCP timeout) and Teams cancels the turn with no
# reply. A short timeout makes the stale call fail fast so we can retry on a
# fresh connection instead.
_EMBED_TIMEOUT_SECONDS = 12.0


def _get_embedding_client():
    global _embedding_client
    if _embedding_client is None:
        from openai import AzureOpenAI

        _embedding_client = AzureOpenAI(
            api_key=Config.AZURE_OPENAI_API_KEY,
            azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
            api_version=Config.AZURE_OPENAI_API_VERSION,
            timeout=_EMBED_TIMEOUT_SECONDS,
            max_retries=0,  # we handle retries ourselves below
        )
    return _embedding_client


def _reset_embedding_client() -> None:
    """Drop the cached client so the next call builds a fresh connection pool.
    Used after a failure that may be caused by a stale keep-alive socket."""
    global _embedding_client
    _embedding_client = None


def embed_text(text: str) -> list[float]:
    """Generate one Azure OpenAI embedding vector."""
    if not text.strip():
        raise ValueError("Cannot embed empty text")
    if not Config.AZURE_OPENAI_ENDPOINT or not Config.AZURE_OPENAI_API_KEY:
        raise RuntimeError("Azure OpenAI embedding configuration is missing")

    started_at = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            client = _get_embedding_client()
            attempt_started_at = time.perf_counter()
            # text-embedding-3-* models default to their native dimensionality
            # (3072 for -large). Pass dimensions explicitly so the returned vector
            # always matches the index's vector field width (Config.EMBEDDING_DIMENSIONS).
            response = client.embeddings.create(
                model=Config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
                input=text[:8000],
                dimensions=Config.EMBEDDING_DIMENSIONS,
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
            # A timeout/connection error is often a stale cached socket — drop the
            # client so the next attempt reconnects fresh.
            _reset_embedding_client()
            logger.warning(
                "Embedding attempt %s/4 failed (%s) — resetting client and retrying",
                attempt + 1,
                type(exc).__name__,
            )
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
