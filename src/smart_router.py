"""
Smart LLM Router for the Teams assistant.

Purpose:
- Decide whether to answer directly, use local attachments, refine prior context,
  or call organizational search tools.
- Let the LLM reason for real work requests.
- Apply hard safety gates for casual conversation and simple acknowledgements so
  they never call SharePoint, Graph, cache, web, or attachment loading.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

from router import decide_route as deterministic_decide_route


Route = dict[str, Any]


def normalize_text(text: str | None) -> str:
    """Normalize user text for routing decisions."""
    if not text:
        return ""
    normalized = re.sub(r"[^a-z0-9' ?.!_-]+", " ", str(text).strip().lower())
    return " ".join(normalized.split())


_GENERIC_SEARCH_STOPWORDS = {
    "about", "document", "documents", "file", "files", "please", "show",
    "tell", "info", "information", "organization", "organisations",
    "organization", "sources", "source", "search", "find", "look", "looked",
    "from", "need", "need to", "help", "revision", "questions", "question",
    "exam", "exams", "pass", "passing", "organization sources", "org sources",
}


def _significant_terms(text: str | None) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []
    tokens = re.findall(r"[a-z0-9][a-z0-9'\-]+", normalized)
    return [tok for tok in tokens if len(tok) > 2 and tok not in _GENERIC_SEARCH_STOPWORDS]


def _normalize_topic(text: str) -> str:
    topic = normalize_text(text)
    topic = re.sub(r"\s*-\s*", "-", topic)
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic:
        return ""
    return topic.upper() if re.search(r"\d", topic) else topic


def _extract_topic_candidate(text: str | None) -> str:
    """Extract a likely concrete topic from a message or history line."""
    normalized = normalize_text(text)
    if not normalized:
        return ""

    patterns = (
        r"\b[a-z]{1,5}\s*[- ]\s*\d{2,4}\b",
        r"\b[a-z]{1,5}\d{2,4}\b",
        r"\b\d{2,4}\s*[- ]\s*[a-z]{1,5}\b",
    )
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            return _normalize_topic(matches[-1])

    terms = _significant_terms(normalized)
    if len(terms) >= 2 and len(terms) <= 6:
        return _normalize_topic(" ".join(terms))

    return ""


def _infer_search_topic(*, user_text: str, last_query: str = "", recent_history: list[str] | None = None) -> str:
    """Infer the concrete search topic from the current request and history."""
    recent_history = recent_history or []

    # Prefer the most recent explicit search topic if it exists.
    for candidate in (last_query, user_text):
        topic = _extract_topic_candidate(candidate)
        if topic:
            return topic

    # Fall back to recent conversation turns, searching newest-first.
    for line in reversed(recent_history):
        topic = _extract_topic_candidate(line)
        if topic:
            return topic

    return ""


def _looks_like_vague_search_request(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    vague_markers = (
        "search from organization sources",
        "search organization sources",
        "search org sources",
        "organization sources",
        "org sources",
        "same topic",
        "that topic",
        "this topic",
        "previous topic",
        "search the organization",
        "search my organization",
        "search the company sources",
        "search company sources",
    )
    if any(marker in normalized for marker in vague_markers):
        return True

    terms = _significant_terms(normalized)
    return bool(terms) and len(terms) <= 2 and any(word in normalized for word in ("search", "find", "look up", "look in"))


def is_small_talk(text: str | None) -> bool:
    """Detect conversation-management turns that must never trigger retrieval."""
    normalized = normalize_text(text)
    if not normalized:
        return False

    exact = {
        "hi", "hello", "hey", "yo", "sup", "hola",
        "good morning", "good afternoon", "good evening", "gm",
        "bye", "goodbye", "see ya", "later",
        "thanks", "thank you", "thanks a lot", "appreciate it",
        "ok", "okay", "k", "yes", "no", "sure", "got it", "understood",
        "cool", "nice", "great", "awesome", "wow", "lol", "haha",
        "i love you", "love you", "ily", "you are amazing", "youre amazing",
        "you are the best", "youre the best", "good job", "well done",
    }
    if normalized in exact:
        return True

    patterns = (
        "how are you",
        "how's it going",
        "hows it going",
        "what's up",
        "whats up",
        "thank you for",
        "thanks for",
        "i appreciate you",
    )
    return any(p in normalized for p in patterns)


def is_personal_advice_request(text: str | None) -> bool:
    """Detect personal/life-advice questions that should not use retrieval."""
    normalized = normalize_text(text)
    if not normalized:
        return False

    personal_markers = (
        "my girlfriend", "my gf", "my boyfriend", "my bf", "my partner",
        "my wife", "my husband", "my friend", "my family", "my mom",
        "my dad", "relationship", "dating", "breakup", "broke up",
        "drunk", "saloon", "salon", "bar", "late at night",
        "what would you have done", "what should i do", "please guide me",
        "guide me through", "i feel", "i'm worried", "im worried",
        "i am worried", "i'm upset", "im upset", "i am upset",
    )
    if any(marker in normalized for marker in personal_markers):
        org_markers = (
            "swope", "sharepoint", "company", "organization", "policy",
            "procedure", "handbook", "employee", "hr", "clinic",
            "document", "file", "report",
        )
        return not any(
            re.search(r"\b" + re.escape(marker).replace(r"\ ", r"\s+") + r"\b", normalized)
            for marker in org_markers
        )

    return False


def small_talk_response(text: str | None) -> str:
    """Short Teams-native response for small-talk turns."""
    normalized = normalize_text(text)

    if normalized in {"hi", "hello", "hey", "yo", "sup", "hola", "good morning", "good afternoon", "good evening", "gm"}:
        return "Hi! How can I help you today?"
    if normalized in {"thanks", "thank you", "thanks a lot", "appreciate it"} or normalized.startswith(("thanks for", "thank you for")):
        return "You're welcome. What would you like to work on next?"
    if normalized in {"bye", "goodbye", "see ya", "later"}:
        return "Goodbye!"
    if normalized in {"i love you", "love you", "ily", "you are amazing", "youre amazing", "you are the best", "youre the best", "good job", "well done"}:
        return "That's kind of you, thank you. How can I help with your work today?"
    if normalized in {"ok", "okay", "k", "yes", "sure", "got it", "understood", "cool", "nice", "great", "awesome", "wow", "lol", "haha"}:
        return "Got it. What should we do next?"
    return "I'm here. How can I help?"


def _route(
    *,
    action: str,
    should_search: bool,
    query: str = "",
    scope: str = "local",
    reason: str = "",
    top_k: int = 10,
) -> Route:
    """Create a normalized route dictionary expected by app.py."""
    action = (action or "respond_direct").strip().lower()
    scope = (scope or "local").strip().lower()
    if action != "search_documents":
        should_search = False
    return {
        "action": action,
        "should_search": bool(should_search),
        "is_followup": action == "refine_previous",
        "query": (query or "").strip(),
        "scope": scope,
        "top_k": int(top_k),
        "reason": reason,
    }


def deterministic_precheck(
    *,
    user_text: str,
    has_attachments: bool = False,
    has_cached_attachments: bool = False,
    last_query: str = "",
    last_source_names: Optional[list[str]] = None,
    recent_history: Optional[list[str]] = None,
) -> Optional[Route]:
    """Hard gates that should not be delegated to the LLM.

    The LLM still handles real routing decisions, but these cases are too obvious
    and too expensive to send into the retrieval flow.
    """
    normalized = normalize_text(user_text)
    last_source_names = last_source_names or []
    recent_history = recent_history or []
    inferred_topic = _infer_search_topic(user_text=user_text, last_query=last_query, recent_history=recent_history)

    new_route = deterministic_decide_route(
        user_text,
        {
            "has_attachment": has_attachments,
            "has_cached_attachments": has_cached_attachments,
            "has_previous_sources": bool(last_source_names),
            "last_query": last_query,
            "recent_history": recent_history,
        },
    )
    if new_route.action == "answer_direct":
        return _route(
            action="respond_direct",
            should_search=False,
            query=new_route.query,
            scope="local",
            reason=new_route.reason,
        ) | {"source_required": new_route.source_required}
    if new_route.action == "use_uploaded_files":
        return _route(
            action="respond_direct",
            should_search=False,
            query=new_route.query,
            scope="local",
            reason=new_route.reason,
        ) | {"source_required": new_route.source_required, "use_attachments": True}
    if new_route.action == "search_ai_index":
        query = new_route.query or user_text
        if _looks_like_vague_search_request(query) or not _significant_terms(query):
            query = inferred_topic or query
        return _route(
            action="search_documents",
            should_search=True,
            query=query,
            scope="ai_search",
            reason=new_route.reason,
        ) | {"source_required": new_route.source_required}
    if new_route.action == "use_previous_context":
        query = new_route.query or inferred_topic
        return _route(
            action="refine_previous",
            should_search=False,
            query=query,
            scope="local",
            reason=new_route.reason,
        ) | {"source_required": new_route.source_required}

    if not normalized:
        return _route(
            action="respond_direct",
            should_search=False,
            query="",
            scope="local",
            reason="Empty message.",
        )

    if is_small_talk(normalized):
        return _route(
            action="respond_direct",
            should_search=False,
            query="",
            scope="local",
            reason="Small talk or social message. Never search.",
        )

    if is_personal_advice_request(normalized):
        return _route(
            action="respond_direct",
            should_search=False,
            query="",
            scope="local",
            reason="Personal advice/support request. Answer directly; do not search documents.",
        )

    # If files are present in this message and the user gives an analysis command,
    # the app should process local file content, not search SharePoint.
    attachment_words = (
        "this file", "these files", "attached", "attachment", "document i uploaded",
        "summarize this", "summarise this", "compare these", "review this",
        "analyze this", "analyse this", "extract from this",
    )
    if has_attachments and any(w in normalized for w in attachment_words):
        return _route(
            action="respond_direct",
            should_search=False,
            query="",
            scope="local",
            reason="Current uploaded attachment request.",
        )

    org_or_doc_markers = (
        "company", "organization", "organisational", "organizational",
        "llc", "inc", "corp", "corporation", "ltd", "policy", "procedure",
        "handbook", "sharepoint", "document cache", "documents cache",
        "document", "documents", "file", "files", "report", "contract",
        "proposal", "customer", "vendor", "client",
    )
    search_verbs = (
        "search", "find", "look up", "lookup", "tell me about", "what is",
        "who is", "summarize", "summarise", "show me", "give me",
        "ask about", "question about",
    )
    local_attachment_reference = any(w in normalized for w in (
        "this file", "these files", "this attachment", "these attachments",
        "uploaded file", "file i uploaded", "document i uploaded",
        "what is in this file", "what is this file",
    ))
    looks_like_org_or_doc_query = (
        not local_attachment_reference
        and
        any(marker in normalized for marker in org_or_doc_markers)
        and (
            any(verb in normalized for verb in search_verbs)
            or len(normalized.split()) <= 8
        )
    )

    # Follow-up questions about cached attachments should stay local unless user
    # explicitly asks to search SharePoint or organizational sources.
    explicit_search = any(w in normalized for w in (
        "search sharepoint", "look in sharepoint", "company policy",
        "organizational policy", "swope policy", "find in documents",
        "search documents", "search document cache", "documents cache",
        "look in documents", "find in files", "company documents",
    )) or looks_like_org_or_doc_query

    # If the user is clearly asking to search organizational sources but their
    # wording is vague, reuse the most recent concrete topic from history.
    if _looks_like_vague_search_request(normalized) and inferred_topic:
        return _route(
            action="search_documents",
            should_search=True,
            query=inferred_topic,
            scope="ai_search",
            reason=f"Vague search request reused prior topic '{inferred_topic}'.",
        )

    if has_cached_attachments and not explicit_search:
        local_followup_words = (
            "this", "that", "the file", "the document", "above", "previous",
            "summarize", "summarise", "what is it about", "what is the document about",
            "make it shorter", "explain more", "continue",
        )
        if any(w in normalized for w in local_followup_words):
            return _route(
                action="refine_previous",
                should_search=False,
                query="",
                scope="local",
                reason="Follow-up about previous/local attachment context.",
            )

    # If the prior answer used source documents, context-dependent follow-ups
    # should reuse that prior content and must not trigger organizational search.
    explicit_new_search = any(w in normalized for w in (
        "search sharepoint", "search again", "new search", "look in sharepoint",
        "find another", "find other", "different document", "another document",
        "other documents", "search for", "find a document", "find document",
    ))
    if (last_source_names or recent_history) and not explicit_new_search:
        previous_context_words = (
            "this", "that", "it", "above", "previous", "the document", "the file",
            "what about", "how about", "and what", "also", "then", "now",
            "i mean", "i meant", "can you also", "can you explain",
            "can you expand", "give me more", "more details", "tell me more",
            "expand on", "continue", "go on", "does it", "do they",
            "is there", "are there", "what should", "what can", "what would",
            "suggest", "recommend", "missing", "gaps", "improve",
        )
        if any(w in normalized for w in previous_context_words):
            return _route(
                action="refine_previous",
                should_search=False,
                query=inferred_topic or "",
                scope="local",
                reason="Follow-up about previous source context. Do not search.",
            )

        if last_source_names and len(normalized.split()) <= 8 and re.search(
            r"\b(what|which|who|where|when|why|how|does|do|is|are|can|should)\b",
            normalized,
        ):
            return _route(
                action="refine_previous",
                should_search=False,
                query=inferred_topic or "",
                scope="local",
                reason="Short context-dependent follow-up. Do not search.",
            )

    return None


def build_router_instructions(
    *,
    has_attachments: bool,
    attachment_names: list[str],
    has_cached_attachments: bool,
    cached_attachment_names: list[str],
    last_query: str,
    last_source_names: list[str],
    recent_history: list[str],
) -> str:
    """Build strict router instructions for the LLM."""
    history_block = "\n  ".join((recent_history or [])[-6:]) or "(new conversation)"
    return f"""
You are a smart tool router for a Microsoft Teams workplace assistant.

Your task is to decide whether the assistant should:
1. answer directly from general knowledge or conversation context,
2. use local uploaded/cached attachments,
3. refine the previous answer,
4. search configured organizational SharePoint/document sources.

Return ONLY valid JSON. No markdown. No explanation.

Required JSON keys:
{{
  "action": "respond_direct" | "search_documents" | "refine_previous",
  "should_search": true | false,
  "search_query": "short query or empty string",
  "scope": "local" | "ai_search",
  "reason": "brief reason"
}}

Context:
- Has attachments in this message: {has_attachments}
- Attachment names: {", ".join(attachment_names) if attachment_names else "none"}
- Has cached attachments from this conversation: {has_cached_attachments}
- Cached attachment names: {", ".join(cached_attachment_names) if cached_attachment_names else "none"}
- Previous search query: {last_query or "none"}
- Previous source names: {", ".join(last_source_names) if last_source_names else "none"}
- Recent conversation:
  {history_block}

Decision policy:
- Casual conversation, greetings, thanks, emotional/social messages, acknowledgements: respond_direct, should_search=false, scope=local.
- Personal advice, relationship concerns, emotional support, or life guidance: respond_direct, should_search=false, scope=local. Do not search SharePoint for these.
- General knowledge questions: respond_direct, should_search=false, scope=local.
- Questions about the bot's abilities: respond_direct, should_search=false, scope=local.
- Current uploaded-file tasks: respond_direct, should_search=false, scope=local.
- If the user asks to search organizational sources with vague wording, reuse the most recent concrete topic from the prior query or recent conversation history instead of the vague phrase.
- Follow-ups about the previous answer/document/file/source: refine_previous, should_search=false, scope=local. Do not search for follow-ups; reuse prior sources and conversation history unless the user explicitly asks for a new search.
- If previous source names exist and the user asks what to add, improve, change, recommend, or what is missing, treat it as a follow-up about the previous document.
- Organizational or company-specific questions: search_documents, should_search=true, scope=ai_search.
- Requests to find, locate, search, open, summarize, or ask about a SharePoint/company document: search_documents, should_search=true, scope=ai_search.
- Policies, procedures, staff, departments, internal services, leadership, HR, compliance, company projects, or organizational facts: search_documents, should_search=true, scope=ai_search.
- Do NOT search just because a message contains a noun.
- Do NOT search for casual messages like "I love you", "hi", "thanks", "ok", "great".

Search query rules:
- Use a short keyword query, 3 to 10 words.
- For document titles, include likely title words.
- If no search is needed, search_query must be empty.

Examples:
Input: "I love you"
Output: {{"action":"respond_direct","should_search":false,"search_query":"","scope":"local","reason":"social message"}}

Input: "hi"
Output: {{"action":"respond_direct","should_search":false,"search_query":"","scope":"local","reason":"greeting"}}

Input: "what is machine learning"
Output: {{"action":"respond_direct","should_search":false,"search_query":"","scope":"local","reason":"general knowledge"}}

Input: "my gf said she was going to a salon and came back late drunk, what should I do?"
Output: {{"action":"respond_direct","should_search":false,"search_query":"","scope":"local","reason":"personal relationship advice"}}

Input: "what is the vacation policy"
Output: {{"action":"search_documents","should_search":true,"search_query":"vacation policy","scope":"ai_search","reason":"organizational policy"}}

Input: "summarize this file" with attachments
Output: {{"action":"respond_direct","should_search":false,"search_query":"","scope":"local","reason":"uploaded file task"}}

Input: "what is the document all about" after a previous document summary
Output: {{"action":"refine_previous","should_search":false,"search_query":"","scope":"local","reason":"follow-up about previous context"}}

Input: "what do you suggest I add to the document" after previous source names include "employee handbook.docx"
Output: {{"action":"refine_previous","should_search":false,"search_query":"","scope":"local","reason":"follow-up asking for suggestions on previous document"}}

Input: "what should I add" after previous source names include "employee handbook.docx"
Output: {{"action":"refine_previous","should_search":false,"search_query":"","scope":"local","reason":"follow-up asking for additions to previous document"}}
""".strip()


def parse_router_json(raw: Any) -> Route:
    """Parse an LLM router response into a safe route."""
    if hasattr(raw, "text"):
        raw = raw.text
    elif hasattr(raw, "response") and hasattr(raw.response, "content"):
        raw = raw.response.content
    elif not isinstance(raw, str):
        raw = str(raw)

    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        text = match.group(0)

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Router output was not a JSON object.")

    action = str(data.get("action") or "respond_direct").strip().lower()
    if action not in {"respond_direct", "search_documents", "refine_previous"}:
        action = "respond_direct"

    should_search = bool(data.get("should_search", False))
    if action != "search_documents":
        should_search = False

    scope = str(data.get("scope") or ("ai_search" if should_search else "local")).strip().lower()
    if scope not in {"local", "ai_search"}:
        scope = "ai_search" if should_search else "local"

    query = str(data.get("search_query") or "").strip()
    if should_search and not query:
        query = ""

    return _route(
        action=action,
        should_search=should_search,
        query=query,
        scope=scope,
        reason=str(data.get("reason") or "LLM route decision."),
        top_k=10,
    )


async def decide_route(
    *,
    model: Any,
    user_text: str,
    chat_prompt_cls: Any,
    llm_semaphore: Any = None,
    call_with_retry: Optional[Callable] = None,
    config: Any = None,
    logger: Optional[logging.Logger] = None,
    conversation_id: str = "",
    has_attachments: bool = False,
    attachment_names: Optional[list[str]] = None,
    has_cached_attachments: bool = False,
    cached_attachment_names: Optional[list[str]] = None,
    last_query: str = "",
    last_source_names: Optional[list[str]] = None,
    recent_history: Optional[list[str]] = None,
) -> Route:
    """Main smart router entry point used by app.py."""
    log = logger or logging.getLogger(__name__)
    attachment_names = attachment_names or []
    cached_attachment_names = cached_attachment_names or []
    last_source_names = last_source_names or []
    recent_history = recent_history or []

    hard_route = deterministic_precheck(
        user_text=user_text,
        has_attachments=has_attachments,
        has_cached_attachments=has_cached_attachments,
        last_query=last_query,
        last_source_names=last_source_names,
        recent_history=recent_history,
    )
    if hard_route:
        log.info(
            "SMART ROUTER hard decision: action=%s search=%s scope=%s reason=%s",
            hard_route["action"], hard_route["should_search"], hard_route["scope"], hard_route.get("reason", "")
        )
        return hard_route

    instructions = build_router_instructions(
        has_attachments=has_attachments,
        attachment_names=attachment_names,
        has_cached_attachments=has_cached_attachments,
        cached_attachment_names=cached_attachment_names,
        last_query=last_query,
        last_source_names=last_source_names,
        recent_history=recent_history,
    )

    prompt = chat_prompt_cls(model)

    async def make_llm_call():
        if llm_semaphore is not None:
            async with llm_semaphore:
                return await prompt.send(
                    input=user_text,
                    instructions=instructions,
                    memory=None,
                )
        return await prompt.send(
            input=user_text,
            instructions=instructions,
            memory=None,
        )

    try:
        if call_with_retry:
            raw = await call_with_retry(make_llm_call)
        else:
            raw = await make_llm_call()

        route = parse_router_json(raw)

        # Final safety normalization. Even if the LLM makes a bad call, casual
        # turns and direct/refine routes cannot search.
        if is_small_talk(user_text) or is_personal_advice_request(user_text):
            route = _route(
                action="respond_direct",
                should_search=False,
                query="",
                scope="local",
                reason="Final guard: casual/personal requests cannot search.",
            )

        if config is not None and getattr(config, "DATA_SOURCE_MODE", "") in {"sharepoint", "sharepoint_uploads_only", "sharepoint_ai_search_uploads_only"} and route["scope"] != "local":
            route["scope"] = "ai_search"

        log.info(
            "SMART ROUTER LLM decision: action=%s search=%s scope=%s query='%s' reason=%s",
            route["action"], route["should_search"], route["scope"], route["query"], route.get("reason", "")
        )
        return route

    except Exception as exc:
        log.error("SMART ROUTER failed. Falling back to safe direct response. Error: %s", exc)
        return _route(
            action="respond_direct",
            should_search=False,
            query="",
            scope="local",
            reason=f"Router fallback after error: {exc}",
            top_k=3,
        )
