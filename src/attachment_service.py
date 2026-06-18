from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentContext:
    snippets: list[dict]
    metadata: list[dict]

    @property
    def has_content(self) -> bool:
        return bool(self.snippets)


def retrieve_attachment_context(query: str, conversation_id: str, user_id: str | None = None, limit: int = 5) -> AttachmentContext:
    from attachment_cache import get_conversation_attachments, search_attachment_contents

    matches = search_attachment_contents(conversation_id, query or "", limit=limit, user_id=user_id)
    snippets = [
        {
            "title": item.get("filename") or "Uploaded file",
            "filename": item.get("filename") or "Uploaded file",
            "snippet": item.get("content_snippet") or "",
            "source_id": item.get("filename") or "",
        }
        for item in matches
        if item.get("content_snippet")
    ]
    metadata = get_conversation_attachments(conversation_id, include_content=False, user_id=user_id)
    return AttachmentContext(snippets=snippets, metadata=metadata)


def cleanup_attachment_cache() -> int:
    from attachment_cache import cleanup_old_cache

    return cleanup_old_cache()
