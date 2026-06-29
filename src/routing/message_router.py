"""Message routing — extracted verbatim from handle_stateful_conversation (Phase 5.2).

Contained move: the decision tree returns the existing route-dict shape; downstream
consumption and mid-flow mutations stay in app.py. The is_small_talk-dependent
predicates (is_small_talk / is_personal_advice_request / is_org_or_document_request /
is_previous_document_followup) intentionally stay in app.py — they are shared with
smart_router and reach conversation-local state, so moving them would exceed a
relocation. classify_message takes the precomputed flags as explicit parameters.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_BOT_SELF_PATTERNS = [
    "how can you help", "how can you help me", "how do you help",
    "what can you help", "what can you help me", "what can you help me with",
    "what can you do", "what do you do", "what are you",
    "what can you assist", "how can you assist", "what do you offer",
    "what are your capabilities", "what are your features",
    "tell me about yourself", "tell me what you can do",  # EXACT: only "yourself", not general topics
    "what is your purpose", "what's your purpose", "whats your purpose",
    "how do you work", "what can this bot do", "what can the bot do",
    "what kind of questions can i ask", "what questions can i ask",
    "what can i ask you", "what can i ask",
    "what should i ask", "how does this work",
    "what kind of help can you", "what type of help can you",
    "who are you",
]


def is_bot_self_question(text: str) -> bool:
    """Check if this is a question about the BOT itself, not a general organizational query.

    A question is bot-self when the whole message (ignoring trailing punctuation)
    is one of the patterns or starts with one. These phrasings are about the
    assistant's abilities, so they should be answered directly rather than
    triggering a SharePoint/document search."""
    t = (text or "").strip().lower().rstrip("?.! ")
    if not t:
        return False
    # Match the whole message or a clean word-boundary prefix only. A bare
    # prefix match would wrongly catch org queries (e.g. "what are your
    # vacation days" should NOT match the "what are you" pattern).
    for pattern in _BOT_SELF_PATTERNS:
        if t == pattern or t.startswith(pattern + " "):
            return True
    return False


def is_general_knowledge_question(text: str) -> bool:
    """Detect general knowledge questions to answer without searching.
    These are questions about universal facts, not organization-specific info."""
    text_lower = text.lower().strip()

    org_terms = [
        "armely", "swope", "sharepoint", "company", "organization", "our ", "we ", "us ",
        "internal", "employee", "hr", "policy", "procedure", "handbook",
        "document", "file", "report", "pdf", "docx", "spreadsheet",
        " llc", " inc", " corp", " corporation", " ltd", " vendor", " client",
        " customer",
    ]
    if any(org in text_lower for org in org_terms):
        return False

    general_starters = (
        "what is ", "what are ", "who is ", "who are ", "where is ",
        "where are ", "when was ", "when did ", "why is ", "why do ",
        "how does ", "how do ", "how can ", "explain ", "define ",
        "tell me about ", "give me facts about ",
    )
    if text_lower.startswith(general_starters):
        return True

    if any(phrase in text_lower for phrase in [
        "where is", "where are", "location of", "country is", "city is",
        "capital of", "located in", "found at", "situated in"
    ]):
        if not any(org in text_lower for org in ["swope", "our office", "our location", "company office"]):
            return True

    if any(phrase in text_lower for phrase in [
        "what is python", "what is ai", "what is machine learning", "what is covid",
        "what is a virus", "what is photosynthesis", "what is gravity", "what is dna",
        "what is climate change", "what are atoms", "what are cells"
    ]):
        return True

    if any(phrase in text_lower for phrase in [
        "how does photosynthesis", "how do plants", "how does gravity", "how do vaccines",
        "explain physics", "explain chemistry", "tell me about", "facts about"
    ]):
        if not any(org in text_lower for org in [
            "swope", "company", "organization", "our", "sharepoint",
            "document", "file", "handbook", "policy", "procedure",
            "employee", "it", "this", "that",
        ]):
            return True

    if any(phrase in text_lower for phrase in [
        "what is", "calculate", "solve", "equals", "plus", "minus", "times"
    ]):
        if re.search(r'\d+\s*[\+\-\*/]\s*\d+', text):
            return True

    if any(phrase in text_lower for phrase in [
        "who won", "who is the", "when was", "what year", "which president",
        "who discovered", "who invented", "what is the meaning"
    ]):
        if not any(org in text_lower for org in ["swope", "our ", "company ", "organization "]):
            return True

    return False


def classify_message(
    user_text: str,
    *,
    data_source_mode: str,
    force_respond_direct: bool,
    looks_like_previous_doc_followup: bool,
    needs_org_search: bool,
    has_attachments: bool,
    has_cached_attachments: bool,
    looks_like_refine: bool,
) -> dict | None:
    """Deterministic routing decision tree (moved verbatim).

    Returns the route dict, or None when no deterministic rule applies (the caller
    falls back to the LLM router). All inputs are explicit parameters — this function
    never reaches into app.py scope.
    """
    if force_respond_direct:
        logger.info(f"⚡ Short-circuited to respond_direct (bot self-knowledge): '{user_text[:60]}'")
        return {"action": "respond_direct", "should_search": False, "search_query": "", "scope": "local"}
    if looks_like_previous_doc_followup:
        logger.info(f"Fast-routed to previous document follow-up: '{user_text[:80]}'")
        return {
            "action": "refine_previous", "should_search": False, "is_followup": True,
            "query": "", "scope": "local", "top_k": 3,
            "reason": "follow-up about previous document/source",
        }
    if (
        data_source_mode in ("sharepoint", "sharepoint_uploads_only", "sharepoint_ai_search_uploads_only")
        and needs_org_search
        and not has_attachments
        and not has_cached_attachments
        and not looks_like_refine
        and not looks_like_previous_doc_followup
    ):
        logger.info(f"Fast-routed to Azure AI Search: '{user_text[:80]}'")
        return {
            "action": "search_documents", "should_search": True, "is_followup": False,
            "query": user_text.strip(), "scope": "ai_search", "top_k": 6,
        }
    if looks_like_refine:
        logger.info(f"Fast-routed to refine_previous: '{user_text[:80]}'")
        return {
            "action": "refine_previous", "should_search": False, "is_followup": True,
            "query": user_text.strip(), "scope": "local", "top_k": 3,
        }
    return None  # caller falls back to llm_decide_routing


# ── Intent classifier (Phase 7.1) — app-only safe, regex only ──────────────────
@dataclass
class QueryIntent:
    route: str
    needs_indexed: bool
    needs_live_data: bool
    needs_web: bool
    live_scope: list[str] = field(default_factory=list)
    confidence: float = 1.0


LIVE_DATA_SIGNALS = [
    r"\b(email|mail|message|sent|received|inbox)\b",
    r"\b(latest|recent|last week|yesterday|today|this morning)\b",
    r"\b(onedrive|my files|my documents|uploaded)\b",
    r"\b(just|currently|right now|at the moment)\b",
    r"\bdid .*(send|share|upload|attach)\b",
]
MAIL_SIGNALS = [r"\b(email|mail|message|inbox|reply|thread|correspondence)\b"]
DRIVE_SIGNALS = [r"\b(file|document|onedrive|my drive|uploaded|attachment|spreadsheet)\b"]


def classify_intent(message: str, route: dict | None) -> QueryIntent:
    """Decide which data sources a message needs. Fast — regex only, no model call.
    `route` is the route dict from classify_message/llm_decide_routing."""
    action = (route or {}).get("action", "respond_direct")
    msg_lower = (message or "").lower()

    if action in ("respond_direct", "refine_previous"):
        return QueryIntent(route=action, needs_indexed=False, needs_live_data=False, needs_web=False, live_scope=[], confidence=1.0)

    needs_live = any(re.search(p, msg_lower) for p in LIVE_DATA_SIGNALS)
    scope: list[str] = []
    if any(re.search(p, msg_lower) for p in MAIL_SIGNALS):
        scope.append("mail")
    if any(re.search(p, msg_lower) for p in DRIVE_SIGNALS):
        scope.append("drive")
    if not scope and needs_live:
        scope = ["all"]

    return QueryIntent(
        route=action, needs_indexed=True, needs_live_data=needs_live,
        needs_web=False, live_scope=scope, confidence=0.85 if needs_live else 1.0,
    )
