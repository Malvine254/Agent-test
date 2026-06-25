from __future__ import annotations

import re

from config import Config


def _overlap_tail(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return text
    tail = text[-overlap_chars:]
    boundary = tail.find(" ")
    return tail[boundary + 1 :] if boundary > 0 else tail


def _lead_summary(content: str, max_chars: int = 240) -> str:
    """Short lead snippet used to populate the index 'summary' field.

    The semantic configuration and scoring profile both reference 'summary'; an
    empty value (the previous behaviour) gave the reranker nothing to work with.
    """
    snippet = " ".join((content or "").split())
    return snippet[:max_chars]


def chunk_document(
    text: str,
    max_chars: int | None = None,
    overlap_chars: int | None = None,
) -> list[dict]:
    """Split document text into paragraph-aware chunks.

    Defaults come from Config (CHUNK_MAX_CHARS / CHUNK_OVERLAP_CHARS). Smaller chunks
    give the retriever far more precise matches and sharply reduce hallucination.
    """
    if max_chars is None:
        max_chars = Config.CHUNK_MAX_CHARS
    if overlap_chars is None:
        overlap_chars = Config.CHUNK_OVERLAP_CHARS
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text or "") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in (text or "").splitlines() if p.strip()]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        separator = "\n\n" if current else ""
        candidate = f"{current}{separator}{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = _overlap_tail(current, overlap_chars)
        while len(paragraph) > max_chars:
            prefix = paragraph[:max_chars].rstrip()
            chunks.append(prefix)
            paragraph = _overlap_tail(prefix, overlap_chars) + paragraph[max_chars:]
        current = f"{current}\n\n{paragraph}".strip() if current else paragraph

    if current:
        chunks.append(current)

    return [
        {
            "chunk_id": f"{idx:04d}",
            "content": content,
            "summary": _lead_summary(content),
            "char_count": len(content),
        }
        for idx, content in enumerate(chunks, start=1)
        if content.strip()
    ]
