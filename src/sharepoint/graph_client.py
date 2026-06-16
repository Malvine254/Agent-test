from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import requests

from config import Config

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_TOKEN_CACHE: dict[str, object] = {"token": "", "expires_at": 0.0}


def get_graph_access_token() -> str:
    cached_token = str(_TOKEN_CACHE.get("token") or "")
    expires_at = float(_TOKEN_CACHE.get("expires_at") or 0)
    if cached_token and expires_at > time.time() + 120:
        return cached_token

    import msal

    authority = f"https://login.microsoftonline.com/{Config.GRAPH_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        Config.GRAPH_CLIENT_ID,
        authority=authority,
        client_credential=Config.GRAPH_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    token = result.get("access_token")
    if not token:
        raise RuntimeError("Could not acquire Microsoft Graph access token")
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = time.time() + int(result.get("expires_in", 3600))
    return token


def graph_get(path_or_url: str) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith("https://") else f"{GRAPH_ROOT}{path_or_url}"
    response = requests.get(url, headers={"Authorization": f"Bearer {get_graph_access_token()}"}, timeout=Config.GRAPH_TIMEOUT)
    response.raise_for_status()
    return response.json()


def list_configured_sharepoint_drives() -> list[dict]:
    drives: list[dict] = []
    for site_url in Config.get_sharepoint_sites_list():
        parsed = urlparse(site_url)
        host = parsed.netloc
        path = parsed.path.rstrip("/")
        if not host or not path:
            logger.warning("Skipping invalid SharePoint site URL: %s", site_url)
            continue
        site = graph_get(f"/sites/{host}:{path}:")
        for drive in graph_get(f"/sites/{site['id']}/drives").get("value", []):
            drives.append({"site_id": site["id"], "drive_id": drive["id"], "drive": drive})
    logger.info("Discovered %s configured SharePoint document libraries", len(drives))
    return drives


def list_drive_items(site_id: str, drive_id: str, *, max_items: int | None = None, max_depth: int = 8) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    stack = [(f"/drives/{drive_id}/root/children?$top=200", 0)]
    while stack:
        path, depth = stack.pop()
        data = graph_get(path)
        for item in data.get("value", []):
            item["site_id"] = site_id
            item["drive_id"] = drive_id
            item_key = str(item.get("webUrl") or item.get("id") or "")
            if item_key in seen:
                continue
            seen.add(item_key)
            if "folder" in item and depth < max_depth:
                stack.append((f"/drives/{drive_id}/items/{item['id']}/children?$top=200", depth + 1))
            elif "file" in item:
                items.append(item)
                if max_items and len(items) >= max_items:
                    return items
        next_link = data.get("@odata.nextLink")
        if next_link:
            stack.append((next_link, depth))
    return items


def download_file_bytes(drive_id: str, item_id: str) -> bytes:
    url = f"{GRAPH_ROOT}/drives/{drive_id}/items/{item_id}/content"
    response = requests.get(url, headers={"Authorization": f"Bearer {get_graph_access_token()}"}, timeout=Config.GRAPH_TIMEOUT)
    response.raise_for_status()
    return response.content
