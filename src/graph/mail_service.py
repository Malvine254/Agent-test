"""App-only Microsoft Graph mail operations (acting for a specific user).

All functions take ``user_id`` (the caller's Entra object id) and target
``/users/{user_id}/...`` using application permissions (Mail.ReadWrite,
Mail.Send). Functions return plain Python structures; formatting for the model
lives in :mod:`agent.graph_tools`.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Optional

from graph.client import graph_get, graph_get_all, graph_post, user_segment

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

_MESSAGE_SELECT = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "isRead,hasAttachments,importance,webLink,bodyPreview"
)


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _addr(recipient: dict) -> str:
    ea = (recipient or {}).get("emailAddress") or {}
    name = ea.get("name") or ""
    address = ea.get("address") or ""
    if name and address and name != address:
        return f"{name} <{address}>"
    return address or name


def _kql_quote(value: str) -> str:
    value = (value or "").strip().replace('"', "")
    return f'"{value}"' if " " in value else value


def build_search(query: str = "", sender: str = "", recipient: str = "",
                 subject: str = "") -> str:
    """Build a KQL $search string from structured email criteria."""
    parts: list[str] = []
    if sender:
        parts.append(f"from:{_kql_quote(sender)}")
    if recipient:
        parts.append(f"to:{_kql_quote(recipient)}")
    if subject:
        parts.append(f"subject:{_kql_quote(subject)}")
    if query:
        parts.append(query.strip())
    return " ".join(parts).strip()


def search_messages(user_id: str, *, query: str = "", sender: str = "",
                    recipient: str = "", subject: str = "", top: int = 10) -> list[dict]:
    """Search the user's mailbox. Uses $search (KQL) when criteria are given,
    otherwise returns the most recent messages."""
    seg = user_segment(user_id)
    top = max(1, min(int(top or 10), 25))
    search = build_search(query, sender, recipient, subject)
    if search:
        # $search cannot be combined with $orderby.
        url = f"{seg}/messages?$select={_MESSAGE_SELECT}&$top={top}&$search=\"{search}\""
    else:
        url = (f"{seg}/messages?$select={_MESSAGE_SELECT}&$top={top}"
               "&$orderby=receivedDateTime desc")
    items = graph_get_all(url, max_items=top)
    return [_summarize(m) for m in items]


def _summarize(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "subject": m.get("subject") or "(no subject)",
        "from": _addr(m.get("from") or {}),
        "to": ", ".join(_addr(r) for r in (m.get("toRecipients") or [])),
        "received": m.get("receivedDateTime") or "",
        "is_read": m.get("isRead"),
        "has_attachments": m.get("hasAttachments"),
        "importance": m.get("importance"),
        "web_link": m.get("webLink") or "",
        "preview": (m.get("bodyPreview") or "").strip(),
    }


def get_message(user_id: str, message_id: str) -> dict:
    """Fetch one message including its full plain-text body."""
    seg = user_segment(user_id)
    m = graph_get(
        f"{seg}/messages/{message_id}"
        f"?$select={_MESSAGE_SELECT},body,ccRecipients"
    )
    summary = _summarize(m)
    body = m.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = _strip_html(content)
    summary["body"] = content.strip()
    summary["cc"] = ", ".join(_addr(r) for r in (m.get("ccRecipients") or []))
    return summary


def list_attachments(user_id: str, message_id: str) -> list[dict]:
    """List attachments on a message (metadata only)."""
    seg = user_segment(user_id)
    items = graph_get_all(
        f"{seg}/messages/{message_id}/attachments"
        "?$select=id,name,contentType,size,isInline",
        max_items=25,
    )
    return [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "content_type": a.get("contentType"),
            "size": a.get("size"),
            "is_inline": a.get("isInline"),
        }
        for a in items
    ]


def download_attachment(user_id: str, message_id: str, attachment_id: str) -> Optional[tuple[str, bytes]]:
    """Return (filename, bytes) for a fileAttachment, or None."""
    import base64

    seg = user_segment(user_id)
    a = graph_get(f"{seg}/messages/{message_id}/attachments/{attachment_id}")
    if a.get("@odata.type", "").endswith("fileAttachment") and a.get("contentBytes"):
        return a.get("name") or "attachment.bin", base64.b64decode(a["contentBytes"])
    return None


def _recipient_list(addresses) -> list[dict]:
    if isinstance(addresses, str):
        parts = [a.strip() for a in re.split(r"[;,]", addresses) if a.strip()]
    else:
        parts = [str(a).strip() for a in (addresses or []) if str(a).strip()]
    return [{"emailAddress": {"address": a}} for a in parts]


def create_draft(user_id: str, *, to, subject: str, body: str,
                 cc=None, body_type: str = "Text") -> dict:
    """Create a draft message in the user's mailbox. Returns {id, web_link}."""
    seg = user_segment(user_id)
    payload = {
        "subject": subject or "(no subject)",
        "body": {"contentType": body_type, "content": body or ""},
        "toRecipients": _recipient_list(to),
    }
    if cc:
        payload["ccRecipients"] = _recipient_list(cc)
    created = graph_post(f"{seg}/messages", payload)
    return {
        "id": created.get("id"),
        "web_link": created.get("webLink") or "",
        "to": ", ".join(r["emailAddress"]["address"] for r in payload["toRecipients"]),
        "cc": ", ".join(r["emailAddress"]["address"] for r in payload.get("ccRecipients", [])),
        "subject": payload["subject"],
    }


def send_draft(user_id: str, message_id: str) -> None:
    """Send a previously created draft."""
    seg = user_segment(user_id)
    graph_post(f"{seg}/messages/{message_id}/send")


def reply(user_id: str, message_id: str, comment: str, *, reply_all: bool = False) -> None:
    """Reply (or reply-all) to a message with a plain-text comment."""
    seg = user_segment(user_id)
    verb = "replyAll" if reply_all else "reply"
    graph_post(f"{seg}/messages/{message_id}/{verb}", {"comment": comment or ""})
