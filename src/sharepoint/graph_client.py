from __future__ import annotations

import logging
import re
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


# ---------------------------------------------------------------------------
# Security trimming: capture SharePoint ACLs at index time (Phase 2.2)
# ---------------------------------------------------------------------------
# Per-indexing-run cache so we don't re-fetch library permissions for every
# file. Cleared at the start of each run via clear_permission_cache().
_PERMISSION_CACHE: dict[str, dict] = {}

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def clear_permission_cache() -> None:
    """Reset the per-run permission cache. Call at the start of an indexing run."""
    _PERMISSION_CACHE.clear()


def _is_guid(value: Any) -> bool:
    return isinstance(value, str) and bool(_GUID_RE.match(value.strip()))


def _extract_identity(identity: dict, acl_users: set, acl_groups: set) -> None:
    """Pull AAD object ids out of a grantedTo* identity set.

    Only AAD GUIDs are usable for query-time trimming (they match a user's
    transitiveMemberOf). SharePoint siteGroup/sharePointGroup/siteUser ids are
    numeric/site-local and are intentionally skipped (logged by the caller).
    """
    if not isinstance(identity, dict):
        return
    user = identity.get("user") or {}
    group = identity.get("group") or {}
    if _is_guid(user.get("id")):
        acl_users.add(user["id"])
    if _is_guid(group.get("id")):
        acl_groups.add(group["id"])


def _parse_permissions(permissions: list[dict]) -> dict:
    """Parse a Graph /permissions response into {acl_users, acl_groups, acl_everyone}."""
    acl_users: set = set()
    acl_groups: set = set()
    acl_everyone = False
    unresolved_site_groups = 0

    for perm in permissions or []:
        link = perm.get("link") or {}
        if link.get("scope") in ("anonymous", "organization"):
            acl_everyone = True

        _extract_identity(perm.get("grantedToV2") or {}, acl_users, acl_groups)
        for identity in perm.get("grantedToIdentitiesV2") or []:
            _extract_identity(identity, acl_users, acl_groups)

        # Track SharePoint-only groups that carry no AAD id (classic sites). These
        # cannot be matched at query time; they fail closed (no grant captured).
        granted = perm.get("grantedToV2") or {}
        if (granted.get("siteGroup") or granted.get("sharePointGroup")) and not _is_guid((granted.get("group") or {}).get("id")):
            unresolved_site_groups += 1

    if unresolved_site_groups:
        logger.debug(
            "Permission parse: %s SharePoint group grant(s) had no AAD id and were skipped (fail closed)",
            unresolved_site_groups,
        )

    return {"acl_users": sorted(acl_users), "acl_groups": sorted(acl_groups), "acl_everyone": acl_everyone}


def get_user_transitive_groups(user_object_id: str) -> list[str]:
    """All AAD group object ids the user belongs to (transitive).

    Requires GroupMember.Read.All (or Directory.Read.All) on the app registration.
    Handles @odata.nextLink pagination — a user in many groups paginates, and
    missing a page would silently under-grant access.
    """
    if not user_object_id:
        return []
    url = f"/users/{user_object_id}/transitiveMemberOf/microsoft.graph.group?$select=id&$top=100"
    group_ids: list[str] = []
    while url:
        data = graph_get(url)
        group_ids.extend(g["id"] for g in data.get("value", []) if g.get("id"))
        url = data.get("@odata.nextLink") or ""
    return group_ids


def get_library_permissions(site_id: str, drive_id: str) -> dict:
    """Permissions for the document library root, cached per run."""
    cache_key = f"lib:{site_id}:{drive_id}"
    cached = _PERMISSION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    data = graph_get(f"/sites/{site_id}/drives/{drive_id}/root/permissions")
    parsed = _parse_permissions(data.get("value", []))
    _PERMISSION_CACHE[cache_key] = parsed
    return parsed


def get_item_permissions(site_id: str, drive_id: str, item_id: str) -> dict | None:
    """Item-level permissions, or None if the item fully inherits from its parent.

    When None is returned the caller should fall back to get_library_permissions().
    """
    data = graph_get(f"/drives/{drive_id}/items/{item_id}/permissions")
    permissions = data.get("value", [])
    # A permission entry with a non-empty inheritedFrom is inherited; an entry with
    # absent/empty inheritedFrom is unique to this item.
    has_unique = any(not p.get("inheritedFrom") for p in permissions)
    if not has_unique:
        return None
    return _parse_permissions(permissions)
