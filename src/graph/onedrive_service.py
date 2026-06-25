"""App-only Microsoft Graph OneDrive (per-user drive) operations.

The user's personal OneDrive is inherently scoped by ``/users/{user_id}/drive``,
so app-only access here only ever reaches the caller's own files. SharePoint
library search remains served by the indexed Azure AI Search layer with ACL
trimming. Requires Files.Read.All (or Files.ReadWrite.All) application
permission.
"""

from __future__ import annotations

import logging
from typing import Optional

from graph.client import GraphError, graph_get_all, user_segment

logger = logging.getLogger(__name__)

_FILE_SELECT = "id,name,size,webUrl,lastModifiedDateTime,file,folder,parentReference"


def _summarize(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "size": item.get("size"),
        "web_url": item.get("webUrl") or "",
        "modified": item.get("lastModifiedDateTime") or "",
        "is_folder": "folder" in item,
        "drive_id": (item.get("parentReference") or {}).get("driveId") or "",
        "download_url": item.get("@microsoft.graph.downloadUrl") or "",
    }


def search_my_files(user_id: str, query: str, *, top: int = 10) -> list[dict]:
    """Search the user's OneDrive for files matching ``query``."""
    seg = user_segment(user_id)
    safe = (query or "").replace("'", "''").strip()
    if not safe:
        return []
    top = max(1, min(int(top or 10), 25))
    items = graph_get_all(
        f"{seg}/drive/root/search(q='{safe}')?$select={_FILE_SELECT}&$top={top}",
        max_items=top,
    )
    return [_summarize(i) for i in items if "file" in i or "folder" in i]


def recent_files(user_id: str, *, top: int = 10) -> list[dict]:
    """List the user's most recently modified OneDrive files.

    The Graph ``/drive/recent`` endpoint relies on per-user activity signals and
    is rejected under app-only auth (``InvalidRequestVroomException``). Listing the
    drive root ordered by ``lastModifiedDateTime`` is the reliable app-only path.
    """
    seg = user_segment(user_id)
    top = max(1, min(int(top or 10), 25))
    try:
        items = graph_get_all(
            f"{seg}/drive/root/children",
            params={
                "$select": _FILE_SELECT,
                "$top": top,
                "$orderby": "lastModifiedDateTime desc",
            },
            max_items=top,
        )
    except GraphError:
        # Some drives reject $orderby on children; fall back to client-side sort.
        items = graph_get_all(
            f"{seg}/drive/root/children",
            params={"$select": _FILE_SELECT, "$top": top},
            max_items=top,
        )
        items.sort(key=lambda i: i.get("lastModifiedDateTime") or "", reverse=True)
    return [_summarize(i) for i in items if "file" in i or "folder" in i][:top]


def get_file_by_name(user_id: str, name: str) -> Optional[dict]:
    """Return the best file match for an exact/near filename, or None."""
    matches = search_my_files(user_id, name, top=5)
    if not matches:
        return None
    lowered = (name or "").lower().strip()
    for m in matches:
        if m["name"].lower() == lowered:
            return m
    return matches[0]


def download_file(user_id: str, item_id: str) -> bytes:
    """Download a file from the user's OneDrive by item id."""
    import requests

    from graph.client import GRAPH_RW_TIMEOUT
    from sharepoint.graph_client import GRAPH_ROOT, get_graph_access_token

    seg = user_segment(user_id)
    url = f"{GRAPH_ROOT}{seg}/drive/items/{item_id}/content"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {get_graph_access_token()}"},
        timeout=GRAPH_RW_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.content
