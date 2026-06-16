from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

from config import Config
from search.ai_search_client import (
    delete_documents_by_source_url,
    get_document_metadata,
    get_documents_by_source_url,
    upsert_document_chunks,
)
from search.ai_search_index import ensure_sharepoint_index
from search.chunking import chunk_document
from search.embeddings import embed_text
from sharepoint.sharepoint_reader import download_sharepoint_document, extract_document_text, list_sharepoint_documents

logger = logging.getLogger(__name__)


def build_document_id(site_id: str, drive_id: str, item_id: str) -> str:
    return hashlib.sha256(f"{site_id}:{drive_id}:{item_id}".encode("utf-8")).hexdigest()


def calculate_checksum(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def should_reindex(item: dict, existing_metadata: dict | None) -> bool:
    if not existing_metadata:
        return True
    return item.get("checksum") != existing_metadata.get("checksum")


def index_sharepoint_document(item: dict) -> dict:
    file_name = item.get("name") or ""
    ext = os.path.splitext(file_name)[1].lower().lstrip(".")
    file_bytes = download_sharepoint_document(item)
    content = extract_document_text(file_name, file_bytes)
    if not content.strip():
        return {"status": "skipped", "reason": "empty_or_unsupported", "file_name": file_name}

    document_id = build_document_id(item.get("site_id", ""), item.get("drive_id", ""), item.get("id", ""))
    checksum = calculate_checksum(content)
    item_with_checksum = {**item, "checksum": checksum}
    source_url = (item.get("webUrl") or "").strip()
    existing_metadata = get_document_metadata(document_id)
    if not should_reindex(item_with_checksum, existing_metadata):
        return {"status": "skipped", "reason": "unchanged", "document_id": document_id, "file_name": file_name}

    if source_url:
        existing_by_source = get_documents_by_source_url(source_url)
        if existing_by_source:
            same_checksum = any(row.get("checksum") == checksum for row in existing_by_source)
            if same_checksum:
                logger.info(
                    "INDEXING SKIP | file=%s | reason=duplicate_source_url_same_checksum | source_url=%s",
                    file_name,
                    source_url,
                )
                return {
                    "status": "skipped",
                    "reason": "duplicate_source_url_same_checksum",
                    "document_id": document_id,
                    "file_name": file_name,
                }
            delete_documents_by_source_url(source_url)

    now = datetime.now(timezone.utc).isoformat()
    chunks = []
    for chunk in chunk_document(content):
        chunk_id = chunk["chunk_id"]
        chunks.append(
            {
                "id": f"{document_id}-{chunk_id}",
                "document_id": document_id,
                "chunk_id": chunk_id,
                "title": item.get("name") or file_name,
                "file_name": file_name,
                "file_type": ext,
                "source_url": source_url,
                "site_id": item.get("site_id") or "",
                "drive_id": item.get("drive_id") or "",
                "item_id": item.get("id") or "",
                "folder_path": item.get("parentReference", {}).get("path", ""),
                "last_modified": item.get("lastModifiedDateTime"),
                "indexed_at": now,
                "content": chunk["content"],
                "content_vector": embed_text(chunk["content"]),
                "summary": chunk.get("summary", ""),
                "source_type": "sharepoint",
                "checksum": checksum,
                "acl_users": [],
                "acl_groups": [],
            }
        )
    upsert_document_chunks(document_id, chunks)
    return {"status": "indexed", "document_id": document_id, "chunks": len(chunks), "file_name": file_name}


def index_all_sharepoint_documents() -> dict:
    ensure_sharepoint_index()
    summary = {"indexed_documents": 0, "indexed_chunks": 0, "skipped_documents": 0, "failed_documents": 0}
    logger.info(
        "INDEXING RUN STARTED | index=%s | max_items=%s | max_depth=%s",
        Config.AZURE_SEARCH_INDEX_NAME,
        Config.SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN,
        Config.SHAREPOINT_INDEX_MAX_DEPTH,
    )
    documents = list_sharepoint_documents(
        max_items=Config.SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN,
        max_depth=Config.SHAREPOINT_INDEX_MAX_DEPTH,
    )
    summary["discovered_documents"] = len(documents)
    logger.info("INDEXING DISCOVERY | discovered_documents=%s", len(documents))
    for item in documents:
        try:
            result = index_sharepoint_document(item)
            if result.get("status") == "indexed":
                summary["indexed_documents"] += 1
                summary["indexed_chunks"] += int(result.get("chunks", 0))
            else:
                summary["skipped_documents"] += 1
        except Exception as exc:
            logger.warning("INDEXING DOCUMENT FAILED | file=%s | error=%s", item.get("name"), exc)
            summary["failed_documents"] += 1
    logger.info(
        "INDEXING RUN FINISHED | discovered=%s | indexed_docs=%s | indexed_chunks=%s | skipped=%s | failed=%s",
        summary.get("discovered_documents", 0),
        summary["indexed_documents"],
        summary["indexed_chunks"],
        summary["skipped_documents"],
        summary["failed_documents"],
    )
    return summary
