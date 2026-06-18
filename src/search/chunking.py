from __future__ import annotations

import re


def _overlap_tail(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return text
    tail = text[-overlap_chars:]
    boundary = tail.find(" ")
    return tail[boundary + 1 :] if boundary > 0 else tail


def chunk_document(text: str, max_chars: int = 6000, overlap_chars: int = 800) -> list[dict]:
    """Split document text into paragraph-aware chunks."""
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
            "summary": "",
            "char_count": len(content),
        }
        for idx, content in enumerate(chunks, start=1)
        if content.strip()
    ]
