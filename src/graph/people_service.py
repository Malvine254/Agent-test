"""App-only Microsoft Graph people / directory lookup (acting for a user).

Used to turn a person's name into an email address and to list relevant
contacts. Requires People.Read.All / User.Read.All application permissions.
"""

from __future__ import annotations

import logging

from graph.client import graph_get_all, user_segment

logger = logging.getLogger(__name__)


def _person(p: dict) -> dict:
    emails = p.get("scoredEmailAddresses") or []
    address = emails[0].get("address") if emails else (p.get("mail") or p.get("userPrincipalName") or "")
    return {
        "name": p.get("displayName") or "",
        "email": address or "",
        "job_title": p.get("jobTitle") or "",
    }


def find_people(user_id: str, name: str, *, top: int = 5) -> list[dict]:
    """Resolve a name to candidate people/email addresses.

    Tries the user's relevant people first, then the directory.
    """
    seg = user_segment(user_id)
    name = (name or "").strip()
    results: list[dict] = []
    if name:
        try:
            items = graph_get_all(
                f"{seg}/people?$search=\"{name}\""
                "&$select=displayName,scoredEmailAddresses,jobTitle",
                max_items=top,
            )
            results = [_person(p) for p in items if (_person(p)["email"])]
        except Exception as exc:
            logger.debug("people $search failed for %r: %s", name, exc)

    if not results and name:
        try:
            safe = name.replace("'", "''")
            items = graph_get_all(
                "/users?$filter=startswith(displayName,'%s') or startswith(mail,'%s')"
                "&$select=displayName,mail,userPrincipalName,jobTitle" % (safe, safe),
                max_items=top,
            )
            results = [_person(p) for p in items if (_person(p)["email"])]
        except Exception as exc:
            logger.debug("directory lookup failed for %r: %s", name, exc)
    return results[:top]


def list_relevant_people(user_id: str, *, top: int = 15) -> list[dict]:
    """List the people most relevant to the user (frequent contacts)."""
    seg = user_segment(user_id)
    items = graph_get_all(
        f"{seg}/people?$select=displayName,scoredEmailAddresses,jobTitle&$top={int(top)}",
        max_items=top,
    )
    return [_person(p) for p in items if _person(p)["email"]]
