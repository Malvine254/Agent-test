from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal


Action = Literal["answer_direct", "use_uploaded_files", "search_ai_index", "use_previous_context"]


@dataclass(frozen=True)
class RouteDecision:
    action: Action
    source_required: bool
    query: str
    reason: str


def _norm(text: str | None) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9' ?.!_-]+", " ", text)
    return " ".join(text.split())


def _has_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _context_flag(context: dict[str, Any], *names: str) -> bool:
    for name in names:
        if bool(context.get(name)):
            return True
    return False


SMALL_TALK = {
    "hi", "hello", "hey", "yo", "hola", "good morning", "good afternoon",
    "good evening", "thanks", "thank you", "ok", "okay", "k", "got it",
    "understood", "yes", "no", "cool", "great", "bye", "goodbye",
}

ATTACHMENT_MARKERS = (
    "this file", "these files", "attached", "attachment", "uploaded file",
    "file i uploaded", "document i uploaded", "summarize this file",
    "summarise this file", "review this file", "analyze this file",
)

ORG_MARKERS = (
    "armely", "company", "organization", "organisational", "organizational",
    "sharepoint", "policy", "procedure", "handbook", "staff", "department",
    "internal", "project", "client", "vendor", "report", "contract",
    "employee", "hr", "benefits", "vacation", "pto", "swope",
    " llc", " inc", " corp", " corporation", " ltd", " customer",
)

DOC_MARKERS = (
    "document", "documents", "file", "files", "library", "libraries",
    "cached", "source", "sources", "report",
)

FOLLOWUP_MARKERS = (
    "this", "that", "it", "the document", "the file", "what should i add",
    "summarize it", "summarise it", "make it shorter", "shorter",
    "expand on that", "what is missing", "what should", "improve",
    "its ", "their ", "his ", "her ", "contact", "contact details",
    "phone", "phone number", "email", "website", "address",
)

NEW_SEARCH_MARKERS = (
    "search again", "new search", "search for", "find another",
    "another policy", "different document", "look up another",
)

GENERAL_KNOWLEDGE_STARTS = (
    "what is ", "what are ", "how does ", "how do ", "explain ",
    "define ", "tell me about ",
)

QUERY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "about", "be", "by", "can",
    "could", "do", "does", "for", "from", "give", "i", "in", "info",
    "information", "is", "it", "look", "lookup", "me", "of", "on", "or",
    "please", "say", "search", "sharepoint", "show", "tell", "the", "this", "to",
    "use", "using", "want", "wanting", "what", "where", "who", "why",
    "with", "you",
}

SOURCE_ONLY_PATTERNS = (
    "search from sharepoint",
    "search sharepoint",
    "look in sharepoint",
    "from sharepoint",
    "use sharepoint",
    "check sharepoint",
)


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9._-]*", _norm(text))
        if len(token) > 1 and token not in QUERY_STOPWORDS
    ]


def _clean_query(text: str) -> str:
    terms = _tokens(text)
    if not terms:
        return ""
    return " ".join(terms[:8])


def _last_user_topic(context: dict[str, Any]) -> str:
    for key in ("last_query", "last_user_query"):
        query = _clean_query(str(context.get(key) or ""))
        if query:
            return query
    history = context.get("recent_history") or []
    if isinstance(history, str):
        history = [history]
    for item in reversed(history):
        text = str(item or "")
        if "assistant:" in text.lower():
            continue
        text = re.sub(r"^\s*(user|human)\s*:\s*", "", text, flags=re.I)
        query = _clean_query(text)
        if query:
            return query
    return ""


def build_search_query(user_text: str, context: dict[str, Any] | None = None) -> str:
    context = context or {}
    normalized = _norm(user_text)
    if normalized in SOURCE_ONLY_PATTERNS or (_has_any(normalized, SOURCE_ONLY_PATTERNS) and len(_tokens(user_text)) <= 1):
        previous = _last_user_topic(context)
        if previous:
            return previous
    query = _clean_query(user_text)
    if len(query.split()) < 2:
        previous = _last_user_topic(context)
        if previous:
            return previous
    return query or user_text.strip()


def looks_organizational(text: str) -> bool:
    normalized = _norm(text)
    if not normalized:
        return False
    return _has_any(normalized, ORG_MARKERS) or (
        _has_any(normalized, DOC_MARKERS)
        and not _has_any(normalized, ATTACHMENT_MARKERS)
    )


def decide_route(user_text: str, context: dict[str, Any] | None = None) -> RouteDecision:
    """Deterministically route a Teams assistant turn.

    The router is intentionally conservative: workplace/document-shaped questions
    require retrieval, while general knowledge and social turns stay direct.
    """
    context = context or {}
    text = (user_text or "").strip()
    normalized = _norm(text)
    has_attachment = _context_flag(context, "has_attachment", "has_attachments")
    has_previous_sources = _context_flag(context, "has_previous_sources", "last_sources")

    if not normalized:
        return RouteDecision("answer_direct", False, "", "empty message")

    if normalized in SMALL_TALK or _has_any(normalized, ("how are you", "what's up", "whats up")):
        return RouteDecision("answer_direct", False, "", "small talk")

    disabled_source_message = (
        "This assistant is configured to answer from Azure AI Search indexed "
        "SharePoint documents and uploaded files only."
    )

    if _has_any(normalized, ("website", "web site", "web search", "search the web", "search website")):
        return RouteDecision("answer_direct", False, "", disabled_source_message)

    if _has_any(normalized, ("onedrive", "one drive")):
        return RouteDecision("answer_direct", False, "", disabled_source_message)

    explicit_new_search = _has_any(normalized, NEW_SEARCH_MARKERS)
    if has_previous_sources and not explicit_new_search and _has_any(normalized, FOLLOWUP_MARKERS):
        return RouteDecision("use_previous_context", True, text, "follow-up uses previous source snippets")

    attachment_request = has_attachment or _has_any(normalized, ATTACHMENT_MARKERS)
    compare_to_org = attachment_request and _has_any(normalized, ORG_MARKERS)
    if attachment_request and not compare_to_org:
        return RouteDecision("use_uploaded_files", True, text, "uploaded-file request")

    if looks_organizational(normalized) or explicit_new_search or compare_to_org:
        return RouteDecision("search_ai_index", True, build_search_query(text, context), "organizational/document question")

    if normalized.startswith(GENERAL_KNOWLEDGE_STARTS):
        return RouteDecision("answer_direct", False, "", "general knowledge")

    return RouteDecision("answer_direct", False, "", "non-organizational direct answer")
