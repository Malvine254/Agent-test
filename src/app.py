import asyncio
import os
import sys
import logging
import json
import re
import time
import csv
import io
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
import concurrent.futures
import requests

# Load environment variables from .env files BEFORE importing config
# Only load dotenv locally - Azure App Service sets env vars through Application Settings
if not os.environ.get("WEBSITE_SITE_NAME"):  # Not running in Azure
    from dotenv import load_dotenv
    # Find env directory - handle both running from src/ and from project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    env_dir = os.path.join(project_root, "env")
    
    loaded = False
    for env_file in [".env.local", ".env.dev", ".env"]:
        env_path = os.path.join(env_dir, env_file)
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
            print(f"Loaded environment from: {env_path}")
            loaded = True
            break
    if not loaded:
        print(f"No .env file found in {env_dir}")
    
    # Safe startup diagnostic: never print secret values or previews.
    bot_id = os.environ.get("BOT_ID", "NOT SET")
    has_password = bool(os.environ.get("SECRET_BOT_PASSWORD") or os.environ.get("BOT_PASSWORD"))
    print(f"Bot credentials loaded: BOT_ID={'set' if bot_id != 'NOT SET' else 'missing'} | Password={'set' if has_password else 'missing'}")
else:
    print("Running in Azure - using Application Settings")

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))


from microsoft_teams.ai import ChatPrompt, ListMemory
from microsoft_teams.ai.message import UserMessage, ModelMessage
from microsoft_teams.ai.ai_model import AIModel
from microsoft_teams.apps import App, ActivityContext
from microsoft_teams.openai import OpenAICompletionsAIModel
from microsoft_teams.api import (
    MessageActivity,
    MessageActivityInput,
    MessageSubmitActionInvokeActivity,
    TypingActivityInput,


)

from config import Config
# Simple file handler for direct uploads
from simple_file_handler import process_attachment, search_local_files, aggregate_tabular_files
import attachment_resolver  # Graph-based recovery of attachments Teams doesn't deliver inline
# Data calculator for accurate numeric operations (overcomes LLM arithmetic limitations)
from data_calculator import process_calculation_request, detect_calculation_intent, process_multi_file_calculation
# Graph API for SharePoint search and optional Microsoft 365 sources
from knowledge_base import (
    crawl_accessible_documents,
    download_and_extract_content,
    get_graph_token,
    get_user_profile,
    list_user_files,
    list_sharepoint_files,
    search_sharepoint,
)
# Document cache is disabled at runtime; Azure AI Search + live Graph are the only SharePoint paths.
def get_cache():
    return None
# Attachment cache for persisting file contents across follow-up questions
from attachment_cache import (
    cache_attachment,
    get_conversation_attachments,
    search_attachment_contents,
    cleanup_old_cache,
    get_content_for_llm_conversation,  # For conversation display with smart truncation
    get_full_content_for_calculation,  # For accurate calculations with complete data
)
from grounding_guard import before_llm
from routing.message_router import (
    classify_message,
    classify_intent,
    is_bot_self_question,
    is_general_knowledge_question,
)
from prompts.prompt_builder import build_llm_input, _strip_html

# Code interpreter / document generation (Phase 1)
try:
    from agent.tools import InterpreterTurn, build_interpreter_tools
    from generation.file_store import download_url as _artifact_download_url
    _INTERPRETER_AVAILABLE = True
except Exception as _interp_imp_err:  # pragma: no cover
    InterpreterTurn = None  # type: ignore
    build_interpreter_tools = None  # type: ignore
    _INTERPRETER_AVAILABLE = False
    logging.getLogger(__name__).warning(f"Code interpreter unavailable: {_interp_imp_err}")

# Live Microsoft 365 (Graph) + image-generation tools (always-on)
try:
    from agent.graph_tools import (
        GraphToolContext,
        build_graph_tools,
        graph_tools_instructions,
    )
    _GRAPH_TOOLS_AVAILABLE = True
except Exception as _graph_imp_err:  # pragma: no cover
    GraphToolContext = None  # type: ignore
    build_graph_tools = None  # type: ignore
    graph_tools_instructions = None  # type: ignore
    _GRAPH_TOOLS_AVAILABLE = False
    logging.getLogger(__name__).warning(f"Graph tools unavailable: {_graph_imp_err}")


from smart_router import (
    decide_route as smart_decide_route,
    is_personal_advice_request,
    is_small_talk,
    small_talk_response,
)

# ---------------------------
# Logging (only key application events)
# ---------------------------
# Force UTF-8 on the log stream so emoji/Unicode render correctly instead of mojibake
# (Windows consoles default to cp1252, which is what produced the "..."/"ðŸ”’" output).
_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
if hasattr(_log_handler.stream, "reconfigure"):
    try:
        _log_handler.stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
logging.basicConfig(
    level=logging.WARNING,  # Set baseline to WARNING to suppress noise
    handlers=[_log_handler],
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # Only our app logs at INFO level
# Named audit logger — who asked what and which source answered. Routable to App
# Insights / Log Analytics separately from debug output in deployment.
audit_log = logging.getLogger("audit")
audit_log.setLevel(logging.INFO)

# Disable ALL third-party logging
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)
logging.getLogger("azure").setLevel(logging.CRITICAL)
logging.getLogger("msal").setLevel(logging.CRITICAL)
logging.getLogger("openai").setLevel(logging.CRITICAL)
logging.getLogger("microsoft_teams").setLevel(logging.CRITICAL)
logging.getLogger("teams").setLevel(logging.CRITICAL)
logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
logging.getLogger("botbuilder").setLevel(logging.CRITICAL)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
# Enable INFO-level logs for our own sub-modules (indexer, embeddings, search)
for _mod in ("search", "search.ai_search_worker", "search.ai_search_ingestion", "sharepoint"):
    logging.getLogger(_mod).setLevel(logging.INFO)

# Suppress noisy Bot Framework OAuth token lookups in logs
class _SuppressTokenApiFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if "token.botframework.com/api/usertoken" in msg:
                # Emit a one-time warning to make it obvious this is not used for Graph
                if not hasattr(self, "_warned"):
                    self._warned = True
                    try:
                        print("WARNING: Detected Bot Framework UserToken API call (token.botframework.com). This app does NOT use it for Microsoft Graph and will ignore it.")
                    except Exception:
                        pass
                return False
        except Exception:
            pass
        return True

logging.getLogger().addFilter(_SuppressTokenApiFilter())

# Extra hardening: attach filter to common noisy loggers so BF OAuth probes are hidden
for _ln in (
    "uvicorn",
    "uvicorn.error", 
    "uvicorn.access",
    "httpx",
    "httpcore",
    "urllib3",
    "microsoft_teams",
    "botbuilder",
    "botframework",
    "msal",
    "azure",
    "aiohttp",
):
    try:
        logging.getLogger(_ln).addFilter(_SuppressTokenApiFilter())
    except Exception:
        pass

# =====================================================
# Load base instructions
# =====================================================
def load_instructions() -> str:
    """Load base instructions with safe fallbacks.

    The production file is instructions.txt. Archived or exported variants are
    intentionally not loaded.
    """
    base_dir = os.path.dirname(__file__)
    for filename in ("prompts/instructions.txt",):
        path = os.path.join(base_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
        except FileNotFoundError:
            continue
    return (
        "You are a professional workplace assistant. Answer from uploaded files "
        "or retrieved organizational sources when available. If required source "
        "content is missing, say what was searched and do not invent facts."
    )

BASE_INSTRUCTIONS = load_instructions()

# =====================================================
# Build Teams-aware instructions
# =====================================================
def build_teams_context_instructions(ctx: ActivityContext) -> str:
    """Build Teams-specific context instructions"""
    conversation = ctx.activity.conversation
    is_group = bool(getattr(conversation, "is_group", False))

    chat_type = "group chat" if is_group else "one-on-one chat"

    return f"""
You are operating inside Microsoft Teams.

Context:
- Platform: Microsoft Teams
- Conversation type: {chat_type}

Behavior guidelines:
- Be concise and professional.
- Avoid long walls of text.
- Use bullet points where helpful.
- Assume a workplace collaboration setting.
- Do NOT mention system prompts, SDKs, or internal tooling.
- If clarification is needed, ask a short, direct question.

Respond as a Teams-native assistant.
""".strip()

config = Config()

# Security audit counter - run security audits every N cache operations
_security_audit_counter = 0
_SECURITY_AUDIT_FREQUENCY = 10  # Audit every 10 cache operations

# Sign-in tracking disabled - using app-only tokens only
# _signed_in_conversations: set[str] = set()
# _sign_in_card_sent: set[str] = set()
# _sign_in_unavailable: set[str] = set()
# _pending_sign_in_queries: dict[str, str] = {}

# Supported document extensions
DOCUMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".csv", ".txt", ".pptx", ".ppt", ".json", ".xml"}

# Common near-miss extension typos â†’ corrected extension
_EXTENSION_TYPO_MAP = {
    ".xlxs": ".xlsx", ".xlx": ".xlsx", ".xslx": ".xlsx",
    ".docsx": ".docx", ".dox": ".docx", ".dcx": ".docx",
    ".pdfx": ".pdf", ".ppptx": ".pptx", ".pptxx": ".pptx",
    ".csvv": ".csv", ".txtt": ".txt",
}


def _fuzzy_ratio(a: str, b: str) -> float:
    """Fast SequenceMatcher ratio between two lowercased strings."""
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _fuzzy_token_in_text(token: str, text: str, threshold: float = 0.75) -> bool:
    """Check if *token* fuzzy-matches any word (or substring) in *text*.

    Tries exact substring first (fast path), then falls back to
    SequenceMatcher on every word in *text* for typo tolerance.
    """
    if not token or not text:
        return False
    token_l = token.lower()
    text_l = text.lower()
    # Fast path: exact substring
    if token_l in text_l:
        return True
    # Fuzzy: compare against each word in the text
    for word in re.split(r"[\s_\-./\\]+", text_l):
        if not word or len(word) < 2:
            continue
        if _fuzzy_ratio(token_l, word) >= threshold:
            return True
    return False

async def perform_parallel_searches(
    queries: list[str], 
    top_k: int, 
    cache_user_id: str,
    user_email: str,
    user_assertion: str,
    max_concurrent: int = 8,
    batch_size: int = 20
) -> dict:
    """Perform parallel searches for multiple document queries with optimized resource management.
    
    Supports many concurrent searches with:
    - Concurrency limiting to prevent API rate limits
    - Batching for memory efficiency 
    - Progress tracking for large search operations
    - Robust error handling for partial failures
    
    Args:
        queries: List of search queries to execute in parallel
        top_k: Maximum results per query
        cache_user_id: User ID for cache/permissions
        user_email: User email for permissions
        user_assertion: User assertion token for Graph API
        max_concurrent: Maximum concurrent searches per batch (default: 8)
        batch_size: Maximum queries per batch (default: 20)
        
    Returns:
        Dict with keys as query terms and values as search results
    """
    from knowledge_base import unified_search
    
    # Clean and deduplicate queries
    unique_queries = []
    seen = set()
    for q in queries:
        clean_q = q.strip().lower()
        if clean_q and clean_q not in seen:
            unique_queries.append(q.strip())
            seen.add(clean_q)
    
    if not unique_queries:
        logger.warning("No valid queries provided for parallel search")
        return {}
    
    # Apply limits to prevent token overflow
    top_k = min(top_k, Config.MAX_RESULTS_PER_QUERY)  # Configurable via MAX_RESULTS_PER_QUERY env var
    max_concurrent = min(max_concurrent, 4)
    
    logger.info(f"ðŸš€ Starting parallel searches: {len(unique_queries)} unique queries, max_concurrent={max_concurrent}, batch_size={batch_size}")
    
    # Create semaphore for concurrency control
    search_semaphore = asyncio.Semaphore(max_concurrent)
    
    async def search_single_query(query: str, query_index: int, total_queries: int):
        """Execute a single search query with concurrency control"""
        async with search_semaphore:
            try:
                logger.info(f"ðŸ” [{query_index+1}/{total_queries}] Searching: '{query}'")
                results = await asyncio.to_thread(
                    unified_search,
                    query,
                    top=top_k,
                    user_id=cache_user_id,
                    user_upn=user_email or "",
                    user_assertion=user_assertion,
                )
                doc_count = len(results or [])
                logger.info(f"âœ… [{query_index+1}/{total_queries}] '{query}': {doc_count} results")
                return query, results or []
            except Exception as e:
                logger.error(f"âŒ [{query_index+1}/{total_queries}] Error searching '{query}': {e}")
                return query, []
    
    # Process queries in batches for memory efficiency
    all_results = {}
    total_queries = len(unique_queries)
    
    for batch_start in range(0, total_queries, batch_size):
        batch_end = min(batch_start + batch_size, total_queries)
        current_batch = unique_queries[batch_start:batch_end]
        
        if total_queries > batch_size:
            logger.info(f"ðŸ“¦ Processing batch {(batch_start // batch_size) + 1}/{(total_queries + batch_size - 1) // batch_size}: queries {batch_start+1}-{batch_end}")
        
        # Create tasks for current batch
        tasks = [
            search_single_query(query, batch_start + i, total_queries)
            for i, query in enumerate(current_batch)
        ]
        
        # Execute batch with timeout protection
        try:
            batch_results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=60  # 60 second timeout per batch
            )
        except asyncio.TimeoutError:
            logger.error(f"â±ï¸  Batch timeout after 300s for queries {batch_start+1}-{batch_end}")
            batch_results = [(query, []) for query in current_batch]  # Empty results for timeout
        
        # Process batch results
        batch_success = 0
        batch_docs = 0
        for result in batch_results:
            if isinstance(result, Exception):
                logger.error(f"Batch search task failed: {result}")
                continue
            
            if isinstance(result, tuple) and len(result) == 2:
                query, docs = result
                all_results[query] = docs
                batch_success += 1
                batch_docs += len(docs)
            else:
                logger.warning(f"Unexpected result format: {type(result)}")
        
        if total_queries > batch_size:
            logger.info(f"ðŸ“Š Batch complete: {batch_success}/{len(current_batch)} queries successful, {batch_docs} documents")
    
    # Final summary
    total_docs = sum(len(docs) for docs in all_results.values())
    successful_searches = len([q for q, docs in all_results.items() if docs])
    
    logger.info(f"ðŸŽ‰ Parallel searches completed: {len(all_results)}/{total_queries} queries processed, {successful_searches} successful, {total_docs} total documents")
    
    if successful_searches == 0:
        logger.warning("âš ï¸  No searches returned results - check queries and permissions")
    elif successful_searches < len(unique_queries):
        failed_count = len(unique_queries) - successful_searches
        logger.warning(f"âš ï¸  {failed_count} searches returned no results")
    
    return all_results

def clean_search_query(text: str) -> str:
    """Normalize user queries for better document search recall."""
    if not text:
        return ""
    raw_tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    cleaned_tokens = []
    for tok in raw_tokens:
        t = tok.strip().strip("\"'`")
        t = re.sub(r"[^\w.\-]", "", t)
        if not t:
            continue
        t_lower = t.lower()
        # Correct common extension typos (e.g. .xlxs â†’ .xlsx)
        for typo, correct in _EXTENSION_TYPO_MAP.items():
            if t_lower.endswith(typo):
                t = t[: -len(typo)] + correct
                t_lower = t.lower()
                break
        # Strip known extensions to make partial titles match
        for ext in DOCUMENT_EXTENSIONS:
            if t_lower.endswith(ext) and len(t_lower) > len(ext):
                t = t[: -len(ext)]
                break
        if not t:
            continue
        cleaned_tokens.append(t)

    if not cleaned_tokens:
        return text.strip()
    return " ".join(cleaned_tokens)

def enhance_query_with_user_identity(query: str, user_name: str = None, user_email: str = None) -> str:
    """
    Intelligently expand possessive pronouns with actual user identity.
    Examples:
        "my cv" + user_name="Malvine Owuor" â†’ "malvine owuor cv resume"
        "find my documents" + user_name="John Smith" â†’ "john smith documents"
        "our team files" â†’ "our team files" (unchanged, plural possessive)
    """
    if not query or not user_name:
        return query
    
    query_lower = query.lower().strip()
    
    # Possessive pronouns that indicate personal ownership (singular)
    possessive_patterns = [
        (r'\bmy\b', 'first-person singular'),
        (r'\bmine\b', 'first-person singular'),
        (r'\bi\b', 'first-person subject')
    ]
    
    # Check if query contains personal possessive pronouns
    has_possessive = False
    for pattern, _ in possessive_patterns:
        if re.search(pattern, query_lower):
            has_possessive = True
            break
    
    if not has_possessive:
        return query
    
    # Extract first and last name for flexible matching
    name_parts = user_name.strip().split()
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[-1] if len(name_parts) > 1 else ""
    
    # Build enhanced query
    enhanced = query_lower
    
    # Replace "my" with user's name
    enhanced = re.sub(r'\bmy\b', f"{user_name.lower()}", enhanced)
    
    # Replace "mine" with user's name  
    enhanced = re.sub(r'\bmine\b', f"{user_name.lower()}", enhanced)
    
    # Replace standalone "i" (e.g., "documents i created")
    enhanced = re.sub(r'\bi\s+(created|wrote|made|uploaded)\b', f"{user_name.lower()} \\1", enhanced)
    
    # Add common synonyms for better recall
    # If searching for personal documents, add relevant keywords
    if any(keyword in query_lower for keyword in ['cv', 'resume', 'curriculum']):
        if 'resume' not in enhanced:
            enhanced += ' resume'
        if 'cv' not in enhanced and 'curriculum vitae' not in enhanced:
            enhanced += ' cv'
    
    logger.info(f"ðŸ§  Smart query expansion: '{query}' â†’ '{enhanced}'")
    return enhanced.strip()


def query_tokens(text: str) -> list[str]:
    cleaned = clean_search_query(text)
    stopwords = {
        "can", "you", "please", "for", "summarize", "summary", "tell",
        "about", "overview", "explain", "document", "file", "the", "and",
        "that", "this", "with", "from", "what", "is",
    }
    tokens = [t.lower() for t in re.split(r"\s+", cleaned) if len(t) > 2 and t.lower() not in stopwords]
    # Add joined variant to match filenames like "ticketRecords" when query is "ticket records"
    if len(tokens) > 1:
        joined = "".join(tokens)
        if joined and joined not in tokens:
            tokens.append(joined)
    return tokens


SUMMARY_REQUEST_PATTERNS = (
    "summarize", "summary", "overview", "tell me about",
    "what is this document", "explain this document", "review",
    "analyze", "analyse",
)


def is_document_summary_request(text: str) -> bool:
    lower = (text or "").lower()
    return any(p in lower for p in SUMMARY_REQUEST_PATTERNS)


# Deictic references to a document the user is providing *right now* (i.e. an
# attachment), as opposed to a named document in the library. Used to prevent
# the bot from summarizing unrelated search results when an attachment the user
# tried to share (e.g. a OneDrive cloud file) never reached the bot.
_REFERS_TO_ATTACHED_DOC_RE = re.compile(
    r"\b("
    r"this (document|file|attachment|doc|pdf|spreadsheet|sheet|deck|presentation|letter|report|image)|"
    r"the (attached|uploaded|attachment)|"
    r"(attached|uploaded) (document|file|doc|pdf|here)|"
    r"summari[sz]e (this|it)|"
    r"(document|file) (i|we) (just )?(sent|shared|attached|uploaded)"
    r")\b",
    re.IGNORECASE,
)


def refers_to_attached_document(text: str) -> bool:
    """True when the user is clearly referring to a document they just attached."""
    return bool(_REFERS_TO_ATTACHED_DOC_RE.search(text or ""))



def is_document_title_list_request(text: str, recent_history: list[str] | None = None) -> bool:
    """Detect metadata listing requests that should enumerate titles directly."""
    lower = (text or "").lower().strip()
    if not lower:
        return False
    listing_words = ("list", "show", "give me", "what are", "available", "top")
    title_words = ("document", "documents", "file", "files", "title", "titles")
    if any(w in lower for w in listing_words) and any(w in lower for w in title_words):
        return True
    if re.search(r"\btop\s+\d+\s+(document|documents|file|files|title|titles)\b", lower):
        return True
    history_text = " ".join(recent_history or []).lower()
    previous_was_listing = any(w in history_text for w in ("available document", "document titles", "file titles"))
    if previous_was_listing and any(p in lower for p in ("search for more", "find more", "show more", "list more", "more documents", "more files", "next 10", "next ten", "next page", "more")):
        return True
    return False


def is_document_title_pagination_request(text: str) -> bool:
    lower = (text or "").lower().strip()
    return any(p in lower for p in ("next 10", "next ten", "next page", "more", "show more", "list more", "more documents", "more files", "search for more", "find more"))


def is_document_title_summary_request(text: str) -> bool:
    """Detect follow-ups asking to summarize the titles just listed."""
    lower = (text or "").lower().strip()
    if not lower:
        return False
    summary_words = ("summary", "summarize", "summarise", "describe", "short summary", "what is each", "what are these")
    each_words = ("each", "all", "these", "them", "those", "listed", "above")
    url_words = ("url", "link", "links")
    return (
        any(w in lower for w in summary_words)
        and (any(w in lower for w in each_words) or any(w in lower for w in url_words))
    )


def requested_title_limit(text: str, default: int = 10) -> int:
    match = re.search(r"\btop\s+(\d+)\b|\b(\d+)\s+(?:document|documents|file|files|title|titles)\b", text or "", re.IGNORECASE)
    if not match:
        return default
    value = next((g for g in match.groups() if g), None)
    try:
        return max(1, min(int(value), 50))
    except Exception:
        return default


def short_document_summary_from_content(content: str, max_chars: int = 280) -> str:
    """Create a compact extractive summary from cached document text."""
    text = _strip_html(content or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(word|pdf|powerpoint|excel|csv|file)\s*:\s*[^ ]+\s*", "", text, flags=re.IGNORECASE)
    if not text:
        return "No cached text content is available for a summary."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    picked: list[str] = []
    total = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 20:
            continue
        if total + len(sentence) > max_chars and picked:
            break
        picked.append(sentence)
        total += len(sentence) + 1
        if total >= max_chars:
            break
    summary = " ".join(picked).strip() or text[:max_chars].strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
    return summary


def normalized_doc_title(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\.(docx?|pdf|pptx?|xlsx?|csv|txt|md|json|xml)$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def main_query_phrase(text: str) -> str:
    stopwords = {
        "can", "you", "please", "for", "summarize", "summary", "tell",
        "about", "overview", "explain", "document", "file", "the", "and",
        "that", "this", "with", "from", "what", "is",
    }
    cleaned = clean_search_query(text)
    return " ".join(
        t.lower()
        for t in re.split(r"\s+", cleaned)
        if len(t) > 2 and t.lower() not in stopwords
    )


def title_match_strength(doc: dict, query: str) -> tuple[int, str]:
    """Return a title-first match strength and normalized title."""
    title = normalized_doc_title(doc.get("name") or doc.get("title") or "")
    phrase = main_query_phrase(query)
    terms = query_tokens(query)
    if not title or not terms:
        return (0, title)
    if phrase and title == phrase:
        return (100, title)
    if phrase and phrase in title:
        return (90, title)
    matches = sum(1 for t in terms if t in title)
    if matches == len(terms):
        return (80, title)
    if matches >= max(1, len(terms) - 1) and matches >= 2:
        return (65, title)
    if matches:
        return (25 + matches * 10, title)
    return (0, title)


def is_cached_sharepoint_doc(doc: dict) -> bool:
    url = (doc.get("webUrl") or doc.get("url") or doc.get("file_path") or "").lower()
    metadata = doc.get("metadata") or {}
    return bool(
        doc.get("_from_document_cache")
        or doc.get("_from_sharepoint")
        or doc.get("visibility") == "shared"
        or "sharepoint" in url
        or doc.get("drive_id")
        or doc.get("driveId")
        or doc.get("item_id")
        or doc.get("itemId")
        or metadata.get("drive_id")
        or metadata.get("item_id")
    )


def is_document_file(filename: str) -> bool:
    """Check if filename has a supported document extension (not a folder)."""
    if not filename:
        return False
    name_lower = filename.lower()
    # Exclude folders (no extension) and system files
    if name_lower.endswith("/") or name_lower == ".":
        return False
    # Check for valid document extension
    for ext in DOCUMENT_EXTENSIONS:
        if name_lower.endswith(ext):
            return True
    return False


def is_file_attachment(att) -> bool:
    """Return True only for real file attachments (exclude cards/mentions/etc.)."""
    # Log BEFORE try block to ensure visibility
    logger.info(f"[ATTACHMENT CHECK] Checking attachment: type={type(att).__name__}, att_obj={att}")
    
    try:
        # Extract all possible attribute names
        content_type = (
            getattr(att, "content_type", None)
            or getattr(att, "contentType", None)
            or ""
        )
        content_type = (content_type or "").lower()
        name = getattr(att, "name", "") or ""
        
        # DEBUG: Log attachment details for troubleshooting
        logger.info(f"[ATTACHMENT DEBUG] content_type='{content_type}', name='{name}'")
        
        # Log all attributes for comprehensive debugging
        if hasattr(att, "__dict__"):
            logger.info(f"[ATTACHMENT DEBUG] Full attributes: {att.__dict__}")
        else:
            # Try to extract any available attributes
            all_attrs = [attr for attr in dir(att) if not attr.startswith('_')]
            attr_values = {attr: getattr(att, attr, None) for attr in all_attrs[:20]}  # Limit to first 20
            logger.info(f"[ATTACHMENT DEBUG] Available attributes: {attr_values}")
        
        # Check for potential mobile timing issues
        content = getattr(att, "content", None)
        has_content_structure = isinstance(content, dict) or (isinstance(content, str) and content.strip().startswith("{"))
        content_url = getattr(att, "contentUrl", None) or getattr(att, "content_url", None)
        
        # Log potential mobile timing issue
        if content_type == "application/vnd.microsoft.teams.file.download.info" and name:
            if not has_content_structure and not content_url:
                logger.warning(f"[MOBILE ISSUE?] Teams file attachment '{name}' has content_type but missing content/URLs - possible mobile timing issue")
            elif isinstance(content, dict) and not content.get("downloadUrl") and not content_url:
                logger.warning(f"[MOBILE ISSUE?] Teams file attachment '{name}' has content dict but no downloadUrl - possible mobile upload still processing")
        
        # Teams file attachment content type
        if content_type == "application/vnd.microsoft.teams.file.download.info":
            logger.info(f"[ATTACHMENT ACCEPTED] Teams file: '{name}'")
            return True
        # Fallback: treat as file if it has a document-like filename
        if name and is_document_file(name):
            logger.info(f"[ATTACHMENT ACCEPTED] Document file: '{name}'")
            return True
            
        # Special handling for text/html - could be rich message embeds or malformed file attachment
        if content_type == "text/html":
            logger.warning(f"[ATTACHMENT REJECTED] text/html attachment (possibly rich message embed, link preview, or mobile file with missing metadata): name='{name}'")
        else:
            logger.warning(f"[ATTACHMENT REJECTED] Not recognized as file: content_type='{content_type}', name='{name}'")
    except Exception as e:
        logger.error(f"[ATTACHMENT DEBUG] Exception in is_file_attachment: {e}", exc_info=True)
    
    return False


def validate_file_attachment(att) -> tuple[bool, str]:
    """Validate file size and type. Returns (is_valid, error_message)."""
    try:
        att_name = getattr(att, "name", "unknown")
        
        # Check file extension
        if '.' in att_name:
            ext = '.' + att_name.split('.')[-1].lower()
            
            # Check blocked types first
            if ext in Config.BLOCKED_FILE_TYPES:
                return False, f"âŒ File type '{ext}' is not allowed for security reasons."
            
            # Check allowed types
            if ext not in Config.ALLOWED_FILE_TYPES:
                allowed_list = ', '.join(sorted(Config.ALLOWED_FILE_TYPES))
                return False, f"âŒ File type '{ext}' is not supported. Allowed: {allowed_list}"
        
        # Check file size (if available)
        content = getattr(att, "content", None)
        if content and isinstance(content, dict):
            size_bytes = content.get("fileSize") or content.get("sizeInBytes") or 0
            if size_bytes > 0:
                size_mb = size_bytes / (1024 * 1024)
                if size_mb > Config.MAX_FILE_SIZE_MB:
                    return False, f"âŒ File size ({size_mb:.1f}MB) exceeds limit of {Config.MAX_FILE_SIZE_MB}MB."
        
        return True, ""
    except Exception as e:
        logger.error(f"Error validating attachment: {e}")
        return False, f"âŒ Unable to validate file: {str(e)}"


# ---------------------------
# SharePoint/OneDrive link resolution
# ---------------------------
# Cloud-picker files (and pasted document links) frequently arrive as URLs in the
# message text rather than as file attachments the bot can read. We detect those URLs
# and resolve them to real, downloadable attachments via Microsoft Graph.
_CLOUD_FILE_URL_RE = re.compile(r"https?://[^\s)>\]}\"']+", re.IGNORECASE)
_CLOUD_FILE_HOST_RE = re.compile(
    r"(sharepoint\.com|onedrive\.live\.com|1drv\.ms)", re.IGNORECASE
)


def _extract_cloud_file_urls(text: str) -> list[str]:
    """Return de-duplicated SharePoint/OneDrive URLs found in free text."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _CLOUD_FILE_URL_RE.findall(text):
        url = match.rstrip(".,);]}'\"")
        if _CLOUD_FILE_HOST_RE.search(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _resolve_cloud_file_attachments(text: str) -> list:
    """Resolve SharePoint/OneDrive URLs in the text into synthetic attachment objects.

    Each returned object mimics a Teams file attachment (``.name``, ``.content_type``
    and a ``.content`` dict with a pre-authenticated ``downloadUrl``) so the existing
    attachment pipeline can download and extract it unchanged.
    """
    from types import SimpleNamespace

    from sharepoint.graph_client import resolve_sharing_url

    resolved: list = []
    for url in _extract_cloud_file_urls(text):
        try:
            info = resolve_sharing_url(url)
        except Exception as exc:
            logger.info("Cloud URL resolve error for %s: %s", url[:80], type(exc).__name__)
            info = None
        if not info or not info.get("download_url"):
            continue
        name = info.get("name") or "shared-file"
        resolved.append(
            SimpleNamespace(
                name=name,
                content_type="application/vnd.microsoft.teams.file.download.info",
                content={"downloadUrl": info["download_url"], "name": name},
            )
        )
        logger.info("Resolved cloud file link into attachment: %s", name)
    return resolved


# ---------------------------
# Document Access Check - filter out inaccessible documents
# ---------------------------
def is_inaccessible_content(content: str) -> bool:
    """Check if document content indicates access failure or unauthorized.
    
    Returns True if content is a SHORT error message indicating the user cannot access the document.
    Only checks the first 500 chars â€” real documents can contain words like "unauthorized"
    in their body text (e.g. employee handbooks, policy documents) which must NOT trigger this.
    Additionally, if the content is long (>800 chars), it's almost certainly real document text,
    not an error page.
    """
    if not content or not isinstance(content, str):
        return False
    
    stripped = content.strip()
    
    # Real documents are long; error pages are short.
    # If we got >800 chars of content, it's genuine document text â€” never reject.
    if len(stripped) > 800:
        return False
    
    content_lower = stripped.lower()
    
    # Check for explicit error markers (only meaningful in short error responses)
    error_markers = [
        "[unable to download:",
        "[content appears to be an html viewer page",
        "http 400",
        "http 403",
        "http 404",
        "check files.read.all permission",
        "resource not found for the segment",
    ]
    
    # These markers are only checked if the content is SHORT (likely an error page)
    if any(marker in content_lower for marker in error_markers):
        return True
    
    # "access denied" and "unauthorized" only match if the ENTIRE content is an error message
    # (short text that is basically just the error itself, not a real document body)
    if len(stripped) < 300:
        short_error_markers = ["access denied", "unauthorized", "403 forbidden", "401 unauthorized"]
        if any(marker in content_lower for marker in short_error_markers):
            return True
    
    return False


# ---------------------------
# Small-talk / greeting detection (skip Graph/API for these)
# ---------------------------
def is_smalltalk(text: str) -> bool:
    """Detect short greetings/pleasantries that shouldn't trigger search."""
    if not text:
        return False
    t = text.strip().lower()
    short_greets = {
        "hi", "hello", "hey", "yo", "sup", "greetings",
        "good morning", "good afternoon", "good evening",
        "thanks", "thank you", "thanks!", "thank you!", "ok", "okay",
    }
    if t in short_greets:
        return True
    smalltalk_phrases = (
        "how are you",
        "how's it going",
        "what's up",
        "who are you",
        "what are you",
        "what can you do",
        "help me",
        "can you help",
        "tell me about yourself",
        "what can you help",
        "need help",
    )
    return any(p in t for p in smalltalk_phrases)


# ---------------------------
# CSV Chunking - split large CSVs into searchable rows
# ---------------------------
def chunk_csv_for_cache(csv_content: str, filename: str, chunk_size: int = 20) -> list:
    """Split CSV into logical chunks (groups of rows) for better search granularity.
    
    Returns list of (chunk_id, chunk_content) tuples.
    Each chunk contains the header + chunk_size rows.
    """
    content = csv_content.strip()
    if not content:
        return [(f"{filename}:chunk-0", csv_content)]

    try:
        sample = content[:2048]
        has_header = False
        try:
            has_header = csv.Sniffer().has_header(sample)
        except csv.Error:
            has_header = False

        reader = csv.reader(io.StringIO(content, newline=""))
        rows = [row for row in reader if any(cell.strip() for cell in row)]
        if not rows:
            return [(f"{filename}:chunk-0", csv_content)]

        header = rows[0] if has_header else None
        data_rows = rows[1:] if has_header else rows

        # If CSV is small, don't chunk
        if len(data_rows) <= chunk_size:
            return [(f"{filename}:full", csv_content)]

        chunks = []
        for i in range(0, len(data_rows), chunk_size):
            chunk_rows = data_rows[i:i + chunk_size]
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            if header:
                writer.writerow(header)
            writer.writerows(chunk_rows)
            chunk_content = buffer.getvalue().strip()
            chunk_id = f"{filename}:rows-{i+1}-{min(i+chunk_size, len(data_rows))}"
            chunks.append((chunk_id, chunk_content))

        return chunks
    except Exception:
        return [(f"{filename}:full", csv_content)]


# ---------------------------
# User Profile Cache - avoid repeated API calls
# ---------------------------
_user_profile_cache = {}
USER_PROFILES_CACHE_PATH = os.path.join(os.path.dirname(__file__), "user_profiles_cache.json")

def _atomic_write_json(path: str, obj: dict) -> None:
    """Atomically write JSON to disk to avoid partial/empty files on concurrent writes."""
    try:
        dir_name = os.path.dirname(path)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)
        tmp_path = path + ".tmp"
        
        # Write to temp file
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj or {}, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync may fail on some systems, that's OK
        
        # Replace original with temp file (with retry for Windows locking)
        try:
            os.replace(tmp_path, path)
        except FileExistsError:
            # File exists, try to remove it first (Windows)
            try:
                os.remove(path)
                os.replace(tmp_path, path)
            except Exception as e2:
                logger.warning(f"Failed to replace {path}, cleaning up temp: {e2}")
                try:
                    os.remove(tmp_path)
                except:
                    pass
    except Exception as e:
        logger.error(f"Failed atomic write for {path}: {e}")
        # Clean up temp file if it exists
        try:
            if 'tmp_path' in locals():
                os.remove(tmp_path)
        except:
            pass

def _read_user_profiles_cache() -> dict:
    try:
        if not os.path.exists(USER_PROFILES_CACHE_PATH):
            # Initialize empty cache file
            _atomic_write_json(USER_PROFILES_CACHE_PATH, {})
            return {}
        with open(USER_PROFILES_CACHE_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                # Repair empty file
                _atomic_write_json(USER_PROFILES_CACHE_PATH, {})
                return {}
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"Failed to read user profiles cache: {e}")
        # Attempt repair
        try:
            _atomic_write_json(USER_PROFILES_CACHE_PATH, {})
        except Exception:
            pass
        return {}

def _write_user_profiles_cache(cache_obj: dict) -> None:
    try:
        _atomic_write_json(USER_PROFILES_CACHE_PATH, cache_obj or {})
    except Exception as e:
        logger.error(f"Failed to write user profiles cache: {e}")

def get_cached_user_profile(user_id: str, user_assertion: str | None = None) -> dict:
    """Get user profile (Graph-only) from cache or fetch and persist.
    Returns the profile dict (displayName, givenName, mail, jobTitle, ...).
    On disk, stores as { user_id: { "profile": {...}, "cached_at": <epoch> } }.
    """
    if not user_id:
        return {}

    # 1) In-memory
    if user_id in _user_profile_cache:
        cached = _user_profile_cache[user_id]
        # If cached entry has no email, treat as stale and re-fetch from Graph
        if cached.get('mail') or cached.get('userPrincipalName'):
            logger.info(f"Using cached profile for user: {cached.get('displayName')}")
            return cached
        else:
            logger.info(f"Cached profile for {cached.get('displayName')} has no email â€” re-fetching from Graph")

    # 2) Disk-backed JSON cache
    disk_read_started_at = time.perf_counter()
    disk_cache = _read_user_profiles_cache()
    disk_read_elapsed = time.perf_counter() - disk_read_started_at
    if disk_read_elapsed > 0.25:
        logger.info(f"⏱️  User profile disk cache read took {disk_read_elapsed:.2f}s")
    entry = disk_cache.get(user_id)
    if isinstance(entry, dict) and entry:
        if "profile" in entry and isinstance(entry["profile"], dict):
            prof = entry["profile"]
        else:
            # Back-compat: flat format
            prof = entry
        # Only use disk cache if it has email; otherwise re-fetch
        if prof.get('mail') or prof.get('userPrincipalName'):
            _user_profile_cache[user_id] = prof
            logger.info(f"Loaded user profile from disk cache: {prof.get('displayName')}")
            return prof
        else:
            logger.info(f"Disk-cached profile for {prof.get('displayName')} has no email â€” re-fetching from Graph")

    # Fetch from Graph API using app-only tokens
    fetch_started_at = time.perf_counter()
    profile = get_user_profile(user_id, user_assertion=user_assertion)
    fetch_elapsed = time.perf_counter() - fetch_started_at
    if fetch_elapsed > 0.5:
        logger.info(f"⏱️  User profile Graph fetch took {fetch_elapsed:.2f}s")
    if profile:
        _user_profile_cache[user_id] = profile
        # Persist to disk in nested format
        try:
            import time as _time
            disk_cache[user_id] = {"profile": profile, "cached_at": _time.time()}
            _write_user_profiles_cache(disk_cache)
            logger.info(f"Cached new profile for user: {profile.get('displayName')}")
        except Exception as e:
            logger.error(f"Failed to persist user profile to disk: {e}")
    return profile


def _extract_user_assertion_from_activity(ctx: ActivityContext[MessageActivity]) -> str | None:
    """Best-effort extraction of a Teams SSO user token from the incoming activity.
    Note: Not all message activities include a user AAD token; if unavailable, returns None.
    """
    try:
        # Common places various SDKs may surface an SSO token
        # 1) activity.channel_data.*
        cd = getattr(ctx.activity, "channel_data", None) or {}
        for key in ("token", "id_token", "ssoToken", "teamsSsoToken", "accessToken", "authorization"):
            val = cd.get(key)
            if isinstance(val, str) and len(val) > 40 and "." in val:
                logger.info("Detected potential user assertion in channel_data")
                return val

        # 2) activity.value.* (for invoke)
        val = getattr(ctx.activity, "value", None)
        if isinstance(val, dict):
            for key in ("token", "id_token", "ssoToken", "teamsSsoToken", "accessToken"):
                t = val.get(key)
                if isinstance(t, str) and len(t) > 40 and "." in t:
                    logger.info("Detected potential user assertion in activity.value")
                    return t
    except Exception:
        pass
    return None

def _extract_user_upn_from_activity(ctx: ActivityContext[MessageActivity]) -> str | None:
    """Best-effort extraction of user's UPN/email from the incoming activity."""
    try:
        sender = getattr(ctx.activity, "from_property", None)
        if sender:
            for attr in ("userPrincipalName", "mail", "email"):
                v = getattr(sender, attr, None)
                if isinstance(v, str) and "@" in v:
                    return v
        cd = getattr(ctx.activity, "channel_data", None) or {}
        for key in ("userPrincipalName", "upn", "mail", "email"):
            v = cd.get(key)
            if isinstance(v, str) and "@" in v:
                return v
    except Exception:
        pass
    return None

# Persisted user details (displayName, mail, aadObjectId, etc.)
_remembered_users: dict[str, dict] = {}

def remember_user_details(user_key: str, details: dict) -> None:
    """Persist user details for future turns and disk cache.
    Writes Graph-style profile under AAD ID (if present) and user_key, in nested format.
    """
    if not user_key:
        return
    stored = _remembered_users.get(user_key, {})
    for k, v in (details or {}).items():
        if v:
            stored[k] = v
    _remembered_users[user_key] = stored

    # Persist to disk under stable keys
    try:
        aad_id = stored.get("aadObjectId") or details.get("aadObjectId")
        disk_cache = _read_user_profiles_cache()
        if not isinstance(disk_cache, dict):
            disk_cache = {}

        def _merge_into(cache_obj: dict, key: str):
            if not key:
                return
            existing = cache_obj.get(key, {}) if isinstance(cache_obj, dict) else {}
            # Normalize to nested
            if isinstance(existing, dict) and "profile" in existing and isinstance(existing["profile"], dict):
                prof_obj = existing["profile"].copy()
            elif isinstance(existing, dict):
                # Back-compat flat -> treat as profile
                prof_obj = existing.copy()
            else:
                prof_obj = {}
            # Merge selected Graph fields
            for fld in ("displayName", "givenName", "mail", "userPrincipalName", "jobTitle"):
                val = stored.get(fld) or details.get(fld)
                if val:
                    prof_obj[fld] = val
            import time as _time
            cache_obj[key] = {"profile": prof_obj, "cached_at": _time.time()}

        if aad_id:
            _merge_into(disk_cache, aad_id)
        # Always also persist under the user_key
        _merge_into(disk_cache, user_key)
        _write_user_profiles_cache(disk_cache)
        keys_written = ", ".join([k for k in [aad_id, user_key] if k])
        logger.info(f"Persisted user profile to disk cache (keys: {keys_written})")
    except Exception as e:
        logger.error(f"Failed to persist user profile to disk: {e}")


def get_remembered_user_details(user_key: str) -> dict:
    """Retrieve previously stored user details (profile only).
    Falls back to on-disk cache when memory has no entry. Handles nested format.
    """
    mem = _remembered_users.get(user_key)
    if isinstance(mem, dict) and mem:
        return mem.copy()
    try:
        disk_cache = _read_user_profiles_cache()
        data = disk_cache.get(user_key)
        if isinstance(data, dict) and data:
            if "profile" in data and isinstance(data["profile"], dict):
                prof = data["profile"].copy()
            else:
                prof = data.copy()
            _remembered_users[user_key] = prof
            return prof
    except Exception:
        pass
    return {}


# ---------------------------
# Background Tasks - track async web indexing tasks
# ---------------------------
background_tasks = []
user_crawl_tasks: dict[str, asyncio.Task] = {}
shared_crawl_task: asyncio.Task | None = None
crawl_executor = concurrent.futures.ThreadPoolExecutor(max_workers=int(os.environ.get("CRAWL_WORKERS", "2")))

def add_background_task(task, task_name: str = ""):
    """Add a background task with completion logging and cleanup"""
    def task_done_callback(t):
        try:
            if t.cancelled():
                logger.warning(f"Task '{task_name}' was cancelled")
            elif t.exception():
                logger.error(f"Task '{task_name}' failed: {t.exception()}", exc_info=t.exception())
            else:
                result = t.result()
                logger.info(f"âœ“ Task '{task_name}' completed successfully. Result: {result} pages indexed")
        finally:
            # Remove task from background_tasks list once it's done
            try:
                background_tasks.remove(t)
                logger.info(f"Cleaned up task '{task_name}' from background tasks")
            except (ValueError, AttributeError):
                pass  # Task already removed or list doesn't exist

    task.add_done_callback(task_done_callback)
    background_tasks.append(task)


async def ensure_user_crawl(user_id: str):
    """Start a full crawl for the user if one is not already running."""
    if not user_id:
        return

    existing = user_crawl_tasks.get(user_id)
    if existing and not existing.done():
        return

    loop = asyncio.get_running_loop()
    
    # run_in_executor() returns a Future directly (not a coroutine)
    # Don't wrap it in create_task() - just use the Future as-is
    future = loop.run_in_executor(
        crawl_executor,
        crawl_accessible_documents,
        user_id,
        True,  # include_personal
        True,  # include_sites
    )

    user_crawl_tasks[user_id] = future

    def _cleanup(f):
        try:
            if user_crawl_tasks.get(user_id) is f:
                user_crawl_tasks.pop(user_id, None)
        except Exception:
            pass

    add_background_task(future, f"full_crawl_{user_id[:8]}")
    future.add_done_callback(_cleanup)


async def ensure_shared_crawl():
    """Start a one-time shared crawl for configured SharePoint sites."""
    global shared_crawl_task
    if shared_crawl_task and not shared_crawl_task.done():
        return

    loop = asyncio.get_running_loop()
    
    # run_in_executor() returns a Future directly (not a coroutine)
    # Don't wrap it in create_task() - just use the Future as-is
    shared_crawl_task = loop.run_in_executor(
        crawl_executor,
        crawl_accessible_documents,
        "shared",
        False,  # include_personal
        True,   # include_sites
    )
    add_background_task(shared_crawl_task, "shared_crawl")


# ---------------------------
# Instructions
# ---------------------------
def load_instructions() -> str:
    base_dir = os.path.dirname(__file__)
    for filename in ("prompts/instructions.txt",):
        path = os.path.join(base_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
        except FileNotFoundError:
            continue
    return "You are a helpful assistant. Be concise and professional."

BASE_INSTRUCTIONS = load_instructions()


# ---------------------------
# No hardcoded keyword functions
# ---------------------------
# All routing decisions are now made by the LLM router for dynamic, context-aware intelligence


def dedupe_response(text: str) -> str:
    """Remove duplicate paragraphs (case-insensitive, ignoring extra whitespace)."""
    if not text:
        return text
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    seen = set()
    cleaned = []
    for p in parts:
        key = " ".join(p.split()).lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(p)
    return "\n\n".join(cleaned)


# ---------------------------
# Token budgeting helpers (approximate)
# ---------------------------
def _approx_token_count(text: str) -> int:
    """Rough estimate: ~4 chars per token."""
    try:
        return max(0, int(len(text) / 4))
    except Exception:
        return 0

def _trim_to_token_budget(text: str, max_tokens: int) -> str:
    try:
        allowed_chars = max(0, int(max_tokens) * 4)
        return text if len(text) <= allowed_chars else text[:allowed_chars]
    except Exception:
        return text

def _convert_to_network_path(path: str) -> str:
    """
    Convert local drive paths to network UNC paths or format as plain reference.
    Removes clickable links and shows full path for reference.
    
    Examples:
        C:\\Users\\Documents\\file.pdf -> File Path: C:\\Users\\Documents\\file.pdf
        D:\\Shared\\Data\\report.xlsx -> File Path: D:\\Shared\\Data\\report.xlsx
        https://... -> SharePoint/OneDrive: https://...
    """
    if not path or not path.strip():
        return ""
    
    path = path.strip()
    
    # Keep SharePoint/OneDrive URLs as-is but mark them
    if path.startswith(("http://", "https://")):
        # For web URLs, show as reference (not clickable)
        return f"SharePoint/OneDrive: {path}"
    
    # Local file paths - show as plain text reference
    # Check if it looks like a local path (has drive letter or starts with backslash)
    if (len(path) >= 2 and path[1] == ':') or path.startswith('\\\\'):
        # This is a local or network path - show exactly as-is
        return f"File Path: {path}"
    
    # Unknown format - show as-is
    return f"Location: {path}"


# ===== STABILIZATION: Helper Functions =====

def is_small_talk(text: str) -> bool:
    """Detect greetings, acknowledgements, and light social messages.

    These should NEVER trigger SharePoint, cache, Graph, or document retrieval.
    They are conversation-management turns, not knowledge requests.
    """
    if not text:
        return False

    normalized = re.sub(r"[^a-z0-9' ]+", "", text.strip().lower())
    normalized = " ".join(normalized.split())

    small_talk_exact = {
        "hi", "hello", "hey", "yo", "sup", "hola",
        "howdy", "hiya", "heya", "hey there", "hello there", "hi there",
        "good morning", "good afternoon", "good evening", "good day",
        "morning", "afternoon", "evening",
        "gm", "bye", "goodbye", "see ya", "later",
        "thanks", "thank you", "thanks a lot", "appreciate it",
        "ok", "okay", "k", "yes", "no", "sure", "got it", "understood",
        "cool", "nice", "great", "awesome", "wow", "lol", "haha",
        "i love you", "love you", "ily", "you are amazing", "youre amazing",
        "you are the best", "youre the best", "good job", "well done",
    }

    if normalized in small_talk_exact:
        return True

    small_talk_patterns = (
        "how are you",
        "how's it going",
        "hows it going",
        "what's up",
        "whats up",
        "wassup",
        "whatsup",
        "what's good",
        "whats good",
        "how do you do",
        "nice to meet you",
        "thank you for",
        "thanks for",
        "i appreciate you",
    )
    return any(p in normalized for p in small_talk_patterns)


def is_personal_advice_request(text: str) -> bool:
    """Detect personal/life-advice requests that should be answered directly."""
    if not text:
        return False

    normalized = re.sub(r"[^a-z0-9' ]+", " ", text.strip().lower())
    normalized = " ".join(normalized.split())

    personal_markers = (
        "my girlfriend", "my gf", "my boyfriend", "my bf", "my partner",
        "my wife", "my husband", "my friend", "my family", "my mom",
        "my dad", "relationship", "dating", "breakup", "broke up",
        "drunk", "saloon", "salon", "bar", "late at night",
        "what would you have done", "what should i do", "please guide me",
        "guide me through", "i feel", "i'm worried", "im worried",
        "i am worried", "i'm upset", "im upset", "i am upset",
    )
    if not any(marker in normalized for marker in personal_markers):
        return False

    organizational_markers = (
        "swope", "sharepoint", "company", "organization", "policy",
        "procedure", "handbook", "employee", "hr", "clinic",
        "document", "file", "report",
    )
    return not any(
        re.search(r"\b" + re.escape(marker).replace(r"\ ", r"\s+") + r"\b", normalized)
        for marker in organizational_markers
    )


def small_talk_response(text: str) -> str:
    """Return a short Teams-native response for small-talk turns."""
    normalized = re.sub(r"[^a-z0-9' ]+", "", (text or "").strip().lower())
    normalized = " ".join(normalized.split())

    if normalized in {"hi", "hello", "hey", "yo", "sup", "hola", "howdy", "hiya", "heya", "hey there", "hello there", "hi there", "good morning", "good afternoon", "good evening", "good day", "morning", "afternoon", "evening", "gm"} or "how are you" in normalized or "how's it going" in normalized or "hows it going" in normalized:
        return "Hi! How can I help you today?"
    if normalized in {"thanks", "thank you", "thanks a lot", "appreciate it", "thank you for", "thanks for"} or normalized.startswith(("thanks for", "thank you for")):
        return "You're welcome. What would you like to work on next?"
    if normalized in {"bye", "goodbye", "see ya", "later"}:
        return "Goodbye!"
    if normalized in {"i love you", "love you", "ily", "you are amazing", "youre amazing", "you are the best", "youre the best", "good job", "well done"}:
        return "Thatâ€™s kind of you, thank you. How can I help with your work today?"
    if normalized in {"ok", "okay", "k", "yes", "sure", "got it", "understood", "cool", "nice", "great", "awesome", "wow", "lol", "haha"}:
        return "Got it. What should we do next?"
    return "I'm here. How can I help?"


def safe_truncate(text: str, max_chars: int = 6000) -> str:
    """Hard-cap any text for LLM context. Cache may store full text; prompt must use snippets only."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...TRUNCATED...]"

def estimate_tokens(text: str) -> int:
    """Quick token estimation: ~1 token per 4 chars (rough GPT-5.2 estimate)."""
    if not text:
        return 0
    return max(1, len(text) // 4)

def cap_doc_content(text: str, max_chars: int = 12000) -> str:
    """Cap document content to prevent full PDF injection into prompts."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[DOCUMENT CONTENT CAPPED - FULL CONTENT IN CACHE]"




# ---------------------------
# Token factory for Teams app
# ---------------------------
def create_token_factory():
    tenant_id = config.GRAPH_TENANT_ID or config.APP_TENANTID
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    client_id = config.APP_ID
    client_secret = config.APP_PASSWORD

    def _first_scope(scopes) -> str:
        if isinstance(scopes, str):
            return scopes.strip()
        if isinstance(scopes, (list, tuple)) and scopes:
            return str(scopes[0]).strip()
        # Safe default to Graph if not provided
        return "https://graph.microsoft.com/.default"

    def _resource_from_scope(scope: str) -> str:
        return scope[:-9] if scope.endswith("/.default") else scope

    def get_token(scopes, tenant_id=None):
        try:
            requested_scope = _first_scope(scopes)
            resource = _resource_from_scope(requested_scope)

            # Prefer Managed Identity in Azure App Service (UserAssignedMSI configured in bicep)
            identity_endpoint = os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT")
            identity_header = os.environ.get("IDENTITY_HEADER") or os.environ.get("MSI_SECRET")
            if identity_endpoint and identity_header:
                params = {"resource": resource, "api-version": "2019-08-01"}
                if client_id:
                    # Pin to user-assigned identity
                    params["client_id"] = client_id
                headers = {"X-IDENTITY-HEADER": identity_header} if os.environ.get("IDENTITY_HEADER") else {"Secret": identity_header}
                resp = requests.get(identity_endpoint, params=params, headers=headers, timeout=30)
                if resp.status_code == 200:
                    token = (resp.json() or {}).get("access_token")
                    if token:
                        logger.info(f"MSI token acquired for resource: {resource}")
                        return token
                # If MSI fails, fall through to client credentials if available
                logger.error(f"MSI token acquisition failed for {resource}: HTTP {resp.status_code} {resp.text[:200]}")

            # IMDS (VM) fallback for MSI
            if os.environ.get("IDENTITY_ENDPOINT") is None:
                imds = "http://169.254.169.254/metadata/identity/oauth2/token"
                params = {"api-version": "2018-02-01", "resource": resource}
                if client_id:
                    params["client_id"] = client_id
                headers = {"Metadata": "true"}
                try:
                    resp = requests.get(imds, params=params, headers=headers, timeout=30)
                    if resp.status_code == 200:
                        token = (resp.json() or {}).get("access_token")
                        if token:
                            logger.info(f"IMDS token acquired for resource: {resource}")
                            return token
                    logger.error(f"IMDS token acquisition failed for {resource}: HTTP {resp.status_code} {resp.text[:200]}")
                except Exception:
                    pass

            # Client credentials fallback (requires secret). Use provided scope as-is.
            if client_id and client_secret and tenant_id:
                data = {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                    "scope": requested_scope,
                }
                resp = requests.post(token_url, data=data, timeout=30)
                if resp.status_code == 200:
                    token = (resp.json() or {}).get("access_token")
                    if token:
                        logger.info(f"Client credentials token acquired for scope: {requested_scope}")
                        return token
                raise Exception(f"Token acquisition failed (client creds for {requested_scope}): HTTP {resp.status_code} {resp.text[:200]}")

            raise Exception(f"Token acquisition failed: No MSI or client secret available for resource {resource}")
        except Exception:
            raise

    return get_token


# ---------------------------------------------------------------------------
# Bot credential wiring for the Teams SDK.
#
# This bot is registered as an Azure Bot Service (SingleTenant) resource:
#   bot-armelyai-local-dev (RG: ArmelyDefault_RG), app 0d238f90-...
# For a SingleTenant bot, the Bot Connector access token MUST be acquired against
# the bot's HOME tenant (TENANT_ID / TEAMS_APP_TENANT_ID) — the SDK's TokenManager
# derives the bot-token authority from ClientCredentials.tenant_id, so it must stay
# set. (For a MULTI-TENANT bot it is the opposite: tenant_id must be None so the
# token targets botframework.com, otherwise AADSTS7000229 breaks outgoing replies.)
#
# The SDK's App._init_credentials() reads CLIENT_ID / CLIENT_SECRET / TENANT_ID from
# the environment. One problem with the env values here: CLIENT_SECRET in root .env
# may be a placeholder; the real bot password is loaded by config from Key Vault
# (SECRET_BOT_PASSWORD). So we pass client_id + the real client_secret explicitly.
#
# config.* already captured every value it needs at import time, and Graph code reads
# config.GRAPH_TENANT_ID / config.TENANT_ID (not os.environ), so clearing the
# placeholder CLIENT_SECRET from os.environ (multi-tenant branch only) does not
# affect Graph auth.
_bot_client_id = config.APP_ID
_bot_client_secret = config.APP_PASSWORD
_is_multitenant_bot = (config.APP_TYPE or "MultiTenant").strip().lower() in ("multitenant", "multi-tenant", "")

if _is_multitenant_bot:
    # Drop env values the SDK would otherwise read so ClientCredentials.tenant_id
    # stays None and the Bot Connector token targets botframework.com.
    os.environ.pop("TENANT_ID", None)
    os.environ.pop("CLIENT_SECRET", None)

if _bot_client_id and _bot_client_secret:
    if _is_multitenant_bot:
        app = App(
            client_id=_bot_client_id,
            client_secret=_bot_client_secret,
            token=create_token_factory(),
        )
    else:
        # Single-tenant bot: keep the home tenant so token authority is correct.
        app = App(
            client_id=_bot_client_id,
            client_secret=_bot_client_secret,
            tenant_id=config.APP_TENANTID or None,
            token=create_token_factory(),
        )
else:
    # Fall back to env-based resolution if credentials are not available.
    app = App(token=create_token_factory())

# Cleanup old attachment cache entries on startup
try:
    cleaned = cleanup_old_cache()
    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} old attachment cache entries on startup")
except Exception as e:
    logger.warning(f"Failed to cleanup attachment cache on startup: {e}")


# ---------------------------
# Azure OpenAI model
# ---------------------------
model = OpenAICompletionsAIModel(
    key=config.AZURE_OPENAI_API_KEY,
    model=config.AZURE_OPENAI_MODEL_DEPLOYMENT_NAME,
    azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
    api_version="2024-10-21",
)

# Serialize LLM calls to reduce Azure OpenAI 429s
llm_semaphore = asyncio.Semaphore(max(1, int(getattr(Config, "LLM_CONCURRENCY", 1))))


# ---------------------------
# Conversation memory and file storage  
# ---------------------------
# âœ… SIMPLE, CLEAN CONVERSATION MANAGEMENT
conversation_store: dict[str, ListMemory] = {}  # in-process working copy for the current message
conversation_files: dict[str, list] = {}  # Store uploaded files per conversation
conversation_last_query: dict[str, str] = {}  # Store last search query per conversation
conversation_last_sources: dict[str, list] = {}  # Store last doc sources per conversation for follow-up references
conversation_title_list_state: dict[str, dict] = {}  # Store document title pagination state per conversation
conversation_summaries: dict[str, dict] = {}  # Compact running brief per conversation
background_tasks: list = []  # Track background indexing tasks

# Phase 3: durable conversation memory (turns + summary + last_sources) that survives
# restart and can be shared across instances. Backed by Azure Table Storage when
# AZURE_STORAGE_CONNECTION_STRING is set, else a local JSON file. The in-process dicts
# above act as the working copy for one message: hydrated on entry, flushed on exit.
from storage.conversation_store import ConversationStore, ConversationState  # noqa: E402
conversation_db = ConversationStore(connection_string=os.getenv("AZURE_STORAGE_CONNECTION_STRING"))

# Phase 7-Pre: per-user Teams SSO token cache, fed by the signin/tokenExchange handler
# and read by the OBO exchange (get_graph_token_delegated). Empty locally (Playground
# doesn't send SSO tokens), so it's a no-op until the bot runs in real Teams.
from storage.sso_token_cache import SSOTokenCache  # noqa: E402
sso_token_cache = SSOTokenCache()


class _StoredTurn:
    """Lightweight memory item with mutable role/content. The SDK send() uses
    memory=None, so ListMemory items are only read by this module — no SDK type needed."""
    __slots__ = ("role", "content")

    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content

def _get_memory_items(memory: ListMemory) -> list:
    """Access the actual internal storage of ListMemory (NOT .messages which doesn't exist)."""
    storage = getattr(memory, '_storage', None)
    if storage is not None:
        items = getattr(storage, '_items', None)
        if items is not None:
            return items
    # Fallback: try .messages just in case a future SDK version adds it
    if hasattr(memory, 'messages') and memory.messages is not None:
        return memory.messages
    return []


def _set_memory_items(memory: ListMemory, items: list) -> None:
    """Replace the actual internal storage of ListMemory."""
    storage = getattr(memory, '_storage', None)
    if storage is not None and hasattr(storage, '_items'):
        storage._items = items
        return
    if hasattr(memory, 'messages'):
        memory.messages = items


def get_or_create_conversation_memory(conversation_id: str) -> ListMemory:
    """Get or create conversation memory with NUCLEAR automated trimming"""
    if conversation_id not in conversation_store:
        conversation_store[conversation_id] = ListMemory()
        is_group_conv = 'group' in conversation_id.lower() or len(conversation_id) > 50
        conv_type = "GROUP" if is_group_conv else "PERSONAL"
        logger.info(f"Created new {conv_type} conversation memory for {conversation_id[:20]}...")
    
    # NUCLEAR FIX: Enforce HARD LIMIT on conversation memory
    memory = conversation_store[conversation_id]
    max_turns = int(getattr(Config, "MAX_MEMORY_TURNS", 8))
    max_messages = max_turns * 2  # Each turn = user + assistant = 2 messages
    
    # NUCLEAR: ALWAYS enforce max_messages â€” uses _storage._items (the REAL storage)
    items = _get_memory_items(memory)
    if items:
        message_count = len(items)
        
        # Hard trim if exceeded
        if message_count > max_messages:
            _set_memory_items(memory, items[-max_messages:])
            logger.error(f"ðŸ”ªðŸ”ª NUCLEAR TRIM: {message_count} -> {max_messages} messages ({max_turns} turns)")
        
        # Always log memory state for monitoring
        if message_count > 0:
            logger.info(f"Memory state: {message_count}/{max_messages} messages")
    
    return memory

def clear_conversation_memory(conversation_id: str) -> bool:
    """Clear conversation memory - simple approach"""
    try:
        if conversation_id in conversation_store:
            del conversation_store[conversation_id]
            conversation_summaries.pop(conversation_id, None)
            logger.info(f"âœ“ Cleared conversation memory for {conversation_id[:8]}...")
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to clear conversation memory: {e}")
        return False
    finally:
        try:
            conversation_db.delete(conversation_id)
        except Exception:
            pass


def _serialize_turns(memory: ListMemory) -> list[dict]:
    """ListMemory items -> [{role, content}] for durable storage."""
    out: list[dict] = []
    for m in _get_memory_items(memory) or []:
        content = getattr(m, "content", None) or ""
        if not content:
            continue
        out.append({"role": str(getattr(m, "role", "user") or "user"), "content": str(content)})
    return out


def _hydrate_turns(memory: ListMemory, turns: list[dict]) -> None:
    """[{role, content}] -> ListMemory items."""
    items = [
        _StoredTurn(str(t.get("role") or "user"), str(t.get("content") or ""))
        for t in (turns or [])
        if t.get("content")
    ]
    _set_memory_items(memory, items)


def _trim_turns_to_budget(turns: list[dict], max_messages: int, max_bytes: int = 26_000) -> list[dict]:
    """Keep at most max_messages recent messages, and ensure the JSON payload stays
    under max_bytes (Azure Table string property limit is 32KB). Trims oldest first."""
    turns = turns[-max_messages:] if max_messages > 0 else []
    while turns and len(json.dumps(turns, ensure_ascii=False).encode("utf-8")) > max_bytes:
        turns = turns[1:]
    return turns


def _load_conversation_state(conversation_id: str) -> None:
    """Hydrate the in-process working copy from the durable store (called per message)."""
    try:
        state = conversation_db.get(conversation_id)
        memory = get_or_create_conversation_memory(conversation_id)
        _hydrate_turns(memory, state.turns)
        if state.summary:
            conversation_summaries[conversation_id] = {
                "summary": state.summary,
                "turn_count": max(1, len(state.turns) // 2),
                "updated_at": state.updated_at or "",
            }
        if state.last_sources:
            conversation_last_sources[conversation_id] = state.last_sources
    except Exception as exc:
        logger.warning("Conversation state load failed for %s: %s", conversation_id, exc)


def _persist_conversation_state(conversation_id: str) -> None:
    """Flush the in-process working copy to the durable store once per message."""
    try:
        memory = conversation_store.get(conversation_id)
        turns = _serialize_turns(memory) if memory is not None else []
        max_messages = int(getattr(Config, "MAX_MEMORY_TURNS", 5)) * 2
        turns = _trim_turns_to_budget(turns, max_messages)
        summary = (conversation_summaries.get(conversation_id, {}) or {}).get("summary", "")
        raw_sources = conversation_last_sources.get(conversation_id, []) or []
        last_sources = [
            {
                "title": s.get("title", ""),
                "url": s.get("url", ""),
                "snippet": (s.get("snippet", "") or "")[:2000],
                "truncated": bool(s.get("truncated")),
                "primary": bool(s.get("primary")),
                "total_chars": s.get("total_chars"),
            }
            for s in raw_sources[:8]
        ]
        conversation_db.save(
            conversation_id,
            ConversationState(turns=turns, summary=summary, last_sources=last_sources),
        )
    except Exception as exc:
        logger.warning("Conversation state persist failed for %s: %s", conversation_id, exc)

def _compact_for_conversation_summary(text: str, max_chars: int = 700) -> str:
    """Keep a message readable while removing noisy markup."""
    if not text:
        return ""
    clean = _strip_html(str(text))
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"ChatSendResult\(response=ModelMessage\(content=", "", clean)
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3].rstrip() + "..."


def _merge_conversation_summary(previous: str, turn_note: str, max_chars: int = 1800) -> str:
    """Maintain a compact rolling brief without another LLM call."""
    merged = "\n".join(p.strip() for p in (previous, turn_note) if p and p.strip())
    if len(merged) <= max_chars:
        return merged

    lines = [line.strip() for line in merged.splitlines() if line.strip()]
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        line_len = len(line) + 1
        if total + line_len > max_chars:
            break
        kept.append(line)
        total += line_len
    return "\n".join(reversed(kept))


def update_conversation_summary(
    *,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    route: dict,
    source_names: list[str] | None = None,
) -> None:
    """Update a compact running brief used as quick-reference context."""
    try:
        previous_state = conversation_summaries.get(conversation_id, {}) or {}
        previous_summary = previous_state.get("summary", "")
        action = route.get("action", "respond_direct")
        scope = route.get("scope", "local")
        source_part = ""
        if source_names:
            source_part = " Sources: " + ", ".join([s for s in source_names if s][:5]) + "."
        turn_note = (
            f"- User: {_compact_for_conversation_summary(user_text, 450)}\n"
            f"  Assistant: {_compact_for_conversation_summary(assistant_text, 650)}\n"
            f"  Route: {action}/{scope}.{source_part}"
        )
        summary = _merge_conversation_summary(previous_summary, turn_note)
        conversation_summaries[conversation_id] = {
            "summary": summary,
            "turn_count": int(previous_state.get("turn_count", 0)) + 1,
            "last_route": action,
            "last_scope": scope,
            "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        logger.info(
            "Conversation summary updated: turns=%s chars=%s",
            conversation_summaries[conversation_id]["turn_count"],
            len(summary),
        )
    except Exception as err:
        logger.warning("Conversation summary update failed: %s", err)


def get_conversation_summary_text(conversation_id: str) -> str:
    """Return the compact running brief for prompt/router context."""
    state = conversation_summaries.get(conversation_id, {}) or {}
    summary = state.get("summary", "")
    if not summary:
        return ""
    return (
        "[RUNNING CONVERSATION BRIEF]\n"
        f"Updated: {state.get('updated_at', 'unknown')} | Turns summarized: {state.get('turn_count', 0)}\n"
        f"{summary}"
    )

# âœ… REMOVED: Complex JSON persistence - using simple in-memory approach instead

# Helper functions for conversation state
def files_for(conversation_id: str, user_id: str | None = None) -> list:
    """Get cached attachment metadata for a conversation (never full content in chat)."""
    return get_conversation_attachments(conversation_id, include_content=False, user_id=user_id)



def last_query_for(conversation_id: str) -> str:
    return conversation_last_query.get(conversation_id, "")

def set_last_query(conversation_id: str, query: str):
    conversation_last_query[conversation_id] = query


# ---------------------------
# Retry logic for OpenAI calls
# ---------------------------
async def call_llm_with_retry(prompt_func, max_retries: int | None = None, base_delay: float | None = None):
    """
    Call LLM with minimal retry for transient errors (429/5xx), respecting config.

    Args:
        prompt_func: Async function that makes the LLM call
        max_retries: Optional override; defaults to Config.RETRY_MAX_RETRIES
        base_delay: Optional override; defaults to Config.RETRY_BASE_DELAY

    Returns:
        LLM response or raises last exception
    """
    retries = config.RETRY_MAX_RETRIES if max_retries is None else max_retries
    delay = config.RETRY_BASE_DELAY if base_delay is None else base_delay
    attempt = 0

    while True:
        try:
            return await prompt_func()
        except Exception as e:
            error_str = str(e).lower()
            transient = (
                "429" in error_str or
                "rate limit" in error_str or
                "ratelimit" in error_str or
                "temporary" in error_str or
                "timeout" in error_str or
                "503" in error_str or
                "502" in error_str or
                "504" in error_str
            )
            if attempt >= max(0, int(retries)) or not transient:
                # No retries left or non-transient error
                raise

            # Backoff with jitter; respect Retry-After if present on common exception types
            retry_after = None
            try:
                # Some SDKs attach a response or headers to the exception
                resp = getattr(e, "response", None) or getattr(e, "res", None)
                if resp is not None:
                    hdrs = getattr(resp, "headers", {}) or {}
                    retry_after = hdrs.get("Retry-After") or hdrs.get("retry-after")
                    if retry_after:
                        retry_after = float(retry_after)
            except Exception:
                retry_after = None

            import random
            sleep_for = float(retry_after) if retry_after else (delay * (2 ** attempt) + random.uniform(0, 0.5))
            logger.warning(f"LLM call transient error (attempt {attempt+1}/{retries}). Sleeping {sleep_for:.2f}s. Error: {e}")
            try:
                await asyncio.sleep(sleep_for)
            except Exception:
                pass
            attempt += 1


# ---------------------------
# LLM router for intelligent routing decisions
# ---------------------------
async def llm_decide_routing(
    model: AIModel,
    user_text: str,
    conversation_id: str = "",
    has_attachments: bool = False,
    attachment_names: list[str] | None = None,
    has_cached_attachments: bool = False,
    cached_attachment_names: list[str] | None = None,
    last_query: str = "",
    last_source_names: list[str] | None = None,
    recent_history: list[str] | None = None,
) -> dict:
    """Route user requests through the dedicated smart_router module.

    This keeps app.py focused on Teams orchestration while smart_router.py owns
    the LLM-based decision policy. The router uses deterministic hard guards for
    casual/social messages and LLM reasoning for real work requests.
    """
    return await smart_decide_route(
        model=model,
        user_text=user_text or "",
        chat_prompt_cls=ChatPrompt,
        llm_semaphore=llm_semaphore,
        call_with_retry=call_llm_with_retry,
        config=Config,
        logger=logger,
        conversation_id=conversation_id,
        has_attachments=has_attachments,
        attachment_names=attachment_names or [],
        has_cached_attachments=has_cached_attachments,
        cached_attachment_names=cached_attachment_names or [],
        last_query=last_query or "",
        last_source_names=last_source_names or [],
        recent_history=recent_history or [],
    )

# ---------------------------
# Typing indicator helper
# ---------------------------
def _ctx_is_group(ctx: ActivityContext[MessageActivity]) -> bool:
    """Best-effort detection of group/channel conversations (which can't stream)."""
    try:
        conv = getattr(ctx.activity, "conversation", None)
        conv_type = getattr(conv, "conversation_type", None)
        conv_id = getattr(conv, "id", "") or ""
        if conv_type in ("groupChat", "channel"):
            return True
        if "@unq.gbl.spaces" in conv_id or "group" in conv_id.lower():
            return True
    except Exception:
        pass
    return False


async def send_typing_indicator(ctx: ActivityContext[MessageActivity], status: str = "Working on it...") -> None:
    """Show the bot is processing the request.

    For one-on-one chats we use the SDK-native streaming *informative* update
    (``ctx.stream.update``), which Teams renders reliably as an animated
    "Working on..." status. Standalone typing activities are unreliable in
    streaming personal chats, so they are only used as a fallback for group
    chats / channels (which cannot stream). Silently ignores 403/405 errors
    (conversation context closed or streaming unsupported)."""
    try:
        stream = getattr(ctx, "stream", None)
        if stream is not None and not _ctx_is_group(ctx):
            # Informative updates are dropped once real text starts streaming,
            # so this naturally gives way to the answer without leaving a blank.
            try:
                stream.update(status)
                logger.debug("Typing indicator sent (stream informative update)")
                return
            except Exception as e:
                logger.debug(f"Stream informative update failed, falling back to typing activity: {e}")
        # Fallback: standalone typing activity (group chats / no stream)
        await ctx.send(TypingActivityInput())
        logger.debug("Typing indicator sent (standalone activity)")
    except Exception as e:
        error_str = str(e).lower()
        # 403/405 are expected - conversation context closed or streaming unsupported
        if "403" in str(e) or "405" in str(e) or "forbidden" in error_str:
            logger.debug(f"Typing indicator skipped - conversation context closed/unsupported")
        else:
            logger.warning(f"Failed to send typing indicator: {e}")

async def deliver_final(
    ctx: ActivityContext[MessageActivity],
    text: str,
    *,
    is_group: bool = False,
    ai_generated: bool = True,
) -> None:
    """Deliver the assistant's FINAL message for a turn and finalize any open stream.

    In personal chats the bot shows progress via ``ctx.stream.update`` ("Working
    on it..."), which opens an *informative* stream. Teams keeps that status
    animating (with a Stop button) until a final stream message carrying text is
    sent — the SDK auto-closes the stream when the handler returns, but
    ``stream.close()`` is a no-op when no text was ever emitted. So answering an
    early-return path with a separate ``ctx.send`` bubble leaves the status stuck
    and Teams eventually renders "This response was stopped".

    Emitting the answer THROUGH the stream lets the SDK replace the status in
    place and close cleanly. Group chats (which cannot stream) and any stream
    failure fall back to a normal message."""
    msg = MessageActivityInput(text=text)
    if ai_generated:
        msg = msg.add_ai_generated()
    stream = getattr(ctx, "stream", None)
    if stream is not None and not is_group:
        try:
            stream.emit(msg)
            return
        except Exception as e:
            logger.debug(f"deliver_final: stream.emit failed, falling back to ctx.send: {e}")
    await ctx.send(msg)

async def send_typing_with_status(ctx: ActivityContext[MessageActivity], status: str) -> Optional[str]:
    """Send typing indicator with a brief status message for long operations.
    Returns the activity ID of the status message so it can be deleted later."""
    try:
        # Send typing indicator first
        typing_activity = TypingActivityInput()
        await ctx.send(typing_activity)
        
        # Send brief status update
        import asyncio
        await asyncio.sleep(0.1)  # Brief delay to ensure typing shows first
        
        status_activity = MessageActivityInput(
            text=f"ðŸ”„ {status}",
            type="message"
        )
        response = await ctx.send(status_activity)
        activity_id = response.id if response else None
        logger.info(f"Typing indicator with status sent: {status} (activity_id={activity_id})")
        return activity_id
    except Exception as e:
        logger.warning(f"Failed to send typing indicator with status: {e}")
        # Fallback to regular typing indicator
        await send_typing_indicator(ctx)
        return None

class TypingIndicatorManager:
    """Manages periodic typing indicators during long operations to prevent timeout."""
    
    def __init__(self, ctx: ActivityContext[MessageActivity], status: str = "Working on it..."):
        self.ctx = ctx
        self.refresh_task = None
        self.should_refresh = False
        self.status = status

    def set_status(self, status: str):
        """Update the status text shown in the informative typing indicator."""
        self.status = status
    
    async def start_periodic_refresh(self, interval: float = 2.0):
        """Start sending typing indicators every `interval` seconds (default 2s for consistency).
        Teams shows typing for ~10-15s, so 2s interval ensures continuous visibility."""
        self.should_refresh = True
        # Send initial typing indicator immediately
        try:
            await send_typing_indicator(self.ctx, self.status)
        except Exception as e:
            logger.debug(f"Initial typing indicator failed: {e}")
        
        self.refresh_task = asyncio.create_task(self._refresh_loop(interval))
        logger.info(f"ðŸ”„ Started persistent typing indicator (every {interval}s until response ready)")
    
    async def stop_refresh(self):
        """Stop the periodic refresh."""
        self.should_refresh = False
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
            try:
                await self.refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped periodic typing indicator refresh")
    
    async def _refresh_loop(self, interval: float):
        """Internal loop that sends typing indicators periodically.
        Silently stops if conversation context becomes invalid (403 Forbidden)."""
        consecutive_errors = 0
        max_consecutive_errors = 3
        
        try:
            while self.should_refresh:
                await asyncio.sleep(interval)
                if self.should_refresh:  # Check again after sleep
                    try:
                        await send_typing_indicator(self.ctx, self.status)
                        consecutive_errors = 0  # Reset on successful send
                    except Exception as e:
                        error_str = str(e).lower()
                        # If we get 403, the conversation is closed - stop sending
                        if "403" in str(e) or "forbidden" in error_str:
                            logger.debug("Conversation context closed (403) - stopping typing indicators")
                            self.should_refresh = False
                            break
                        else:
                            consecutive_errors += 1
                            logger.debug(f"Typing indicator send failed ({consecutive_errors}/{max_consecutive_errors}): {e}")
                            # Stop if we have too many consecutive errors
                            if consecutive_errors >= max_consecutive_errors:
                                logger.warning(f"Stopping typing indicators after {max_consecutive_errors} consecutive errors")
                                self.should_refresh = False
                                break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Error in typing indicator refresh loop: {e}")
    
    async def __aenter__(self):
        """Context manager entry."""
        await self.start_periodic_refresh()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.stop_refresh()

async def send_typing_with_message(ctx: ActivityContext[MessageActivity], message: str) -> None:
    """Send typing indicator with a status message for long operations."""
    try:
        # Send typing indicator
        typing_activity = TypingActivityInput()
        await ctx.send(typing_activity)
        
        # Send status message
        status_activity = MessageActivityInput(
            text=f"ðŸ”„ {message}",
            type="message"
        )
        await ctx.send(status_activity)
        logger.info(f"Typing indicator with message sent: {message}")
    except Exception as e:
        logger.warning(f"Failed to send typing indicator with message: {e}")

async def process_attachments_with_typing(ctx: ActivityContext[MessageActivity], attachments: list, conversation_id: str, cache_user_id: str, raw_sink: dict | None = None) -> tuple:
    """Process attachments with periodic typing indicators to prevent timeout."""
    MAX_ATTACHMENTS = len(attachments)
    parts = []
    extracted_for_aggregation = []  # For multi-file comparison
    
    if len(attachments) > MAX_ATTACHMENTS:
        logger.info(f"Attachment limit exceeded ({len(attachments)}). Processing first {MAX_ATTACHMENTS} only.")
        await send_typing_indicator(ctx)
    
    total_attachments = min(len(attachments), MAX_ATTACHMENTS)
    
    for i, att in enumerate(attachments[:MAX_ATTACHMENTS], 1):
        att_name = getattr(att, "name", "unknown")
        
        # Send typing indicator before each attachment to keep connection alive
        await send_typing_indicator(ctx)
        
        # Validate file before processing
        is_valid, validation_error = validate_file_attachment(att)
        if not is_valid:
            parts.append(validation_error)
            logger.warning(f"File validation failed for '{att_name}': {validation_error}")
            continue
        
        # Process the attachment with periodic typing indicators for large files
        logger.info(f"Processing attachment {i}/{total_attachments}: {att_name}")
        
        # ALWAYS use async to prevent blocking - wrap in typing manager for all files
        try:
            async with TypingIndicatorManager(ctx):
                file_content = await asyncio.to_thread(process_attachment, att, conversation_id, cache_user_id, raw_sink)
        except MemoryError:
            logger.error(f"Memory error processing '{att_name}' - file too large")
            error_msg = f"""âŒ **Memory Error**: {att_name}

âš ï¸ File is too large to process in available memory.

**Solutions:**
â€¢ Upload a smaller file (< 50 MB recommended)
â€¢ Split large files into sections
â€¢ Use compressed formats
â€¢ Share specific pages/sections instead"""
            parts.append(error_msg)
            continue
        except Exception as proc_err:
            logger.error(f"Error processing '{att_name}': {proc_err}", exc_info=True)
            parts.append(f"âŒ Error processing {att_name}: {str(proc_err)[:200]}")
            continue
        
        # Send another typing indicator after processing (before caching)
        if i < total_attachments:
            await send_typing_indicator(ctx)

        if file_content:
            if file_content.startswith("âŒ"):
                # Surface the failure to the LLM so it doesn't say "no attachment".
                parts.append(file_content)
                continue

            # Success path: cache content to disk for follow-up questions
            # IMPORTANT: Full content is cached without truncation (up to 10M chars / ~50MB)
            # This ensures follow-up questions can access ALL data, including "lower values" in large files
            
            # Include FULL content for LLM - user wants complete extraction
            parts.append(file_content)
            
            # Keep full content for aggregation processing
            extracted_for_aggregation.append((att_name, file_content))
            
            # Cache attachment to disk for follow-up questions with FULL content preserved
            # PERFORMANCE: Run in thread pool to avoid blocking
            # SECURITY: Include user ID for proper isolation
            if cache_user_id:
                try:
                    await asyncio.to_thread(
                        cache_attachment,
                        conversation_id,
                        att_name,
                        file_content,
                        cache_user_id  # User ID for security
                    )
                    logger.info(f"Attachment '{att_name}' processed and cached - {len(file_content):,} chars (FULL content preserved)")
                except Exception as cache_err:
                    logger.warning(f"Failed to cache attachment '{att_name}': {cache_err}")
                    logger.info(f"Attachment '{att_name}' processed (cache failed) - {len(file_content):,} chars")
            else:
                logger.warning(f"Attachment '{att_name}' not cached: no stable user id")
        else:
            # No content returned; provide mobile-friendly guidance
            mobile_guidance = f"""âŒ Unable to read attachment '{att_name}'.

**If using Teams mobile app:**
â€¢ **Wait 30-60 seconds** after selecting files before sending
â€¢ Use the **paperclip button** (not drag-and-drop)
â€¢ Try **desktop/web Teams** for more reliable file uploads
â€¢ Ensure **strong network connection**

â€¢ Make sure file has proper extension (.pdf, .docx, etc.)"""
            
            parts.append(mobile_guidance)
    
    return parts, extracted_for_aggregation

# ---------------------------
# Main handler
# ---------------------------
async def handle_stateful_conversation(model: AIModel, ctx: ActivityContext[MessageActivity]) -> None:
    conversation_id = ctx.activity.conversation.id
    user_text = (ctx.activity.text or "").strip()
    
    # STABILIZATION: Guard against duplicate LLM calls with TTL auto-expiry.
    # Uses a dict {conversation_id: timestamp} instead of a set so that
    # stuck requests auto-expire after GUARD_TTL_SECONDS and never
    # permanently block a conversation.
    GUARD_TTL_SECONDS = 90  # Max time a single request can hold the lock
    if not hasattr(handle_stateful_conversation, 'active_llm_calls'):
        handle_stateful_conversation.active_llm_calls: dict[str, float] = {}
    
    import time as _time
    _now = _time.time()
    # Auto-expire any guard older than TTL (prevents permanent blocking)
    expired = [
        cid for cid, ts in handle_stateful_conversation.active_llm_calls.items()
        if _now - ts > GUARD_TTL_SECONDS
    ]
    for cid in expired:
        logger.warning(f"â° Guard TTL expired for conversation {cid[:20]} â€” auto-clearing after {GUARD_TTL_SECONDS}s")
        handle_stateful_conversation.active_llm_calls.pop(cid, None)
    
    if conversation_id in handle_stateful_conversation.active_llm_calls:
        elapsed = _now - handle_stateful_conversation.active_llm_calls[conversation_id]
        logger.warning(f"âš ï¸ DUPLICATE LLM CALL GUARD: Conversation {conversation_id[:20]} already processing ({elapsed:.0f}s ago) - skipping")
        return
    
    handle_stateful_conversation.active_llm_calls[conversation_id] = _now
    # One persistent typing indicator for the whole turn. Started early (during
    # search) so the bot never looks "blank" while a slow query runs, and always
    # stopped here in finally so no path can leak the background refresh task.
    typing_mgr = TypingIndicatorManager(ctx)
    try:  # STABILIZATION FIX 6: Wrap entire handler in try/finally to always clear the guard
        await _handle_stateful_conversation_inner(model, ctx, conversation_id, typing_mgr)
    finally:
        try:
            await typing_mgr.stop_refresh()
        except Exception:
            pass
        handle_stateful_conversation.active_llm_calls.pop(conversation_id, None)
        logger.info(f"ðŸ”“ LLM call guard cleared for conversation {conversation_id[:20]}")


# Keywords that hint the user wants computation, data manipulation, charting,
# or document generation — i.e. the code interpreter should be made available.
_INTERPRETER_INTENT_RE = re.compile(
    r"("
    r"calculat|comput|average|median|how many|"
    r"chart|graph|plot|visuali[sz]|diagram|histogram|pie chart|"
    r"generat|create|make me|build me|produc|export|convert|turn (this|it) into|"
    r"download|"
    r"excel|spreadsheet|xlsx|csv|docx|word doc|powerpoint|pptx|slide|"
    r"presentation|\bpdf\b|\breport\b|\bzip\b|"
    r"summari[sz]e|compare|comparison|forecast|pivot|"
    r"deduplicat|reformat"
    r")",
    re.IGNORECASE,
)


def _should_enable_interpreter(user_text: str, has_raw_files: bool) -> bool:
    """Decide whether to expose the code-interpreter tool for this turn.

    Enabled when raw file bytes are available (so the model can manipulate them)
    or when the user's text expresses compute/visualization/generation intent.
    """
    if not _INTERPRETER_AVAILABLE:
        return False
    if has_raw_files:
        return True
    return bool(_INTERPRETER_INTENT_RE.search(user_text or ""))


async def _handle_stateful_conversation_inner(model: AIModel, ctx: ActivityContext[MessageActivity], conversation_id: str, typing_mgr: "TypingIndicatorManager") -> None:
    """Inner implementation of handle_stateful_conversation (wrapped in try/finally by caller)."""
    user_text = (ctx.activity.text or "").strip()
    attachments_raw = ctx.activity.attachments or []
    status_activity_ids = []  # Track status messages to delete after final response

    # ENHANCED LOGGING: Detect conversation type for group chat debugging
    conversation_type = getattr(ctx.activity.conversation, 'conversation_type', 'unknown')
    is_group = conversation_type in ['groupChat', 'channel'] or 'group' in conversation_id.lower()
    
    logger.info(f"ðŸ” CONVERSATION DEBUG - ID: {conversation_id[:20]}... Type: {conversation_type} IsGroup: {is_group}")
    
    # Log raw attachments BEFORE filtering
    logger.info(f"Raw attachments received: {len(attachments_raw)}")
    for idx, raw_att in enumerate(attachments_raw, 1):
        logger.info(f"  Raw attachment {idx}: type={type(raw_att).__name__}")

    # DEEP DIAGNOSTIC: dump the full inbound activity so we can see exactly how
    # Teams delivers OneDrive/SharePoint "cloud" file attachments (which arrive
    # differently from direct uploads). Helps locate the real file reference.
    try:
        _diag = {
            "text": (user_text or "")[:200],
            "attachments": [],
            "entities": None,
            "channel_data_keys": None,
            "value": None,
        }
        for raw_att in attachments_raw:
            _c = getattr(raw_att, "content", None)
            _diag["attachments"].append({
                "content_type": getattr(raw_att, "content_type", None) or getattr(raw_att, "contentType", None),
                "content_url": getattr(raw_att, "content_url", None) or getattr(raw_att, "contentUrl", None),
                "name": getattr(raw_att, "name", None),
                "content_type_of_content": type(_c).__name__,
                "content_preview": (str(_c)[:400] if _c is not None else None),
            })
        _ents = getattr(ctx.activity, "entities", None)
        if _ents:
            try:
                _diag["entities"] = [
                    (getattr(e, "type", None) or (e.get("type") if isinstance(e, dict) else None), str(e)[:300])
                    for e in _ents
                ]
            except Exception:
                _diag["entities"] = str(_ents)[:600]
        _cd = getattr(ctx.activity, "channel_data", None)
        if _cd is not None:
            try:
                if isinstance(_cd, dict):
                    _diag["channel_data"] = json.dumps(_cd, default=str)[:600]
                elif hasattr(_cd, "model_dump"):
                    _diag["channel_data"] = json.dumps(_cd.model_dump(), default=str)[:600]
                else:
                    _diag["channel_data"] = str(_cd)[:600]
            except Exception:
                _diag["channel_data"] = str(_cd)[:600]
        _val = getattr(ctx.activity, "value", None)
        if _val is not None:
            _diag["value"] = str(_val)[:400]
        logger.info(f"ðŸ§ª ACTIVITY DIAGNOSTIC: {json.dumps(_diag, default=str)[:2500]}")
    except Exception as _diag_err:
        logger.warning(f"Activity diagnostic logging failed: {_diag_err}")
    
    attachments = [a for a in attachments_raw if is_file_attachment(a)]
    web_results = []  # Initialize to avoid NameError if search is skipped

    # Resolve any SharePoint/OneDrive links pasted in the message into real
    # attachments. Cloud-picker files are frequently NOT delivered to bots as file
    # attachments, so without this the user's shared document would be invisible.
    if user_text and _CLOUD_FILE_HOST_RE.search(user_text):
        try:
            cloud_atts = await asyncio.to_thread(
                _resolve_cloud_file_attachments, user_text
            )
        except Exception as _cf_err:
            cloud_atts = []
            logger.debug(f"Cloud file URL resolution failed: {_cf_err}")
        if cloud_atts:
            attachments = attachments + cloud_atts
            logger.info(
                f"Added {len(cloud_atts)} resolved cloud file attachment(s) from message links"
            )

    logger.info(
        f"User: '{user_text[:60]}...' | Attachments: {len(attachments)} (raw: {len(attachments_raw)})"
    )

    # EARLY DETECTION: Handle empty messages (no text, no valid attachments)
    if not user_text and not attachments:
        # Check if attachments were sent but rejected
        if attachments_raw:
            logger.info(f"Attachments detected ({len(attachments_raw)}) but none were valid - sending guidance")
            await ctx.send(
                MessageActivityInput(
                    text="ðŸ¤” I detected an attachment, but couldn't recognize it as a file.\n\n"
                         "**This usually happens because:**\n"
                         "â€¢ Teams sent a link preview or rich message embed (not a file)\n"
                         "â€¢ Mobile file upload hasn't finished yet\n"
                         "â€¢ File metadata is missing\n\n"
                         "**To fix this:**\n"
                         "1ï¸âƒ£ **Wait 10-30 seconds** after selecting your file, then send\n"
                         "2ï¸âƒ£ **Use the paperclip button** (ðŸ“Ž) to attach files\n"
                         "3ï¸âƒ£ **Try desktop or web Teams** for best results\n"
                         "4ï¸âƒ£ **Make sure it's a supported file**: PDF, Word, Excel, PowerPoint, CSV, TXT\n\n"
                         "ðŸ’¡ *Tip: You can also just ask me a question or search your documents without attachments!*"
                ).add_ai_generated()
            )
        else:
            logger.info("Empty message detected (no text, no attachments) - sending clarification")
            await ctx.send(
                MessageActivityInput(
                    text="ðŸ‘‹ Hi! I'm here to help. You can:\n\n"
                         "ðŸ“Ž **Upload documents** (PDF, Word, Excel, PowerPoint, CSV)\n"
                         "ðŸ’¬ **Ask questions** about your files or information\n"
                         "ðŸ” **Search** configured SharePoint library documents\n\n"
                         "ðŸ’¡ *Tip: If you tried to upload a file from mobile, wait 10-30 seconds after selecting it before sending your message, or use the desktop/web app for best results.*"
                ).add_ai_generated()
            )
        return

    # FAST PATH: greetings, thanks, emotional/social messages, and acknowledgements.
    # These are not document questions, so do not route, search, or inject previous sources.
    if user_text and not attachments and is_small_talk(user_text):
        logger.info(f"ðŸ’¬ Small-talk bypass: '{user_text[:60]}'")
        await deliver_final(ctx, small_talk_response(user_text), is_group=is_group)
        return

    # Send typing indicator IMMEDIATELY to show the bot is processing
    # Start the PERSISTENT typing indicator now and keep it visible through the
    # whole turn (search + LLM). A single typing activity expires after a few
    # seconds in Teams, which made the bot look "blank" during slow searches.
    typing_mgr.set_status("Reading your message...")
    await typing_mgr.start_periodic_refresh(interval=3.0)

    # GROUP CHAT PERMISSIONS CHECK: Verify bot can send messages in this context
    if is_group:
        try:
            # Test if we can send a message by sending typing indicator
            await send_typing_indicator(ctx)
            logger.info(f"âœ… Group chat permissions verified - can send activities")
        except Exception as perm_error:
            logger.error(f"âŒ GROUP CHAT PERMISSION ISSUE: {perm_error}")
            try:
                await ctx.send(MessageActivityInput(text="âš ï¸ I seem to have permission issues in this group chat. Please try:\n\n1ï¸âƒ£ Remove me from the group and add me back\n2ï¸âƒ£ Check if I have permission to send messages\n3ï¸âƒ£ Try mentioning me with @").add_ai_generated())
            except:
                pass  # If we can't even send this error message, there's a deeper permission issue
            return

    # OPTIMIZATION: Check for cached attachments BEFORE routing
    # This avoids expensive Graph/AI searches when follow-up questions are about uploaded files
    has_cached_attachments = False
    cached_attachment_filenames = []
    try:
        # âœ… SIMPLIFIED: No complex user ID tracking needed
        user_id = (
            getattr(ctx.activity.from_, "aadObjectId", None)
            or getattr(ctx.activity.from_, "aad_object_id", None)
            or getattr(ctx.activity.from_, "id", None)
            or f"fallback-{conversation_id[:8]}"
        )
        # Wrap attachment check in asyncio.to_thread() to prevent blocking
        cached_attachments_check = await asyncio.wait_for(
            asyncio.to_thread(
                get_conversation_attachments,
                conversation_id,
                False,
                user_id
            ),
            timeout=Config.ATTACHMENT_CHECK_TIMEOUT
        )
        if cached_attachments_check:
            has_cached_attachments = True
            cached_attachment_filenames = [f.get("filename") or f.get("name", "unknown") for f in cached_attachments_check]
            logger.info(f"Found {len(cached_attachments_check)} cached attachment(s) in conversation: {', '.join(cached_attachment_filenames)}")
    except asyncio.TimeoutError:
        logger.debug("Cached attachment check TIMED OUT - skipping")
    except Exception:
        pass

    # ── ATTACHMENT RECOVERY ────────────────────────────────────────────────
    # Teams often does NOT deliver OneDrive/SharePoint "cloud" files to bots
    # inline (especially in 1:1 chats — only the typed text arrives). Before we
    # give up, try every other channel to recover the file via Microsoft Graph:
    # links in the text/attachment body, the real group/channel message, or a
    # file the user named. Recovered files become normal attachments so the rest
    # of the pipeline (download → Document Intelligence/pypdf → answer) is
    # unchanged. Only runs when the user seems to want a document but none
    # arrived — so plain chat never pays for a Graph call.
    if (
        not attachments
        and (refers_to_attached_document(user_text) or attachment_resolver.extract_filename_query(user_text))
    ):
        try:
            _chatter_id = (
                getattr(ctx.activity.from_, "aad_object_id", None)
                or getattr(ctx.activity.from_, "aadObjectId", None)
                or ""
            )
            recovered = await asyncio.wait_for(
                asyncio.to_thread(
                    attachment_resolver.resolve_extra_attachments,
                    user_text=user_text or "",
                    attachments_raw=attachments_raw,
                    conversation_id=conversation_id,
                    is_group=is_group,
                    chatter_aad_id=str(_chatter_id),
                    message_id=str(getattr(ctx.activity, "id", "") or ""),
                ),
                timeout=20,
            )
        except Exception as _rec_err:
            recovered = []
            logger.info(f"Attachment recovery skipped/failed: {_rec_err}")
        if recovered:
            attachments = attachments + recovered
            logger.info(
                f"✅ Recovered {len(recovered)} attachment(s) via Graph fallback: "
                f"{[getattr(a, 'name', '?') for a in recovered]}"
            )

    # ── ANTI-HALLUCINATION GUARD ───────────────────────────────────────────
    # If the user refers to a document they just attached ("summarize this
    # document") but no file actually reached the bot this turn (a OneDrive/
    # SharePoint "cloud" attachment is frequently NOT delivered to bots), DO NOT
    # fall through to a generic SharePoint/AI Search summary — that produces a
    # confident summary of an unrelated indexed document (a hallucination).
    #
    # IMPORTANT: We fire this guard even when there IS a cached attachment from
    # earlier in the conversation, because a stale/unrelated cached file (e.g. a
    # leftover "build localhost.pdf") must NOT be silently summarized as if it
    # were the file the user just attached. Instead we tell the user the file
    # didn't arrive and, if a cached file exists, let them request it BY NAME
    # (an explicit filename request is not deictic, so it bypasses this guard).
    if (
        refers_to_attached_document(user_text)
        and not attachments
    ):
        logger.warning(
            "🛑 ANTI-HALLUCINATION: user referenced an attached document but no file "
            "reached the bot this turn — refusing to summarize unrelated search/cache "
            "results. has_cached_attachments=%s cached=%s",
            has_cached_attachments,
            cached_attachment_filenames,
        )
        await typing_mgr.stop_refresh()
        if has_cached_attachments and cached_attachment_filenames:
            _names = "\n".join(f"• **{n}**" for n in cached_attachment_filenames[:5])
            _msg = (
                "I don't see a file attached to this message, so I won't guess at its "
                "contents.\n\n"
                "Teams often attaches files **from OneDrive/SharePoint as a cloud "
                "link**, which it doesn't always pass to me.\n\n"
                "**To get an accurate result, do one of these:**\n"
                "1️⃣ Use the **paperclip → Upload from this device** so the actual file "
                "is sent (not a link), then resend your request.\n"
                f"2️⃣ Or, if you meant a file you already shared, ask for it **by name**:\n{_names}\n"
                "   (e.g. \"summarize " + cached_attachment_filenames[0] + "\")"
            )
        else:
            _msg = (
                "I can see you wanted me to work with a document, but Teams didn't "
                "pass the file to me — so I won't guess at its contents.\n\n"
                "This happens in 1:1 chats when a file is attached **from OneDrive/"
                "SharePoint as a cloud link**, which Teams doesn't deliver to bots.\n\n"
                "**Any of these will work:**\n"
                "1️⃣ **Tell me the file name** (e.g. \"summarize Edgar Offer "
                "Letter\") — I'll find it in your OneDrive or our SharePoint and read it.\n"
                "2️⃣ Use the **paperclip → Upload from this device** so the actual "
                "file is sent.\n"
                "3️⃣ Or **paste the file's link** in your message."
            )
        await deliver_final(ctx, _msg, is_group=is_group)
        return

    # Add simple conversation reset functionality
    if user_text and user_text.lower().strip() in ["reset", "clear conversation", "debug conversation"]:
        if user_text.lower().strip() in ["reset", "clear conversation"]:
            cleared = clear_conversation_memory(conversation_id)
            await ctx.send(
                MessageActivityInput(
                    text=f"ðŸ”§ **Conversation Reset**\n\n"
                         f"ðŸ†• Starting fresh conversation.\n"
                         f"**Status:** {'âœ… Memory cleared' if cleared else 'âŒ Nothing to clear'}\n\n"
                         f"ðŸ’¬ Previous messages have been cleared. How can I help you?"
                ).add_ai_generated()
            )
            return
        else:
            # Debug info
            memory = get_or_create_conversation_memory(conversation_id)
            msg_count = len(_get_memory_items(memory))
            summary_state = conversation_summaries.get(conversation_id, {}) or {}
            summary_chars = len(summary_state.get("summary", "") or "")
            await ctx.send(
                MessageActivityInput(
                    text=f"ðŸ”§ **Conversation Debug**\n\n"
                         f"**Conversation ID:** `{conversation_id[:20]}...`\n"
                         f"**Messages in memory:** {msg_count}\n\n"
                         f"**Running summary:** {summary_chars} chars across {summary_state.get('turn_count', 0)} summarized turns\n\n"
                         f"Type `reset` to clear this conversation."
                ).add_ai_generated()
            )
            return
    # LLM router: decide routing and extract action, route, etc.
    # Keep connection alive during ALL processing (routing, token extraction, search, response)
    # Wrap conversation history lookups in asyncio.to_thread()
    try:
        _prev_query = await asyncio.wait_for(
            asyncio.to_thread(last_query_for, conversation_id),
            timeout=Config.CONVERSATION_HISTORY_TIMEOUT
        )
    except (asyncio.TimeoutError, Exception):
        _prev_query = None
    
    _prev_sources = conversation_last_sources.get(conversation_id, [])
    _prev_source_names = [s.get("title") or s.get("name") or "" for s in _prev_sources]
    
    # Gather recent conversation history for the LLM router (last 3 turns)
    _recent_history: list[str] = []
    try:
        mem = get_or_create_conversation_memory(conversation_id)
        # Access internal storage directly (ListMemory._storage._items)
        raw_items = getattr(getattr(mem, '_storage', None), '_items', None) or []
        for m in raw_items[-6:]:
            role = getattr(m, 'role', 'user') or 'user'
            content = getattr(m, 'content', '') or ''
            if content:
                _recent_history.append(f"{role}: {content[:200]}")
        _running_summary_for_router = get_conversation_summary_text(conversation_id)
        if _running_summary_for_router:
            _recent_history.insert(0, _running_summary_for_router[:800])
    except Exception:
        pass

    _conversation_source_summary = ""
    try:
        _summary_parts: list[str] = []
        _running_summary = get_conversation_summary_text(conversation_id)
        if _running_summary:
            _summary_parts.append(_running_summary)
        if _prev_query:
            _summary_parts.append(f"Previous search query: {_prev_query}")
        if _prev_source_names:
            _summary_parts.append("Previous source documents: " + ", ".join(_prev_source_names[:5]))
        if _recent_history:
            _summary_parts.append("Recent conversation:\n" + "\n".join(_recent_history[-4:]))
        _conversation_source_summary = "\n".join(_summary_parts)
    except Exception:
        _conversation_source_summary = ""
    
    # â”€â”€ Fast pre-check: bot self-knowledge questions (skip LLM â€” respond_direct per bot instructions) â”€â”€
    # These match the 'respond_direct' cases defined in the router: questions about the bot ITSELF,
    # not about external topics, documents, or organizational content.
    _user_text_lower = user_text.lower().strip().rstrip("?!.")
    def _is_org_or_document_request(text: str) -> bool:
        """Default to organizational/document retrieval for any substantive turn.

        For a SharePoint-backed assistant the safe default is to search: answering
        organizational questions from the model's general knowledge produces empty,
        unsourced replies. We therefore SEARCH unless the message is confirmed small
        talk, a greeting/acknowledgement, or a trivially short non-question input.

        Follow-ups ("tell me more about it", "summarize that") are intentionally NOT
        skipped here — they are handled by the previous-document / refine gates in the
        routing tree below, which run before the search branch.
        """
        t = (text or "").strip().lower()
        if not t:
            return False
        if is_small_talk(t) or is_smalltalk(t):
            return False
        # Greetings / acknowledgements / emoji-only / ultra-short inputs never search.
        small_talk_only = (
            r"^(hi|hello|hey|yo|hiya|good\s?(morning|afternoon|evening|day)|"
            r"thanks?|thank you|thx|ty|cheers|bye|goodbye|see ya|"
            r"ok|okay|kk|sure|fine|cool|great|nice|yes|yeah|yep|yup|no|nope|nah|"
            r"lol|haha|hehe|hmm|oh|ah)[\s!.?]*$"
        )
        if re.match(small_talk_only, t, re.IGNORECASE):
            return False
        if len(t) <= 2:  # stray single chars / lone emoji
            return False
        return True  # default: substantive input searches the index

    def _is_previous_document_followup(text: str) -> bool:
        """Detect follow-ups that should reuse the last document context."""
        t = text.lower().strip()
        if not t or not (_prev_sources or _recent_history):
            return False
        if is_small_talk(t):
            return False

        explicit_new_search = (
            "search sharepoint", "search again", "find another", "find other",
            "look up", "look in sharepoint", "new document", "different document",
            "another document", "other documents", "new search", "search for",
            "find a document", "find document", "retrieve",
        )
        if any(p in t for p in explicit_new_search):
            return False

        list_style_followups = (
            "just list", "list the names", "list names", "show the names",
            "employees names", "employee names", "people names",
            "nicely", "bullet list", "bullets", "make it a list",
            "format it as a list", "put it in a list", "list them",
        )
        if any(p in t for p in list_style_followups):
            return True

        improvement_followups = (
            "what do you suggest", "what should i add", "what can i add",
            "what would you add", "suggest i add", "suggestions",
            "recommend", "recommendations", "improve it", "improve this",
            "improve the document", "what is missing", "what's missing",
            "missing from it", "anything missing", "gaps", "add to the document",
            "add to it", "make it better", "how can i improve", "what else should",
            "based on this", "based on that", "based on the document",
            "from this document", "for this document", "about this document",
            "the handbook", "this handbook", "that handbook",
            "its contact", "their contact", "contact details", "phone number",
            "email address", "website", "address", "contact info",
        )
        if any(p in t for p in improvement_followups):
            return True

        followup_starters = (
            "what about", "how about", "and what", "also", "then", "now",
            "i mean", "i meant", "i'm referring", "im referring",
            "can you also", "can you explain", "can you expand", "can you give",
            "give me more", "more details", "tell me more", "expand on",
            "continue", "go on", "does it", "do they", "is there", "are there",
            "where does it", "why does it", "how does it",
        )
        if any(t.startswith(p) for p in followup_starters):
            return True

        # Short, context-dependent questions after a sourced answer usually refer
        # to the previous result. Keep them local unless the user asks for a new search.
        if _prev_sources and len(t.split()) <= 8 and re.search(r"\b(what|which|who|where|when|why|how|does|do|is|are|can|should)\b", t):
            return True

        return bool(re.search(r"\b(it|its|their|this|that|above|previous|the document|the file|contact|phone|email|website)\b", t))

    _needs_org_search = _is_org_or_document_request(user_text)
    _force_respond_direct = (
        is_small_talk(user_text)
        or is_personal_advice_request(user_text)
        or is_bot_self_question(_user_text_lower)
        or (is_general_knowledge_question(user_text) and not _needs_org_search)
    )

    _refine_phrases = (
        "make it shorter", "shorter", "summarize that", "bullet points",
        "add more detail", "expand on that", "rephrase", "rewrite that",
    )
    _looks_like_refine = any(p in _user_text_lower for p in _refine_phrases)
    _looks_like_previous_doc_followup = _is_previous_document_followup(user_text)

    # Route decision — deterministic tree extracted to routing/message_router.classify_message;
    # falls back to the LLM router (llm_decide_routing) when no deterministic rule applies.
    route = classify_message(
        user_text,
        data_source_mode=Config.DATA_SOURCE_MODE,
        force_respond_direct=_force_respond_direct,
        looks_like_previous_doc_followup=_looks_like_previous_doc_followup,
        needs_org_search=_needs_org_search,
        has_attachments=bool(attachments),
        has_cached_attachments=has_cached_attachments,
        looks_like_refine=_looks_like_refine,
    )
    if route is None:
        route = await llm_decide_routing(
            model,
            user_text,
            conversation_id,
            has_attachments=bool(attachments),
            attachment_names=[getattr(a, "name", "unknown") for a in attachments],
            has_cached_attachments=has_cached_attachments,
            cached_attachment_names=cached_attachment_filenames,
            last_query=_prev_query,
            last_source_names=_prev_source_names,
            recent_history=_recent_history,
        )
    # Phase 7.1 intent (app-only safe; consumed by the parallel Graph path in Step 7).
    intent = classify_intent(user_text, route)
    # Final safety gate: casual/social messages must never call retrieval tools.
    if user_text and (is_small_talk(user_text) or is_personal_advice_request(user_text)):
        route = {"action": "respond_direct", "should_search": False, "search_query": "", "scope": "local"}

    # Final follow-up guard: previous-context questions must not trigger any
    # SharePoint/Graph/cache search. They should reuse prior sources/history.
    if _looks_like_previous_doc_followup and not attachments:
        route = {
            "action": "refine_previous",
            "should_search": False,
            "is_followup": True,
            "query": "",
            "scope": "local",
            "top_k": 3,
            "reason": "final guard: follow-up uses previous context without search",
        }
        logger.info(f"Follow-up guard forced refine_previous with no search: '{user_text[:80]}'")

    action = route.get("action", "respond_direct")
    if action == "refine_previous" and _prev_source_names:
        followup_note = (
            "Current user message is a follow-up. Reuse the previous source document(s) "
            "unless the user explicitly asks for a new search."
        )
        _conversation_source_summary = (
            f"{_conversation_source_summary}\n{followup_note}"
            if _conversation_source_summary else followup_note
        )
    search_attempted = False
    search_yielded_results = False
    
    # CRITICAL: When attachments are present with analysis intent, skip ALL searches
    # User uploaded files should be analyzed immediately, not searched for
    # External sources are searched only when the LLM routes to search.

    # Extract and remember user identity
    # Use 'from_' attribute (Python renames 'from' to 'from_' since 'from' is a reserved keyword)
    sender = getattr(ctx.activity, "from_", None)
    channel_data = getattr(ctx.activity, "channel_data", None)
    
    aad_id = None
    try:
        aad_id = (
            getattr(sender, "aadObjectId", None)
            or getattr(sender, "aad_object_id", None)
            or (getattr(channel_data, "aadObjectId", None) if channel_data else None)
            or (getattr(channel_data, "userObjectId", None) if channel_data else None)
            or getattr(sender, "id", None)
        )
    except Exception:
        aad_id = None

    # Using app-only Graph tokens only
    user_assertion = None
    _deferred_sign_in = None
    
    logger.info("Using app-only Graph tokens")
    logger.debug("ðŸ” About to extract user key")
    
    # Wrap UPN extraction in asyncio.to_thread() to prevent blocking
    try:
        extracted_upn_initial = await asyncio.wait_for(
            asyncio.to_thread(_extract_user_upn_from_activity, ctx),
            timeout=Config.USER_DETAILS_TIMEOUT
        ) or ""
    except (asyncio.TimeoutError, Exception):
        logger.debug("UPN extraction TIMED OUT or failed - using empty")
        extracted_upn_initial = ""

    # â”€â”€ ATTACHMENT ANALYSIS GATEKEEPER â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # If the LLM wanted to search, but we have new OR cached attachments, and it's an analysis request,
    # skip the external search. Requirement: "Newlyuploaded attachment must always be processed... and must not call graph or ai search at all."
    if action == "search_documents":
        user_key_for_cache = aad_id or extracted_upn_initial or conversation_id
        
        # Load cached attachment names to see if current query overlaps
        cached_fnames = []
        try:
            cached_files = await asyncio.to_thread(get_conversation_attachments, conversation_id, False, user_key_for_cache)
            cached_fnames = [f.get("name", "").lower() for f in cached_files]
        except Exception:
            pass

        analysis_keywords = [
            "calculate", "compute", "sum", "total", "analyze", "analyse",
            "summarize", "summarise", "review", "check", "what is in",
            "what's in", "tell me about", "explain", "describe",
            "compare", "comparison", "difference", "similar", "diff", "list",
            "parse", "extract", "read", "process"
        ]
        user_text_lower = user_text.lower()
        # Intent based on keywords OR if message is just attachments with no/brief text
        is_analysis_intent = any(keyword in user_text_lower for keyword in analysis_keywords) or len(user_text.split()) < 3
        
        # Also check if searching specifically for the filenames of what was just uploaded or cached
        filename_overlap = False
        if not is_analysis_intent:
            att_names = " ".join([getattr(a, "name", "").lower() for a in attachments] + cached_fnames)
            search_q = (route.get("query") or user_text).lower()
            if search_q:
                # If any significant query terms (length > 3) appear in attached filenames
                query_terms = [t for t in search_q.split() if len(t) > 3]
                if query_terms and any(t in att_names for t in query_terms):
                    filename_overlap = True
        
        if (attachments or cached_fnames) and (is_analysis_intent or filename_overlap):
            _src = "Newly uploaded" if attachments else "Cached"
            logger.info(f"ðŸŽ¯ ATTACHMENT ANALYSIS GATEKEEPER: Prioritizing {_src} file(s) - skipping external search")
            action = "respond_direct"
            route["action"] = "respond_direct"
            route["should_search"] = False
            if attachments:
                logger.info(f"   Attachment(s): {', '.join([getattr(a, 'name', 'Unknown') for a in attachments])}")
            if cached_fnames:
                logger.info(f"   Cached file(s): {', '.join(cached_fnames)}")

    # Update status after routing so user sees what phase we're entering
    typing_mgr.set_status("Searching your documents...")
    
    user_key = aad_id or extracted_upn_initial or conversation_id
    stable_lookup_key = aad_id or extracted_upn_initial
    
    logger.debug("ðŸ” About to load remembered user details")
    # Wrap disk I/O in asyncio.to_thread() to prevent blocking event loop
    remembered = {}
    if stable_lookup_key:
        try:
            remembered = await asyncio.wait_for(
                asyncio.to_thread(get_remembered_user_details, stable_lookup_key),
                timeout=Config.USER_DETAILS_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.debug("Remembered user details load TIMED OUT - skipping")
        except Exception as e:
            logger.debug(f"Error loading remembered user details: {e}")
    logger.debug("âœ… Remembered user details loaded")
    user_name = remembered.get("displayName") or ""
    user_email = remembered.get("mail") or remembered.get("userPrincipalName") or extracted_upn_initial or ""

    logger.debug("Document cache disabled for runtime SharePoint search")
    cache = None

    # Identity snapshot logging for diagnostics
    try:
        from_id = getattr(sender, "id", None)
        from_name = getattr(sender, "name", None)
        tenant_id = None
        t = getattr(channel_data, "tenant", None)
        if t:
            if isinstance(t, dict):
                tenant_id = t.get("id")
            else:
                tenant_id = getattr(t, "id", None)
        tenant_id = tenant_id or getattr(channel_data, "tenantId", None) or getattr(channel_data, "teamTenantId", None)
        channel_id = getattr(ctx.activity, "channel_id", None) or getattr(channel_data, "channelId", None)
        looks_guid = bool(aad_id and len(str(aad_id)) > 30 and '-' in str(aad_id))
        logger.info(
            f"Identity snapshot: from.id={from_id}, from.name={from_name}, aad_id={aad_id}, looks_guid={looks_guid}, "
            f"conversation={conversation_id}, channel={channel_id}, tenant={tenant_id}"
        )
    except Exception as e:
        logger.error(f"Error logging identity snapshot: {e}", exc_info=True)

    # Fallback to sender name if not already known
    if not user_name and sender:
        try:
            user_name = getattr(sender, "name", None) or ""
        except Exception:
            pass

    # Enrich user profile via Graph API if we have AAD id
    # Always try to resolve email â€” even when name is already known from cache
    # CRITICAL: Wrap Graph calls in asyncio.to_thread() to prevent blocking
    if aad_id and not user_email:
        try:
            logger.info("Profile lookup: token=app-only, endpoint=/users/{id}")
            
            # Wrap in asyncio.to_thread() + timeout to prevent blocking on Graph API
            try:
                profile_start = time.time()
                profile = await asyncio.wait_for(
                    asyncio.to_thread(get_cached_user_profile, aad_id, user_assertion),
                    timeout=Config.PROFILE_LOOKUP_TIMEOUT  # Configurable timeout for profile lookup
                ) or {}
                profile_elapsed = time.time() - profile_start
                if profile_elapsed > 0.5:
                    logger.info(f"â±ï¸  Profile lookup took {profile_elapsed:.2f}s")
            except asyncio.TimeoutError:
                logger.debug(f"Profile lookup TIMED OUT after {Config.PROFILE_LOOKUP_TIMEOUT}s - skipping")
                profile = {}
            
            if profile:
                user_name = profile.get("displayName") or user_name
                user_email = profile.get("mail") or profile.get("userPrincipalName") or user_email
                logger.info(f"Profile fetched: name={user_name}, email={user_email}")
            else:
                logger.debug(f"Profile lookup returned None/empty for aad_id={aad_id[:8]}...")
        except Exception as e:
            logger.debug(f"Error fetching profile: {type(e).__name__}")
        # Persist only Graph-derived profile (non-blocking)
        if user_name or user_email:
            await asyncio.to_thread(
                remember_user_details,
                user_key,
                {
                    "displayName": user_name,
                    "mail": user_email,
                    "userPrincipalName": user_email,
                    "aadObjectId": aad_id,
                }
            )
    else:
        # Fallback: if we have a sender.id that looks like a GUID, use it to fetch Graph profile
        try:
            from_id = getattr(sender, "id", None)
            if not aad_id and from_id and len(str(from_id)) > 30 and '-' in str(from_id) and not (user_name or user_email):
                logger.info("Profile lookup (fallback): using from.id as AAD object id for app-only /users/{id}")
                
                # Wrap in asyncio.to_thread() + timeout to prevent blocking
                try:
                    profile = await asyncio.wait_for(
                        asyncio.to_thread(get_cached_user_profile, str(from_id), user_assertion),
                        timeout=Config.PROFILE_LOOKUP_TIMEOUT
                    ) or {}
                except asyncio.TimeoutError:
                    logger.debug("Fallback profile lookup TIMED OUT - skipping")
                    profile = {}
                
                if profile:
                    user_name = profile.get("displayName") or user_name
                    user_email = profile.get("mail") or profile.get("userPrincipalName") or user_email
                    aad_id = str(from_id)
                    user_key = aad_id
                    cache_user_id = aad_id
                    logger.info(f"Fallback profile fetched: name={user_name}, email={user_email}")
                    await asyncio.to_thread(
                        remember_user_details,
                        user_key,
                        {
                            "displayName": user_name,
                            "mail": user_email,
                            "userPrincipalName": user_email,
                            "aadObjectId": aad_id,
                        }
                    )
                else:
                    logger.warning(f"Fallback profile lookup returned None for from.id={from_id[:8]}...")
        except Exception as e:
            logger.error(f"Error in fallback profile lookup: {e}", exc_info=True)
        # Safety: do NOT infer identity from cache unless explicitly enabled
        try:
            if cache and getattr(Config, "ALLOW_CACHE_USER_INFERENCE", False):
                users_map = (cache.cache or {}).get("users", {})
                user_ids = [uid for uid in users_map.keys() if uid]
                if not aad_id and len(user_ids) == 1 and not (user_name or user_email):
                    inferred_id = user_ids[0]
                    logger.info(f"Inferring user id from document cache: {inferred_id}")
                    # Wrap in asyncio.to_thread() to prevent blocking
                    try:
                        user_assertion = await asyncio.wait_for(
                            asyncio.to_thread(_extract_user_assertion_from_activity, ctx),
                            timeout=Config.USER_DETAILS_TIMEOUT
                        )
                    except (asyncio.TimeoutError, Exception):
                        user_assertion = None
                    
                    # Wrap in asyncio.to_thread() + timeout to prevent blocking
                    try:
                        prof = await asyncio.wait_for(
                            asyncio.to_thread(get_cached_user_profile, inferred_id, user_assertion),
                            timeout=Config.PROFILE_LOOKUP_TIMEOUT
                        ) or {}
                    except asyncio.TimeoutError:
                        logger.debug("Inferred profile lookup TIMED OUT - skipping")
                        prof = {}
                    
                    if prof:
                        user_name = prof.get("displayName") or user_name
                        user_email = prof.get("mail") or prof.get("userPrincipalName") or user_email
                        # Switch keys to inferred AAD id for consistency
                        aad_id = inferred_id
                        user_key = aad_id
                        cache_user_id = aad_id
                        await asyncio.to_thread(
                            remember_user_details,
                            user_key,
                            {
                                "displayName": user_name,
                                "mail": user_email,
                                "userPrincipalName": user_email,
                                "aadObjectId": aad_id,
                            }
                        )
        except Exception:
            pass

    # Timezone-aware current date/time
    tz_name = getattr(Config, "APP_TIMEZONE", "UTC")
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.utcnow()
    current_datetime = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    weekday_name = now.strftime("%A")
    date_friendly = now.strftime("%B %d, %Y")
    if not user_email:
        # Try to extract UPN/mail directly from activity if not yet found
        try:
            extracted_upn = await asyncio.wait_for(
                asyncio.to_thread(_extract_user_upn_from_activity, ctx),
                timeout=Config.USER_DETAILS_TIMEOUT
            ) or ""
        except (asyncio.TimeoutError, Exception):
            extracted_upn = ""
        if extracted_upn:
            user_email = extracted_upn
            await asyncio.to_thread(
                remember_user_details,
                user_key,
                {
                    "mail": user_email,
                    "userPrincipalName": user_email,
                    "aadObjectId": aad_id or user_key,
                }
            )
    # Final identity summary
    logger.info(f"Final user identity: name='{user_name or '(not set)'}', email='{user_email or '(not set)'}', aad_id={aad_id[:8] if aad_id else '(not set)'}...")
    
    # SECURITY: If user email is unavailable, warn about limited access
    if not user_email:
        logger.warning(f"ðŸ”’ SECURITY: User email unavailable - personal OneDrive access will be blocked for safety")
    
    memory_user_key = aad_id or user_email
    
    # DEBUG: Log the full cached profile to see what fields we have
    if aad_id:
        try:
            full_profile = await asyncio.wait_for(
                asyncio.to_thread(get_cached_user_profile, aad_id),
                timeout=2.0
            ) or {}
            logger.debug(f"DEBUG Full cached profile: {list(full_profile.keys())}")
        except asyncio.TimeoutError:
            logger.debug("DEBUG profile inspection TIMED OUT")
        except Exception as e:
            logger.debug(f"DEBUG profile inspection error: {type(e).__name__}")
    
    if user_email:
        try:
            logger.info(f"User UPN detected: {user_email}")
            if user_name:
                logger.info(f"User identity confirmed: name={user_name}, upn={user_email}")
        except Exception:
            pass
        # Re-bind user_key to a stable identifier if AAD id is unavailable
        try:
            if not aad_id and user_email:
                previous_key = user_key
                user_key = user_email
                await asyncio.to_thread(
                    remember_user_details,
                    user_key,
                    {
                        "displayName": user_name,
                        "mail": user_email,
                        "userPrincipalName": user_email,
                        "aadObjectId": aad_id or user_email,
                    }
                )
                logger.info(f"User key updated from {previous_key} to {user_key} (stable)")
        except Exception:
            pass
    # If we at least have a display name, persist minimal profile to disk for continuity
    if user_name and not (remembered.get("displayName") or user_email):
        # Persist minimal profile under stable identifiers if available; otherwise use conversation-scoped key
        try:
            stable_key = aad_id or user_email
            if stable_key:
                await asyncio.to_thread(
                    remember_user_details,
                    stable_key,
                    {
                        "displayName": user_name,
                        "aadObjectId": aad_id or stable_key,
                    }
                )
                logger.info(f"Persisted minimal profile: name={user_name}, key={stable_key}")
            else:
                conv_key = f"conv:{conversation_id}"
                await asyncio.to_thread(
                    remember_user_details,
                    conv_key,
                    {
                        "displayName": user_name,
                    }
                )
                logger.info(f"Persisted minimal profile under conversation key: {conv_key}")
        except Exception:
            pass

    # Background personal crawl DISABLED - using live Graph search only
    # Cache is populated only from documents actually used in responses
    logger.info("Personal crawl: DISABLED (live Graph search only)")

    # Cache partition key: prefer AAD id, else UPN/email (do NOT fall back to conversation id)
    cache_user_id = aad_id or user_email
    # Resolve UPN: try user_email first, then fall back to full profile cache
    cache_user_upn = user_email or ""
    if not cache_user_upn and aad_id:
        try:
            _prof = await asyncio.wait_for(
                asyncio.to_thread(get_cached_user_profile, aad_id),
                timeout=1.0
            ) or {}
            cache_user_upn = _prof.get("mail") or _prof.get("userPrincipalName") or ""
            if cache_user_upn:
                user_email = cache_user_upn
                logger.info(f"Resolved UPN from profile cache: {cache_user_upn}")
        except Exception:
            pass
    if not cache_user_id:
        logger.warning("Attachment caching disabled: no stable user id available")

    if conversation_id in conversation_title_list_state and is_document_title_summary_request(user_text):
        await send_typing_indicator(ctx)
        state = conversation_title_list_state.get(conversation_id, {}) or {}
        selected_titles = state.get("last_titles") or (state.get("titles") or [])[:10]
        if not selected_titles:
            selected_titles = []

        docs_by_title: dict[str, dict] = {}
        try:
            cache_obj = get_cache()
            if cache_obj:
                cache_key = cache_user_id or Config.SHAREPOINT_CACHE_USER_ID
                cached_docs = await asyncio.to_thread(cache_obj.get_all_documents, cache_key, True)
                for doc in cached_docs or []:
                    name = (doc.get("name") or doc.get("title") or "").strip()
                    if name and name.lower() not in docs_by_title:
                        docs_by_title[name.lower()] = doc
        except Exception as summary_cache_err:
            logger.warning("Failed to load cached docs for title summaries: %s", summary_cache_err)

        if selected_titles:
            lines = []
            for idx, item in enumerate(selected_titles, 1):
                name = (item.get("name") or "").strip()
                url = item.get("url") or ""
                doc = docs_by_title.get(name.lower(), {})
                content = doc.get("content") or doc.get("snippet") or ""
                if not url:
                    url = doc.get("url") or doc.get("webUrl") or doc.get("original_url") or ""
                summary = short_document_summary_from_content(content)
                link = f" [Open document]({url})" if url else " URL not available in cache."
                lines.append(f"{idx}. **{name}**\n   - Summary: {summary}\n   - URL:{link}")
            await ctx.send(
                MessageActivityInput(
                    text=(
                        "Here are short summaries and URLs for the document titles I just listed:\n\n"
                        + "\n".join(lines)
                    )
                ).add_ai_generated()
            )
        else:
            await ctx.send(
                MessageActivityInput(
                    text="I do not have a previous document-title list to summarize yet. Ask me to list document titles first."
                ).add_ai_generated()
            )
        return

    _has_title_list_state = conversation_id in conversation_title_list_state
    _is_title_pagination = _has_title_list_state and is_document_title_pagination_request(user_text)
    if is_document_title_list_request(user_text, _recent_history) or _is_title_pagination:
        await send_typing_indicator(ctx)
        limit = requested_title_limit(user_text, default=10)
        start_index = 0
        if _is_title_pagination:
            start_index = int(conversation_title_list_state.get(conversation_id, {}).get("next_index", 0) or 0)
        titles: list[dict] = []
        seen_titles: set[str] = set()

        def _add_title(name: str, url: str = "", source: str = "cache") -> None:
            clean_name = (name or "").strip()
            if not clean_name:
                return
            key = clean_name.lower()
            if key in seen_titles:
                return
            seen_titles.add(key)
            titles.append({"name": clean_name, "url": url or "", "source": source})

        try:
            cache_obj = get_cache()
            if cache_obj:
                cache_key = cache_user_id or Config.SHAREPOINT_CACHE_USER_ID
                cached_docs = await asyncio.to_thread(
                    cache_obj.get_all_documents,
                    cache_key,
                    True,
                )
                for doc in cached_docs or []:
                    if not is_cached_sharepoint_doc(doc):
                        continue
                    _add_title(
                        doc.get("name") or doc.get("title") or "Untitled",
                        doc.get("url") or doc.get("webUrl") or doc.get("original_url") or "",
                        "cache",
                    )
                logger.info("Document title list: loaded %s cached SharePoint title(s)", len(titles))
        except Exception as list_cache_err:
            logger.warning("Failed to list cached document titles: %s", list_cache_err)

        # Primary source: enumerate distinct documents from the Azure AI Search index.
        # The legacy cache above is empty in the current AI-Search-based design, so this
        # is what makes "what documents do you have" actually return real titles.
        if len(titles) < limit:
            try:
                from search.ai_search_retriever import list_indexed_documents
                indexed_docs = await asyncio.to_thread(list_indexed_documents, max(limit, 50))
                for d in indexed_docs:
                    _add_title(d.get("title"), d.get("url"), "ai_search")
                logger.info("Document title list: after AI Search index=%s title(s)", len(titles))
            except Exception as idx_list_err:
                logger.warning("Failed to list AI Search index titles: %s", idx_list_err)

        if len(titles) < limit and Config.ENABLE_SHAREPOINT_SEARCH:
            try:
                site_urls = (
                    Config.get_sharepoint_sites()
                    if hasattr(Config, "get_sharepoint_sites")
                    else [s.strip() for s in str(Config.SHAREPOINT_SITES or "").split(",") if s.strip()]
                )
                live_items = await asyncio.to_thread(
                    list_sharepoint_files,
                    site_urls,
                    user_assertion,
                    max(limit, 10),
                )
                for item in live_items or []:
                    _add_title(
                        item.get("name") or "Untitled",
                        item.get("webUrl") or "",
                        "sharepoint",
                    )
                    if len(titles) >= limit:
                        break
                logger.info("Document title list: after live SharePoint listing=%s title(s)", len(titles))
            except Exception as live_list_err:
                logger.warning("Failed to list live SharePoint titles: %s", live_list_err)

        if titles:
            if start_index >= len(titles):
                await ctx.send(
                    MessageActivityInput(
                        text=(
                            f"I found {len(titles)} available document title(s), and you've reached the end of the list."
                        )
                    ).add_ai_generated()
                )
                conversation_title_list_state[conversation_id] = {
                    "next_index": len(titles),
                    "total": len(titles),
                    "titles": titles,
                    "last_titles": [],
                }
                return

            selected = titles[start_index:start_index + limit]
            next_index = start_index + len(selected)
            conversation_title_list_state[conversation_id] = {
                "next_index": next_index,
                "total": len(titles),
                "titles": titles,
                "last_titles": selected,
                "last_start_index": start_index,
                "last_end_index": next_index,
            }
            lines = [f"{idx}. **{item['name']}**" for idx, item in enumerate(selected, start_index + 1)]
            source_note = "the indexed SharePoint documents"
            if any(item.get("source") == "sharepoint" for item in selected):
                source_note = "indexed and live SharePoint documents"
            more_note = (
                f"\n\nShowing {start_index + 1}-{next_index} of {len(titles)}. Say **show more** for the next {min(limit, max(0, len(titles) - next_index))}."
                if next_index < len(titles)
                else f"\n\nShowing {start_index + 1}-{next_index} of {len(titles)}. That's the end of the list."
            )
            await ctx.send(
                MessageActivityInput(
                    text=(
                        f"Here are document titles I found from {source_note}:\n\n"
                        + "\n".join(lines)
                        + more_note
                    )
                ).add_ai_generated()
            )
        else:
            await ctx.send(
                MessageActivityInput(
                    text=(
                        "I checked the Azure AI Search index of SharePoint documents, "
                        "but I could not find any available document titles."
                    )
                ).add_ai_generated()
            )
        return

    attachment_context = ""
    attachment_texts_for_llm: list[str] = []
    search_context = ""
    cached_results = []
    doc_summaries = []
    web_results = []
    scope = route.get("scope", "graph")
    if Config.DATA_SOURCE_MODE in ("sharepoint", "sharepoint_uploads_only", "sharepoint_ai_search_uploads_only") and scope not in ("local",):
        scope = "ai_search"
        route["scope"] = scope
    # Ensure AI search results variable exists for all paths
    ai_search_results = []
    combined_doc_results = []

    # STABILIZATION: Skip attachment/source loading for small-talk.
    skip_attachments_for_small_talk = (
        action == "respond_direct"
        and route.get("scope", "local") == "local"
        and not attachments
    )
    if skip_attachments_for_small_talk:
        logger.info("SMALL-TALK MODE: Skipping retrieval and attachment loading")
        # Force direct response and bypass source retrieval.
        if False and action == "search_documents":
            action = "respond_direct"
            should_search = False
            route["action"] = "respond_direct"
            route["should_search"] = False
            logger.info("   â†’ Overrode search_documents â†’ respond_direct for greeting")
    else:
        logger.info("ðŸ“š FULL MODE: Loading attachments and documents")

    # On-demand cache seeding DISABLED - using purely live Graph search
    # Cache is populated ONLY from documents actually used in responses
    # This ensures no background crawling and immediate live results
    logger.info("Cache seeding: DISABLED (live Graph search only)")

    # Get stored files from previous messages in this conversation (non-blocking)
    try:
        file_storage = await asyncio.wait_for(
            asyncio.to_thread(files_for, conversation_id, cache_user_id) if cache_user_id else asyncio.to_thread(lambda: []),
            timeout=2.0
        )
    except asyncio.TimeoutError:
        logger.debug("File storage lookup TIMED OUT - skipping")
        file_storage = []
    except Exception as e:
        logger.debug(f"Error loading file storage: {e}")
        file_storage = []
    # Track current attachments for calculations in this request
    current_attachment_files: list[dict] = []
    # Raw downloaded bytes for the code interpreter (real xlsx/pdf/docx manipulation).
    interpreter_input_files: dict[str, bytes] = {}
    
    # Attachment processing - files uploaded directly to chat
    if attachments and not skip_attachments_for_small_talk:
        typing_mgr.set_status("Reading attached file...")
        MAX_ATTACHMENTS = len(attachments)
        parts = []
        extracted_for_aggregation = []  # For multi-file comparison
        
        if len(attachments) > MAX_ATTACHMENTS:
            logger.info(f"Attachment limit exceeded ({len(attachments)}). Processing first {MAX_ATTACHMENTS} only.")
            await send_typing_indicator(ctx)
        
        total_attachments = min(len(attachments), MAX_ATTACHMENTS)
        
        for i, att in enumerate(attachments[:MAX_ATTACHMENTS], 1):
            att_name = getattr(att, "name", "unknown")
            
            # Update status per attachment so user sees progress
            if total_attachments > 1:
                typing_mgr.set_status(f"Reading {att_name} ({i} of {total_attachments})...")
                status_id = await send_typing_with_status(ctx, f"Reading {att_name} ({i}/{total_attachments})")
                if status_id:
                    status_activity_ids.append(status_id)
            else:
                typing_mgr.set_status(f"Reading {att_name}...")
            
            # Validate file before processing
            is_valid, validation_error = validate_file_attachment(att)
            if not is_valid:
                parts.append(validation_error)
                logger.warning(f"File validation failed for '{att_name}': {validation_error}")
                continue
            
            # Send another typing indicator before the heavy processing
            await send_typing_indicator(ctx)
            logger.info(f"Processing attachment {i}/{total_attachments}: {att_name}")
            
            # Wrap processing in try-except to prevent crashes
            file_content = None
            try:
                # ALWAYS use async with typing manager to prevent timeout
                file_size_mb = getattr(att, 'content_size', 0) / (1024 * 1024) if hasattr(att, 'content_size') else 0
                logger.info(f"Processing {att_name} (size: {file_size_mb:.1f}MB) with periodic typing refresh")
                
                async with TypingIndicatorManager(ctx):
                    file_content = await asyncio.to_thread(process_attachment, att, conversation_id, cache_user_id, interpreter_input_files)
            except MemoryError as mem_err:
                logger.error(f"MEMORY ERROR processing '{att_name}': {mem_err}")
                file_content = f"âŒ **File too large**: {att_name}\n\nThis file caused a memory error. Try:\nâ€¢ Splitting into smaller files\nâ€¢ Reducing file size\nâ€¢ Asking about specific sections"
            except Exception as proc_err:
                logger.error(f"ERROR processing attachment '{att_name}': {proc_err}", exc_info=True)
                file_content = f"âŒ **Processing failed**: {att_name}\n\nError: {str(proc_err)[:200]}"
            
            # Send typing indicator after processing (before caching)
            if file_content and len(file_content) > 10000:  # Large files get extra typing indicator
                await send_typing_indicator(ctx)

            if file_content:
                if file_content.startswith("âŒ"):
                    # Surface the failure to the LLM so it doesn't say "no attachment".
                    parts.append(file_content)
                    continue

                # Success path: cache content to disk for follow-up questions
                # This persists attachment content so memory limits aren't hit on follow-ups
                
                # Include FULL content for LLM - user wants complete extraction
                parts.append(file_content)
                
                # Keep full content for aggregation processing
                extracted_for_aggregation.append((att_name, file_content))
                # Note: Full content stored in cache and sent to LLM for complete analysis
                
                # Track current attachment for calculation path
                current_attachment_files.append({
                    "name": att_name,
                    "content": file_content,
                })
                
                # Cache attachment to disk for follow-up questions
                # PERFORMANCE: Async caching to avoid blocking
                # SECURITY: User ID for isolation
                if cache_user_id:
                    try:
                        await asyncio.to_thread(
                            cache_attachment,
                            conversation_id,
                            att_name,
                            file_content,
                            cache_user_id
                        )
                        logger.info(f"Attachment '{att_name}' processed and cached - {len(file_content)} chars (FULL content)")
                    except Exception as cache_err:
                        logger.warning(f"Failed to cache attachment '{att_name}': {cache_err}")
                        logger.info(f"Attachment '{att_name}' processed (cache failed) - {len(file_content)} chars")
                else:
                    logger.warning(f"Attachment '{att_name}' not cached: no stable user id")
            else:
                # No content returned; provide mobile-friendly guidance
                mobile_guidance = f"""âŒ Unable to read attachment '{att_name}'.

**If using Teams mobile app:**
â€¢ **Wait 30-60 seconds** after selecting files before sending
â€¢ Use the **paperclip button** (not drag-and-drop)
â€¢ Try **desktop/web Teams** for more reliable file uploads
â€¢ Ensure **strong network connection**

**File troubleshooting:**
â€¢ Check file size (keep under 250 MB)
â€¢ Verify file isn't corrupted or password-protected
â€¢ Make sure file has proper extension (.pdf, .docx, etc.)"""
                
                parts.append(mobile_guidance)
        
        # Final typing indicator after all attachments processed
        if total_attachments > 1:
            await send_typing_indicator(ctx)

        # If multiple tabular files uploaded, add aggregated comparison
        if len(extracted_for_aggregation) >= 2:
            try:
                aggregated = aggregate_tabular_files(extracted_for_aggregation)
                if aggregated:
                    # Prepend aggregated summary before individual files
                    parts.insert(0, aggregated)
                    logger.info(f"Added aggregated analysis across {len(extracted_for_aggregation)} files")
            except Exception as e:
                logger.warning(f"Failed to aggregate files: {e}")

        if parts:
            attachment_context = "\n\n" + "\n---\n".join(parts)
            attachment_texts_for_llm = parts
    
    # FOLLOW-UP SUPPORT: Include previously uploaded files from this conversation
    # First check disk cache, then fall back to in-memory storage
    # This enables questions like "top paid players" after uploading a FIFA dataset
    # PERFORMANCE: Use asyncio.to_thread for non-blocking I/O
    doc_cache_match = False
    if not attachments and not skip_attachments_for_small_talk:
        # Try to load from persistent disk cache first (survives restarts, avoids memory limits)
        cached_attachments = []
        try:
            # NON-BLOCKING: Run cache I/O in thread pool
            cached_attachments = await asyncio.to_thread(
                get_conversation_attachments, conversation_id, True, cache_user_id or None
            )
        except Exception as cache_err:
            logger.warning(f"Failed to load cached attachments: {cache_err}")
        
        if cached_attachments:
            logger.info(f"Loading {len(cached_attachments)} attachment(s) from disk cache for follow-up")
            parts = []
            max_cached_files = len(cached_attachments)
            for cached_file in cached_attachments[:max_cached_files]:
                fname = cached_file.get("name", "unknown")
                fcontent = cached_file.get("content", "")
                if fcontent:
                    # STABILIZATION: Chunk+compress cached content (ISSUE 1+2+6)
                    from utils.context_budget import select_relevant_chunks
                    _cap = int(getattr(Config, 'MAX_ATTACH_CHARS', 40000))
                    capped = select_relevant_chunks(fcontent, user_text or "", max_chars=_cap, label=fname[:30])
                    parts.append(f"[Previously uploaded: {fname}]\n{capped}")
                    logger.info(f"Loaded cached content for {fname}: {len(fcontent):,} chars -> {len(capped):,} chars (chunked+compressed)")
            if parts:
                attachment_context = "\n\n" + "\n---\n".join(parts)
                attachment_texts_for_llm = parts
                total_size = sum(len(p) for p in parts)
                logger.info(f"Loaded {len(parts)} cached file(s) with {total_size:,} total chars")
        elif file_storage:
            # Use cached attachments as primary source (file_storage is now cache-based)
            logger.info(f"Including {len(file_storage)} previously uploaded file(s) from cache")
            parts = []
            for stored_file in file_storage:
                fname = stored_file.get("name", "unknown")
                fcontent = stored_file.get("content", "")
                if fcontent:
                    # STABILIZATION: Chunk+compress cached content (ISSUE 1+2+6)
                    from utils.context_budget import select_relevant_chunks
                    _cap = int(getattr(Config, 'MAX_ATTACH_CHARS', 40000))
                    capped = select_relevant_chunks(fcontent, user_text or "", max_chars=_cap, label=fname[:30])
                    parts.append(f"[Previously uploaded: {fname}]\n{capped}")
                    logger.info(f"Including chunked+compressed cached file {fname}: {len(fcontent):,} -> {len(capped):,} chars")
            if parts:
                attachment_context = "\n\n" + "\n---\n".join(parts)
                attachment_texts_for_llm = parts
                logger.info(f"Loaded {len(parts)} stored file(s) with {sum(len(p) for p in parts)} total chars")

    # No hard-coded intent overrides; rely on LLM routing.

    # Re-sync action variable with route after any overrides
    action = route.get("action", action)

    # File listing intent handling removed to avoid keyword-based flow.
    list_files_intent = False
    random_list_intent = False

    # No keyword-based routing overrides; rely on LLM decision only.

    # List-files handler removed to keep conversation flow consistent.

    # LLM is the sole decision-maker â€” no rule-based overrides here.
                # Handle clarify action: Force search instead of asking questions
    if False and action == "clarify":
        # NEVER ask clarifying questions - always search instead
        search_terms = user_text.strip()
        if len(search_terms) > 2:  # Any meaningful input
            action = "search_documents"
            should_search = True
            search_query = search_terms
            logger.info(f"Converting clarify to SharePoint search: '{search_query}'")
        else:
            # Even for very vague input, provide search guidance without asking questions
            search_msg = (
                "I'll search across all available documents and sources. "
                "What specific topic, person, or information would help you most?"
            )
            await ctx.send(MessageActivityInput(text=search_msg).add_ai_generated())
            return

    # Route based on intent
    if action == "refine_previous":
        logger.info("Refinement detected; using conversation memory")
    elif action == "search_documents":
        # Normalize action for downstream logging
        action = "search_documents"
        search_attempted = True

    # â”€â”€ ATTACHMENT GATEKEEPER â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # If newly uploaded attachments OR cached files are present, and the router said 'search', 
    # we double-check if the search is actually necessary. 
    # Requirement: "Newlyuploaded attachment must always be processed... and must not call graph or ai search at all."
    _explicit_attachment_org_compare = bool(
        attachments
        and re.search(
            r"\b(company|organizational|organisation|policy|procedure|handbook|sharepoint|hr|employee)\b",
            user_text or "",
            flags=re.I,
        )
    )
    if attachments and action == "search_documents" and not _explicit_attachment_org_compare:
        # Force respond_direct to prioritize the uploaded/cached files
        _src_type = "Newly uploaded" if attachments else "Cached"
        logger.info(f"ðŸ›¡ï¸ ATTACHMENT GATEKEEPER: {_src_type} files found. Forcing bypass of external search to focus on document analysis.")
        action = "respond_direct"
        should_search = False
        search_attempted = False
    elif attachment_texts_for_llm and action == "search_documents":
        logger.info("ATTACHMENT GATEKEEPER: Cached files present, but preserving universal document search.")
        attachment_context = ""
        attachment_texts_for_llm = []

    if action == "search_documents":
        q = route.get("query", user_text).strip()

    if action == "search_documents":
        set_last_query(conversation_id, q)
        if q:
            # Use the LLM router's search_query directly â€” no stopword filtering
            llm_search_query = (route.get("query") or "").strip()
            search_query = llm_search_query if llm_search_query and llm_search_query != user_text else q
            
            # ðŸ§  INTELLIGENT QUERY ENHANCEMENT: Expand possessive pronouns with user identity
            # This makes "my cv" â†’ "malvine owuor cv resume" for better search recall
            original_query = search_query
            search_query = enhance_query_with_user_identity(search_query, user_name, user_email)
            
            if search_query != q:
                if search_query != original_query:
                    logger.info(f"LLM + Identity enhancement: '{q[:80]}' -> '{original_query[:80]}' -> '{search_query[:100]}'")
                else:
                    logger.info(f"Using LLM search query: '{q[:120]}' -> '{search_query[:120]}'")
            else:
                if search_query != original_query:
                    logger.info(f"Identity-enhanced query: '{original_query[:80]}' -> '{search_query[:100]}'")
                else:
                    logger.info(f"Search query (passthrough): '{search_query[:120]}'")

            is_summary_request = is_document_summary_request(user_text)
            strong_title_match_doc = None
            logger.info(f"Detected document summary request: {is_summary_request}")

            typing_mgr.set_status("Searching your documents...")
            
            top_k = int(route.get("top_k", 10))
            
            # Initialize result containers before searching
            doc_entries = []
            sources_refs = []
            full_contents = []
            cached_attachment_parts = []  # Track cached attachments for search optimization
            
            # STEP 0: Search cached attachments if no current attachments in context
            # This allows follow-up questions to access previously uploaded files
            # NOTE: Full content is preserved in cache (no truncation) to ensure all data is available
            if not attachment_context and not attachments and not list_files_intent:
                logger.info(f"No current attachments - searching cached attachments for: {search_query}")
                try:
                    # FIX: search_attachment_contents now correctly accepts user_id
                    cached_search_results = search_attachment_contents(conversation_id, search_query, limit=5, user_id=cache_user_id)
                    if cached_search_results:
                        logger.info(f"Found {len(cached_search_results)} relevant cached attachment(s)")
                        for result in cached_search_results:
                            filename = result.get("filename", "Unknown")
                            snippet = result.get("content_snippet", "")
                            score = result.get("relevance_score", 0)
                            full_content = result.get("full_content", "")
                            content_size = len(full_content)
                            
                            # SEPARATION OF CONCERNS: Use full content for calculations, truncated for LLM conversations
                            # Full content is always preserved in cache for accurate calculations
                            # Only apply truncation when displaying to user in conversation context
                            
                            if len(full_content) > 0:
                                # For follow-up conversations, apply chat mode logic
                                content_for_llm = get_content_for_llm_conversation(
                                    full_content, filename, mode="chat"
                                )
                                logger.info(f"Including cached attachment: {filename} (relevance: {score}, size: {content_size:,} chars, conversation_size: {len(content_for_llm):,} chars)")
                                cached_attachment_parts.append(f"[Cached attachment: {filename}]\n{content_for_llm}")
                        
                        # Add cached attachments to context
                        if cached_attachment_parts:
                            attachment_context = "\n\n" + "\n---\n".join(cached_attachment_parts)
                            attachment_texts_for_llm = cached_attachment_parts
                            total_cached_chars = sum(len(part) for part in cached_attachment_parts)
                            logger.info(f"Added {len(cached_attachment_parts)} cached attachment(s) to context ({total_cached_chars:,} chars total)")
                except Exception as cache_search_err:
                    logger.warning(f"Failed to search cached attachments: {cache_search_err}")
            
            # STEP 1: Search local document cache first (fastest)
            # Scope enforcement: skip cache for web-only OR when targeting specific source
            skip_cache_for_scope = scope in ("web", "onedrive", "network", "drives", "ai_search")
            if skip_cache_for_scope:
                logger.info(f"ðŸŽ¯ SCOPE '{scope}': Skipping document cache (searching specific source only)")
                cached_results = []
            else:
                logger.info(f"Searching document cache for: {search_query}")
                try:
                    if cache:
                        scored = cache.search_cache_scored(search_query, user_id=cache_user_id or Config.SHAREPOINT_CACHE_USER_ID, limit=top_k, include_shared=True)
                    else:
                        logger.warning("Cache is None - skipping cache search")
                        scored = []
                except Exception:
                    scored = []
                cached_results = [r.get("doc", {}) for r in scored]
                # FIX: cached SharePoint documents must be treated as organizational
                # SharePoint sources later in scope filters. Previously cached docs
                # often had only `url` and no `_from_document_cache` / `webUrl`, so
                # strict sharepoint filtering removed them even when they matched.
                for _doc in cached_results:
                    if isinstance(_doc, dict):
                        _doc.setdefault("_from_document_cache", True)
                        _doc.setdefault("_from_sharepoint", True)
                        if _doc.get("url") and not _doc.get("webUrl"):
                            _doc["webUrl"] = _doc.get("url")
                        if _doc.get("content") and not _doc.get("snippet"):
                            _doc["snippet"] = str(_doc.get("content") or "")[:1000]
                top_score = max([int(r.get("score", 0)) for r in scored], default=0)
                logger.info(f"Document cache returned {len(cached_results)} results (top score={top_score}) for user_id={cache_user_id}")
                try:
                    logger.debug(
                        "Cached result names: %s",
                        ", ".join([d.get("name", "(no-name)") for d in cached_results])
                    )
                except Exception:
                    pass
                
                # Filter out unrelated cached docs â€” use stricter matching for typo tolerance
                try:
                    _stop_terms = {
                        "can", "you", "for", "the", "a", "an", "please", "me", "to", "of", "about",
                        "llc", "inc", "corp", "corporation", "ltd", "limited", "company",
                    }
                    q_tokens = [t.lower() for t in clean_search_query(search_query or "").split() if len(t) > 2 and t.lower() not in _stop_terms]
                    if q_tokens and cached_results:
                        def _doc_matches(doc: dict) -> bool:
                            name = (doc.get("name") or doc.get("title") or "").lower()
                            content = (doc.get("snippet") or doc.get("content") or "")[:5000].lower()
                            text = name + " " + content
                            # Strong title match should never be discarded. Example:
                            # query "summarize employee handbook" should keep
                            # "employee handbook.docx" even if strict token ratio fails.
                            title_matches = sum(1 for tok in q_tokens if tok in name)
                            if title_matches >= 1 and any(tok in name for tok in ("handbook", "policy", "manual", "guide", "procedure")):
                                return True
                            matches = sum(1 for tok in q_tokens if tok in text or _fuzzy_token_in_text(tok, text, threshold=0.82))
                            threshold = 1 if len(q_tokens) <= 2 else max(2, int(len(q_tokens) * 0.5))
                            return matches >= threshold
                        before = len(cached_results)
                        cached_results = [d for d in cached_results if _doc_matches(d)]
                        filtered = before - len(cached_results)
                        if filtered:
                            logger.info(f"Filtered out {filtered} cached docs unrelated to query '{search_query}'")
                except Exception:
                    pass

            web_results = []

            # STEP 2: Route search to knowledge base (unified_search or parallel searches)
            unified_search_results = []
            parallel_results = {}
            
            typing_mgr.set_status("Searching your documents...")
            
            # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
            # STRICT SCOPE ENFORCEMENT - User specifies WHERE to search
            # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
            # Scope:
            #   "ai_search" = Search indexed SharePoint chunks in Azure AI Search.
            # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
            
            # Determine which sources to search based on scope
            logger.info("SEARCH SCOPE | scope=%s | ai_search=%s", scope, scope == "ai_search")
            
            if scope == "web":
                logger.info("Website search is disabled for this assistant")
                unified_search_results = []
            elif scope == "ai_search":
                from search.ai_search_retriever import search_sharepoint_chunks

                ai_search_started_at = time.perf_counter()
                logger.info(
                    "AI SEARCH QUERY | index=%s | query='%s' | top=%s",
                    Config.AZURE_SEARCH_INDEX_NAME,
                    search_query,
                    top_k,
                )
                ai_docs = await asyncio.to_thread(
                    search_sharepoint_chunks,
                    search_query,
                    user_email or None,
                    top_k,
                    aad_id,
                )
                logger.info(
                    "AI SEARCH TOTAL | query='%s' | seconds=%.2f | results=%s | source=app_block",
                    search_query,
                    time.perf_counter() - ai_search_started_at,
                    len(ai_docs or []),
                )
                unified_search_results = []
                for doc in ai_docs or []:
                    unified_search_results.append(
                        {
                            "id": f"{doc.get('document_id', '')}:{doc.get('chunk_id', '')}",
                            "name": doc.get("title") or doc.get("file_name") or "SharePoint document",
                            "title": doc.get("title") or doc.get("file_name") or "SharePoint document",
                            "content": doc.get("content") or doc.get("snippet") or "",
                            "snippet": doc.get("snippet") or doc.get("content") or "",
                            "url": doc.get("url") or doc.get("source_url") or "",
                            "webUrl": doc.get("url") or doc.get("source_url") or "",
                            "source_url": doc.get("url") or doc.get("source_url") or "",
                            "source_type": "sharepoint",
                            "score": doc.get("score"),
                            "_from_ai_search": True,
                            "_from_sharepoint": True,
                        }
                    )
                logger.info("AI SEARCH RESULTS | count=%s", len(unified_search_results))
            # Check if this is a parallel search request (pipe-separated queries)
            elif '|' in search_query:
                logger.info(f"Detected parallel search request: {search_query}")
                queries = [q.strip() for q in search_query.split('|') if q.strip()]
                logger.info(f"Executing {len(queries)} parallel searches: {queries}")
                typing_mgr.set_status(f"Running {len(queries)} searches in parallel...")

                parallel_results = await perform_parallel_searches(
                    queries=queries,
                    top_k=top_k,
                    cache_user_id=cache_user_id,
                    user_email=user_email,
                    user_assertion=user_assertion
                )  # Scope filtering applied after results return
                
                # Flatten results for combined processing while preserving source info
                unified_search_results = []
                for query, results in parallel_results.items():
                    for doc in results:
                        # Add source query info to each document
                        doc['_source_query'] = query
                        unified_search_results.append(doc)
                        
                logger.info(f"âœ… Parallel knowledge base searches completed: {len(unified_search_results)} total results")
            else:
                # Standard single search
                from knowledge_base import unified_search
                logger.info(f"DEBUG: Search parameters - user_id='{cache_user_id}', user_email='{user_email}', search_query='{search_query}', scope='{scope}'")
                unified_search_results = await asyncio.to_thread(
                    unified_search,
                    search_query,
                    top=top_k,
                    user_id=cache_user_id,
                    user_upn=user_email or "",
                    user_assertion=user_assertion,
                )
                
                # Post-filter only when an optional non-SharePoint scope is enabled.
                if scope not in ("graph",) and unified_search_results:
                    before_filter = len(unified_search_results)
                    filtered_results = []
                    for doc in unified_search_results:
                        _doc_url = (doc.get("webUrl") or doc.get("url") or doc.get("file_path") or "").lower()
                        is_sharepoint = (
                            doc.get("_from_sharepoint")
                            or doc.get("_from_document_cache")
                            or "sharepoint" in _doc_url
                        )
                        is_onedrive = doc.get("_from_onedrive_search") or doc.get("_from_live_graph") or "onedrive" in (doc.get("webUrl") or "").lower() or "my.sharepoint" in (doc.get("webUrl") or "").lower()
                        is_ai_search = doc.get("_from_ai_search")
                        
                        if scope == "sharepoint" and is_sharepoint:
                            filtered_results.append(doc)
                        elif scope == "onedrive" and is_onedrive:
                            filtered_results.append(doc)
                        elif scope in ("network", "drives", "ai_search") and is_ai_search:
                            filtered_results.append(doc)
                        elif scope == "graph":
                            filtered_results.append(doc)
                    
                    unified_search_results = filtered_results
                    logger.info(f"ðŸŽ¯ Post-filter for scope '{scope}': {before_filter} â†’ {len(unified_search_results)} results")
                
                logger.info(f"âœ… Knowledge base search completed: {len(unified_search_results or [])} results returned from unified search")
            
            # Combine Azure AI Search results only.
            combined_doc_results = []
            result_sources = []
            if unified_search_results:
                combined_doc_results.extend(unified_search_results)
                result_sources.append(f"Azure AI Search ({len(unified_search_results)})")
            
            result_source = " + ".join(result_sources) if result_sources else "None"
            logger.info(f"ðŸ“Š Combined results from {result_source}: {len(combined_doc_results)} total documents")

            
            # Strict person-name lookup: keep only docs that actually mention the
            # requested names/terms. This prevents broad employee directories
            # from being used to answer a specific person lookup.
            try:
                lookup_terms = [
                    term
                    for term in query_tokens(search_query)
                    if len(term) > 3 and term not in {"about", "document", "documents", "file", "files", "please", "show", "tell", "info", "information"}
                ]
                specific_lookup = (
                    bool(lookup_terms)
                    and (
                        " or " in (search_query or "").lower()
                        or " and " in (search_query or "").lower()
                        or (search_query or "").lower().startswith(("do you have", "do you know", "find", "search", "who is", "what about", "is there"))
                        or len(lookup_terms) >= 3
                    )
                )
                if specific_lookup and combined_doc_results:
                    def _doc_text(doc: dict) -> str:
                        return " ".join(
                            str(doc.get(field) or "") for field in ("name", "title", "file_name", "content", "snippet")
                        ).lower()

                    before_lookup_filter = len(combined_doc_results)
                    combined_doc_results = [
                        doc for doc in combined_doc_results
                        if any(term in _doc_text(doc) for term in lookup_terms)
                    ]
                    if combined_doc_results:
                        logger.info(
                            "Specific lookup filter: %s -> %s docs for query '%s'",
                            before_lookup_filter,
                            len(combined_doc_results),
                            search_query,
                        )
                    else:
                        logger.info(
                            "Specific lookup filter removed all docs for query '%s'",
                            search_query,
                        )
            except Exception as specific_lookup_err:
                logger.warning(f"Specific lookup filter error: {specific_lookup_err}")

            # Azure AI Search answers must be grounded only in indexed chunks.
            # Do not let stale local cache documents backfill empty AI Search results.
            if scope != "ai_search" and not combined_doc_results and cached_results:
                combined_doc_results.extend(cached_results)
                result_sources.append(f"Cache ({len(cached_results)})")
            
            result_source = " + ".join(result_sources) if result_sources else "None"
            logger.info(f"ðŸ“Š Combined results from {result_source}: {len(combined_doc_results)} total documents")

            # Log relevance assessment for debugging
            if combined_doc_results and search_query:
                query_terms = set(query_tokens(search_query))
                logger.info(f"ðŸ” Query terms for relevance check: {query_terms}")
                for i, doc in enumerate(combined_doc_results[:3], 1):
                    name = doc.get("name", "").lower()
                    content = doc.get("snippet", doc.get("content", "")).lower()
                    matching_terms = [term for term in query_terms if term in name or term in content]
                    source_type = "ðŸŒ" if doc.get("_from_web_cache") else "ðŸ“„"
                    logger.info(f"  {source_type}[{i}] {doc.get('name', '(no-name)')}: matching terms = {matching_terms if matching_terms else 'NONE'}")

            if combined_doc_results:
                search_yielded_results = True
            
            # Ensure variables have default values
            if 'result_source' not in locals():
                result_source = "None"
            
            # Format combined results (both cache and AI search)
            if combined_doc_results:
                # Deduplicate by document name (case-insensitive) to avoid processing duplicates
                # IMPORTANT: Keep the _from_ai_search flag from ANY matching document
                try:
                    seen_names = {}  # name_key -> doc
                    deduped_results = []
                    for d in combined_doc_results:
                        name_key = (d.get("name") or d.get("title") or "").strip().lower()
                        if not name_key:
                            deduped_results.append(d)
                            continue
                        if name_key in seen_names:
                            # Merge flags from duplicate
                            existing = seen_names[name_key]
                            if d.get("_from_ai_search") and not existing.get("_from_ai_search"):
                                existing["_from_ai_search"] = True
                            continue
                        seen_names[name_key] = d
                        deduped_results.append(d)
                    if len(deduped_results) != len(combined_doc_results):
                        logger.info(f"Deduped documents by name: {len(combined_doc_results)} -> {len(deduped_results)}")
                    combined_doc_results = deduped_results
                except Exception as dedupe_err:
                    logger.warning(f"Deduplication error: {dedupe_err}")
                # â”€â”€ PRIORITY: Scope-aware ordering â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                # (1) Separate documents by enabled source.
                # Cached docs are SharePoint-library content by default.
                web_docs = [d for d in combined_doc_results if d.get("_from_web_cache")]
                # Organizational docs are SharePoint Graph/cache by default.
                org_docs = []
                other_docs = []
                for d in combined_doc_results:
                    if d.get("_from_web_cache"):
                        continue
                    if is_cached_sharepoint_doc(d) or d.get("_from_live_graph") or d.get("_from_ai_search"):
                        d["_from_sharepoint"] = True
                        if is_cached_sharepoint_doc(d):
                            d["_from_document_cache"] = True
                        org_docs.append(d)
                    else:
                        other_docs.append(d)
                
                logger.info(f"Pre-filter split: Web={len(web_docs)} | Organizational={len(org_docs)} | Other={len(other_docs)}")
                if web_docs:
                    logger.info(f"  Web docs: {[d.get('name', 'unknown')[:40] for d in web_docs[:5]]}")
                if org_docs:
                    logger.info(f"  Organizational docs (PRIMARY - SharePoint): {[d.get('name', 'unknown')[:40] for d in org_docs[:5]]}")
                if other_docs:
                    logger.info(f"  Other docs: {[d.get('name', 'unknown')[:40] for d in other_docs[:5]]}")
                
                # (2) Organizational docs are already ranked by SharePoint/cache search - keep them.
                # FIX: Cached SharePoint docs may arrive without provider flags from older cache files.
                # Promote them into org_docs if their URL/name indicates they are cached SharePoint docs.
                if other_docs:
                    promoted_docs = []
                    remaining_other = []
                    for _doc in other_docs:
                        if is_cached_sharepoint_doc(_doc):
                            _doc["_from_document_cache"] = True
                            _doc["_from_sharepoint"] = True
                            promoted_docs.append(_doc)
                        else:
                            remaining_other.append(_doc)
                    if promoted_docs:
                        org_docs.extend(promoted_docs)
                        other_docs = remaining_other
                        logger.info(f"Promoted {len(promoted_docs)} cached SharePoint doc(s) from Other â†’ Organizational")

                if scope == "ai_search":
                    org_docs = [d for d in org_docs if d.get("_from_ai_search")]
                    other_docs = []
                    web_docs = []
                    logger.info(f"Azure AI Search docs ready for selection: {len(org_docs)}")
                else:
                    logger.info(f"Organizational docs ready from SharePoint/cache: {len(org_docs)}")

                if org_docs:
                    title_matches = sorted(
                        ((title_match_strength(d, search_query)[0], d) for d in org_docs),
                        key=lambda item: item[0],
                        reverse=True,
                    )
                    best_strength, best_doc = title_matches[0]
                    if best_strength >= 65:
                        strong_title_match_doc = best_doc
                        strong_title_match_doc["_strong_title_match"] = True
                        strong_title_match_doc["_primary_document"] = True
                        logger.info(
                            "Strong title match: %s (strength=%s)",
                            best_doc.get("name") or best_doc.get("title") or "Untitled",
                            best_strength,
                        )
                        if is_summary_request:
                            org_docs = [best_doc]
                            logger.info(
                                "Selected primary document: %s",
                                best_doc.get("name") or best_doc.get("title") or "Untitled",
                            )
                
                # (3) Re-rank org docs by query term matches to prioritize actually relevant results
                # Semantic search may return docs that mention keywords in unrelated contexts
                # (e.g., "RFP Response.pdf" mentioning "Swope Health" as a customer, not ABOUT Swope Health)
                try:
                    q_tokens = query_tokens(search_query)
                    if q_tokens and org_docs:
                        def _relevance_score(doc: dict) -> tuple:
                            """Score doc by query term matches in title/name (higher = more relevant)"""
                            name = (doc.get("name") or doc.get("title") or "").lower()
                            title_strength, _ = title_match_strength(doc, search_query)
                            # Count matching tokens in document name
                            matches = sum(1 for tok in q_tokens if tok in name)
                            # Also check content snippet for matches
                            content = (doc.get("snippet") or doc.get("content") or "")[:500].lower()
                            content_matches = sum(1 for tok in q_tokens if tok in content)
                            # Azure relevance score (secondary tiebreaker)
                            azure_score = float(doc.get("score") or doc.get("@search.score") or 0)
                            # Return tuple: title match dominates body matches.
                            return (title_strength, matches, content_matches, azure_score)
                        
                        # Sort by relevance: docs with query terms in name first
                        org_docs_sorted = sorted(org_docs, key=_relevance_score, reverse=True)
                        
                        # Log re-ranking results
                        top_3_before = [d.get('name', 'unknown')[:40] for d in org_docs[:3]]
                        top_3_after = [d.get('name', 'unknown')[:40] for d in org_docs_sorted[:3]]
                        if top_3_before != top_3_after:
                            logger.info(f"ðŸ”„ Re-ranked org docs by query term matches:")
                            logger.info(f"   Before: {top_3_before}")
                            logger.info(f"   After:  {top_3_after}")
                        org_docs = org_docs_sorted
                except Exception as rank_err:
                    logger.warning(f"Re-ranking error (using original order): {rank_err}")
                
                logger.info(f"âœ… Organizational docs ready for selection: {len(org_docs)} total")
                
                # Combine based on scope: Web scope puts web first, otherwise organizational docs first
                # Priority: SharePoint/cache first, then any optional sources.
                # âš ï¸ Dynamic allocation based on what sources returned results
                MAX_TOTAL_DOCS = 4  # Target total docs â€” reduced for faster processing
                
                if scope == "web":
                    # WEB SCOPE: Web results ONLY (strict scope enforcement)
                    logger.info("ðŸŒ WEB SCOPE: Returning web results ONLY (strict scope)")
                    web_to_include = web_docs[:MAX_TOTAL_DOCS]
                    combined_doc_results = web_to_include
                    logger.info(f"Post-filter combined (WEB SCOPE - STRICT): Web={len(web_to_include)} total")
                elif scope in ("sharepoint", "onedrive", "network", "drives", "ai_search"):
                    # SPECIFIC SCOPE: Return only from requested source (already filtered above)
                    logger.info(f"ðŸŽ¯ SCOPE '{scope}': Returning organizational docs ONLY (strict scope)")
                    org_to_include = org_docs[:MAX_TOTAL_DOCS]
                    combined_doc_results = org_to_include
                    logger.info(f"Post-filter combined ({scope.upper()} SCOPE - STRICT): Org={len(org_to_include)} total")
                else:
                    # DEFAULT/GRAPH SCOPE: Organizational docs first, then web for breadth
                    logger.info("ðŸ“š DEFAULT SCOPE: Organizational docs first, web supplement")
                    org_to_include = org_docs[:3]  # Top 3 org docs â€” quality over quantity
                    remaining_slots = MAX_TOTAL_DOCS - len(org_to_include)
                    web_to_include = web_docs[:remaining_slots] if web_docs and remaining_slots > 0 else []
                    remaining_slots -= len(web_to_include)
                    other_to_include = other_docs[:remaining_slots] if other_docs and remaining_slots > 0 else []
                    combined_doc_results = org_to_include + web_to_include + other_to_include
                    logger.info(f"Post-filter combined (DEFAULT): Org={len(org_to_include)} | Web={len(web_to_include)} | Other={len(other_to_include)} = {len(combined_doc_results)} total")
                
                # (3) Cap result count (now after filtering, so we keep priority docs)
                max_combined = int(os.getenv("MAX_COMBINED_DOCS", "4"))
                if len(combined_doc_results) > max_combined:
                    combined_doc_results = combined_doc_results[:max_combined]
                    org_preserved = sum(1 for d in combined_doc_results if 
                        d.get("_from_live_graph") or d.get("_from_onedrive_search") or 
                        d.get("_from_document_cache") or d.get("_from_ai_search") or d.get("_from_sharepoint"))
                    logger.info(f"Capped combined document results to top {max_combined} ({org_preserved} organizational docs)")

                # (4) File-type aware doc count limit â€” some formats are heavier to process
                # Web cache results are already parsed and don't need download â€” exclude from weight calculation
                def _get_file_weight(doc: dict) -> str:
                    """Classify file by processing weight: heavy, medium, or light"""
                    # Web cache results are pre-parsed, no download needed
                    if doc.get("_from_web_cache"):
                        return "none"  # No download cost
                    name = (doc.get("name") or "").lower()
                    # Heavy: PDFs (text extraction), large Office docs
                    if name.endswith(".pdf"):
                        return "heavy"
                    # Medium: Office docs (docx, xlsx, pptx) - need parsing but faster than PDF
                    if name.endswith((".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt")):
                        return "medium"
                    # Light: plain text, markdown, etc.
                    return "light"
                
                file_weights = [_get_file_weight(d) for d in combined_doc_results]
                heavy_count = file_weights.count("heavy")
                medium_count = file_weights.count("medium")
                web_count = file_weights.count("none")  # Web cache (no download)
                
                # STRICT LIMIT: Adjust max docs based on file complexity
                # Web cache results don't count toward heavy limits since they're pre-parsed
                if heavy_count >= 2:
                    _max_docs_for_dl = 2 + web_count  # 2+ PDFs = limit to 2 downloads, but keep all web
                elif heavy_count >= 1:
                    _max_docs_for_dl = 3 + web_count  # 1 PDF + others
                elif medium_count >= 3:
                    _max_docs_for_dl = 3 + web_count  # Many Office docs
                else:
                    _max_docs_for_dl = 5 + web_count  # Light files only - can handle more
                
                if len(combined_doc_results) > _max_docs_for_dl:
                    logger.info(
                        f"ðŸ“„ File weights: {heavy_count} heavy, {medium_count} medium, {web_count} web (no download) â€” "
                        f"Limiting docs from {len(combined_doc_results)} to {_max_docs_for_dl}"
                    )
                    combined_doc_results = combined_doc_results[:_max_docs_for_dl]
                    org_final = sum(1 for d in combined_doc_results if 
                        d.get("_from_live_graph") or d.get("_from_onedrive_search") or 
                        d.get("_from_document_cache") or d.get("_from_ai_search") or d.get("_from_sharepoint"))
                    web_final = sum(1 for d in combined_doc_results if d.get("_from_web_cache"))
                    logger.info(f"  Final selection: {org_final} organizational docs + {web_final} web + {len(combined_doc_results) - org_final - web_final} others")

                # (5) Prepare download
                max_download = _max_docs_for_dl  # Never download more than the limit
                max_extract = min(max_download, len(combined_doc_results))
                logger.info(f"Preparing content for top {max_extract} document(s) (limit={_max_docs_for_dl})...")
                
                # Send typing indicator before document downloads (can be slow)
                await send_typing_indicator(ctx)

                # Pre-download dedupe: skip identical items and highly similar names
                try:
                    from difflib import SequenceMatcher
                    def _norm_name(n: str) -> str:
                        return " ".join((n or "").lower().replace("_", " ").replace("-", " ").split())

                    deduped = []
                    seen_ids = set()
                    seen_names = []
                    for doc in combined_doc_results:
                        drive_id = doc.get("driveId") or ""
                        item_id = doc.get("itemId") or ""
                        web_url = doc.get("webUrl") or doc.get("url") or ""
                        key = f"{drive_id}:{item_id}" if drive_id and item_id else web_url
                        if key and key in seen_ids:
                            continue
                        name_norm = _norm_name(doc.get("name") or doc.get("title") or "")
                        if name_norm:
                            is_similar = any(SequenceMatcher(None, name_norm, prev).ratio() >= 0.98 for prev in seen_names)
                            if is_similar:
                                continue
                            seen_names.append(name_norm)
                        if key:
                            seen_ids.add(key)
                        deduped.append(doc)
                    if len(deduped) != len(combined_doc_results):
                        logger.info(f"Pre-download dedupe reduced {len(combined_doc_results)} -> {len(deduped)}")
                    combined_doc_results = deduped
                    max_extract = min(max_download, len(combined_doc_results))
                except Exception:
                    pass
                
                download_jobs = []
                graph_token = None
                download_sem = asyncio.Semaphore(3)

                DOWNLOAD_TIMEOUT = int(os.getenv("DOCUMENT_DOWNLOAD_TIMEOUT", "8"))  # seconds - fast-fail per doc download

                async def _download_for_doc(doc: dict, name: str, web_url: str, drive_id: str, item_id: str):
                    nonlocal graph_token
                    async with download_sem:
                        if graph_token is None:
                            graph_token = await asyncio.to_thread(get_graph_token, user_assertion)
                        if not graph_token:
                            return None
                        try:
                            return await asyncio.wait_for(
                                asyncio.to_thread(
                                    download_and_extract_content,
                                    web_url, graph_token, name, drive_id, item_id,
                                ),
                                timeout=DOWNLOAD_TIMEOUT,
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                f"â° Download TIMED OUT after {DOWNLOAD_TIMEOUT}s: {name} "
                                f"({web_url[:80]})"
                            )
                            return None

                for idx_doc, doc in enumerate(combined_doc_results, 1):
                    if idx_doc > max_extract:
                        break
                    # Skip if we already have content (e.g., from cache search results)
                    if doc.get("content") and len(doc.get("content", "").strip()) > 50:
                        # FIX: Cap existing content â€” never let raw content bypass compression
                        _existing = doc["content"]
                        _CAP = int(getattr(Config, 'MAX_DOC_SNIPPET_CHARS', 6000))
                        if is_summary_request and doc.get("_primary_document"):
                            _CAP = max(_CAP, int(os.getenv("SUMMARY_PRIMARY_DOC_CHARS", "20000")))
                        if len(_existing) > _CAP:
                            from utils.context_budget import select_relevant_chunks as _src
                            if is_summary_request and doc.get("_primary_document"):
                                doc["content"] = _existing[:_CAP]
                            else:
                                doc["content"] = _src(
                                    _existing, search_query or user_text or "",
                                    max_chars=_CAP, label=doc.get('name', 'cached')[:30]
                                )
                            doc["_content_truncated"] = len(_existing) > len(doc["content"])
                            logger.info(
                                f"Capped existing content for: {doc.get('name', 'unknown')} "
                                f"({len(_existing):,} â†’ {len(doc['content']):,} chars)"
                            )
                        else:
                            logger.info(f"Using existing content for: {doc.get('name', 'unknown')} ({len(_existing)} chars)")
                        continue
                    
                    # For Graph results without content, check cache first before downloading
                    if doc.get("_from_live_graph") or doc.get("driveId") or doc.get("itemId"):
                        try:
                            name = doc.get("name") or "Untitled"
                            web_url = doc.get("webUrl") or doc.get("url") or ""
                            drive_id = doc.get("driveId") or ""
                            item_id = doc.get("itemId") or ""
                            
                            # PRE-CHECK: Skip documents in other users' personal OneDrives (no download needed)
                            url_lower = web_url.lower()
                            if "/personal/" in url_lower:
                                try:
                                    parts = url_lower.split("/personal/")
                                    if len(parts) > 1:
                                        owner_part = parts[1].split("/")[0]
                                        current_user_upn = (cache_user_upn or "").lower()
                                        if not current_user_upn:
                                            # Fallback: match user's display name tokens against
                                            # the personal OneDrive owner slug.
                                            # e.g. name="Malvine Owuor" â†’ ["malvine","owuor"]
                                            #      owner_part="malvine_owuor_armely_com" â†’ match
                                            _name_tokens = [t.lower() for t in (user_name or "").split() if len(t) > 1]
                                            if _name_tokens and all(t in owner_part for t in _name_tokens):
                                                logger.info(f"âœ“ Name-based match: user '{user_name}' appears to own /personal/{owner_part}")
                                            else:
                                                logger.info(f"â­ï¸  Skipping {name}: user UPN unavailable for personal OneDrive access check")
                                                doc["_access_denied"] = True
                                                doc["_denial_reason"] = "User UPN unavailable for personal OneDrive access check"
                                                continue
                                        # Skip if this personal OneDrive belongs to someone else
                                        if owner_part and not current_user_upn.replace("@", "_").replace(".", "_").startswith(owner_part):
                                            logger.info(f"â­ï¸  Skipping {name}: belongs to another user's OneDrive ({owner_part})")
                                            doc["_access_denied"] = True
                                            doc["_denial_reason"] = f"Document is in another user's personal OneDrive ({owner_part})"
                                            continue
                                except Exception:
                                    pass

                            # OPTIMIZATION: Check cache first to avoid redundant downloads
                            doc_id = f"{drive_id}:{item_id}" if drive_id and item_id else web_url
                            cached_doc = None
                            try:
                                if cache:
                                    all_cached = cache.get_all_documents(cache_user_id or Config.SHAREPOINT_CACHE_USER_ID, include_shared=True)
                                else:
                                    all_cached = []
                                cached_doc = next((d for d in all_cached if d.get("id") == doc_id or d.get("url") == web_url or d.get("name") == name), None)
                            except Exception:
                                pass

                            if cached_doc and cached_doc.get("content") and len(cached_doc.get("content", "").strip()) > 50:
                                # Use cached content instead of downloading
                                doc["content"] = cached_doc["content"]
                                logger.info(f"âœ“ Using cached content for: {name} ({len(cached_doc['content'])} chars)")
                            else:
                                # Download content from Graph (not in cache or cache has no content)
                                task = asyncio.create_task(_download_for_doc(doc, name, web_url, drive_id, item_id))
                                download_jobs.append((doc, name, task))
                        except Exception as dl_err:
                            logger.warning(f"Error preparing content for {doc.get('name', 'unknown')}: {dl_err}")

                if download_jobs:
                    GATHER_TIMEOUT = int(os.getenv("DOCUMENT_GATHER_TIMEOUT", "18"))  # Outer safety net for all downloads
                    try:
                        results = await asyncio.wait_for(
                            asyncio.gather(*(t for _, _, t in download_jobs), return_exceptions=True),
                            timeout=GATHER_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.error(f"â° ALL downloads timed out after {GATHER_TIMEOUT}s â€” skipping all {len(download_jobs)} documents")
                        results = [TimeoutError(f"Gather timeout {GATHER_TIMEOUT}s")] * len(download_jobs)
                    for (doc, name, _task), result in zip(download_jobs, results):
                        web_url = doc.get("webUrl") or doc.get("url") or ""
                        
                        if isinstance(result, Exception):
                            logger.warning(f"Failed to download content for: {name}")
                            doc["_access_denied"] = True
                            continue
                        content = result or ""
                        
                        # Check if content indicates access failure
                        if is_inaccessible_content(content):
                            # Provide detailed explanation for why access was denied
                            url_lower = web_url.lower()
                            reason = "Unknown reason"
                            
                            if "/personal/" in url_lower:
                                # Extract the user's OneDrive identifier
                                try:
                                    parts = url_lower.split("/personal/")
                                    if len(parts) > 1:
                                        owner_part = parts[1].split("/")[0]
                                        reason = f"Document is in another user's personal OneDrive ({owner_part})"
                                except Exception:
                                    reason = "Document is in another user's personal OneDrive"
                            elif "403" in content or "unauthorized" in content.lower():
                                reason = "Unauthorized - insufficient permissions"
                            elif "404" in content or "not found" in content.lower():
                                reason = "Document not found or path unavailable"
                            else:
                                reason = "Content download failed"
                            
                            logger.warning(f"âŒ Access denied for '{name}': {reason}")
                            logger.debug(f"   URL: {web_url[:150]}...")
                            logger.debug(f"   Error content: {content[:150]}")
                            doc["_access_denied"] = True
                            doc["_denial_reason"] = reason
                            continue
                        
                        if content and len(content.strip()) >= 10:
                            # ISSUE 1+2+6: Chunkâ†’rankâ†’compress after download (NEVER inject raw)
                            from utils.context_budget import select_relevant_chunks, summarize_text as _summarize
                            LLM_DOC_CAP = int(getattr(Config, 'MAX_DOC_SNIPPET_CHARS', 6000))
                            if is_summary_request and doc.get("_primary_document"):
                                LLM_DOC_CAP = max(LLM_DOC_CAP, int(os.getenv("SUMMARY_PRIMARY_DOC_CHARS", "20000")))
                            if len(content) > LLM_DOC_CAP:
                                # For PDFs: chunk by relevance. For others: summarize.
                                _is_pdf = (name or "").lower().endswith(".pdf")
                                if is_summary_request and doc.get("_primary_document"):
                                    content = content[:LLM_DOC_CAP]
                                elif _is_pdf:
                                    content = select_relevant_chunks(
                                        content, search_query or user_text or "",
                                        max_chars=LLM_DOC_CAP, label=name[:30]
                                    )
                                else:
                                    content = _summarize(content, max_chars=LLM_DOC_CAP, label=name[:30])
                                doc["_content_truncated"] = True
                                logger.info(f"ðŸ“ Chunked/compressed downloaded content for '{name}': -> {len(content):,} chars")
                            doc["content"] = content
                            logger.info(f"Downloaded content for: {name} ({len(content):,} chars)")
                        else:
                            logger.warning(f"Failed to download content for: {name} (insufficient content)")
                            doc["_access_denied"] = True
                            doc["_denial_reason"] = "Insufficient content returned"
                
                # Filter out documents marked as inaccessible
                before_filter = len(combined_doc_results)
                filtered_docs = [doc for doc in combined_doc_results if doc.get("_access_denied")]
                combined_doc_results = [doc for doc in combined_doc_results if not doc.get("_access_denied")]
                filtered_count = before_filter - len(combined_doc_results)
                
                if filtered_count > 0:
                    logger.info(f"ðŸš« Filtered out {filtered_count} inaccessible document(s) that user cannot access:")
                    for i, doc in enumerate(filtered_docs[:5], 1):  # Show first 5 filtered docs
                        name = doc.get("name", "Unknown")
                        reason = doc.get("_denial_reason", "Unknown")
                        logger.info(f"   [{i}] {name}: {reason}")
                    if len(filtered_docs) > 5:
                        logger.info(f"   ... and {len(filtered_docs) - 5} more")
                
                # Improve ordering: prioritize query token matches and any source relevance score.
                try:
                    def _rank(doc: dict) -> tuple:
                        score = float(doc.get("score") or doc.get("_cache_score") or 0)
                        text = (doc.get("name") or "") + " " + (doc.get("snippet") or doc.get("content") or "")
                        match_hits = sum(1 for tok in q_tokens if _fuzzy_token_in_text(tok, text))
                        # Tuple: (has_matches, score) so matches take precedence, then score
                        title_strength, _ = title_match_strength(doc, search_query)
                        return (title_strength, 1 if match_hits > 0 else 0, score)
                    combined_doc_results.sort(key=_rank, reverse=True)
                except Exception:
                    pass
                logger.info(f"Formatting {len(combined_doc_results)} combined document results")
                def _format_ref_line(idx: int, name: str, url: str) -> str:
                    if url and (url.startswith("http://") or url.startswith("https://")):
                        return (
                            f"<div style=\"margin: 0.15rem 0;\">"
                            f"<a href=\"{url}\" style=\"color: #0078d4; text-decoration: none;\">[" 
                            f"{idx}]</a> {name}</div>"
                        )
                    extra = f" - {url}" if url else ""
                    return f"<div style=\"margin: 0.15rem 0;\">[{idx}] {name}{extra}</div>"

                for idx, doc in enumerate(combined_doc_results, 1):
                    name = doc.get("name") or doc.get("title") or "Untitled"
                    url = doc.get("url") or doc.get("file_path") or doc.get("webUrl") or ""
                    content = doc.get("content", "")
                    # STABILIZATION: Compress content to prevent token overflow
                    from utils.context_budget import compress_for_llm as _compress_fmt
                    content = _compress_fmt(content, 6000, label=(name or 'doc')[:30])
                    snippet = doc.get("snippet", content if content else "")
                    from urllib.parse import quote
                    clean_url = quote(url, safe=':/?#[]@!$&\'()*+,;=')
                    
                    # Mark source for clarity
                    is_cached = doc in cached_results
                    source_label = "Cached" if is_cached else "SharePoint"
                    
                    doc_entries.append(
                        f"<div style=\"margin-bottom: 1.5rem;\">"
                        f"<h4 style=\"margin: 0.5rem 0; font-size: 1rem;\">[{idx}] {name} {source_label}</h4>"
                        f"<p style=\"margin: 0.25rem 0; font-size: 0.85rem; color: #666;\">"
                        f"<a href=\"{clean_url}\" style=\"text-decoration: none; color: #0078d4;\">View Document</a></p>"
                        f"<p style=\"margin: 0.5rem 0; font-size: 0.9rem; line-height: 1.4;\">{snippet if len(snippet) <= 500 else snippet[:500] + '…'}</p>"
                        f"</div>"
                    )
                    sources_refs.append(_format_ref_line(idx, name, clean_url))
                    full_contents.append({
                        "idx": idx,
                        "name": name,
                        "url": clean_url,
                        "content": content  # Already capped above
                    })

                # Note: Web cache results will be processed separately and included alongside document results
                
                # Prepare doc items for model
                pass
                pass
            
            # Do not force references based on sources searched. References should only
            # appear when extracted content is actually used in the response.

    # Add personalization context and build full input
    # Build personalization without placeholder name
    info_parts = []
    if user_name and user_name.strip() and user_name.strip().lower() != "user":
        info_parts.append(f"User's name is {user_name.strip()}")
    if user_email:
        info_parts.append(f"email: {user_email}")
    info_str = "; ".join(info_parts)
    if info_str:
        personalization = f"\n\n[CONTEXT: Today is {weekday_name}, {date_friendly} ({current_datetime}). {info_str}.]"
    else:
        personalization = f"\n\n[CONTEXT: Today is {weekday_name}, {date_friendly} ({current_datetime}).]"
    # Prepare inputs for model (plain text, limited)
    attachment_texts = attachment_texts_for_llm if attachment_texts_for_llm else ([] if not attachment_context else [attachment_context])
    primary_summary_doc = locals().get("strong_title_match_doc")
    if is_document_summary_request(user_text) and primary_summary_doc:
        primary_title_norm = normalized_doc_title(primary_summary_doc.get("name") or primary_summary_doc.get("title") or "")

        def _same_primary_doc(doc: dict) -> bool:
            return normalized_doc_title(doc.get("name") or doc.get("title") or "") == primary_title_norm

        for _doc in (cached_results or []) + (combined_doc_results or []):
            if _same_primary_doc(_doc):
                _doc["_primary_document"] = True
                _doc["_strong_title_match"] = True
        cached_results = [d for d in (cached_results or []) if _same_primary_doc(d)]
        combined_doc_results = [d for d in (combined_doc_results or []) if _same_primary_doc(d)]
        logger.info(
            "Selected primary document: %s",
            primary_summary_doc.get("name") or primary_summary_doc.get("title") or "Untitled",
        )
    # Collect doc items for model â€” use combined_doc_results (post access-filter)
    # so we never expose documents the user cannot access
    # IMPORTANT: Add search results (cached + combined) FIRST before web results
    # This ensures high-relevance documents get priority when limiting docs (e.g., PDF limit)

    # â”€â”€ ATTACHMENT ISOLATION (anti-hallucination) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # When the user uploaded a file THIS turn (and isn't explicitly asking to
    # compare it against org/SharePoint content), analyze/summarize ONLY that
    # file. Drop any index / cache / AI-Search docs AND the prior-source memory
    # summary that may have been collected, so unrelated indexed documents
    # cannot bleed into the prompt and produce a confident summary of the WRONG
    # document. (Root cause of the "public health messages" hallucination: a
    # tiny 140-char e-ticket was blended with 3174 chars of unrelated indexed
    # docs, and the model summarized the larger unrelated text.)
    if attachments and attachment_texts and not _explicit_attachment_org_compare:
        _dropped = len(cached_results or []) + len(combined_doc_results or []) + len(ai_search_results or [])
        if _dropped or _conversation_source_summary:
            logger.info(
                "ATTACHMENT ISOLATION: focusing on uploaded file(s) only - dropped %d index/cache docs "
                "and %d chars of prior-source memory to prevent cross-document hallucination.",
                _dropped,
                len(_conversation_source_summary or ""),
            )
        cached_results = []
        combined_doc_results = []
        ai_search_results = []
        _conversation_source_summary = ""

    model_doc_items = []
    
    # STABILIZATION: Chunk+compress all doc snippets entering model_doc_items (ISSUE 1+2)
    from utils.context_budget import compress_for_llm as _compress_doc, select_relevant_chunks as _chunk_doc
    DOC_SNIPPET_CAP = int(getattr(Config, 'MAX_DOC_SNIPPET_CHARS', 6000))
    # For document summary/overview requests, allow a larger source window so
    # cached docs are actually useful instead of only sending a tiny excerpt.
    if re.search(r"\b(summarize|summary|overview|tell me about|what is|explain)\b", (user_text or "").lower()):
        DOC_SNIPPET_CAP = max(DOC_SNIPPET_CAP, int(os.getenv("SUMMARY_DOC_SNIPPET_CHARS", "18000")))

    def _encode_sharepoint_url(url: str) -> str:
        """URL-encode SharePoint paths while preserving the domain and slashes."""
        if not url or not url.startswith("https://"):
            return url
        try:
            protocol_end = url.find("://") + 3
            domain_end = url.find("/", protocol_end)
            if domain_end > 0:
                protocol_domain = url[:domain_end]
                path = url[domain_end:]
                # Encode spaces and special characters in path, preserving slashes
                encoded_path = quote(path, safe="/:?&=")
                return protocol_domain + encoded_path
        except Exception:
            pass
        return url
    
    _seen_doc_titles: set[str] = set()  # deduplicate by normalised title
    
    def _add_doc_item(
        title: str,
        url: str,
        snippet: str,
        *,
        total_chars: int | None = None,
        truncated: bool = False,
        primary: bool = False,
    ) -> None:
        """Add to model_doc_items with deduplication by title."""
        norm = title.strip().lower()
        if norm in _seen_doc_titles:
            return
        _seen_doc_titles.add(norm)
        model_doc_items.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "total_chars": total_chars if total_chars is not None else len(snippet or ""),
            "included_chars": len(snippet or ""),
            "truncated": truncated,
            "primary": primary,
        })
    
    # (1) Add CACHED results first for legacy/local document paths only.
    # Azure AI Search queries use indexed chunks directly, avoiding stale cache bleed.
    if not list_files_intent and scope != "ai_search":
        for d in (cached_results or []):
            if d.get("_access_denied"):
                continue
            raw_content = d.get("content") or ""
            doc_snippet = raw_content if is_summary_request else (d.get("snippet") or raw_content)
            if not doc_snippet or not doc_snippet.strip():
                continue
            raw_len = len(doc_snippet)
            cap = DOC_SNIPPET_CAP
            if is_summary_request and d.get("_primary_document"):
                cap = max(cap, int(os.getenv("SUMMARY_PRIMARY_DOC_CHARS", "20000")))
            # ISSUE 1+2: Hard cap â€” chunk if oversized, compress always
            if len(doc_snippet) > cap:
                if is_summary_request and d.get("_primary_document"):
                    doc_snippet = doc_snippet[:cap]
                else:
                    doc_snippet = _chunk_doc(doc_snippet, user_text or "", max_chars=cap, label=d.get('name','cached')[:30])
                d["_content_truncated"] = True
            else:
                doc_snippet = _compress_doc(doc_snippet, cap, label=d.get('name','cached')[:30])
            _add_doc_item(
                d.get("name") or "Untitled",
                _encode_sharepoint_url(d.get("url") or ""),
                doc_snippet,
                total_chars=raw_len,
                truncated=bool(d.get("_content_truncated") or raw_len > len(doc_snippet)),
                primary=bool(d.get("_primary_document")),
            )
            logger.info(
                "Content passed to LLM: %s %s chars of %s total chars | Truncated: %s",
                d.get("name") or "Untitled",
                len(doc_snippet),
                raw_len,
                bool(d.get("_content_truncated") or raw_len > len(doc_snippet)),
            )
    
    def _hydrate_doc_from_cache_if_needed(d: dict) -> dict:
        """Ensure search-result docs carry full cached content when available.

        Graph/search results often contain title + URL + tiny preview only, while
        the local document cache contains the extracted full text. This helper
        hydrates by id or filename before building the LLM prompt.
        """
        try:
            if not isinstance(d, dict):
                return d
            current = (d.get("content") or d.get("snippet") or "").strip()
            title = (d.get("name") or d.get("title") or "").strip()
            logger.info("Hydration check: %s before=%s chars", title or "Untitled", len(current))
            if len(current) >= 1000 and not is_summary_request:
                return d
            cache_obj = get_cache()
            if not cache_obj:
                return d
            # Prefer direct id lookup if the cache exposes it.
            candidates = []
            doc_id = d.get("id") or d.get("document_id")
            if doc_id:
                try:
                    got = cache_obj.get_document(doc_id, cache_user_id or Config.SHAREPOINT_CACHE_USER_ID)
                    if got:
                        candidates.append(got)
                except Exception:
                    pass
                try:
                    got = cache_obj.get_shared_document(doc_id)
                    if got:
                        candidates.append(got)
                except Exception:
                    pass
            # Fallback: search by title/name and take exact filename match.
            if title and hasattr(cache_obj, "search_cache_scored"):
                try:
                    scored_docs = cache_obj.search_cache_scored(title, user_id=cache_user_id or Config.SHAREPOINT_CACHE_USER_ID, limit=5, include_shared=True)
                    for item in scored_docs or []:
                        cd = item.get("doc", {})
                        if (cd.get("name") or cd.get("title") or "").strip().lower() == title.lower():
                            candidates.append(cd)
                except Exception:
                    pass
            for cd in candidates:
                full = (cd.get("content") or "").strip()
                if len(full) > len(current):
                    d["content"] = full
                    d.setdefault("snippet", full[:1000])
                    d.setdefault("url", cd.get("url") or cd.get("webUrl") or d.get("url") or d.get("webUrl") or "")
                    d.setdefault("webUrl", d.get("url") or cd.get("webUrl") or "")
                    d["_from_document_cache"] = True
                    d["_from_sharepoint"] = True
                    logger.info("Hydrated cached content for LLM: %s before=%s after=%s chars", title, len(current), len(full))
                    break
        except Exception as hydrate_err:
            logger.warning(f"Cache hydration failed for doc: {hydrate_err}")
        return d

    # (2) Add COMBINED/AI Search results (second priority - most relevant search results)
    for d in (combined_doc_results or ai_search_results or []):
        if scope != "ai_search":
            d = _hydrate_doc_from_cache_if_needed(d)
        if d.get("_access_denied"):
            continue
        doc_content = d.get("content", "")
        doc_title = d.get("name") or d.get("title") or "Untitled"
        source_query = d.get("_source_query")  # From parallel search
        
        if not doc_content or not doc_content.strip():
            logger.warning(f"âš ï¸  Skipping doc without content for LLM input: {doc_title}")
            continue
        
        # STABILIZATION FIX 2: Block oversized documents entirely
        raw_len = len(doc_content)
        if raw_len > 500_000:
            logger.warning(f"ðŸš« BLOCKING oversized doc from LLM: {doc_title} ({raw_len:,} chars) - too large")
            continue
        
        # ISSUE 1+2: Chunk+compress BEFORE prompt assembly
        cap = DOC_SNIPPET_CAP
        if is_summary_request and d.get("_primary_document"):
            cap = max(cap, int(os.getenv("SUMMARY_PRIMARY_DOC_CHARS", "20000")))
        if len(doc_content) > cap:
            if is_summary_request and d.get("_primary_document"):
                doc_content = doc_content[:cap]
            else:
                doc_content = _chunk_doc(doc_content, user_text or "", max_chars=cap, label=f"doc:{doc_title[:30]}")
            d["_content_truncated"] = True
        else:
            doc_content = _compress_doc(doc_content, cap, label=f"doc:{doc_title[:30]}")
        content_len = len(doc_content)
        content_preview = doc_content[:100].replace("\n", " ") + ("..." if content_len > 100 else "")
        
        # Add source query info to title for parallel searches
        if source_query and parallel_results:
            display_title = f"{doc_title} (from '{source_query}' search)"
            logger.info(f"ðŸ“ Adding to LLM input: {display_title} | {content_len} chars (capped) | {content_preview}")
        else:
            display_title = doc_title
            logger.info(f"ðŸ“ Adding to LLM input: {doc_title} | {content_len} chars (capped) | {content_preview}")
        
        _add_doc_item(
            display_title,
            _encode_sharepoint_url(d.get("file_path") or d.get("url") or d.get("webUrl") or ""),
            doc_content,
            total_chars=raw_len,
            truncated=bool(d.get("_content_truncated") or raw_len > content_len),
            primary=bool(d.get("_primary_document")),
        )
        logger.info(
            "Content passed to LLM: %s %s chars of %s total chars | Truncated: %s",
            display_title,
            content_len,
            raw_len,
            bool(d.get("_content_truncated") or raw_len > content_len),
        )
    
    # (3) Add WEB RESULTS last (lowest priority - only if no documents found)
    # Skip web results entirely if we have document results - they're less reliable
    has_doc_results = bool(combined_doc_results or ([] if scope == "ai_search" else cached_results))
    if web_results and not has_doc_results:
        logger.info(f"âš ï¸  No documents found - adding {len(web_results)} web result(s) as fallback)")
        for w in web_results:
            w_content = w.get("content", "")
            w_title = w.get("title") or w.get("url") or "Untitled Web Page"
            if not w_content:
                continue
            # Chunk/Compress web content
            from utils.context_budget import compress_for_llm as _compress_web, select_relevant_chunks as _chunk_web
            if len(w_content) > DOC_SNIPPET_CAP:
                w_content = _chunk_web(w_content, user_text or "", max_chars=DOC_SNIPPET_CAP, label=f"web:{w_title[:30]}")
            else:
                w_content = _compress_web(w_content, DOC_SNIPPET_CAP, label=f"web:{w_title[:30]}")
            
            _add_doc_item(f"{w_title} ðŸŒ", w.get("url") or "", w_content)
        logger.info(f"Added {len(web_results)} web result(s) to model_doc_items (at end, low priority)")
    elif web_results and has_doc_results:
        logger.info(f"âœ… Document results found - skipping {len(web_results)} web result(s) to prioritize document relevance")
    
    # For follow-up questions, reload previous sources into context
    # so the LLM has the data it discussed previously.
    # CRITICAL: never reload old sources when a file was uploaded THIS turn —
    # that was the root cause of the "public health messages" hallucination where
    # ATTACHMENT ISOLATION cleared model_doc_items but this block re-added 3
    # completely unrelated docs from a previous session.
    if not model_doc_items and action == "refine_previous" and not attachments and not attachment_texts:
        _prev_source_docs = conversation_last_sources.get(conversation_id, [])
        if _prev_source_docs:
            logger.info(f"Follow-up: reloading {len(_prev_source_docs)} previous source(s) into context")
            for prev_doc in _prev_source_docs:
                if prev_doc.get("snippet"):
                    _add_doc_item(
                        prev_doc.get("title", "Untitled"),
                        prev_doc.get("url", ""),
                        prev_doc.get("snippet", ""),
                        total_chars=prev_doc.get("total_chars") or len(prev_doc.get("snippet", "") or ""),
                        truncated=bool(prev_doc.get("truncated")),
                        primary=bool(prev_doc.get("primary")),
                    )

    logger.info(f"model_doc_items built: {len(model_doc_items)} item(s) | titles={[d['title'] for d in model_doc_items[:5]]}")

    # Retrieval audit trail — who asked, which source answered, how many chunks.
    _audit_source = (
        "upload" if (attachments or has_cached_attachments)
        else ("none" if not model_doc_items else (scope or "ai_search"))
    )
    audit_log.info(
        "RETRIEVAL | user=%s | source=%s | chunks=%d | trimming=%s | query=%s",
        aad_id or "unknown",
        _audit_source,
        len(model_doc_items),
        Config.ENABLE_SECURITY_TRIMMING,
        (user_text or "")[:80],
    )

    # No last-chance search for general responses; avoid injecting documents into
    # unrelated questions that should be answered directly.

    if model_doc_items:
        total_content_chars = sum(len(d.get('snippet', '')) for d in model_doc_items)
        logger.info(f"ðŸ”¢ Total content for LLM: {total_content_chars} chars across {len(model_doc_items)} documents")
    # Persist last sources so follow-up questions know what was discussed
    # Include snippet content so follow-ups can reuse it
    try:
        if model_doc_items:
            conversation_last_sources[conversation_id] = [
                {
                    "title": d.get("title", ""),
                    "url": d.get("url", ""),
                    "snippet": d.get("snippet", ""),
                    "total_chars": d.get("total_chars") or len(d.get("snippet", "") or ""),
                    "included_chars": d.get("included_chars") or len(d.get("snippet", "") or ""),
                    "truncated": bool(d.get("truncated")),
                    "primary": bool(d.get("primary")),
                }
                for d in model_doc_items
            ]
    except Exception:
        pass
    # Guard: if LLM requested search but no content is available, ask for the document
    try:
        source_required = bool(route.get("source_required") or action in ("search_documents", "refine_previous"))
        guard = before_llm(
            {
                "source_required": source_required,
                "query": route.get("query") or user_text,
            },
            {"sources": model_doc_items},
            attachment_texts,
        )
        if not guard.allowed:
            await typing_mgr.stop_refresh()
            await deliver_final(ctx, guard.response, is_group=is_group)
            return
    except Exception:
        pass

    # Send typing indicator before LLM processing
    await send_typing_indicator(ctx)
    
    llm_input, llm_log = build_llm_input(
        user_text=user_text or "",
        attachment_texts=attachment_texts,
        doc_items=model_doc_items,
        personalization=personalization,
        memory_text=_conversation_source_summary,
        is_summary_request=is_document_summary_request(user_text or ""),
    )
    
    # â”€â”€ ISSUE 9: Log final prompt size â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info(f"Final Prompt Chars: {len(llm_input):,}")
    logger.info(f"Estimated Tokens: {estimate_tokens(llm_input):,}")
    logger.info(f"LLM input sizes: {llm_log['sizes']} | docs={llm_log['doc_count']} | actions={llm_log['truncation_actions']}")
    
    # â”€â”€ ISSUE 4+8: Token-aware gatekeeper (FINAL safety net) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # This loop iteratively removes lowest-priority blocks until
    # prompt fits within MAX_PROMPT_TOKENS. Runs IMMEDIATELY before LLM call.
    try:
        from utils.context_budget import token_gatekeeper
        MAX_PROMPT_TOKENS = int(getattr(Config, "MAX_PROMPT_TOKENS_APPROX", 40000))
        pre_gate_chars = len(llm_input)
        llm_input = token_gatekeeper(llm_input, max_tokens=MAX_PROMPT_TOKENS)
        if len(llm_input) != pre_gate_chars:
            logger.warning(
                f"ðŸš§ token_gatekeeper reduced prompt: {pre_gate_chars:,} â†’ {len(llm_input):,} chars "
                f"(~{estimate_tokens(llm_input):,} tokens)"
            )
        # Final log after gatekeeper
        logger.info(f"POST-GATEKEEPER Final Prompt Chars: {len(llm_input):,}")
        logger.info(f"POST-GATEKEEPER Estimated Tokens: {estimate_tokens(llm_input):,}")
    except Exception as gate_err:
        logger.error(f"token_gatekeeper error: {gate_err}")

    # CALCULATION INTERCEPTOR: use deterministic pandas logic for spreadsheet math.
    # The LLM is still used for narrative/report writing, but counts, totals,
    # averages, rankings, and categorical breakdowns should be computed first.
    calculation_result = None
    try:
        is_calc_request, _calc_type = detect_calculation_intent(user_text or "")
        if is_calc_request:
            calc_files: list[dict] = []

            for f in current_attachment_files:
                if f.get("content"):
                    calc_files.append({"name": f.get("name", "attachment"), "content": f.get("content", "")})

            if not calc_files and cache_user_id:
                cached_calc_files = await asyncio.to_thread(
                    get_conversation_attachments,
                    conversation_id,
                    True,
                    cache_user_id,
                )
                for f in cached_calc_files or []:
                    if f.get("content"):
                        calc_files.append({"name": f.get("filename") or f.get("name", "attachment"), "content": f.get("content", "")})

            if len(calc_files) >= 2:
                calculation_result = await asyncio.to_thread(
                    process_multi_file_calculation,
                    user_text or "",
                    calc_files,
                )
            elif len(calc_files) == 1:
                calculation_result = await asyncio.to_thread(
                    process_calculation_request,
                    user_text or "",
                    calc_files[0].get("content", ""),
                    calc_files[0].get("name", ""),
                )

            if calculation_result:
                logger.info("Deterministic calculation response generated before LLM")
                await typing_mgr.stop_refresh()
                await deliver_final(ctx, calculation_result, is_group=is_group)
                return
    except Exception as calc_err:
        logger.warning(f"Deterministic calculation interceptor failed; falling back to LLM: {calc_err}")

    # Skip secondary token budget enforcement - build_llm_input already handles truncation
    # Azure OpenAI will handle final token limits gracefully
    # The build_llm_input function already truncated content appropriately

    # â”€â”€ CODE INTERPRETER (Phase 1) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Expose the run_python tool when the user wants computation, data manipulation,
    # charting, or document generation. The model decides when to call it; any files
    # it produces are delivered to the user after the response completes.
    interpreter_turn = None
    interpreter_tools = None
    try:
        _enable_interp = _should_enable_interpreter(user_text or "", bool(interpreter_input_files))
        if _enable_interp:
            interpreter_turn = InterpreterTurn(input_files=dict(interpreter_input_files))
            interpreter_tools = build_interpreter_tools(interpreter_turn)
            logger.info(
                f"ðŸ§® Code interpreter ENABLED for this turn "
                f"(input_files={len(interpreter_input_files)}, tools={len(interpreter_tools)})"
            )
    except Exception as _interp_err:
        logger.warning(f"Failed to enable code interpreter: {_interp_err}")
        interpreter_turn = None
        interpreter_tools = None

    # â”€â”€ LIVE MICROSOFT 365 (GRAPH) + IMAGE TOOLS (always-on) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # These act in the user's Microsoft 365 (mail/calendar/planner/files) and can
    # generate images. They are exposed every turn; the model decides when to call.
    graph_ctx = None
    graph_tools = None
    if _GRAPH_TOOLS_AVAILABLE:
        try:
            _sender = getattr(ctx.activity, "from_", None)
            _graph_user_id = (
                getattr(_sender, "aad_object_id", None)
                or getattr(_sender, "aadObjectId", None)
                or getattr(_sender, "id", None)
                or ""
            )
            if _graph_user_id:
                graph_ctx = GraphToolContext(
                    user_id=str(_graph_user_id),
                    conversation_id=conversation_id,
                    display_name=getattr(_sender, "name", "") or "",
                )
                graph_tools = build_graph_tools(graph_ctx)
                logger.info(f"ðŸŒ Microsoft 365 tools ENABLED for this turn (tools={len(graph_tools)})")
            else:
                logger.info("Microsoft 365 tools skipped: no user object id on activity")
        except Exception as _graph_err:
            logger.warning(f"Failed to enable Microsoft 365 tools: {_graph_err}")
            graph_ctx = None
            graph_tools = None

    _combined_tools = list(interpreter_tools or []) + list(graph_tools or [])
    chat_prompt = ChatPrompt(model, functions=_combined_tools) if _combined_tools else ChatPrompt(model)

    # When the interpreter is active, tell the model how/when to use it.
    _effective_instructions = BASE_INSTRUCTIONS
    if interpreter_tools:
        _effective_instructions = (_effective_instructions or "") + (
            "\n\n## CODE INTERPRETER\n"
            "You have a `run_python` tool that executes Python in a secure sandbox "
            "(pandas, numpy, openpyxl, matplotlib, python-docx, python-pptx, reportlab, "
            "Pillow, pypdf; NO internet). Use it to:\n"
            "- Do accurate calculations, statistics, and data analysis on uploaded files "
            "(never guess numbers — compute them).\n"
            "- Create charts/visualizations and SAVE them as image files.\n"
            "- Generate documents the user asks for: .xlsx, .docx, .pptx, .pdf, .csv, "
            ".txt, .zip — save them to the current directory.\n"
            "Uploaded files are already in the working directory under their original "
            "filenames. After a tool run, briefly explain what you found or created. "
            "Generated files are delivered to the user automatically — do NOT fabricate "
            "download links or claim a file exists unless run_python actually created it."
        )
    if graph_tools:
        try:
            _now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            _now_iso = ""
        _effective_instructions = (_effective_instructions or "") + graph_tools_instructions(_now_iso)

    _artifacts_delivered = {"done": False}

    def _collect_turn_artifacts():
        arts = []
        if interpreter_turn and interpreter_turn.artifacts:
            arts.extend(interpreter_turn.artifacts)
        if graph_ctx and graph_ctx.artifacts:
            arts.extend(graph_ctx.artifacts)
        return arts

    async def deliver_interpreter_artifacts():
        """Send files produced this turn (run_python docs, generated images) to the user."""
        if _artifacts_delivered["done"]:
            return
        _artifacts_delivered["done"] = True
        artifacts = _collect_turn_artifacts()
        if not artifacts:
            return
        try:
            _img_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
            lines = ["**Generated file(s):**", ""]
            for art in artifacts:
                size_kb = max(1, art.size // 1024)
                url = art.url
                is_image = art.filename.lower().endswith(_img_exts)
                if url.startswith("http"):
                    if is_image:
                        # Render inline; Teams shows image URLs in markdown.
                        lines.append(f"![{art.filename}]({url})")
                        lines.append(f"- [{art.filename}]({url}) ({size_kb} KB)")
                    else:
                        lines.append(f"- [{art.filename}]({url}) ({size_kb} KB)")
                else:
                    # No absolute base URL configured; show filename only.
                    lines.append(f"- {art.filename} ({size_kb} KB)")
            msg = "\n".join(lines)
            await ctx.send(MessageActivityInput(text=msg).add_ai_generated())
            logger.info(f"ðŸ“Ž Delivered {len(artifacts)} artifact(s)")
        except Exception as _deliver_err:
            logger.warning(f"Failed to deliver interpreter artifacts: {_deliver_err}")

    
    # Throttle streaming to prevent Bot Framework 429 errors
    last_emit_time = 0
    chunk_buffer = []
    full_response_chunks: list[str] = []  # accumulate ALL chunks to detect cited references
    MIN_CHUNK_INTERVAL = config.STREAM_CHUNK_INTERVAL  # From config (default 300ms)
    
    async def throttled_emit(chunk: str):
        """Emit chunks with rate limiting to prevent Bot Framework 429 errors"""
        nonlocal last_emit_time, chunk_buffer
        
        full_response_chunks.append(chunk)  # always keep a copy
        
        # GROUP CHAT FIX: Skip streaming for group chats to avoid 405 errors
        if is_group:
            # For group chats, just buffer chunks - don't attempt streaming during generation
            chunk_buffer.append(chunk)
            return
        
        # For personal chats, continue with normal streaming logic
        chunk_buffer.append(chunk)
        current_time = time.time()
        time_since_last = current_time - last_emit_time
        
        # If enough time has passed, emit buffered chunks
        if time_since_last >= MIN_CHUNK_INTERVAL:
            if chunk_buffer:
                combined = "".join(chunk_buffer)
                try:
                    ctx.stream.emit(combined)
                    chunk_buffer = []
                    last_emit_time = current_time
                except Exception as stream_error:
                    # Log streaming errors but don't fail - chunks will be sent at end
                    logger.warning(f"âš ï¸ Streaming emit failed in personal chat: {stream_error}")
                    # Keep chunks in buffer for final emission
        # Otherwise, buffer chunks and they'll be emitted in next interval or at end
    
    # Use Teams SDK built-in streaming with retry logic and throttling.
    # Reuse the persistent typing manager already started during the search phase
    # so the indicator stays continuous from the moment the user sent their message.
    try:
        # Ensure typing is running (it normally already is from the search phase).
        typing_mgr.set_status("Reasoning through your answer...")
        if not getattr(typing_mgr, "should_refresh", False):
            await typing_mgr.start_periodic_refresh(interval=3.0)
                
        # GROUP CHAT DEBUGGING: Log before making chat call
        logger.info(f"ðŸš€ Starting LLM call for conversation: {conversation_id[:20]}... (IsGroup: {is_group})")
        
        # NUCLEAR: Get memory with hardcoded enforcement BEFORE send
        conversation_memory = get_or_create_conversation_memory(conversation_id)
        max_turns = int(getattr(Config, "MAX_MEMORY_TURNS", 8))
        max_messages = max_turns * 2
        if get_conversation_summary_text(conversation_id):
            max_messages = min(
                max_messages,
                int(os.getenv("MAX_MEMORY_MESSAGES_WITH_SUMMARY", "2")),
            )
        
        # NUCLEAR FAILSAFE: Emergency clear if exceeded even by 1 message
        # Uses _get_memory_items/_set_memory_items to access ListMemory's REAL storage
        _mem_items = _get_memory_items(conversation_memory)
        if _mem_items:
            msg_count = len(_mem_items)
            logger.info(f"ðŸ“Š Pre-LLM memory: {msg_count} messages (limit {max_messages})")
            if msg_count > max_messages:
                safe_count = max(2, max_messages // 2)  # Cut to 50% minimum 2 messages
                logger.error(f"âŒ NUCLEAR FAILSAFE: {msg_count} > {max_messages}. Emergency cutting to {safe_count}")
                _set_memory_items(conversation_memory, _mem_items[-safe_count:])
                logger.error(f"âœ‚ï¸ Cut to {safe_count} messages to prevent token overflow")
            # Also cap individual message sizes â€” each message limited to 4000 chars
            _MEM_MSG_CAP = 4000
            _capped_items = _get_memory_items(conversation_memory)
            _total_mem_chars = 0
            for _mi, _item in enumerate(_capped_items):
                _content = getattr(_item, 'content', None) or ''
                if len(_content) > _MEM_MSG_CAP:
                    _item.content = _content[:_MEM_MSG_CAP] + '... [trimmed]'
                    logger.info(f"âœ‚ï¸ Capped memory message {_mi} from {len(_content)} to {_MEM_MSG_CAP} chars")
                _total_mem_chars += len(getattr(_item, 'content', '') or '')
            logger.info(f"ðŸ“ Total memory chars for LLM: {_total_mem_chars:,} across {len(_capped_items)} messages")
            
            # TOTAL TOKEN BUDGET CHECK: instructions + memory + prompt
            _instructions_chars = len(BASE_INSTRUCTIONS or '')
            _prompt_chars = len(llm_input or '')
            _grand_total_chars = _instructions_chars + _total_mem_chars + _prompt_chars
            _grand_total_tokens = _grand_total_chars // 4
            logger.info(
                f"ðŸŽ¯ GRAND TOTAL estimate before LLM: {_grand_total_chars:,} chars (~{_grand_total_tokens:,} tokens) "
                f"[instructions={_instructions_chars:,} + memory={_total_mem_chars:,} + prompt={_prompt_chars:,}]"
            )
            if _grand_total_tokens > 250_000:
                logger.error(f"ðŸš¨ DANGER: Estimated {_grand_total_tokens:,} tokens > 250K limit! Emergency memory wipe!")
                _set_memory_items(conversation_memory, [])
                _total_mem_chars = 0
                _grand_total_chars = _instructions_chars + _prompt_chars
                logger.error(f"ðŸ§¹ Memory wiped. New total: {_grand_total_chars:,} chars (~{_grand_total_chars // 4:,} tokens)")
        
        async def make_chat_call():
            async with llm_semaphore:
                return await chat_prompt.send(
                    input=llm_input,
                    memory=None,
                    instructions=_effective_instructions,
                    on_chunk=throttled_emit,
                )
        
        # Make LLM call with retry logic
        chat_result = await call_llm_with_retry(make_chat_call)
        
        # CRITICAL: Guard against empty responses
        full_response_text = "".join(full_response_chunks).strip()
        if not full_response_text or full_response_text == "":
            # SDK bug: generate_text() recurses for tool-call continuations but
            # does NOT pass on_chunk, so streaming never fires for the final response
            # after a function call.  The answer IS in chat_result.response.content.
            _fallback_text = ""
            try:
                _fallback_text = (
                    (chat_result.response.content or "").strip()
                    if chat_result and getattr(chat_result, "response", None)
                    else ""
                )
            except Exception:
                pass
            if _fallback_text:
                logger.info(
                    f"📌 Recovered tool-call response from chat_result "
                    f"({len(_fallback_text)} chars) — SDK on_chunk not called for continuation"
                )
                full_response_text = _fallback_text
                full_response_chunks.append(_fallback_text)
            else:
                logger.error("ðŸš¨ CRITICAL: LLM returned empty response - sending fallback message")
                empty_msg = "I encountered an issue generating a response. Please try again."
                await typing_mgr.stop_refresh()
                await deliver_final(ctx, empty_msg, is_group=is_group)
                return
        
        def _source_citation(idx: int, source: dict) -> str:
            url = (source.get("url") or "").strip()
            if url and url.startswith(("http://", "https://")):
                return f"[[{idx}]]({url})"
            return f"[[{idx}]]"

        def _cited_source_indices(text: str, source_count: int) -> set[int]:
            cited: set[int] = set()
            for idx in range(1, source_count + 1):
                if re.search(rf"(\[\[{idx}\]\]|\[{idx}\])", text or ""):
                    cited.add(idx)
            return cited

        def _reference_lines(sources: list[dict], cited_indices: set[int]) -> list[str]:
            ref_lines = []
            for idx, d in enumerate(sources, 1):
                if idx not in cited_indices:
                    continue
                title = (d.get("title") or "Untitled").strip()
                url = (d.get("url") or "").strip()
                if url and url.startswith(("http://", "https://")):
                    ref_lines.append(f"[{idx}] [{title}]({url})")
                elif url:
                    ref_lines.append(f"[{idx}] {title}\n    URL: {url}")
                else:
                    ref_lines.append(f"[{idx}] {title}")
            return ref_lines

        def _ensure_source_citation(text: str, sources: list[dict]) -> tuple[str, set[int]]:
            cited = _cited_source_indices(text, len(sources))
            if cited or not sources:
                return text, cited
            return text, cited

        # GROUP CHAT DEBUGGING: Log after LLM call  
        logger.info(f"âœ… LLM call completed ({len(full_response_text)} chars), emitting final chunks for conversation: {conversation_id[:20]}...")
        
        # ðŸ›‘ STOP TYPING BEFORE FINAL EMISSION: Prevent 429 rate limiting from concurrent requests
        await typing_mgr.stop_refresh()
        # Brief pause to let Teams API catch up after stopping typing indicator
        await asyncio.sleep(0.3)
        
        # GROUP CHAT FIX: Use regular messages instead of streaming for group chats
        if is_group:
            # For group chats, send complete response as regular message (avoid streaming 405 errors)
            logger.info(f"ðŸ“¢ GROUP CHAT: Sending complete response as regular message (bypassing streaming)")
            try:
                full_text = "".join(full_response_chunks)
                if chunk_buffer:
                    full_text += "".join(chunk_buffer)
                    chunk_buffer = []
                
                # Handle references for group chats too (before sending)
                allow_references = bool(model_doc_items) and (
                    action in ("search_documents", "refine_previous") or attachments or attachment_context
                )
                if allow_references:
                    full_text, cited_indices = _ensure_source_citation(full_text, model_doc_items)
                    ref_lines = _reference_lines(model_doc_items, cited_indices)
                    
                    if ref_lines:
                        refs_block = "\n\n---\n**References:**\n\n" + "\n\n".join(ref_lines) + "\n"
                        full_text += refs_block
                        logger.info(f"Added references footer with {len(ref_lines)} source(s) to group chat message")
                    else:
                        logger.info("No cited references detected for group chat")
                else:
                    logger.info("References disabled for group chat non-document response")
                
                await ctx.send(MessageActivityInput(text=full_text).add_ai_generated())
                logger.info(f"âœ… Group chat message sent successfully ({len(full_text)} chars)")
                
                # Log response for debugging
                response_text = str(chat_result).strip() if chat_result else "(empty)"
                logger.info(f"Group chat response completed | Length: {len(response_text)} chars | Preview: {response_text[:100]}...")
                update_conversation_summary(
                    conversation_id=conversation_id,
                    user_text=user_text,
                    assistant_text=full_text,
                    route=route,
                    source_names=[d.get("title") or d.get("name") or "" for d in model_doc_items],
                )
                
                # Deliver any files produced by the code interpreter
                await deliver_interpreter_artifacts()

                # Return early to prevent duplicate messages from normal flow
                return
                
            except Exception as group_send_error:
                if "405" in str(group_send_error) and "Method Not Allowed" in str(group_send_error):
                    logger.error(f"ðŸš¨ GROUP CHAT 405 ERROR: {group_send_error}")
                    logger.error(f"ðŸ’¡ Bot not properly installed in group chat. Try: Remove bot â†’ Re-add bot â†’ @mention bot")
                logger.error(f"âŒ Group chat message failed: {group_send_error}")
                raise
        else:
            # For personal chats, use normal streaming
            if chunk_buffer:
                combined = "".join(chunk_buffer)
                try:
                    # Add small delay before final emission to prevent 429 rate limiting
                    await asyncio.sleep(0.3)
                    ctx.stream.emit(combined)
                    # Add delay after emission to let Teams process
                    await asyncio.sleep(0.5)
                    logger.info(f"âœ… Final chunks emitted successfully ({len(combined)} chars)")
                except Exception as emit_error:
                    logger.error(f"âŒ FAILED to emit final chunks in personal chat: {emit_error}")
                    
                    # Fallback: Try sending as regular message instead of streaming
                    try:
                        await ctx.send(MessageActivityInput(text=full_response_text).add_ai_generated())
                        logger.info(f"âœ… Sent as regular message instead (fallback)")
                    except Exception as fallback_error:
                        logger.error(f"âŒ Fallback message send also failed: {fallback_error}")
                        raise
                chunk_buffer = []
            else:
                # CRITICAL: Even if chunk_buffer is empty, we MUST send the response
                # This handles cases where streaming didn't emit or LLM didn't chunk response
                try:
                    logger.info(f"ðŸ“¤ No buffered chunks, streaming complete response ({len(full_response_text)} chars)")
                    # Emit through the stream (not a separate ctx.send) so the
                    # "Working on it..." informative status is finalized in place
                    # instead of being left dangling.
                    await deliver_final(ctx, full_response_text, is_group=False)
                    logger.info(f"âœ… Complete response sent successfully")
                except Exception as send_error:
                    logger.error(f"âŒ FATAL: Failed to send response: {send_error}")
                    # Last resort: send error message
                    try:
                        await ctx.send(MessageActivityInput(text=f"I generated a response but couldn't deliver it: {str(send_error)[:100]}").add_ai_generated())
                    except:
                        pass
                    raise
        
        # Append numbered references footer only for document-driven responses
        allow_references = bool(model_doc_items) and (
            action in ("search_documents", "refine_previous") or attachments or attachment_context
        )
        if allow_references:
            # Reconstruct the full LLM response to detect which [N] markers it used
            full_response_text = "".join(full_response_chunks)
            cited_response_text, cited_indices = _ensure_source_citation(full_response_text, model_doc_items)
            ref_lines = _reference_lines(model_doc_items, cited_indices)
            if ref_lines:
                refs_block = "\n\n---\n**References:**\n\n" + "\n\n".join(ref_lines) + "\n"
                # Add delay before references to prevent 429 rate limiting
                await asyncio.sleep(0.5)
                ctx.stream.emit(refs_block)
                logger.info(f"Appended references footer with {len(ref_lines)} source(s) (cited out of {len(model_doc_items)} total)")
            else:
                logger.info("No cited references detected â€” skipping references footer")
        else:
            logger.info("References disabled for non-document response")
        
        # Log response for debugging
        try:
            response_text = str(chat_result).strip() if chat_result else "(empty)"
            logger.info(f"Chat response completed | Length: {len(response_text)} chars | Preview: {response_text[:100]}...")
        except Exception:
            logger.info("Chat response completed")
        update_conversation_summary(
            conversation_id=conversation_id,
            user_text=user_text,
            assistant_text=full_response_text,
            route=route,
            source_names=[d.get("title") or d.get("name") or "" for d in model_doc_items],
        )
        
        # Deliver any files produced by the code interpreter this turn
        await deliver_interpreter_artifacts()
        
        # Save conversation memory after response for instant recovery on next turn
        # âœ… SIMPLIFIED: No manual memory saving needed with simple approach
        # The ChatPrompt automatically handles memory management
        
        # This ensures we don't cache all searched documents, only the ones that provided value
        try:
            # SECURITY: Only cache documents that were actually included in the response AND passed security checks
            graph_docs_to_cache = []
            if action == "search_documents" and combined_doc_results:
                # CRITICAL SECURITY: Only cache documents that passed initial filtering
                # Never cache documents that were filtered out during search
                filtered_doc_names = set()
                try:
                    # Track which documents were actually included in the response
                    # Use combined_doc_results (list of dicts), NOT doc_entries (list of formatted strings)
                    included_docs = {
                        (d.get("name") or d.get("title"))
                        for d in combined_doc_results
                        if isinstance(d, dict) and (d.get("name") or d.get("title"))
                    }
                    
                    # CRITICAL SECURITY: Check ALL documents in search results, not just Graph
                    # This includes documents from cache, web, and other sources that were shown to user
                    for doc in (ai_search_results or []):
                        doc_name = doc.get("name") or doc.get("title")
                        doc_url = doc.get("webUrl") or doc.get("url") or ""
                        
                        # Skip if not a Graph document (cache docs shouldn't be re-cached)
                        # But DO verify permissions for documents from cache that were shown to user
                        is_from_graph = doc.get("_from_live_graph", False)
                        is_from_cache = doc.get("_from_cache", False)
                        
                        # SECURITY CHECK 1: Must have been included in actual response to user
                        if doc_name not in included_docs:
                            filtered_doc_names.add(doc_name or "unknown")
                            continue
                        
                        # SECURITY CHECK 2: Re-verify access permissions for ALL document sources
                        from knowledge_base import is_url_accessible_by_user
                        if doc_url and not is_url_accessible_by_user(doc_url, user_email or "", user_context=False):
                            logger.info(f"ðŸ”’ SECURITY: Blocking document user cannot access: {doc_name} (source: {'graph' if is_from_graph else 'cache' if is_from_cache else 'other'})")
                            filtered_doc_names.add(doc_name or "unknown")
                            continue
                            
                        # SECURITY CHECK 3: User email must be available for personal OneDrive documents
                        if doc_url and "/personal/" in doc_url.lower() and not user_email:
                            logger.warning(f"ðŸ”’ SECURITY: Blocking personal OneDrive due to missing user email: {doc_name}")
                            filtered_doc_names.add(doc_name or "unknown")
                            continue
                        
                        # SECURITY CHECK 4: Only cache Graph documents (don't re-cache what's already cached)
                        if not is_from_graph:
                            # Document from cache was shown to user and passed verification
                            # No need to cache again, but log that it was verified
                            if is_from_cache:
                                logger.debug(f"âœ“ Verified cached document shown to user: {doc_name}")
                            continue
                            
                        # Passed all security checks - safe to cache
                        graph_docs_to_cache.append(doc)
                        
                    if filtered_doc_names:
                        logger.info(f"ðŸ”’ Security filtered {len(filtered_doc_names)} document(s) from caching: {', '.join(list(filtered_doc_names)[:3])}")
                        
                except Exception as filter_err:
                    logger.error(f"Error during cache security filtering: {filter_err}")
                    graph_docs_to_cache = []  # Fail closed - don't cache anything if filtering fails
            
            if graph_docs_to_cache:
                logger.info(f"ðŸ“¦ Post-response caching: Found {len(graph_docs_to_cache)} Graph documents actually used in response")
                
                # Simple heuristic: Cache all returned documents from Graph since they were shown to user
                # In a more sophisticated implementation, we could parse the response to see which citations were used
                token = await asyncio.to_thread(get_graph_token, user_assertion)
                if token and cache_user_id and user_email:  # SECURITY: Require user email for caching
                    cache = get_cache()
                    cached_count = 0
                    cache_sem = asyncio.Semaphore(3)
                    
                    def _is_personal_url(u: str) -> bool:
                        if not u:
                            return False
                        ul = u.lower()
                        return ("/personal/" in ul) or ("my.sharepoint.com/personal" in ul)
                    
                    async def _download_for_cache(doc: dict):
                        async with cache_sem:
                            name = doc.get("name") or doc.get("title") or "Untitled"
                            web_url = doc.get("webUrl") or doc.get("url") or ""
                            drive_id = doc.get("driveId") or ""
                            item_id = doc.get("itemId") or ""
                            existing_content = doc.get("content", "")
                            
                            # TRIPLE SECURITY CHECK: Re-verify all access permissions before download
                            from knowledge_base import is_url_accessible_by_user
                            
                            # Check 1: General URL access permission
                            if not is_url_accessible_by_user(web_url, user_email, user_context=False):
                                logger.info(f"ðŸš« SECURITY: Cache blocked - user cannot access: {name}")
                                return None, None
                                
                            # Check 2: Personal OneDrive strict validation
                            if "/personal/" in web_url.lower():
                                from knowledge_base import _extract_owner_from_personal_url
                                owner = _extract_owner_from_personal_url(web_url)
                                if owner:
                                    user_normalized = user_email.lower().replace("@", ".").replace(".", "_")
                                    owner_normalized = owner.lower().replace("@", ".").replace(".", "_")
                                    if owner_normalized != user_normalized:
                                        logger.warning(f"ðŸš« SECURITY: Cache blocked - personal OneDrive owner mismatch: {name} (owner: {owner}, user: {user_email})")
                                        return None, None
                                        
                            # Check 3: User must be authenticated with email for any download
                            if not user_email:
                                logger.warning(f"ðŸš« SECURITY: Cache blocked - no user email for authentication: {name}")
                                return None, None
                            
                            if existing_content and len(existing_content.strip()) >= 50:
                                return doc, existing_content
                            content = await asyncio.to_thread(download_and_extract_content, web_url, token, name, drive_id, item_id)
                            return doc, content

                    tasks = [asyncio.create_task(_download_for_cache(doc)) for doc in graph_docs_to_cache]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for result in results:
                        if isinstance(result, Exception):
                            logger.warning("Failed to cache document (download error)")
                            continue
                        if result == (None, None):  # Filtered out for security
                            continue
                        doc, content = result
                        try:
                            name = doc.get("name") or doc.get("title") or "Untitled"
                            web_url = doc.get("webUrl") or doc.get("url") or ""
                            drive_id = doc.get("driveId") or ""
                            item_id = doc.get("itemId") or ""
                            
                            # Skip caching if content indicates access failure
                            if is_inaccessible_content(content):
                                logger.info(f"â­ï¸  Skipping cache for inaccessible document: {name}")
                                continue
                            
                            # Only cache if content is valid
                            if content and len(content.strip()) >= 10:
                                # Use composite id for stability
                                doc_id = f"{drive_id}:{item_id}" if drive_id and item_id else (web_url or name)
                                
                                # Extract owner info for metadata (helps identify shared documents)
                                from knowledge_base import _extract_owner_from_personal_url
                                doc_owner = _extract_owner_from_personal_url(web_url) if web_url else None
                                is_teams_chat = "/microsoft teams chat files/" in (web_url or "").lower() or "/microsoft%20teams%20chat%20files/" in (web_url or "").lower()
                                
                                # Build metadata with ownership info and strict user isolation
                                base_metadata = {
                                    "source": "graph_search_used",
                                    "owner": doc_owner if doc_owner else "unknown",
                                    "is_teams_chat_file": is_teams_chat,
                                    "cached_for_user": user_email,  # SECURITY: Track which user cached this
                                    "cached_user_id": cache_user_id,  # SECURITY: Also track by user ID
                                    "access_verified": True          # SECURITY: Mark as access-verified
                                }
                                
                                # For CSV files, chunk the content for better search granularity
                                is_csv = name.lower().endswith('.csv')
                                if cache and is_csv:
                                    chunks = chunk_csv_for_cache(content, name)
                                    for chunk_id, chunk_content in chunks:
                                        chunk_metadata = {**base_metadata, "is_csv_chunk": True, "chunk_id": chunk_id}
                                        cache.add_document(
                                            f"{doc_id}:{chunk_id}",
                                            f"{name} (chunk)",
                                            web_url,
                                            chunk_content,
                                            user_id=cache_user_id,
                                            metadata=chunk_metadata
                                        )
                                elif cache:
                                    # Non-CSV files cached normally with strict user isolation
                                    cache.add_document(
                                        doc_id, 
                                        name, 
                                        web_url, 
                                        content, 
                                        user_id=cache_user_id,  # CRITICAL: User-specific cache
                                        metadata=base_metadata
                                    )
                                else:
                                    logger.warning("Cache is None - skipping document caching")
                                
                                cached_count += 1
                                logger.info(f"Cached document: {name} (from Graph live search)")
                        except Exception as cache_err:
                            logger.warning(f"Failed to cache document {doc.get('name', 'unknown')}: {cache_err}")
                    
                    if cached_count > 0:
                        logger.info(f"âœ“ Post-response caching completed: {cached_count}/{len(graph_docs_to_cache)} documents cached for user {user_email}")
                        
                        # SECURITY: Periodic security audit (every N cache operations)
                        global _security_audit_counter
                        _security_audit_counter += 1
                        
                        if _security_audit_counter >= _SECURITY_AUDIT_FREQUENCY:
                            _security_audit_counter = 0  # Reset counter
                            try:
                                if cache:
                                    audit = cache.security_audit()
                                    violations = audit.get("violations", [])
                                    if violations:
                                        logger.warning(f"ðŸš¨ SECURITY AUDIT: Found {len(violations)} violations - auto-cleaning")
                                        removed_count = cache.clean_security_violations()
                                        logger.warning(f"ðŸ›¡ï¸ SECURITY: Removed {removed_count} violating documents")
                                    else:
                                        logger.info(f"ðŸ›¡ï¸ Periodic security audit passed: {audit.get('total_docs', 0)} documents across {audit.get('total_users', 0)} users")
                            except Exception as audit_err:
                                logger.error(f"Periodic security audit failed: {audit_err}")
                    else:
                        logger.info("Post-response caching: No documents were cached (all failed or already cached)")
                elif not user_email:
                    logger.warning("ðŸ”’ Post-response caching BLOCKED: User email unavailable (security requirement)")
                else:
                    logger.warning("Post-response caching skipped: Graph token or user ID unavailable")
        except Exception as post_cache_err:
            logger.error(f"Post-response caching error: {post_cache_err}", exc_info=False)
        
        # Clean up status messages after response is sent
        if status_activity_ids:
            logger.info(f"Cleaning up {len(status_activity_ids)} status message(s)")
            for activity_id in status_activity_ids:
                try:
                    await ctx.delete_activity(activity_id)
                    logger.debug(f"Deleted status message: {activity_id}")
                except Exception as del_err:
                    logger.debug(f"Failed to delete status message {activity_id}: {del_err}")
            logger.info("âœ“ Status messages cleaned up")

    except Exception as e:
        # ðŸ›‘ STOP TYPING: Stop the persistent indicator when error occurs
        await typing_mgr.stop_refresh()
        
        error_msg = str(e)
        if "429" in error_msg:
            logger.error(f"Rate limit error (Bot Framework API): {e}", exc_info=True)
            await ctx.send(MessageActivityInput(text="âš ï¸ The bot is sending messages too quickly. Please wait a moment and try again.").add_ai_generated())
        else:
            logger.error(f"âŒ CRITICAL CHAT ERROR: {e}", exc_info=True)
            logger.error(f"Error type: {type(e).__name__}")
            logger.error(f"Error details: {error_msg}")
            await ctx.send(MessageActivityInput(text="Sorry, I encountered an error processing your request. Please try again in a moment.").add_ai_generated())
        return
    finally:
        # ðŸ›‘ FINAL CLEANUP: Ensure typing indicator stops before returning (double safety)
        await typing_mgr.stop_refresh()


# ---------------------------
# Event handlers
# ---------------------------


@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]):
    try:
        logger.info("=" * 60)
        
        # CRITICAL: Validate basic message structure first
        if not ctx or not ctx.activity:
            logger.error("âŒ CRITICAL: Received null or invalid activity context")
            return
        
        user_text = (ctx.activity.text or "").strip()
        if not user_text:
            logger.warning("âš ï¸ Empty message received - ignoring")
            return
        
        logger.info(f"MESSAGE RECEIVED | Text: {user_text[:100]}")
        logger.info(f"From: {getattr(ctx.activity.from_, 'name', 'Unknown')} | Conv: {ctx.activity.conversation.id if ctx.activity.conversation else 'Unknown'}")
        
        # GROUP CHAT DEBUGGING: Enhanced context logging
        if hasattr(ctx.activity, 'conversation') and ctx.activity.conversation:
            conv = ctx.activity.conversation
            conv_type = getattr(conv, 'conversation_type', 'unknown')
            conv_id = getattr(conv, 'id', 'unknown')
            is_group_chat = '@unq.gbl.spaces' in conv_id or conv_type in ['groupChat', 'channel']
            
            logger.info(f"CONVERSATION DEBUG - Type: {conv_type} | IsGroup: {is_group_chat} | ConvID: {conv_id[:50]}...")
            
            if is_group_chat and "405" in str(getattr(ctx, '_last_error', '')):
                logger.error(f"ðŸš¨ DETECTED GROUP CHAT PERMISSION ISSUE")
                logger.error(f"ðŸ’¡ TROUBLESHOOTING STEPS:")
                logger.error(f"   1. Remove bot from group chat")
                logger.error(f"   2. Check Bot Framework registration has group chat permissions")
                logger.error(f"   3. Re-add bot to group chat")
                logger.error(f"   4. Try @mentioning the bot: @{getattr(ctx.activity.recipient, 'name', 'Bot')} hello")

        # Check for @mentions which might be required in groups
        mentions = getattr(ctx.activity, 'entities', []) or []
        mention_found = any(getattr(entity, 'type', None) == 'mention' for entity in mentions)
        logger.info(f"MENTIONS DEBUG - Has mentions: {mention_found} | Entity count: {len(mentions)}")
        logger.info("=" * 60)

        # Phase 3: hydrate durable conversation state on entry, flush on exit. The
        # finally clause guarantees a single save regardless of how the handler returns.
        _conv_id = ctx.activity.conversation.id if getattr(ctx.activity, "conversation", None) else None
        if _conv_id:
            _load_conversation_state(_conv_id)
        try:
            await handle_stateful_conversation(model, ctx)
        finally:
            if _conv_id:
                _persist_conversation_state(_conv_id)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Message handling error: {error_msg}", exc_info=True)
        
        # Provide user-friendly error messages based on error type
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            user_message = "âš ï¸ I'm experiencing a temporary connection issue. Please try your message again in a moment."
        elif "token" in error_msg.lower() or "auth" in error_msg.lower():
            user_message = "âš ï¸ There was an authentication issue. Please try again. If this persists, the bot may need to be restarted."
        elif "openai" in error_msg.lower() or "429" in error_msg:
            user_message = "âš ï¸ The AI service is temporarily busy. Please wait a moment and try again."
        elif "graph" in error_msg.lower():
            user_message = "âš ï¸ I had trouble accessing Microsoft 365 resources. Please try again."
        else:
            user_message = f"âš ï¸ Sorry, I encountered an unexpected error. Please try again.\n\n_Error: {error_msg[:100]}_"
        
        try:
            await ctx.send(MessageActivityInput(text=user_message).add_ai_generated())
        except Exception as send_error:
            # Check if it's a group chat permission error (405 Method Not Allowed)
            if "405" in str(send_error) and "Method Not Allowed" in str(send_error):
                logger.error(f"âŒ GROUP CHAT PERMISSION ERROR: {send_error}")
                logger.error(f"ðŸ’¡ FIX: Bot needs to be properly installed in group chat with messaging permissions")
            else:
                logger.error(f"Failed to send error message to user: {send_error}", exc_info=True)


# Global error handler for unhandled exceptions (if supported by SDK)
try:
    @app.on_error
    async def handle_error(ctx: ActivityContext, error: Exception):
        """Handle errors that occur outside of message handlers"""
        error_msg = str(error)
        logger.error(f"Unhandled bot error: {error_msg}", exc_info=True)
        
        try:
            if "timeout" in error_msg.lower() or "jwt" in error_msg.lower():
                user_message = "âš ï¸ I'm experiencing connectivity issues. Please try your message again."
            else:
                user_message = "âš ï¸ Something went wrong. Please try again in a moment."
            
            await ctx.send(MessageActivityInput(text=user_message).add_ai_generated())
        except Exception as send_error:
            logger.error(f"Failed to send error notification: {send_error}")
except AttributeError:
    # SDK doesn't support @app.on_error decorator - that's okay, message handler has its own error handling
    logger.info("Note: SDK does not support @app.on_error decorator, using message handler error handling only")


@app.on_message_submit_feedback
async def handle_message_feedback(ctx: ActivityContext[MessageSubmitActionInvokeActivity]):
    feedback = ctx.activity.value.action_value
    logger.info(f"Feedback: {feedback}")


# Phase 7-Pre: capture the user's Teams SSO token on each signin/tokenExchange invoke.
# Only fires in real Teams with SSO configured; the Playground never sends it. The exact
# response contract is verified in Step 8 (real Teams) — the SDK handles the ack envelope.
try:
    @app.on_signin_token_exchange
    async def handle_signin_token_exchange(ctx: ActivityContext):
        try:
            value = getattr(ctx.activity, "value", None)
            token = getattr(value, "token", None)
            if token is None and isinstance(value, dict):
                token = value.get("token")
            sender = getattr(ctx.activity, "from_", None)
            uid = (
                getattr(sender, "aad_object_id", None)
                or getattr(sender, "aadObjectId", None)
                or getattr(sender, "id", None)
            )
            if token and uid:
                sso_token_cache.set(str(uid), token)
                logger.info("SSO token captured for user %s", str(uid)[:8])
            else:
                logger.debug("signin/tokenExchange received but token or user id missing")
        except Exception as exc:
            logger.warning("SSO token capture failed: %s", exc)
except AttributeError:
    logger.info("Note: SDK has no on_signin_token_exchange decorator; SSO capture disabled")


def check_web_indexing_dependencies():
    """Website indexing is disabled for this SharePoint/uploads-only assistant."""
    return False


# ---------------------------
# Startup initialization
# ---------------------------
async def startup():
    """Initialize app and start background tasks"""
    logger.info("=" * 60)
    logger.info("Starting Mela AI Agent...")
    logger.info("=" * 60)
    # Print identity context and confirm token strategy
    try:
        graph_tenant = config.GRAPH_TENANT_ID or config.APP_TENANTID
        logger.info(f"Configured GRAPH_TENANT_ID (fallback TENANT_ID): {graph_tenant or 'not set'}")
        logger.info(f"Configured CLIENT_ID: {config.APP_ID or 'not set'}")
        logger.info(f"Configured SENDER_UPN: {config.SENDER_UPN or 'not set'}")
        logger.info("Token strategy: app-only client credentials (delegated tokens disabled)")
        logger.info(
            "Effective SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN: %s (too low silently leaves later docs unindexed)",
            config.SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN,
        )
        # Single-line effective-config summary — the first thing to check in any deployment log.
        logger.info(
            "STARTUP | index=%s | security_trimming=%s | graph_search=%s | memory_turns=%s | kv=%s",
            config.AZURE_SEARCH_INDEX_NAME,
            config.ENABLE_SECURITY_TRIMMING,
            config.ENABLE_GRAPH_SEARCH,
            config.MAX_MEMORY_TURNS,
            "set" if os.environ.get("AZURE_KEY_VAULT_URL") else "not set",
        )
    except Exception:
        pass
    
    # Start SharePoint -> Azure AI Search indexing in the background.
    try:
        from search.ai_search_worker import indexing_worker

        task = asyncio.create_task(indexing_worker())
        add_background_task(task, "sharepoint_ai_search_indexing_worker")
    except Exception as e:
        logger.error(f"Failed to start SharePoint AI Search indexing worker: {e}", exc_info=True)

    # Warm up the embedding model + AI Search connection so the FIRST real user
    # query doesn't pay the cold-start penalty (which previously took ~50s and
    # could surface as an error to the user). Runs in the background; failures
    # here are harmless and never block startup.
    async def _warm_up_search() -> None:
        try:
            import time as _t
            from search.embeddings import embed_text
            from search.ai_search_retriever import get_search_client, SELECT_FIELDS

            _started = _t.perf_counter()
            await asyncio.to_thread(embed_text, "warm up")
            try:
                client = get_search_client()
                rows = await asyncio.to_thread(
                    lambda: list(client.search(search_text="warm up", top=1, select=SELECT_FIELDS))
                )
                _ = len(rows)
            except Exception as inner:
                logger.info("WARMUP | search probe skipped: %s", inner)
            logger.info("WARMUP | embedding+search primed | seconds=%.2f", _t.perf_counter() - _started)
        except Exception as warm_err:
            logger.info("WARMUP | skipped: %s", warm_err)

    try:
        warm_task = asyncio.create_task(_warm_up_search())
        add_background_task(warm_task, "search_warm_up")
    except Exception as e:
        logger.info(f"Failed to schedule search warm-up: {e}")

    logger.info("Starting Teams AI app...")
    logger.info("=" * 60)

    # Register the secure artifact download route (GET /files/{token}) on the
    # bot's FastAPI server BEFORE it starts, so code-interpreter outputs can be
    # delivered to users via expiring links.
    if _INTERPRETER_AVAILABLE:
        try:
            from generation.file_store import register_file_routes
            register_file_routes(app.server.adapter.app)
        except Exception as _route_err:
            logger.warning(f"Could not register artifact download route: {_route_err}")

    await app.start()


# Entry point
# ---------------------------
if __name__ == "__main__":
    asyncio.run(startup())
