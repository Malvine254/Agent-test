"""Secure, expiring artifact store + download endpoint.

Generated files (charts, .xlsx, .docx, .pdf, .zip, ...) produced by the code
interpreter are saved here and exposed through a single unguessable, time-limited
URL on the bot's own public origin (dev tunnel locally, Container Apps ingress in
production). This is the same security model as a SAS / signed URL:

  * 256-bit cryptographically random token in the path (not guessable / not listable)
  * one token == one file
  * short expiry (default 60 min), swept lazily
  * path-traversal safe (files live in a private temp dir, served by token only)
  * Content-Disposition: attachment so clients download rather than execute

No directory listing is exposed. Tokens are the only way to reach a file.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = int(os.environ.get("ARTIFACT_TTL_SECONDS", "3600"))
_ROUTE_PREFIX = "/files"


def _sanitize_filename(name: str) -> str:
    """Strip path components and dangerous characters from a download name."""
    base = os.path.basename(str(name or "")).strip() or "download.bin"
    safe = "".join(c for c in base if c.isalnum() or c in (" ", ".", "-", "_", "(", ")")).strip()
    return safe or "download.bin"


@dataclass
class _Artifact:
    path: str
    download_name: str
    media_type: str
    expires_at: float


class ArtifactStore:
    """Thread-safe store of downloadable artifacts keyed by random tokens."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._dir = tempfile.mkdtemp(prefix="ci_artifacts_")
        self._items: dict[str, _Artifact] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def save_bytes(self, data: bytes, filename: str) -> str:
        """Persist raw bytes and return a download token."""
        token = secrets.token_urlsafe(32)
        download_name = _sanitize_filename(filename)
        path = os.path.join(self._dir, token)
        with open(path, "wb") as fh:
            fh.write(data)
        media_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        with self._lock:
            self._items[token] = _Artifact(
                path=path,
                download_name=download_name,
                media_type=media_type,
                expires_at=time.time() + self._ttl,
            )
        self._sweep()
        return token

    def save_file(self, src_path: str, filename: Optional[str] = None) -> str:
        """Copy an existing file into the store and return a download token."""
        with open(src_path, "rb") as fh:
            data = fh.read()
        return self.save_bytes(data, filename or os.path.basename(src_path))

    def resolve(self, token: str) -> Optional[_Artifact]:
        """Return a live artifact for ``token`` or None if missing/expired."""
        with self._lock:
            item = self._items.get(token)
            if not item:
                return None
            if item.expires_at < time.time():
                self._items.pop(token, None)
                self._safe_unlink(item.path)
                return None
            return item

    # ------------------------------------------------------------------
    def _sweep(self) -> None:
        now = time.time()
        with self._lock:
            expired = [t for t, a in self._items.items() if a.expires_at < now]
            for t in expired:
                a = self._items.pop(t, None)
                if a:
                    self._safe_unlink(a.path)

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
_store: Optional[ArtifactStore] = None
_store_lock = threading.Lock()


def get_artifact_store() -> ArtifactStore:
    """Return the process-wide artifact store (created on first use)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ArtifactStore()
    return _store


def public_base_url() -> str:
    """Best-effort public base URL for building download links.

    Prefers an explicitly configured public URL, then the dev-tunnel endpoint /
    domain written by the Teams toolkit. Returns "" if unknown (caller should
    fall back to a relative path)."""
    for key in ("PUBLIC_BASE_URL", "BOT_ENDPOINT"):
        val = os.environ.get(key, "").strip()
        if val.startswith("http"):
            # BOT_ENDPOINT may point at /api/messages; keep only the origin.
            from urllib.parse import urlparse

            p = urlparse(val)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}"
    domain = os.environ.get("BOT_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    return ""


def download_url(token: str) -> str:
    """Build a (preferably absolute) download URL for a token."""
    base = public_base_url()
    return f"{base}{_ROUTE_PREFIX}/{token}"


def register_file_routes(fastapi_app) -> None:
    """Register the GET ``/files/{token}`` download route on the FastAPI app.

    Must be called before the server starts. Safe to call once.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    store = get_artifact_store()

    async def _download(token: str):
        # Token is opaque and validated by lookup; never used as a path component.
        if not token or len(token) > 128 or "/" in token or "\\" in token:
            raise HTTPException(status_code=404, detail="Not found")
        item = store.resolve(token)
        if not item or not os.path.isfile(item.path):
            raise HTTPException(status_code=404, detail="Not found or expired")
        return FileResponse(
            path=item.path,
            media_type=item.media_type,
            filename=item.download_name,
            headers={"Cache-Control": "no-store"},
        )

    fastapi_app.add_api_route(
        f"{_ROUTE_PREFIX}/{{token}}",
        _download,
        methods=["GET"],
        include_in_schema=False,
    )
    logger.info("Artifact download route registered at %s/{token}", _ROUTE_PREFIX)
