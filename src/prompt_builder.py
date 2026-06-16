from __future__ import annotations


def _clip(text: str, remaining: int) -> tuple[str, int, bool]:
    text = text or ""
    if remaining <= 0:
        return "", 0, bool(text)
    if len(text) <= remaining:
        return text, remaining - len(text), False
    return text[:remaining].rstrip(), 0, True


def _field(item: object, name: str, default: str = "") -> str:
    if isinstance(item, dict):
        return item.get(name) or default
    return getattr(item, name, default) or default


def build_prompt(
    *,
    user_question: str,
    source_snippets: list[dict] | None = None,
    uploaded_file_snippets: list[dict] | None = None,
    memory_summary: str = "",
    citation_rules: str = "",
    max_chars: int = 24000,
) -> str:
    remaining = max_chars
    parts: list[str] = []

    header = (
        "You are Armely AI. Answer the user. Never invent organizational facts. "
        "Use retrieved sources or uploaded-file snippets for workplace/document claims."
    )
    clipped, remaining, _ = _clip(header, remaining)
    parts.append(clipped)

    if memory_summary:
        clipped, remaining, _ = _clip(f"\n\nCompact memory:\n{memory_summary}", remaining)
        parts.append(clipped)

    if uploaded_file_snippets:
        parts.append("\n\nUploaded file snippets:")
        for idx, item in enumerate(uploaded_file_snippets, start=1):
            text = f"\n[File {idx}] {item.get('title') or item.get('filename') or 'Uploaded file'}\n{item.get('snippet') or item.get('content') or ''}"
            clipped, remaining, _ = _clip(text, remaining)
            parts.append(clipped)

    allowed_source_snippets = [
        item for item in (source_snippets or [])
        if _field(item, "source_type", "sharepoint").lower() in {"sharepoint", "upload", "uploaded_file"}
    ]

    if allowed_source_snippets:
        parts.append("\n\nRetrieved sources:")
        for idx, item in enumerate(allowed_source_snippets, start=1):
            title = _field(item, "title") or _field(item, "name") or f"Source {idx}"
            url = _field(item, "source_url") or _field(item, "url") or _field(item, "webUrl") or _field(item, "file_path")
            snippet = _field(item, "snippet") or _field(item, "content")
            text = f"\n[Source {idx}] {title}\nURL: {url}\n{snippet}"
            clipped, remaining, _ = _clip(text, remaining)
            parts.append(clipped)

    rules = citation_rules or (
        "Cite every organizational fact inline with [[number]](URL). "
        "Do not cite sources that are not listed above. If source content is missing, say no matching information was found."
    )
    clipped, remaining, _ = _clip(f"\n\nCitation rules:\n{rules}", remaining)
    parts.append(clipped)

    clipped, remaining, _ = _clip(f"\n\nUser question:\n{user_question}", remaining)
    parts.append(clipped)
    return "".join(parts).strip()
