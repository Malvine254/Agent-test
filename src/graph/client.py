"""Generic app-only Microsoft Graph request helpers.

Reuses the application-permission token from :mod:`sharepoint.graph_client`
(already pointed at the configured Graph app registration) and adds the verbs
the productivity features need (POST/PATCH/DELETE + transparent paging). The
short ``Config.GRAPH_TIMEOUT`` used for indexing is too aggressive for write
operations like sending mail, so this layer uses its own, longer timeout.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

from sharepoint.graph_client import GRAPH_ROOT, get_graph_access_token

logger = logging.getLogger(__name__)

# Writes (sendMail, create event/task, drafts) can take noticeably longer than
# the 8s indexing fast-fail. Keep a separate, generous timeout.
GRAPH_RW_TIMEOUT = int(os.environ.get("GRAPH_RW_TIMEOUT", "30"))

# Max pages to follow on a paged collection, to bound latency/cost.
_MAX_PAGES = int(os.environ.get("GRAPH_MAX_PAGES", "10"))


class GraphError(RuntimeError):
    """Raised when a Graph request fails; carries status + a readable message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _full_url(path_or_url: str) -> str:
    if path_or_url.startswith("https://"):
        return path_or_url
    return f"{GRAPH_ROOT}{path_or_url}"


def _headers(extra: Optional[dict] = None) -> dict:
    headers = {"Authorization": f"Bearer {get_graph_access_token()}"}
    if extra:
        headers.update(extra)
    return headers


def _raise_for_status(resp: requests.Response, context: str) -> None:
    if resp.status_code < 400:
        return
    detail = ""
    try:
        body = resp.json()
        err = (body or {}).get("error") or {}
        detail = err.get("message") or str(body)
    except Exception:
        detail = (resp.text or "")[:300]
    logger.warning("Graph %s -> HTTP %s: %s", context, resp.status_code, detail[:200])
    raise GraphError(resp.status_code, detail or f"HTTP {resp.status_code}")


def graph_get(path_or_url: str, *, params: Optional[dict] = None) -> dict[str, Any]:
    """GET a single Graph resource/collection page."""
    resp = requests.get(
        _full_url(path_or_url),
        headers=_headers({"ConsistencyLevel": "eventual"}),
        params=params,
        timeout=GRAPH_RW_TIMEOUT,
    )
    _raise_for_status(resp, f"GET {path_or_url[:80]}")
    return resp.json() if resp.content else {}


def graph_get_all(path_or_url: str, *, params: Optional[dict] = None, max_items: int = 50) -> list[dict]:
    """GET a collection, following @odata.nextLink up to limits."""
    items: list[dict] = []
    url: Optional[str] = path_or_url
    first = True
    pages = 0
    while url and pages < _MAX_PAGES and len(items) < max_items:
        data = graph_get(url, params=params if first else None)
        items.extend(data.get("value", []) or [])
        url = data.get("@odata.nextLink")
        first = False
        pages += 1
    return items[:max_items]


def graph_post(path_or_url: str, json_body: Optional[dict] = None) -> dict[str, Any]:
    """POST to Graph. Returns the JSON body (empty dict for 202/204)."""
    resp = requests.post(
        _full_url(path_or_url),
        headers=_headers({"Content-Type": "application/json"}),
        json=json_body or {},
        timeout=GRAPH_RW_TIMEOUT,
    )
    _raise_for_status(resp, f"POST {path_or_url[:80]}")
    return resp.json() if resp.content else {}


def graph_patch(path_or_url: str, json_body: dict, *, etag: Optional[str] = None) -> dict[str, Any]:
    """PATCH a Graph resource. Planner requires the item's ETag as If-Match."""
    extra = {"Content-Type": "application/json"}
    if etag:
        extra["If-Match"] = etag
    resp = requests.patch(
        _full_url(path_or_url),
        headers=_headers(extra),
        json=json_body,
        timeout=GRAPH_RW_TIMEOUT,
    )
    _raise_for_status(resp, f"PATCH {path_or_url[:80]}")
    return resp.json() if resp.content else {}


def graph_delete(path_or_url: str, *, etag: Optional[str] = None) -> None:
    extra = {}
    if etag:
        extra["If-Match"] = etag
    resp = requests.delete(_full_url(path_or_url), headers=_headers(extra), timeout=GRAPH_RW_TIMEOUT)
    _raise_for_status(resp, f"DELETE {path_or_url[:80]}")


# ---------------------------------------------------------------------------
# User identity resolution
# ---------------------------------------------------------------------------
_USER_CACHE: dict[str, dict] = {}


def resolve_user(user_id: str) -> dict:
    """Resolve an Entra object id (or UPN) to a user object.

    Returns ``{"id", "userPrincipalName", "displayName", "mail"}``. Cached per
    process. Raises GraphError if the user can't be resolved.
    """
    key = (user_id or "").strip()
    if not key:
        raise GraphError(400, "No user id available to act on behalf of.")
    if key in _USER_CACHE:
        return _USER_CACHE[key]
    data = graph_get(
        f"/users/{key}",
        params={"$select": "id,userPrincipalName,displayName,mail"},
    )
    _USER_CACHE[key] = data
    return data


def user_segment(user_id: str) -> str:
    """Return the ``/users/{id}`` path segment, validating the id is usable."""
    key = (user_id or "").strip()
    if not key:
        raise GraphError(400, "No signed-in user context is available for this action.")
    return f"/users/{key}"
