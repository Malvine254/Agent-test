from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RouteAction = Literal["answer_direct", "search_ai_index", "use_uploaded_files", "use_previous_context"]


@dataclass
class SourceDocument:
    title: str
    source_url: str
    content: str
    snippet: str
    source_type: str
    document_id: str = ""
    chunk_id: str = ""
    score: float | None = None

    @property
    def url(self) -> str:
        return self.source_url


@dataclass
class RetrievalResult:
    sources: list[SourceDocument] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    weak: bool = False


def _as_source(doc: dict, idx: int) -> SourceDocument:
    snippet = doc.get("snippet") or doc.get("content") or ""
    return SourceDocument(
        title=doc.get("title") or doc.get("file_name") or doc.get("name") or f"Source {idx}",
        source_url=doc.get("source_url") or doc.get("url") or doc.get("webUrl") or doc.get("file_path") or "",
        content=doc.get("content") or snippet,
        snippet=snippet,
        source_type=doc.get("source_type") or "ai_search",
        document_id=doc.get("document_id") or doc.get("id") or "",
        chunk_id=doc.get("chunk_id") or "",
        score=doc.get("score") or doc.get("_cache_score"),
    )


def _source_to_dict(source: SourceDocument) -> dict[str, Any]:
    return {
        "title": source.title,
        "file_name": source.title,
        "source_url": source.source_url,
        "url": source.source_url,
        "content": source.content,
        "snippet": source.snippet,
        "source_type": source.source_type,
        "document_id": source.document_id,
        "chunk_id": source.chunk_id,
        "score": source.score,
    }


def _route_action(route: Any) -> str:
    if isinstance(route, dict):
        return str(route.get("action") or "")
    return str(getattr(route, "action", "") or "")


def _route_query(route: Any) -> str:
    if isinstance(route, dict):
        return str(route.get("query") or "")
    return str(getattr(route, "query", "") or "")


def retrieve_ai_search_context(query: str, *, user_context: dict[str, Any] | None = None) -> RetrievalResult:
    user_context = user_context or {}
    query = (query or "").strip()
    if not query:
        return RetrievalResult()

    from search.ai_search_retriever import search_ai_index

    docs = search_ai_index(query, top=int(user_context.get("top", 8)))
    sources = [_as_source(doc, idx) for idx, doc in enumerate(docs or [], start=1)]
    return RetrievalResult(sources=sources, searched=["azure_ai_search"], weak=not sources)


def retrieve_uploaded_file_context(
    query: str,
    conversation_id: str,
    *,
    user_context: dict[str, Any] | None = None,
) -> RetrievalResult:
    from attachment_service import retrieve_attachment_context

    user_context = user_context or {}
    attachment_context = retrieve_attachment_context(
        query,
        conversation_id,
        user_id=user_context.get("user_id") or user_context.get("user_upn"),
        limit=int(user_context.get("top", 5)),
    )
    sources = [
        _as_source(
            {
                "title": item.get("title") or item.get("filename") or "Uploaded file",
                "source_url": item.get("source_url") or item.get("url") or "",
                "content": item.get("snippet") or item.get("content") or "",
                "snippet": item.get("snippet") or item.get("content") or "",
                "source_type": "upload",
                "document_id": item.get("source_id") or item.get("filename") or "",
                "chunk_id": "",
                "score": item.get("relevance_score"),
            },
            idx,
        )
        for idx, item in enumerate(attachment_context.snippets or [], start=1)
    ]
    return RetrievalResult(sources=sources, searched=["uploaded_files"], weak=not sources)


def get_previous_source_context(conversation_id: str, user_context: dict[str, Any] | None = None) -> RetrievalResult:
    memory = (user_context or {}).get("previous_sources") or (user_context or {}).get("last_sources") or []
    sources = [_as_source(item, idx) for idx, item in enumerate(memory, start=1)]
    return RetrievalResult(sources=sources, searched=["previous_context"], weak=not sources)


def retrieve_for_route(route: Any, conversation_id: str, user_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    user_context = user_context or {}
    action = _route_action(route)
    query = _route_query(route)

    if action == "search_ai_index":
        return [_source_to_dict(source) for source in retrieve_ai_search_context(query, user_context=user_context).sources]
    if action == "use_uploaded_files":
        return [_source_to_dict(source) for source in retrieve_uploaded_file_context(query, conversation_id, user_context=user_context).sources]
    if action == "use_previous_context":
        return [_source_to_dict(source) for source in get_previous_source_context(conversation_id, user_context=user_context).sources]
    return []


def retrieve_sources(query: str, scope: str = "sharepoint", user_context: dict[str, Any] | None = None) -> RetrievalResult:
    return retrieve_ai_search_context(query, user_context=user_context)


def retrieve_for_question(route: Any, query: str, context: dict[str, Any] | None = None) -> RetrievalResult:
    route_action = _route_action(route)
    if route_action:
        route = {"action": route_action, "query": query}
    else:
        route = {"action": "search_ai_index", "query": query}
    sources = retrieve_for_route(route, conversation_id=str((context or {}).get("conversation_id") or ""), user_context=context)
    return RetrievalResult(sources=[_as_source(source, idx) for idx, source in enumerate(sources, start=1)], searched=["azure_ai_search"], weak=not sources)
