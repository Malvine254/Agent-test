"""Best-effort recovery of file attachments Teams did not deliver inline.

In 1:1 (personal) chats Teams frequently does NOT pass OneDrive/SharePoint
"cloud-picker" files to bots — the bot only receives the typed text. This module
tries every other channel to recover the file so the assistant can read it:

  1. URLs embedded in the message text or in a text/html attachment body.
  2. A ``contentUrl`` / content dict on any inbound attachment (reference type).
  3. For GROUP chats and channels: the real message is fetched via Microsoft
     Graph (Chat.Read.All), whose attachments DO carry the file ``contentUrl``.
  4. When the user names a file ("summarize Edgar Offer Letter"), the file is
     located by name in the chatter's OneDrive and the configured SharePoint
     libraries via app-only Graph.

Every recovered file is returned as a synthetic attachment that mirrors a Teams
file attachment (``name`` + ``content`` dict with a pre-authenticated
``downloadUrl``) so the existing download/extraction pipeline handles it
unchanged. All Graph access is app-only and best-effort: any failure yields no
attachment rather than raising.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from sharepoint.graph_client import (
    get_item_download_url,
    get_teams_message_file_attachments,
    list_configured_sharepoint_drives,
    resolve_sharing_url,
    search_drive_items,
    search_user_drive,
)

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s)>\]}\"']+", re.IGNORECASE)
_CLOUD_HOST_RE = re.compile(r"(sharepoint\.com|onedrive\.live\.com|1drv\.ms)", re.IGNORECASE)
_FILE_EXTS = (
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".txt", ".csv", ".json", ".md", ".rtf",
)
# Deictic phrases that name no concrete file — searching for these is pointless.
_DEICTIC = (
    "this document", "this file", "the document", "the file", "this doc",
    "the attachment", "this attachment", "attached file", "attached document",
    "the pdf", "this pdf", "above document", "that document", "that file",
    "it", "this", "that",
)


def _synthetic(name: str, download_url: str):
    """Mirror a Teams file attachment so the existing pipeline can download it."""
    return SimpleNamespace(
        name=name or "shared-file",
        content_type="application/vnd.microsoft.teams.file.download.info",
        content={"downloadUrl": download_url, "name": name or "shared-file"},
    )


def _cloud_urls_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(".,);]}'\"")
        if _CLOUD_HOST_RE.search(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _urls_from_attachments(attachments_raw: list) -> list[str]:
    """Pull SharePoint/OneDrive URLs from attachment contentUrls and html bodies."""
    urls: list[str] = []
    for att in attachments_raw or []:
        content_url = getattr(att, "content_url", None) or getattr(att, "contentUrl", None)
        if content_url and _CLOUD_HOST_RE.search(str(content_url)):
            urls.append(str(content_url))
        content = getattr(att, "content", None)
        if isinstance(content, str):
            urls.extend(_cloud_urls_from_text(content))
        elif isinstance(content, dict):
            for v in content.values():
                if isinstance(v, str) and _CLOUD_HOST_RE.search(v):
                    urls.extend(_cloud_urls_from_text(v))
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_filename_query(text: str) -> str:
    """Best-effort: pull a concrete file name/title the user referenced, or "".

    Returns a search term only when the user actually named something (a filename
    with an extension, a quoted phrase, or the words after an action verb). Returns
    "" for purely deictic requests like "summarize this document" — where no file
    can be identified without the inline attachment.
    """
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()

    # 1) An explicit filename with a known extension.
    m = re.search(r"([\w][\w \-]{0,80}?\.(?:pdf|docx|doc|xlsx|xls|pptx|ppt|txt|csv|json|md|rtf))", low)
    if m:
        return m.group(1).strip()

    # 2) A quoted phrase.
    m = re.search(r"[\"'“‘]([^\"'”’]{2,80})[\"'”’]", t)
    if m and m.group(1).strip().lower() not in _DEICTIC:
        return m.group(1).strip()

    # 3) Words after an action verb ("summarize/open/read/analyze/review ... X").
    m = re.search(
        r"\b(?:summari[sz]e|open|read|analy[sz]e|review|show|find|get|fetch|"
        r"tell me about|explain)\b\s+(?:the|this|that|my|a|an)?\s*(.{2,80})",
        low,
    )
    if m:
        cand = m.group(1).strip(" .?!,")
        # Strip a trailing generic noun ("... offer letter document" -> keep core)
        if cand and cand not in _DEICTIC and not all(
            w in {"document", "file", "doc", "pdf", "attachment", "this", "that", "the", "it"}
            for w in cand.split()
        ):
            return cand
    return ""


def _resolve_urls(urls: list[str], limit: int = 5) -> list:
    out: list = []
    for url in urls[:limit]:
        try:
            info = resolve_sharing_url(url)
        except Exception as exc:
            logger.info("Cloud URL resolve error for %s: %s", url[:80], type(exc).__name__)
            info = None
        if info and info.get("download_url"):
            out.append(_synthetic(info.get("name") or "shared-file", info["download_url"]))
            logger.info("Recovered attachment from URL: %s", info.get("name"))
    return out


def _items_to_attachments(items: list[dict], limit: int = 3) -> list:
    out: list = []
    for item in items[:limit]:
        drive_id = (item.get("parentReference") or {}).get("driveId") or item.get("drive_id") or ""
        item_id = item.get("id") or ""
        name = item.get("name") or "shared-file"
        dl = item.get("@microsoft.graph.downloadUrl") or ""
        if not dl and drive_id and item_id:
            dl = get_item_download_url(drive_id, item_id)
        if dl:
            out.append(_synthetic(name, dl))
            logger.info("Recovered attachment by name search: %s", name)
    return out


def _search_named_file(query: str, chatter_aad_id: str) -> list:
    """Find a named file in the chatter's OneDrive, then configured SharePoint.

    Returns ONLY the single best (highest-ranked) match — Graph search is fuzzy
    and a name query can return unrelated files, so reading more than the top hit
    risks summarizing the wrong document.
    """
    if chatter_aad_id:
        try:
            found = _items_to_attachments(search_user_drive(chatter_aad_id, query), limit=1)
        except Exception as exc:
            logger.info("OneDrive name search failed: %s", type(exc).__name__)
            found = []
        if found:
            return found
    # Fall back to the configured SharePoint document libraries.
    try:
        drives = list_configured_sharepoint_drives()
    except Exception as exc:
        logger.info("SharePoint drive discovery failed: %s", type(exc).__name__)
        drives = []
    for entry in drives[:6]:
        drive_id = entry.get("drive_id") or ""
        try:
            hits = search_drive_items(drive_id, query) if drive_id else []
        except Exception:
            hits = []
        atts = _items_to_attachments(hits, limit=1)
        if atts:
            return atts
    return []


def resolve_extra_attachments(
    *,
    user_text: str,
    attachments_raw: list,
    conversation_id: str,
    is_group: bool,
    chatter_aad_id: str = "",
    message_id: str = "",
) -> list:
    """Recover attachments Teams did not deliver inline. Returns synthetic attachments.

    Safe to call whenever the inbound activity carried no usable file attachment.
    Ordered cheapest-first; returns as soon as something is recovered.
    """
    # 1) URLs in the text or attachment bodies (cheapest, no Graph dependency on chat).
    urls = _cloud_urls_from_text(user_text) + _urls_from_attachments(attachments_raw)
    recovered = _resolve_urls(urls)
    if recovered:
        return recovered

    # 2) Group chats / channels: fetch the real message via Graph and resolve its
    #    file attachments (1:1 bot chats are not Graph-addressable and yield nothing).
    if is_group and conversation_id and "@thread" in conversation_id:
        try:
            msg_atts = get_teams_message_file_attachments(conversation_id, message_id)
        except Exception as exc:
            logger.info("Group message attachment fetch error: %s", type(exc).__name__)
            msg_atts = []
        recovered = _resolve_urls([a["content_url"] for a in msg_atts if a.get("content_url")])
        if recovered:
            return recovered

    # 3) The user named a specific file — locate it by name in OneDrive/SharePoint.
    query = extract_filename_query(user_text)
    if query:
        logger.info("Attempting named-file recovery for query: %r", query)
        recovered = _search_named_file(query, chatter_aad_id)
        if recovered:
            return recovered

    return []
