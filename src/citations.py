from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CitationSource:
    title: str
    url: str = ""
    source_id: str = ""


def inline_citation(index: int, url: str = "") -> str:
    if url:
        return f"[[{index}]]({url})"
    return f"[[{index}]]"


def strip_duplicate_references(answer: str) -> str:
    parts = re.split(r"\n\s*references\s*:?\s*\n", answer or "", flags=re.I)
    if len(parts) <= 2:
        return answer or ""
    return parts[0].rstrip() + "\n\nReferences\n" + parts[-1].strip()


def append_references(answer: str, sources: list[dict]) -> str:
    answer = strip_duplicate_references(answer or "").rstrip()
    if re.search(r"\n\s*references\s*:?", answer, flags=re.I):
        return answer
    lines = []
    seen = set()
    for idx, source in enumerate(sources or [], start=1):
        title = source.get("title") or source.get("name") or f"Source {idx}"
        url = source.get("source_url") or source.get("url") or source.get("webUrl") or source.get("file_path") or ""
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{idx}. {title}" + (f" - {url}" if url else ""))
    if not lines:
        return answer
    return answer + "\n\nReferences\n" + "\n".join(lines)


def cited_numbers(answer: str) -> set[int]:
    return {int(n) for n in re.findall(r"\[\[(\d+)\]\]", answer or "")}
