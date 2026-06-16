from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class ConversationState:
    conversation_id: str
    last_route: str = ""
    last_query: str = ""
    summary: str = ""
    last_sources: list[dict[str, Any]] = field(default_factory=list)
    current_uploaded_attachment_metadata: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MemoryStore:
    def __init__(self, ttl_hours: int = 24, max_summary_chars: int = 1800, max_snippet_chars: int = 700):
        self.ttl = timedelta(hours=ttl_hours)
        self.max_summary_chars = max_summary_chars
        self.max_snippet_chars = max_snippet_chars
        self._items: dict[str, ConversationState] = {}

    def get(self, conversation_id: str) -> ConversationState:
        self.cleanup()
        if conversation_id not in self._items:
            self._items[conversation_id] = ConversationState(conversation_id=conversation_id)
        return self._items[conversation_id]

    def update(
        self,
        conversation_id: str,
        *,
        last_route: str | None = None,
        last_query: str | None = None,
        summary_note: str | None = None,
        last_sources: list[dict[str, Any]] | None = None,
        attachment_metadata: list[dict[str, Any]] | None = None,
    ) -> ConversationState:
        state = self.get(conversation_id)
        if last_route is not None:
            state.last_route = last_route
        if last_query is not None:
            state.last_query = last_query
        if summary_note:
            combined = (state.summary + "\n" + summary_note).strip()
            state.summary = combined[-self.max_summary_chars :]
        if last_sources is not None:
            state.last_sources = [
                {
                    "title": s.get("title") or s.get("name") or "Untitled",
                    "url": s.get("url") or s.get("webUrl") or s.get("file_path") or "",
                    "source_id": s.get("source_id") or s.get("id") or "",
                    "snippet": (s.get("snippet") or s.get("content") or "")[: self.max_snippet_chars],
                    "truncated": bool(s.get("truncated") or len(s.get("snippet") or s.get("content") or "") > self.max_snippet_chars),
                }
                for s in last_sources[:8]
            ]
        if attachment_metadata is not None:
            state.current_uploaded_attachment_metadata = [
                {k: v for k, v in item.items() if k != "content"}
                for item in attachment_metadata[:8]
            ]
        state.updated_at = datetime.now(timezone.utc).isoformat()
        return state

    def clear(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)

    def cleanup(self) -> int:
        cutoff = datetime.now(timezone.utc) - self.ttl
        expired = []
        for cid, state in self._items.items():
            try:
                updated = datetime.fromisoformat(state.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < cutoff:
                    expired.append(cid)
            except ValueError:
                expired.append(cid)
        for cid in expired:
            self._items.pop(cid, None)
        return len(expired)

    def as_dict(self, conversation_id: str) -> dict[str, Any]:
        return asdict(self.get(conversation_id))


memory_store = MemoryStore()
