from __future__ import annotations

import logging

from config import Config

logger = logging.getLogger(__name__)


def get_search_client():
    """Read/query client — uses the query-only key (falls back to admin if unset)."""
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient

    key = Config.AZURE_SEARCH_QUERY_KEY or Config.AZURE_SEARCH_ADMIN_KEY
    if not Config.AZURE_SEARCH_ENDPOINT or not key:
        raise RuntimeError("Azure AI Search endpoint/query key is missing")
    return SearchClient(Config.AZURE_SEARCH_ENDPOINT, Config.AZURE_SEARCH_INDEX_NAME, AzureKeyCredential(key))


def get_admin_search_client():
    """Write client (upload/delete) — requires the admin key. Used only by the indexer."""
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient

    key = Config.AZURE_SEARCH_ADMIN_KEY
    if not Config.AZURE_SEARCH_ENDPOINT or not key:
        raise RuntimeError("Azure AI Search endpoint/admin key is missing")
    return SearchClient(Config.AZURE_SEARCH_ENDPOINT, Config.AZURE_SEARCH_INDEX_NAME, AzureKeyCredential(key))


def upload_chunks(chunks: list[dict]) -> None:
    if not chunks:
        return
    result = get_admin_search_client().upload_documents(chunks)
    failures = [r for r in result if not getattr(r, "succeeded", False)]
    if failures:
        raise RuntimeError(f"Failed to upload {len(failures)} Azure AI Search chunks")
    logger.info("Uploaded %s chunks to Azure AI Search", len(chunks))


def get_document_metadata(document_id: str) -> dict | None:
    client = get_search_client()
    safe_document_id = document_id.replace("'", "''")
    results = client.search(
        search_text="*",
        filter=f"document_id eq '{safe_document_id}'",
        select=["document_id", "checksum", "last_modified", "indexed_at"],
        top=1,
    )
    for row in results:
        return dict(row)
    return None


def get_documents_by_source_url(source_url: str) -> list[dict]:
    client = get_search_client()
    safe_source_url = source_url.replace("'", "''")
    results = client.search(
        search_text="*",
        filter=f"source_url eq '{safe_source_url}'",
        select=["id", "document_id", "source_url", "checksum", "indexed_at"],
        top=1000,
    )
    return [dict(row) for row in results]


def delete_document_chunks(document_id: str) -> None:
    client = get_admin_search_client()
    safe_document_id = document_id.replace("'", "''")
    results = client.search(search_text="*", filter=f"document_id eq '{safe_document_id}'", select=["id"], top=1000)
    docs = [{"id": row["id"]} for row in results]
    if docs:
        client.delete_documents(docs)
    logger.info("Deleted %s old chunks for document_id=%s", len(docs), document_id)


def delete_documents_by_source_url(source_url: str) -> None:
    client = get_admin_search_client()
    safe_source_url = source_url.replace("'", "''")
    results = client.search(search_text="*", filter=f"source_url eq '{safe_source_url}'", select=["id"], top=1000)
    docs = [{"id": row["id"]} for row in results]
    if docs:
        client.delete_documents(docs)
    logger.info("Deleted %s old chunks for source_url=%s", len(docs), source_url)


def upsert_document_chunks(document_id: str, chunks: list[dict]) -> None:
    delete_document_chunks(document_id)
    upload_chunks(chunks)
