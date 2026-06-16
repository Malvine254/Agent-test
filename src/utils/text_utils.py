import re


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(re.sub(r"[^a-z0-9' ?.!_-]+", " ", text.lower()).split())
