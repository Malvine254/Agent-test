"""Azure Blob Storage helper for caching uploaded/attachment bytes.

Used to persist files that arrive via Teams/OneDrive so they can be re-read
reliably (e.g. for the code interpreter or repeated questions) without going
back to Graph each time. Degrades gracefully to a no-op when the storage
account or the ``azure-storage-blob`` package is not available, so the bot
keeps working without it.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

_UNAVAILABLE_LOGGED = False


def _container() -> str:
    return (Config.AZURE_STORAGE_CONTAINER_UPLOADS or "uploads").strip()


def is_enabled() -> bool:
    """True when a connection string and the SDK are both available."""
    if not getattr(Config, "AZURE_STORAGE_CONNECTION_STRING", ""):
        return False
    try:
        import azure.storage.blob  # noqa: F401
    except Exception:
        return False
    return True


def _client():
    from azure.storage.blob import BlobServiceClient

    return BlobServiceClient.from_connection_string(Config.AZURE_STORAGE_CONNECTION_STRING)


def _ensure_container(svc, name: str) -> None:
    try:
        svc.create_container(name)
    except Exception:
        # Already exists (or no permission to create) — assume it exists.
        pass


def blob_name_for(conversation_id: str, filename: str) -> str:
    """Deterministic blob path: conversation-scoped, filename-stable."""
    conv = (conversation_id or "shared").replace("/", "_")[:80]
    safe = (filename or "file.bin").replace("/", "_").replace("\\", "_")[:120]
    digest = hashlib.sha256(f"{conv}/{safe}".encode("utf-8")).hexdigest()[:16]
    return f"{conv}/{digest}_{safe}"


def upload_bytes(data: bytes, conversation_id: str, filename: str) -> Optional[str]:
    """Cache ``data`` to blob storage. Returns the blob path or None on failure."""
    global _UNAVAILABLE_LOGGED
    if not is_enabled():
        if not _UNAVAILABLE_LOGGED:
            logger.info("Blob cache disabled (no connection string / SDK); skipping upload")
            _UNAVAILABLE_LOGGED = True
        return None
    try:
        svc = _client()
        name = _container()
        _ensure_container(svc, name)
        blob_path = blob_name_for(conversation_id, filename)
        svc.get_blob_client(container=name, blob=blob_path).upload_blob(data, overwrite=True)
        logger.info("Cached attachment to blob %s/%s (%d bytes)", name, blob_path, len(data))
        return blob_path
    except Exception as exc:
        logger.warning("Blob upload failed for %s: %s", filename, exc)
        return None


def download_bytes(conversation_id: str, filename: str) -> Optional[bytes]:
    """Read a previously cached attachment. Returns bytes or None."""
    if not is_enabled():
        return None
    try:
        svc = _client()
        name = _container()
        blob_path = blob_name_for(conversation_id, filename)
        return svc.get_blob_client(container=name, blob=blob_path).download_blob().readall()
    except Exception as exc:
        logger.debug("Blob download miss for %s: %s", filename, exc)
        return None
