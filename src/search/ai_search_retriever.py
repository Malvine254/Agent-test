from __future__ import annotations

import logging
import re
import time
from typing import Any

from config import Config
from search.ai_search_client import get_search_client
from search.embeddings import embed_text

logger = logging.getLogger(__name__)
# Dedicated audit channel — who asked what and which security filter was applied.
# Routed separately (e.g. to App Insights/Log Analytics) without debug noise.
audit_logger = logging.getLogger("audit")

SELECT_FIELDS = [
    "id",
    "document_id",
    "chunk_id",
    "title",
    "file_name",
    "file_type",
    "source_url",
    "site_id",
    "drive_id",
    "item_id",
    "folder_path",
    "last_modified",
    "indexed_at",
    "content",
    "summary",
    "source_type",
    "checksum",
    "acl_users",
    "acl_groups",
]


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _caption_text(row: dict) -> str:
    captions = row.get("@search.captions") or []
    for caption in captions:
        if isinstance(caption, dict):
            text = caption.get("text") or caption.get("highlights") or ""
        else:
            text = getattr(caption, "text", "") or getattr(caption, "highlights", "") or ""
        text = str(text or "").strip()
        if text:
            return text
    highlights = row.get("@search.highlights") or {}
    if isinstance(highlights, dict):
        for value in highlights.values():
            if isinstance(value, list):
                for item in value:
                    text = str(item or "").strip()
                    if text:
                        return text
            else:
                text = str(value or "").strip()
                if text:
                    return text
    return ""


def _normalize_result(row: dict) -> dict:
    content = _first_non_empty(row.get("content"), row.get("chunk"), row.get("text"))
    snippet = _caption_text(row) or content[:1000]
    if not content:
        content = snippet
    source_url = _first_non_empty(row.get("source_url"), row.get("url"))
    reranker_score = row.get("@search.reranker_score")
    return {
        "title": _first_non_empty(row.get("title"), row.get("file_name"), "SharePoint document"),
        "file_name": _first_non_empty(row.get("file_name")),
        "source_url": source_url,
        "url": source_url,
        "content": content,
        "snippet": snippet,
        "source_type": "ai_search",
        "document_id": _first_non_empty(row.get("document_id"), row.get("id")),
        "chunk_id": _first_non_empty(row.get("chunk_id")),
        "reranker_score": reranker_score,
        "score": row.get("@search.score") if row.get("@search.score") is not None else reranker_score,
    }


def _log_diagnostics(query: str, raw_count: int, mapped: list[dict], *, status: str) -> None:
    logger.info(
        "AI SEARCH DIAGNOSTICS | query=%r | index=%s | status=%s | raw_results=%s | mapped_results=%s",
        query,
        Config.AZURE_SEARCH_INDEX_NAME,
        status,
        raw_count,
        len(mapped),
    )
    logger.info(
        "AI SEARCH DIAGNOSTICS TOP3 | titles=%s | scores=%s | snippet_lengths=%s",
        [item.get("title", "") for item in mapped[:3]],
        [item.get("score") for item in mapped[:3]],
        [len(item.get("snippet") or "") for item in mapped[:3]],
    )


def _normalize_graph_result(row: dict) -> dict:
    return {
        "title": _first_non_empty(row.get("name"), row.get("title"), "SharePoint document"),
        "file_name": _first_non_empty(row.get("name"), row.get("title")),
        "source_url": _first_non_empty(row.get("webUrl"), row.get("url")),
        "url": _first_non_empty(row.get("webUrl"), row.get("url")),
        "content": _first_non_empty(row.get("summary"), row.get("snippet"), row.get("content")),
        "snippet": _first_non_empty(row.get("summary"), row.get("snippet"), row.get("content")),
        "source_type": "graph",
        "document_id": _first_non_empty(row.get("id")),
        "chunk_id": "",
        "score": row.get("relevance_score") or row.get("@search.score"),
    }


def _targeted_document_search(query: str, *, max_files: int = 2) -> list[dict]:
    """Filename-targeted fallback: search configured drives for a named document and
    extract its text on the fly.

    This catches the case where a user names a specific file that the AI Search index
    doesn't (yet) contain — e.g. a freshly uploaded document or one named differently
    from its body text. Returns normalized results WITH content so the model can answer
    instead of hallucinating. Returns [] when nothing matches (caller answers honestly).
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        from sharepoint.graph_client import (
            download_file_bytes,
            list_configured_sharepoint_drives,
            search_drive_items,
        )
        from sharepoint.sharepoint_reader import extract_document_text
    except Exception as exc:
        logger.info("Targeted document search unavailable: %s", exc)
        return []

    try:
        drives = list_configured_sharepoint_drives()
    except Exception as exc:
        logger.info("Targeted document search: could not list drives: %s", exc)
        return []

    seen_items: set[str] = set()
    results: list[dict] = []
    for drive in drives:
        drive_id = drive.get("drive_id") or drive.get("id")
        if not drive_id:
            continue
        for item in search_drive_items(drive_id, query, top=3):
            item_id = item.get("id") or ""
            if not item_id or item_id in seen_items:
                continue
            seen_items.add(item_id)
            name = item.get("name") or "document"
            try:
                file_bytes = download_file_bytes(drive_id, item_id)
                content = extract_document_text(name, file_bytes) or ""
            except Exception as exc:
                logger.info("Targeted search: failed to read %s: %s", name, type(exc).__name__)
                content = ""
            results.append({
                "title": name,
                "file_name": name,
                "source_url": item.get("webUrl") or "",
                "url": item.get("webUrl") or "",
                "content": content,
                "snippet": content[:600],
                "source_type": "graph_targeted",
                "document_id": item_id,
                "chunk_id": "",
                "score": item.get("@search.score") or 1.0,
            })
            if len(results) >= max_files:
                logger.info("Targeted document search matched %s file(s) for %r", len(results), query[:60])
                return results
    if results:
        logger.info("Targeted document search matched %s file(s) for %r", len(results), query[:60])
    return results


def _search_kwargs(query: str, filter_expr: str, top: int) -> dict:
    kwargs = {
        "search_text": query,
        "top": top,
        "query_type": "semantic",
        "semantic_query": query,
        "semantic_configuration_name": Config.AZURE_SEARCH_SEMANTIC_CONFIG,
        "query_caption": "extractive",
        "query_answer": "extractive",
        "query_answer_count": 3,
        "search_fields": ["content", "summary", "title", "file_name", "folder_path"],
        "select": SELECT_FIELDS,
        "semantic_error_mode": "partial",
        "semantic_max_wait_in_milliseconds": 2500,
    }
    if filter_expr:
        kwargs["filter"] = filter_expr
    return kwargs


def _materialize(rows) -> list[dict]:
    return [_normalize_result(dict(row)) for row in rows]


def _apply_score_threshold(query: str, results: list[dict]) -> list[dict]:
    """Drop semantic results whose reranker score is below Config.MIN_RERANKER_SCORE.

    The reranker score (Azure scale 0-4) is only present for semantic queries. Weak
    matches are a primary cause of hallucination — the model treats a barely-relevant
    chunk as authoritative. Filtering them here means a query with no strong match
    returns nothing, so the caller can answer honestly ('not found') instead of
    inventing an answer. Keyword-only results (no reranker score) are never filtered.
    """
    threshold = float(getattr(Config, "MIN_RERANKER_SCORE", 0) or 0)
    if threshold <= 0:
        return results
    scored = [r for r in results if isinstance(r.get("reranker_score"), (int, float))]
    if not scored:
        return results  # no reranker scores present (keyword path) — nothing to filter
    kept = [r for r in results if not isinstance(r.get("reranker_score"), (int, float)) or r["reranker_score"] >= threshold]
    if len(kept) != len(results):
        logger.info(
            "AI SEARCH SCORE THRESHOLD | query=%r | threshold=%.2f | kept=%s | dropped=%s",
            query,
            threshold,
            len(kept),
            len(results) - len(kept),
        )
    return kept


def _apply_precision_filters(query: str, results: list[dict]) -> list[dict]:
    normalized = (query or "").lower()
    query_terms = [
        term
        for term in re.findall(r"[a-z0-9][a-z0-9'\-]+", normalized)
        if len(term) > 3 and term not in {"about", "document", "documents", "file", "files", "please", "show", "tell", "info", "information"}
    ]
    haystack = lambda result: " ".join(
        str(result.get(field) or "") for field in ("title", "file_name", "content", "snippet")
    ).lower()

    # Specific-person lookups should only keep documents that actually mention
    # at least one queried name/term. This prevents broad employee lists or
    # unrelated datasets from being returned as if they contained the target.
    specific_lookup = (
        bool(query_terms)
        and (
            " or " in normalized
            or " and " in normalized
            or normalized.startswith(("do you have", "do you know", "find", "search", "who is", "what about", "is there"))
            or len(query_terms) >= 3
        )
    )
    if specific_lookup:
        filtered = [result for result in results if any(term in haystack(result) for term in query_terms)]
        if filtered:
            logger.info(
                "AI SEARCH PRECISION FILTER | query=%r | mode=specific_lookup | kept=%s | discarded=%s",
                query,
                len(filtered),
                len(results) - len(filtered),
            )
            results = filtered
        else:
            logger.info(
                "AI SEARCH PRECISION FILTER | query=%r | mode=specific_lookup | kept=0 | discarded=%s",
                query,
                len(results),
            )
            return []

    if re.search(r"\bceo\b|chief executive", normalized):
        terms = ["ceo", "chief executive", "chief executive officer"]
        filtered = [result for result in results if any(term in haystack(result) for term in terms)]
        if filtered:
            logger.info("AI SEARCH PRECISION FILTER | query=%r | kept=%s | discarded=%s", query, len(filtered), len(results) - len(filtered))
            return filtered
    return results


def _run_search(client, *, query: str, top: int, filter_expr: str, vector_queries: list[Any] | None = None) -> list[dict]:
    kwargs = _search_kwargs(query, filter_expr, top)
    started_at = time.perf_counter()
    rows = client.search(vector_queries=vector_queries, **kwargs) if vector_queries else client.search(**kwargs)
    raw_rows = list(rows)
    results = _apply_precision_filters(query, _materialize(raw_rows))
    results = _apply_score_threshold(query, results)
    _log_diagnostics(query, len(raw_rows), results, status="200")
    logger.info(
        "AI SEARCH PHASE | query=%r | phase=%s | seconds=%.2f | raw_results=%s | mapped_results=%s",
        query,
        "vector+semantic" if vector_queries else "semantic",
        time.perf_counter() - started_at,
        len(raw_rows),
        len(results),
    )
    return results


def _odata_escape(value: str) -> str:
    return str(value or "").replace("'", "''")


def _build_security_filter(user_id: str, group_ids: list[str]) -> str:
    """OData filter: chunks where acl_everyone, or the user is in acl_users, or one
    of the user's groups is in acl_groups."""
    clauses = [
        "acl_everyone eq true",
        f"acl_users/any(u: u eq '{_odata_escape(user_id)}')",
    ]
    for gid in group_ids:
        clauses.append(f"acl_groups/any(g: g eq '{_odata_escape(gid)}')")
    return " or ".join(f"({c})" for c in clauses)


def _resolve_user_group_ids(user_id: str) -> list[str]:
    """User's AAD group ids, via a 5-minute session cache. Fail closed (no groups)
    on Graph error so users never see more than they should."""
    from storage.user_permission_cache import user_permission_cache

    cached = user_permission_cache.get(user_id)
    if cached is not None:
        return cached
    try:
        from sharepoint.graph_client import get_user_transitive_groups

        group_ids = get_user_transitive_groups(user_id)
    except Exception as exc:
        logger.warning("Group membership fetch failed for %s: %s. Defaulting to no groups (fail closed).", user_id, exc)
        group_ids = []
    user_permission_cache.set(user_id, group_ids)
    return group_ids


def search_ai_index(query: str, top: int = 8, user_id: str | None = None) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    started_at = time.perf_counter()
    client = get_search_client()

    # Phase 2: per-user security trimming. When enabled and we know the requesting
    # user's AAD object id, restrict results to documents they may access.
    filter_expr = ""
    trimming = bool(Config.ENABLE_SECURITY_TRIMMING and user_id)
    group_ids: list[str] = []
    if trimming:
        group_ids = _resolve_user_group_ids(user_id)
        filter_expr = _build_security_filter(user_id, group_ids)

    logger.info(
        "AI SEARCH QUERY | query=%r | index=%s | top=%s | semantic_config=%s | trimming=%s",
        query,
        Config.AZURE_SEARCH_INDEX_NAME,
        top,
        Config.AZURE_SEARCH_SEMANTIC_CONFIG,
        trimming,
    )

    try:
        from azure.search.documents.models import VectorizedQuery

        vector_started_at = time.perf_counter()
        vector = embed_text(query)
        vector_queries = [VectorizedQuery(vector=vector, k_nearest_neighbors=top, fields="content_vector")]
        logger.info(
            "AI SEARCH PHASE | query=%r | phase=embedding | seconds=%.2f",
            query,
            time.perf_counter() - vector_started_at,
        )
        results = _run_search(client, query=query, top=top, filter_expr=filter_expr, vector_queries=vector_queries)
        if results:
            logger.info("AI SEARCH TOTAL | query=%r | seconds=%.2f | results=%s", query, time.perf_counter() - started_at, len(results))
            return results
    except Exception as exc:
        logger.warning("AI Search semantic hybrid failed; trying semantic keyword. error=%s", exc)

    try:
        results = _run_search(client, query=query, top=top, filter_expr=filter_expr)
        if results:
            logger.info("AI SEARCH TOTAL | query=%r | seconds=%.2f | results=%s", query, time.perf_counter() - started_at, len(results))
            return results
    except Exception as exc:
        logger.warning("AI Search semantic keyword failed; trying plain keyword. error=%s", exc)

    rows = client.search(
        search_text=query,
        filter=filter_expr or None,
        top=top,
        search_fields=["content", "summary", "title", "file_name", "folder_path"],
        select=SELECT_FIELDS,
    )
    raw_rows = list(rows)
    results = _apply_precision_filters(query, _materialize(raw_rows))
    _log_diagnostics(query, len(raw_rows), results, status="200")
    logger.info(
        "AI SEARCH PHASE | query=%r | phase=%s | seconds=%.2f | raw_results=%s | mapped_results=%s",
        query,
        "plain keyword",
        time.perf_counter() - started_at,
        len(raw_rows),
        len(results),
    )
    if results:
        logger.info("AI SEARCH TOTAL | query=%r | seconds=%.2f | results=%s", query, time.perf_counter() - started_at, len(results))
        return results

    # The live Graph fallback is NOT ACL-filtered, so skip it when security trimming
    # is active — surfacing it would bypass per-user permissions (fail closed).
    if trimming:
        logger.info("AI Search returned no results; live Graph fallback skipped under security trimming")
        return []

    # Document-specific fallback: the user may be naming a file that the index does
    # not contain yet (just uploaded, or named differently from its body text). Search
    # the configured drives by filename and read the match directly.
    try:
        targeted = _targeted_document_search(query)
        if targeted:
            logger.info(
                "AI SEARCH TOTAL | query=%r | seconds=%.2f | results=%s | source=targeted_document",
                query,
                time.perf_counter() - started_at,
                len(targeted),
            )
            return targeted
    except Exception as exc:
        logger.warning("Targeted document search failed: %s", exc)

    logger.info("AI Search returned no results; falling back to live Graph search for %r", query)
    try:
        from knowledge_base import get_graph_token_app_only, search_sharepoint

        fallback_started_at = time.perf_counter()
        token = get_graph_token_app_only()
        if not token:
            logger.warning("Live Graph fallback skipped because no app-only token is available")
            return []
        graph_data = search_sharepoint(query, token, user_context=False)
        graph_results = graph_data.get("results", []) if isinstance(graph_data, dict) else []
        mapped = [_normalize_graph_result(dict(row)) for row in graph_results]
        logger.info(
            "LIVE GRAPH FALLBACK | query=%r | raw_results=%s | mapped_results=%s",
            query,
            len(graph_results),
            len(mapped),
        )
        logger.info(
            "AI SEARCH TOTAL | query=%r | seconds=%.2f | results=%s | source=live_graph_fallback",
            query,
            time.perf_counter() - fallback_started_at,
            len(mapped),
        )
        return mapped
    except Exception as exc:
        logger.warning("Live Graph fallback failed: %s", exc)
        return []


def search_sharepoint_chunks(query: str, user_email: str | None = None, top: int = 8, user_id: str | None = None) -> list[dict]:
    results = search_ai_index(query, top=top, user_id=user_id)
    # Audit trace: which security filter was applied and how many results came back.
    trimming = bool(Config.ENABLE_SECURITY_TRIMMING and user_id)
    group_ids = _resolve_user_group_ids(user_id) if trimming else []  # cached — no extra Graph call
    filter_expr = _build_security_filter(user_id, group_ids) if trimming else ""
    audit_logger.info(
        "RETRIEVAL | user=%s | security_trimming=%s | group_count=%d | filter=%s | results=%d | query=%s",
        user_id or "anon",
        trimming,
        len(group_ids),
        filter_expr[:120] if filter_expr else "none",
        len(results),
        (query or "")[:80],
    )
    return results


def list_indexed_documents(limit: int = 50) -> list[dict]:
    """Return distinct indexed documents (one row per document, newest first).

    The index stores one row per chunk, so we dedupe by source_url/title. Used by
    the "what documents do you have" listing path so it reflects what is actually
    searchable in Azure AI Search instead of the legacy (empty) document cache.
    """
    client = get_search_client()
    seen: set[str] = set()
    docs: list[dict] = []
    try:
        rows = client.search(
            search_text="*",
            select=["title", "file_name", "source_url", "site_id", "last_modified"],
            order_by=["last_modified desc"],
            top=1000,
        )
    except Exception as exc:
        logger.warning("list_indexed_documents failed: %s", exc)
        return []

    for row in rows:
        row = dict(row)
        url = _first_non_empty(row.get("source_url"))
        title = _first_non_empty(row.get("title"), row.get("file_name"))
        if not title:
            continue
        key = (url or title).lower()
        if key in seen:
            continue
        seen.add(key)
        docs.append(
            {
                "title": title,
                "url": url,
                "site_id": _first_non_empty(row.get("site_id")),
                "last_modified": row.get("last_modified"),
            }
        )
        if len(docs) >= limit:
            break
    return docs
