"""Live Microsoft Graph Search (mail, OneDrive, SharePoint) via delegated auth.

Uses the user's OBO Graph token (get_graph_token_delegated). Returns [] when no SSO
token is available (e.g. local/Playground) or Graph doesn't respond within timeout_ms.
Results are normalized to the AI Search chunk format so the prompt builder is unchanged.
Never raises — failures are logged and return an empty list.
"""
from __future__ import annotations

import logging
import time

import requests

from sharepoint.graph_client import get_graph_token_delegated

logger = logging.getLogger(__name__)
audit_log = logging.getLogger("audit")

GRAPH_SEARCH_ENDPOINT = "https://graph.microsoft.com/v1.0/search/query"


def search_graph(query: str, user_id: str, scope: list[str], timeout_ms: int = 800) -> list[dict]:
    token = get_graph_token_delegated(user_id)
    if not token:
        logger.debug("Graph search skipped — no delegated token for user %s", (user_id or "")[:8])
        return []

    payload = {
        "requests": [
            {
                "entityTypes": _scope_to_entity_types(scope),
                "query": {"queryString": query},
                "from": 0,
                "size": 5,
                "fields": [
                    "subject", "from", "receivedDateTime", "bodyPreview",
                    "name", "webUrl", "lastModifiedDateTime", "summary",
                ],
            }
        ]
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    start = time.time()
    try:
        response = requests.post(GRAPH_SEARCH_ENDPOINT, json=payload, headers=headers, timeout=timeout_ms / 1000)
        elapsed_ms = int((time.time() - start) * 1000)

        if response.status_code == 401:
            logger.warning("Graph search 401 — SSO token may be stale for user %s", (user_id or "")[:8])
            return []
        if response.status_code != 200:
            logger.warning("Graph search %s in %dms: %s", response.status_code, elapsed_ms, response.text[:200])
            return []

        results = _parse_graph_response(response.json())
        audit_log.info(
            "GRAPH_SEARCH | user=%s | scope=%s | chunks=%d | elapsed_ms=%d",
            (user_id or "")[:8], scope, len(results), elapsed_ms,
        )
        return results
    except requests.exceptions.Timeout:
        logger.warning("Graph search timeout after %dms — proceeding without live data", timeout_ms)
        return []
    except Exception as exc:
        logger.warning("Graph search failed: %s", exc)
        return []


def _scope_to_entity_types(scope: list[str]) -> list[str]:
    mapping = {
        "mail": ["message"],
        "drive": ["driveItem"],
        "sharepoint": ["listItem", "site"],
        "all": ["message", "driveItem", "listItem"],
    }
    types: set[str] = set()
    for s in scope or []:
        types.update(mapping.get(s, ["message", "driveItem", "listItem"]))
    return list(types) or ["message", "driveItem", "listItem"]


def _parse_graph_response(response: dict) -> list[dict]:
    """Normalize Graph hits to the chunk format used by AI Search."""
    chunks: list[dict] = []
    for container in response.get("value", []):
        hits = (container.get("hitsContainers") or [{}])[0].get("hits", [])
        for hit in hits:
            resource = hit.get("resource", {})
            dtype = resource.get("@odata.type", "")
            summary = hit.get("summary", "")

            if "message" in dtype:
                from_addr = (resource.get("from", {}).get("emailAddress", {}).get("address", ""))
                chunk = {
                    "title": resource.get("subject", "Email"),
                    "content": (
                        f"From: {from_addr}\n"
                        f"Received: {resource.get('receivedDateTime', '')}\n"
                        f"Preview: {resource.get('bodyPreview', summary)[:500]}"
                    ),
                    "source_url": resource.get("webLink", ""),
                    "source_type": "email",
                }
            elif "driveItem" in dtype:
                chunk = {
                    "title": resource.get("name", "File"),
                    "content": summary or resource.get("name", ""),
                    "source_url": resource.get("webUrl", ""),
                    "source_type": "onedrive",
                }
            else:
                chunk = {
                    "title": resource.get("displayName", "Document"),
                    "content": summary,
                    "source_url": resource.get("webUrl", ""),
                    "source_type": "sharepoint_live",
                }

            if (chunk.get("content") or "").strip():
                chunks.append(chunk)
    return chunks
