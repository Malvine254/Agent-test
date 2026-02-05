import asyncio
import os
import logging
import json
import time
import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
import concurrent.futures
import requests


from microsoft_teams.ai import ChatPrompt, ListMemory
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
# Data calculator for accurate numeric operations (overcomes LLM arithmetic limitations)
from data_calculator import process_calculation_request, detect_calculation_intent, process_multi_file_calculation
# Graph API for OneDrive/SharePoint search
from knowledge_base import (
    crawl_accessible_documents,
    download_and_extract_content,
    get_graph_token,
    get_user_profile,
    search_sharepoint,
)
# Document cache for local indexing
from document_cache import get_cache
# Web indexer for background website crawling
from web_indexer import get_web_indexer
# Attachment cache for persisting file contents across follow-up questions
from attachment_cache import (
    cache_attachment,
    get_conversation_attachments,
    search_attachment_contents,
    cleanup_old_cache,
)

# ---------------------------
# Logging (only key application events)
# ---------------------------
logging.basicConfig(
    level=logging.WARNING,  # Set baseline to WARNING to suppress noise
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # Only our app logs at INFO level

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

config = Config()

# Supported document extensions
DOCUMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".csv", ".txt", ".pptx", ".ppt", ".json", ".xml"}

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
                return False, f"❌ File type '{ext}' is not allowed for security reasons."
            
            # Check allowed types
            if ext not in Config.ALLOWED_FILE_TYPES:
                allowed_list = ', '.join(sorted(Config.ALLOWED_FILE_TYPES))
                return False, f"❌ File type '{ext}' is not supported. Allowed: {allowed_list}"
        
        # Check file size (if available)
        content = getattr(att, "content", None)
        if content and isinstance(content, dict):
            size_bytes = content.get("fileSize") or content.get("sizeInBytes") or 0
            if size_bytes > 0:
                size_mb = size_bytes / (1024 * 1024)
                if size_mb > Config.MAX_FILE_SIZE_MB:
                    return False, f"❌ File size ({size_mb:.1f}MB) exceeds limit of {Config.MAX_FILE_SIZE_MB}MB."
        
        return True, ""
    except Exception as e:
        logger.error(f"Error validating attachment: {e}")
        return False, f"❌ Unable to validate file: {str(e)}"


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
        logger.info(f"Using cached profile for user: {_user_profile_cache[user_id].get('displayName')}")
        return _user_profile_cache[user_id]

    # 2) Disk-backed JSON cache
    disk_cache = _read_user_profiles_cache()
    entry = disk_cache.get(user_id)
    if isinstance(entry, dict) and entry:
        if "profile" in entry and isinstance(entry["profile"], dict):
            prof = entry["profile"]
        else:
            # Back-compat: flat format
            prof = entry
        _user_profile_cache[user_id] = prof
        logger.info(f"Loaded user profile from disk cache: {prof.get('displayName')}")
        return prof

    # 3) Graph fallback (prefer delegated via OBO when user_assertion is provided)
    profile = get_user_profile(user_id, user_assertion=user_assertion)
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
                logger.info(f"✓ Task '{task_name}' completed successfully. Result: {result} pages indexed")
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
    path = os.path.join(os.path.dirname(__file__), "instructions.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
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

def _strip_html(text: str) -> str:
    """Convert simple HTML to plain text by removing tags."""
    try:
        import re, html
        # Remove script/style content
        text = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        # Replace <br> and <p> with newlines
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        # Remove remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Unescape HTML entities
        text = html.unescape(text)
        # Normalize whitespace
        return " ".join(text.split())
    except Exception:
        return text

def build_llm_input(
    user_text: str,
    attachment_texts: list[str],
    doc_items: list[dict],
    web_text: str,
    personalization: str,
    memory_text: str = ""
) -> tuple[str, dict]:
    """Construct prompt with full document content - minimal truncation.

    doc_items: [{"title": str, "url": str, "snippet": str}]
    Returns (prompt_text, log_info)
    """
    # Config budgets - REDUCED to prevent crashes with large attachments
    # GPT-4.1 supports 128K tokens but we limit for memory safety
    MAX_PROMPT_TOKENS = int(getattr(Config, "MAX_PROMPT_TOKENS_APPROX", 40000))  # ~40K tokens = safe
    MAX_PROMPT_CHARS = int(getattr(Config, "MAX_PROMPT_CHARS", 160000))  # ~160KB total
    MAX_DOCS = int(getattr(Config, "MAX_DOCS", 20))
    MAX_SNIPPET_CHARS = int(getattr(Config, "MAX_SNIPPET_CHARS", 100000))
    MAX_ATTACH_CHARS = int(getattr(Config, "MAX_ATTACH_CHARS", 120000))  # ~120KB for all attachments
    MAX_WEB_CHARS = int(getattr(Config, "MAX_WEB_CHARS", 20000))
    MAX_MEMORY_TURNS = int(getattr(Config, "MAX_MEMORY_TURNS", 20))

    # Prepare sections (HTML to text) - PRESERVE FULL CONTENT
    utext = (user_text or "").strip()
    ptext = _strip_html(personalization or "")
    web_plain = _strip_html(web_text or "")  # No truncation
    # Attachments: concat ALL content without artificial limits
    compare_intent = False
    try:
        utext_lower = (utext or "").lower()
        compare_keywords = ("compare", "difference", "differences", "diff", "similar", "similarities", "contrast", "vs", "versus")
        compare_intent = len(attachment_texts or []) > 1 and any(k in utext_lower for k in compare_keywords)
        if len(attachment_texts or []) > 1 and "summarize" in utext_lower and not compare_intent:
            utext = f"{utext}\nPlease summarize each attachment separately."
    except Exception:
        compare_intent = False

    # PRESERVE ALL ATTACHMENT CONTENT - no artificial per-attachment limits
    attach_segments = [_strip_html(t) for t in (attachment_texts or []) if t]
    attach_plain = "\n\n---\n\n".join(attach_segments) if attach_segments else ""

    # Doc snippets: preserve FULL content from each document
    docs = []
    for d in (doc_items or []):
        title = (d.get("title") or d.get("name") or "Untitled").strip()
        url = (d.get("url") or "").strip()
        # Convert local paths to network paths and format as non-clickable reference
        url_reference = _convert_to_network_path(url) if url else ""
        # FULL content - no truncation
        snippet = _strip_html((d.get("snippet") or d.get("content") or ""))
        if not snippet:
            continue
        docs.append({"title": title, "url": url_reference, "snippet": snippet})
    # Respect MAX_DOCS hard limit initially
    docs = docs[:MAX_DOCS]

    # Build initial plain-text prompt sections
    parts = []
    parts.append(utext)
    if ptext:
        parts.append(ptext)
    if attach_plain:
        parts.append(f"[ATTACHMENTS]\n{attach_plain}")
    if docs:
        doc_lines = []
        for i, d in enumerate(docs, 1):
            lines = [f"[DOC {i}] {d['title']}"]
            if d['url']:
                lines.append(d['url'])
            lines.append(d['snippet'])
            doc_lines.append("\n".join(lines))
        parts.append("\n\n".join(doc_lines))
    if web_plain:
        parts.append(f"[WEB]\n{web_plain}")
    if memory_text:
        parts.append(f"[MEMORY (last {MAX_MEMORY_TURNS})]\n{_strip_html(memory_text)}")

    prompt = "\n\n".join([p for p in parts if p])

    # Section sizes for logging
    sizes = {
        "user": len(utext),
        "attachments": len(attach_plain),
        "docs": sum(len(d['snippet']) for d in docs),
        "web": len(web_plain),
        "memory": len(memory_text or ""),
    }

    # Log budget values for debugging
    logger.info(f"Budget check: MAX_PROMPT_TOKENS={MAX_PROMPT_TOKENS}, MAX_PROMPT_CHARS={MAX_PROMPT_CHARS}, MAX_ATTACH_CHARS={MAX_ATTACH_CHARS}")
    
    # Truncation guard - use configured limits to prevent crashes
    actions = []
    def within_budget(text: str) -> bool:
        return len(text) <= MAX_PROMPT_CHARS
    
    # Truncate if content exceeds configured limit
    if len(prompt) > MAX_PROMPT_CHARS:
        # 1) Drop web first
        if web_plain:
            actions.append("drop:web")
            web_plain = ""
            prompt = "\n\n".join([utext, ptext, (f"[ATTACHMENTS]\n{attach_plain}" if attach_plain else ""),
                                       ("\n\n".join([f"[DOC {i+1}] {d['title']}\n{d['url']}\n{d['snippet']}" for i, d in enumerate(docs)]) if docs else ""),
                                       (f"[MEMORY (last {MAX_MEMORY_TURNS})]\n{_strip_html(memory_text)}" if memory_text else "")]).strip()
    
    if len(prompt) > MAX_PROMPT_CHARS:
        # 2) Reduce docs count
        if len(docs) > 1:
            new_docs = docs[:max(1, len(docs)-1)]
            actions.append(f"reduce:docs:{len(docs)}->{len(new_docs)}")
            docs = new_docs
            prompt = "\n\n".join([utext, ptext, (f"[ATTACHMENTS]\n{attach_plain}" if attach_plain else ""),
                                       ("\n\n".join([f"[DOC {i+1}] {d['title']}\n{d['url']}\n{d['snippet']}" for i, d in enumerate(docs)]) if docs else ""),
                                       (f"[MEMORY (last {MAX_MEMORY_TURNS})]\n{_strip_html(memory_text)}" if memory_text else "")]).strip()
    
    if len(prompt) > MAX_PROMPT_CHARS:
        # 3) Shorten attachments to fit budget
        if attach_plain and len(attach_plain) > MAX_ATTACH_CHARS:
            available = MAX_ATTACH_CHARS
            actions.append(f"shorten:attachments:{len(attach_plain)}->{available}")
            attach_plain = attach_plain[:available]
            prompt = "\n\n".join([utext, ptext, (f"[ATTACHMENTS]\n{attach_plain}" if attach_plain else ""),
                                       ("\n\n".join([f"[DOC {i+1}] {d['title']}\n{d['url']}\n{d['snippet']}" for i, d in enumerate(docs)]) if docs else ""),
                                       (f"[MEMORY (last {MAX_MEMORY_TURNS})]\n{_strip_html(memory_text)}" if memory_text else "")]).strip()
    
    if len(prompt) > MAX_PROMPT_CHARS:
        # 4) Drop memory if still over
        if memory_text:
            actions.append("drop:memory")
            memory_text = ""
            prompt = "\n\n".join([utext, ptext, (f"[ATTACHMENTS]\n{attach_plain}" if attach_plain else ""),
                                       ("\n\n".join([f"[DOC {i+1}] {d['title']}\n{d['url']}\n{d['snippet']}" for i, d in enumerate(docs)]) if docs else "")]).strip()
    
    # SAFETY: Final hard limit to prevent token limit crashes
    if len(prompt) > MAX_PROMPT_CHARS:
        logger.warning(f"EMERGENCY TRUNCATION: Prompt still too large ({len(prompt):,} chars) - applying hard limit")
        actions.append(f"emergency_truncate:{len(prompt)}->{MAX_PROMPT_CHARS}")
        prompt = prompt[:MAX_PROMPT_CHARS]
        prompt += "\n\n⚠️ [CONTENT TRUNCATED - Input exceeded maximum size]"

    log_info = {
        "sizes": sizes,
        "estimated_tokens": _approx_token_count(prompt),
        "truncation_actions": actions,
        "doc_count": len(docs)
    }
    return prompt, log_info


# ---------------------------
# Token factory for Teams app
# ---------------------------
def create_token_factory():
    token_url = f"https://login.microsoftonline.com/{config.APP_TENANTID}/oauth2/v2.0/token"
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
            if client_id and client_secret and config.APP_TENANTID:
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
conversation_store: dict[str, ListMemory] = {}
conversation_files: dict[str, list] = {}  # Store uploaded files per conversation
conversation_last_query: dict[str, str] = {}  # Store last search query per conversation
background_tasks: list = []  # Track background indexing tasks
CONVERSATION_MEMORY_PATH = os.path.join(os.path.dirname(__file__), "conversation_memory.json")
_conversation_memory_cache = {}  # In-memory cache of conversation history for instant access

def _save_conversation_memory(conversation_id: str, memory: ListMemory) -> None:
    """Persist conversation memory to disk for instant recovery"""
    try:
        # Convert ListMemory items to serializable format
        items = []
        try:
            # Access the internal messages list if available
            if hasattr(memory, 'messages') and memory.messages:
                items = [{"role": m.get("role", ""), "content": m.get("content", "")} for m in memory.messages]
            elif hasattr(memory, 'get_messages'):
                messages = memory.get_messages()
                items = [{"role": m.get("role", ""), "content": m.get("content", "")} for m in (messages or [])]
        except Exception:
            items = []
        
        # Load existing memory file
        mem_data = {}
        if os.path.exists(CONVERSATION_MEMORY_PATH):
            try:
                with open(CONVERSATION_MEMORY_PATH, 'r', encoding='utf-8') as f:
                    mem_data = json.load(f) or {}
            except Exception:
                mem_data = {}
        
        # Update with current conversation
        mem_data[conversation_id] = {
            "messages": items,
            "saved_at": datetime.now().isoformat()
        }
        
        # Write back to disk
        with open(CONVERSATION_MEMORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(mem_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Persisted conversation memory: {conversation_id} ({len(items)} messages)")
    except Exception as e:
        logger.debug(f"Failed to persist conversation memory: {e}")

def _load_conversation_memory(conversation_id: str) -> ListMemory:
    """Load persisted conversation memory from disk for instant recovery"""
    try:
        if not os.path.exists(CONVERSATION_MEMORY_PATH):
            return ListMemory()
        
        with open(CONVERSATION_MEMORY_PATH, 'r', encoding='utf-8') as f:
            mem_data = json.load(f) or {}
        
        conv_mem = mem_data.get(conversation_id)
        if not conv_mem or not conv_mem.get("messages"):
            return ListMemory()
        
        # Reconstruct ListMemory with messages
        memory = ListMemory()
        for msg in conv_mem.get("messages", []):
            try:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role and content:
                    # Add to memory internal state
                    if hasattr(memory, 'messages'):
                        memory.messages.append({"role": role, "content": content})
                    else:
                        # Fallback: try to use public API
                        pass
            except Exception:
                pass
        
        logger.info(f"✓ Loaded persisted conversation memory: {conversation_id} ({len(conv_mem.get('messages', []))} messages)")
        return memory
    except Exception as e:
        logger.debug(f"Failed to load conversation memory: {e}")
        return ListMemory()

def memory_for(conversation_id: str) -> ListMemory:
    if conversation_id not in conversation_store:
        # Load from disk if available, otherwise create new
        memory = _load_conversation_memory(conversation_id)
        conversation_store[conversation_id] = memory
        _conversation_memory_cache[conversation_id] = True  # Mark as loaded
    return conversation_store[conversation_id]

def files_for(conversation_id: str) -> list:
    """Get cached attachment files for a conversation from persistent storage."""
    return get_conversation_attachments(conversation_id)

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
async def llm_decide_routing(model: AIModel, user_text: str, conversation_id: str = "") -> dict:
    """Ask the LLM to intelligently analyze user intent and decide routing.
    Returns a dict: {"action": str, "should_search": bool, "search_query": str, "scope": "graph|local"}
    action: "respond_direct", "search_documents", "refine_previous", "clarify"
    """
    
    # Pre-check: If user explicitly uses search keywords, force search action
    text_lower = user_text.lower().strip()
    search_keywords = ["search", "find", "lookup", "look for", "summarize", "tell me about", "who is", "what is", "get info"]
    
    has_search_keyword = any(keyword in text_lower for keyword in search_keywords)
    
    # Fast-path: explicit search keywords
    if has_search_keyword:
        # Smart query extraction - preserve meaningful content
        query = user_text.strip()
        
        # Remove search prefixes but keep the rest intact
        removals = [
            ("search for ", ""),
            ("search from ", ""),
            ("search about ", ""),
            ("search ", ""),
            ("find me ", ""),
            ("find ", ""),
            ("lookup ", ""),
            ("look for ", ""),
            ("summarize ", ""),
            ("tell me about ", ""),
            ("who is ", ""),
            ("what is ", ""),
            ("get info about ", ""),
            ("get info on ", ""),
        ]
        
        query_lower = query.lower()
        for prefix, replacement in removals:
            if query_lower.startswith(prefix):
                query = query[len(prefix):].strip()
                break
        
        # If query is too generic after extraction, use last search query as context
        generic_terms = ["the document", "it", "that", "this", "documents", "files", "data", "information", "again", "document again", "the document again"]
        if query.lower().strip() in generic_terms or len(query.strip()) < 3:
            # Try to use last query from conversation
            last_query = last_query_for(conversation_id) if conversation_id else ""
            if last_query:
                query = last_query
                logger.info(f"Using last query from context: '{query}'")
            else:
                # Keep original text but remove just the verb
                query = user_text.strip()
                for verb in ["search", "find", "lookup", "look for"]:
                    query = query.lower().replace(verb + " for", "").replace(verb + " from", "").replace(verb + " about", "").replace(verb, "").strip()
        
        # Final fallback - if still empty, use full text
        if not query or len(query.strip()) < 2:
            query = user_text
        
        logger.info(f"LLM Decision: action='search_documents' | search=True | query='{query}' | scope='local' (keyword)")
        return {
            "action": "search_documents",
            "should_search": True,
            "is_followup": False,
            "query": query,
            "scope": "local",
            "top_k": 3
        }
    
    # Fast-path: treat file-like or URL queries as search, no LLM call
    try:
        text_lower = user_text.lower().strip()
        looks_like_file = any(ext in text_lower for ext in [
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".txt"
        ])
        looks_like_url = text_lower.startswith("http://") or text_lower.startswith("https://")
        if looks_like_file or looks_like_url:
            query = user_text.strip()
            logger.info(f"LLM Decision: action='search_documents' | search=True | query='{query}' | scope='local' (file/url heuristic)")
            return {
                "action": "search_documents",
                "should_search": True,
                "is_followup": False,
                "query": query,
                "scope": "local",
                "top_k": 3
            }
    except Exception:
        pass

    try:
        router_instructions = (
            "You are an intent analyzer. Read the user's message and determine what they want.\n"
            "Return ONLY strict JSON with keys: action (string), should_search (bool), search_query (string), scope ('graph'|'local').\n"
            "\n"
            "ACTION TYPES:\n"
            "\n"
            "'respond_direct': Just talk/chat.\n"
            "  When: 'hi', 'hello', 'how are you', 'thanks', general conversation WITHOUT asking for information.\n"
            "\n"
            "'refine_previous': Improve the last response.\n"
            "  When: 'make it shorter', 'add details', 'explain more', 'change tone', 'fix that'.\n"
            "\n"
            "'search_documents': Find information in documents/SharePoint.\n"
            "  When: User asks about ANY person, topic, or information that would be in documents.\n"
            "  Examples:\n"
            "    - 'search for employee data' → action='search_documents', query='employee data'\n"
            "    - 'summarize employee data' → action='search_documents', query='employee data'\n"
            "    - 'tell me about [person/topic]' → action='search_documents', query='[person/topic]'\n"
            "    - 'what is [topic]' → action='search_documents', query='[topic]'\n"
            "    - 'who is [person]' → action='search_documents', query='[person]'\n"
            "    - 'find policies' → action='search_documents', query='policies'\n"
            "    - Any question asking for factual information → action='search_documents'\n"
            "\n"
            "'clarify': Need more info (ONLY use if truly ambiguous).\n"
            "  When: Request is genuinely vague and you cannot extract what they want.\n"
            "\n"
            "RULES:\n"
            "1. If user asks ABOUT something (person, topic, data) → action='search_documents' (DO NOT use 'respond_direct').\n"
            "2. Extract the search query from what they said (e.g., 'tell me about John' → query='John').\n"
            "3. scope: 'graph' unless they mention 'uploaded' or 'attached' files.\n"
            "4. Default to 'search_documents' when in doubt - better to search than to guess.\n"
            "\n"
            "OUTPUT FORMAT (must be valid JSON):\n"
            "{ \"action\": \"search_documents\", \"should_search\": true, \"search_query\": \"the query\", \"scope\": \"graph\" }"
        )

        prompt = ChatPrompt(model)
        
        # Use retry logic for LLM call
        async def make_llm_call():
            async with llm_semaphore:
                return await prompt.send(
                input=user_text,
                instructions=router_instructions,
                memory=None,
            )
        
        result_text = await call_llm_with_retry(make_llm_call)

        # ChatPrompt may return str or object; ensure str
        if hasattr(result_text, "text"):
            result_text = result_text.text
        elif hasattr(result_text, "response") and hasattr(result_text.response, "content"):
            result_text = result_text.response.content
        elif not isinstance(result_text, str):
            result_text = str(result_text)

        # Log the raw LLM response for debugging
        logger.info(f"Raw LLM router response: {result_text}")

        try:
            # Clean up common LLM formatting issues
            cleaned_text = (result_text or "").strip()
            
            # Remove markdown code blocks if present
            if cleaned_text.startswith("```"):
                # Remove opening ```json or ``` 
                cleaned_text = cleaned_text.split("\n", 1)[1] if "\n" in cleaned_text else cleaned_text[3:]
                # Remove closing ```
                if cleaned_text.endswith("```"):
                    cleaned_text = cleaned_text[:-3]
                cleaned_text = cleaned_text.strip()
            
            data = json.loads(cleaned_text)
            # Basic validation and defaults
            if not isinstance(data, dict):
                raise ValueError("Router output not a dict")
            
            action = str(data.get("action", "respond_direct")).lower()
            should_search = action == "search_documents"
            
            result = {
                "action": action,
                "should_search": should_search,
                "is_followup": action == "refine_previous",
                "query": (data.get("search_query") or user_text).strip(),
                "scope": (data.get("scope") or "graph").lower(),
                "top_k": 3
            }
            
            # Log the routing decision for debugging
            logger.info(f"LLM Decision: action='{action}' | search={should_search} | query='{result['query']}' | scope='{result['scope']}'")
            
            return result
        except Exception as parse_err:
            # Fallback: respond directly (safe default)
            logger.error(f"Failed to parse LLM router response. Error: {parse_err}. Response was: {result_text}")
            logger.info(f"LLM Decision: action='respond_direct' | search=False | query='' | scope='graph' (fallback due to parse error)")
            return {
                "action": "respond_direct",
                "should_search": False,
                "is_followup": False,
                "query": user_text.strip(),
                "scope": "graph",
                "top_k": 3,
            }
    except Exception as e:
        logger.error(f"Router error: {e}")
        logger.info(f"LLM Decision: action='respond_direct' | search=False | query='' | scope='graph' (fallback due to error)")
        return {
            "action": "respond_direct",
            "should_search": False,
            "is_followup": False,
            "query": user_text.strip(),
            "scope": "graph",
            "top_k": 3,
        }

# ---------------------------
# Typing indicator helper
# ---------------------------
async def send_typing_indicator(ctx: ActivityContext[MessageActivity]) -> None:
    """Send a typing indicator to show the bot is processing the request."""
    try:
        typing_activity = TypingActivityInput()
        await ctx.send(typing_activity)
        logger.info("Typing indicator sent")
    except Exception as e:
        logger.warning(f"Failed to send typing indicator: {e}")

async def send_typing_with_status(ctx: ActivityContext[MessageActivity], status: str) -> None:
    """Send typing indicator with a brief status message for long operations."""
    try:
        # Send typing indicator first
        typing_activity = TypingActivityInput()
        await ctx.send(typing_activity)
        
        # Send brief status update
        import asyncio
        await asyncio.sleep(0.1)  # Brief delay to ensure typing shows first
        
        status_activity = MessageActivityInput(
            text=f"🔄 {status}",
            type="message"
        )
        await ctx.send(status_activity)
        logger.info(f"Typing indicator with status sent: {status}")
    except Exception as e:
        logger.warning(f"Failed to send typing indicator with status: {e}")
        # Fallback to regular typing indicator
        await send_typing_indicator(ctx)

class TypingIndicatorManager:
    """Manages periodic typing indicators during long operations to prevent timeout."""
    
    def __init__(self, ctx: ActivityContext[MessageActivity]):
        self.ctx = ctx
        self.refresh_task = None
        self.should_refresh = False
    
    async def start_periodic_refresh(self, interval: float = 5.0):
        """Start sending typing indicators every `interval` seconds (default 5s to prevent Teams timeout)."""
        self.should_refresh = True
        self.refresh_task = asyncio.create_task(self._refresh_loop(interval))
        logger.info(f"Started periodic typing indicator refresh (every {interval}s)")
    
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
        """Internal loop that sends typing indicators periodically."""
        try:
            while self.should_refresh:
                await asyncio.sleep(interval)
                if self.should_refresh:  # Check again after sleep
                    await send_typing_indicator(self.ctx)
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
            text=f"🔄 {message}",
            type="message"
        )
        await ctx.send(status_activity)
        logger.info(f"Typing indicator with message sent: {message}")
    except Exception as e:
        logger.warning(f"Failed to send typing indicator with message: {e}")

async def process_attachments_with_typing(ctx: ActivityContext[MessageActivity], attachments: list, conversation_id: str, cache_user_id: str) -> tuple:
    """Process attachments with periodic typing indicators to prevent timeout."""
    MAX_ATTACHMENTS = 5
    parts = []
    extracted_for_aggregation = []  # For multi-file comparison
    
    if len(attachments) > MAX_ATTACHMENTS:
        logger.info(f"Attachment limit exceeded ({len(attachments)}). Processing first {MAX_ATTACHMENTS} only.")
        await send_typing_indicator(ctx)
    
    total_attachments = min(len(attachments), MAX_ATTACHMENTS)
    
    for i, att in enumerate(attachments[:MAX_ATTACHMENTS], 1):
        att_name = getattr(att, "name", "unknown")
        
        # Send typing indicator before each attachment to keep connection alive
        if total_attachments > 1:
            await send_typing_with_message(ctx, f"Processing attachment {i}/{total_attachments}: {att_name}")
        else:
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
                file_content = await asyncio.to_thread(process_attachment, att, conversation_id, user_id=cache_user_id)
        except MemoryError:
            logger.error(f"Memory error processing '{att_name}' - file too large")
            error_msg = f"""❌ **Memory Error**: {att_name}

⚠️ File is too large to process in available memory.

**Solutions:**
• Upload a smaller file (< 50 MB recommended)
• Split large files into sections
• Use compressed formats
• Share specific pages/sections instead"""
            parts.append(error_msg)
            continue
        except Exception as proc_err:
            logger.error(f"Error processing '{att_name}': {proc_err}", exc_info=True)
            parts.append(f"❌ Error processing {att_name}: {str(proc_err)[:200]}")
            continue
        
        # Send another typing indicator after processing (before caching)
        if i < total_attachments:
            await send_typing_indicator(ctx)

        if file_content:
            if file_content.startswith("❌"):
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
            try:
                cache_attachment(conversation_id, att_name, file_content)
                logger.info(f"Attachment '{att_name}' processed and cached - {len(file_content):,} chars (FULL content preserved)")
            except Exception as cache_err:
                logger.warning(f"Failed to cache attachment '{att_name}': {cache_err}")
                logger.info(f"Attachment '{att_name}' processed (cache failed) - {len(file_content):,} chars")
        else:
            # No content returned; provide mobile-friendly guidance
            mobile_guidance = f"""❌ Unable to read attachment '{att_name}'.

**If using Teams mobile app:**
• **Wait 30-60 seconds** after selecting files before sending
• Use the **paperclip button** (not drag-and-drop)
• Try **desktop/web Teams** for more reliable file uploads
• Ensure **strong network connection**

• Make sure file has proper extension (.pdf, .docx, etc.)"""
            
            parts.append(mobile_guidance)
    
    return parts, extracted_for_aggregation

# ---------------------------
# Main handler
# ---------------------------
async def handle_stateful_conversation(model: AIModel, ctx: ActivityContext[MessageActivity]) -> None:
    conversation_id = ctx.activity.conversation.id
    user_text = (ctx.activity.text or "").strip()
    attachments_raw = ctx.activity.attachments or []
    
    # Log raw attachments BEFORE filtering
    logger.info(f"Raw attachments received: {len(attachments_raw)}")
    for idx, raw_att in enumerate(attachments_raw, 1):
        logger.info(f"  Raw attachment {idx}: type={type(raw_att).__name__}")
    
    attachments = [a for a in attachments_raw if is_file_attachment(a)]

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
                    text="🤔 I detected an attachment, but couldn't recognize it as a file.\n\n"
                         "**This usually happens because:**\n"
                         "• Teams sent a link preview or rich message embed (not a file)\n"
                         "• Mobile file upload hasn't finished yet\n"
                         "• File metadata is missing\n\n"
                         "**To fix this:**\n"
                         "1️⃣ **Wait 10-30 seconds** after selecting your file, then send\n"
                         "2️⃣ **Use the paperclip button** (📎) to attach files\n"
                         "3️⃣ **Try desktop or web Teams** for best results\n"
                         "4️⃣ **Make sure it's a supported file**: PDF, Word, Excel, PowerPoint, CSV, TXT\n\n"
                         "💡 *Tip: You can also just ask me a question or search your documents without attachments!*"
                ).add_ai_generated()
            )
        else:
            logger.info("Empty message detected (no text, no attachments) - sending clarification")
            await ctx.send(
                MessageActivityInput(
                    text="👋 Hi! I'm here to help. You can:\n\n"
                         "📎 **Upload documents** (PDF, Word, Excel, PowerPoint, CSV)\n"
                         "💬 **Ask questions** about your files or information\n"
                         "🔍 **Search** your OneDrive/SharePoint documents\n\n"
                         "💡 *Tip: If you tried to upload a file from mobile, wait 10-30 seconds after selecting it before sending your message, or use the desktop/web app for best results.*"
                ).add_ai_generated()
            )
        return

    # Send typing indicator IMMEDIATELY to show the bot is processing
    await send_typing_indicator(ctx)

    # OPTIMIZATION: Check for cached attachments BEFORE routing
    # This avoids expensive Graph/AI searches when follow-up questions are about uploaded files
    has_cached_attachments = False
    cached_attachment_filenames = []
    try:
        # Quick check without loading full content (faster)
        cached_attachments_check = get_conversation_attachments(conversation_id, include_content=False)
        if cached_attachments_check:
            has_cached_attachments = True
            cached_attachment_filenames = [f.get("filename") or f.get("name", "unknown") for f in cached_attachments_check]
            logger.info(f"Found {len(cached_attachments_check)} cached attachment(s) in conversation: {', '.join(cached_attachment_filenames)}")
    except Exception:
        pass

    # --- FIX: ensure all variables are defined before use and routing is done at the start ---
    # LLM router: decide routing and extract action, route, etc.
    # Keep connection alive during LLM call
    async with TypingIndicatorManager(ctx):
        route = await llm_decide_routing(model, user_text, conversation_id)
    action = route.get("action", "respond_direct")

    # OPTIMIZATION OVERRIDE: For follow-up questions when cached attachments exist, skip external searches
    # This reduces latency from ~8-10s to <2s by avoiding Graph API and AI Search calls
    # Can be disabled via SKIP_SEARCH_FOR_CACHED_FOLLOWUPS=false config
    if (has_cached_attachments 
        and action == "search_documents" 
        and not attachments 
        and getattr(Config, "SKIP_SEARCH_FOR_CACHED_FOLLOWUPS", True)):
        
        # Check if this looks like a follow-up question about the uploaded files
        user_text_lower = (user_text or "").lower()
        
        # Simple heuristic: short questions without external indicators are likely follow-ups
        is_likely_followup = (
            len(user_text.split()) < 15  # Short question
            and not any(keyword in user_text_lower for keyword in ["sharepoint", "onedrive", "find", "search for", "look for", "document", "file"])  # No external search intent
            and any(keyword in user_text_lower for keyword in ["what", "who", "how", "show", "list", "tell", "any", "which"])  # Typical question words
        )
        
        if is_likely_followup:
            logger.info(f"⚡ FAST PATH: Follow-up detected with {len(cached_attachment_filenames)} cached attachment(s) - skipping Graph/AI Search")
            logger.info(f"   Cached files: {', '.join(cached_attachment_filenames)}")
            action = "respond_direct"
            route["action"] = "respond_direct"
            # Clear search query to prevent fallthrough
            route["query"] = ""
            route["should_search"] = False

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

    # Capture user assertion (not used for app-only tokens, kept for profile caching logic)
    user_assertion = _extract_user_assertion_from_activity(ctx)
    # Prefer a stable user key: AAD object ID, else UPN/email, else conversation id
    extracted_upn_initial = _extract_user_upn_from_activity(ctx) or ""
    user_key = aad_id or extracted_upn_initial or conversation_id
    # Only use remembered details under stable identifiers (AAD object ID or UPN/email)
    stable_lookup_key = aad_id or extracted_upn_initial
    remembered = get_remembered_user_details(stable_lookup_key) if stable_lookup_key else {}
    user_name = remembered.get("displayName") or ""
    user_email = remembered.get("mail") or remembered.get("userPrincipalName") or extracted_upn_initial or ""

    # Early cache access (used for optional inference path below)
    cache = get_cache()

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

    # Enrich via Graph if we have AAD id. Prefer delegated (OBO) using Teams SSO token when available.
    # Skip Graph entirely when we already have cached profile data
    if aad_id and not (user_name or user_email):
        try:
            if user_assertion:
                logger.info("Profile lookup: token=obo, endpoint=/me")
            else:
                logger.info("Profile lookup: token=app-only, endpoint=/users/{id}")
            profile = get_cached_user_profile(aad_id, user_assertion=user_assertion) or {}
            if profile:
                user_name = profile.get("displayName") or user_name
                user_email = profile.get("mail") or profile.get("userPrincipalName") or user_email
                logger.info(f"Profile fetched: name={user_name}, email={user_email}")
            else:
                logger.warning(f"Profile lookup returned None for aad_id={aad_id[:8]}...")
        except Exception as e:
            logger.error(f"Error fetching profile for aad_id={aad_id[:8] if aad_id else 'None'}...: {e}", exc_info=True)
        # Persist only Graph-derived profile
        if user_name or user_email:
            remember_user_details(user_key, {
                "displayName": user_name,
                "mail": user_email,
                "userPrincipalName": user_email,
                "aadObjectId": aad_id,
            })
    else:
        # Fallback: if we have a sender.id that looks like a GUID, use it to fetch Graph profile
        try:
            from_id = getattr(sender, "id", None)
            if not aad_id and from_id and len(str(from_id)) > 30 and '-' in str(from_id) and not (user_name or user_email):
                logger.info("Profile lookup (fallback): using from.id as AAD object id for app-only /users/{id}")
                profile = get_cached_user_profile(str(from_id), user_assertion=user_assertion) or {}
                if profile:
                    user_name = profile.get("displayName") or user_name
                    user_email = profile.get("mail") or profile.get("userPrincipalName") or user_email
                    aad_id = str(from_id)
                    user_key = aad_id
                    cache_user_id = aad_id
                    logger.info(f"Fallback profile fetched: name={user_name}, email={user_email}")
                    remember_user_details(user_key, {
                        "displayName": user_name,
                        "mail": user_email,
                        "userPrincipalName": user_email,
                        "aadObjectId": aad_id,
                    })
                else:
                    logger.warning(f"Fallback profile lookup returned None for from.id={from_id[:8]}...")
        except Exception as e:
            logger.error(f"Error in fallback profile lookup: {e}", exc_info=True)
        # Safety: do NOT infer identity from cache unless explicitly enabled
        try:
            if getattr(Config, "ALLOW_CACHE_USER_INFERENCE", False):
                users_map = (cache.cache or {}).get("users", {})
                user_ids = [uid for uid in users_map.keys() if uid]
                if not aad_id and len(user_ids) == 1 and not (user_name or user_email):
                    inferred_id = user_ids[0]
                    logger.info(f"Inferring user id from document cache: {inferred_id}")
                    user_assertion = _extract_user_assertion_from_activity(ctx)
                    prof = get_cached_user_profile(inferred_id, user_assertion=user_assertion) or {}
                    if prof:
                        user_name = prof.get("displayName") or user_name
                        user_email = prof.get("mail") or prof.get("userPrincipalName") or user_email
                        # Switch keys to inferred AAD id for consistency
                        aad_id = inferred_id
                        user_key = aad_id
                        cache_user_id = aad_id
                        remember_user_details(user_key, {
                            "displayName": user_name,
                            "mail": user_email,
                            "userPrincipalName": user_email,
                            "aadObjectId": aad_id,
                        })
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
        extracted_upn = _extract_user_upn_from_activity(ctx) or ""
        if extracted_upn:
            user_email = extracted_upn
            remember_user_details(user_key, {
                "mail": user_email,
                "userPrincipalName": user_email,
                "aadObjectId": aad_id or user_key,
            })
    # Final identity summary
    logger.info(f"Final user identity: name='{user_name or '(not set)'}', email='{user_email or '(not set)'}', aad_id={aad_id[:8] if aad_id else '(not set)'}...")
    
    # DEBUG: Log the full cached profile to see what fields we have
    if aad_id:
        try:
            full_profile = get_cached_user_profile(aad_id) or {}
            logger.info(f"DEBUG Full cached profile: {full_profile}")
        except Exception as e:
            logger.error(f"DEBUG profile inspection error: {e}")
    
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
                remember_user_details(user_key, {
                    "displayName": user_name,
                    "mail": user_email,
                    "userPrincipalName": user_email,
                    "aadObjectId": aad_id or user_email,
                })
                logger.info(f"User key updated from {previous_key} to {user_key} (stable)")
        except Exception:
            pass
    # If we at least have a display name, persist minimal profile to disk for continuity
    if user_name and not (remembered.get("displayName") or user_email):
        # Persist minimal profile under stable identifiers if available; otherwise use conversation-scoped key
        try:
            stable_key = aad_id or user_email
            if stable_key:
                remember_user_details(stable_key, {
                    "displayName": user_name,
                    "aadObjectId": aad_id or stable_key,
                })
                logger.info(f"Persisted minimal profile: name={user_name}, key={stable_key}")
            else:
                conv_key = f"conv:{conversation_id}"
                remember_user_details(conv_key, {
                    "displayName": user_name,
                })
                logger.info(f"Persisted minimal profile under conversation key: {conv_key}")
        except Exception:
            pass

    # Background personal crawl DISABLED - using live Graph search only
    # Cache is populated only from documents actually used in responses
    logger.info("Personal crawl: DISABLED (live Graph search only)")

    # Cache partition key: prefer AAD id, else UPN/email, else conversation id
    cache_user_id = aad_id or user_email or conversation_id
    attachment_context = ""
    attachment_texts_for_llm: list[str] = []
    search_context = ""
    cached_results = []
    doc_summaries = []
    web_results = []
    scope = route.get("scope", "graph")
    # Ensure AI search results variable exists for all paths
    ai_search_results = []

    # On-demand cache seeding DISABLED - using purely live Graph search
    # Cache is populated ONLY from documents actually used in responses
    # This ensures no background crawling and immediate live results
    logger.info("Cache seeding: DISABLED (live Graph search only)")

    # Get stored files from previous messages in this conversation
    file_storage = files_for(conversation_id)
    
    # Attachment processing - files uploaded directly to chat
    if attachments:
        await send_typing_indicator(ctx)
        MAX_ATTACHMENTS = 5
        parts = []
        extracted_for_aggregation = []  # For multi-file comparison
        
        if len(attachments) > MAX_ATTACHMENTS:
            logger.info(f"Attachment limit exceeded ({len(attachments)}). Processing first {MAX_ATTACHMENTS} only.")
            await send_typing_indicator(ctx)
        
        total_attachments = min(len(attachments), MAX_ATTACHMENTS)
        
        for i, att in enumerate(attachments[:MAX_ATTACHMENTS], 1):
            att_name = getattr(att, "name", "unknown")
            
            # Send typing indicator before each attachment to keep connection alive
            if total_attachments > 1:
                await send_typing_with_status(ctx, f"Processing {att_name} ({i}/{total_attachments})")
            else:
                await send_typing_indicator(ctx)
            
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
                    file_content = await asyncio.to_thread(process_attachment, att, conversation_id, user_id=cache_user_id)
            except MemoryError as mem_err:
                logger.error(f"MEMORY ERROR processing '{att_name}': {mem_err}")
                file_content = f"❌ **File too large**: {att_name}\n\nThis file caused a memory error. Try:\n• Splitting into smaller files\n• Reducing file size\n• Asking about specific sections"
            except Exception as proc_err:
                logger.error(f"ERROR processing attachment '{att_name}': {proc_err}", exc_info=True)
                file_content = f"❌ **Processing failed**: {att_name}\n\nError: {str(proc_err)[:200]}"
            
            # Send typing indicator after processing (before caching)
            if file_content and len(file_content) > 10000:  # Large files get extra typing indicator
                await send_typing_indicator(ctx)

            if file_content:
                if file_content.startswith("❌"):
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
                
                # Cache attachment to disk for follow-up questions
                try:
                    cache_attachment(conversation_id, att_name, file_content)
                    logger.info(f"Attachment '{att_name}' processed and cached - {len(file_content)} chars (FULL content)")
                except Exception as cache_err:
                    logger.warning(f"Failed to cache attachment '{att_name}': {cache_err}")
                    logger.info(f"Attachment '{att_name}' processed (cache failed) - {len(file_content)} chars")
            else:
                # No content returned; provide mobile-friendly guidance
                mobile_guidance = f"""❌ Unable to read attachment '{att_name}'.

**If using Teams mobile app:**
• **Wait 30-60 seconds** after selecting files before sending
• Use the **paperclip button** (not drag-and-drop)
• Try **desktop/web Teams** for more reliable file uploads
• Ensure **strong network connection**

**File troubleshooting:**
• Check file size (keep under 250 MB)
• Verify file isn't corrupted or password-protected
• Make sure file has proper extension (.pdf, .docx, etc.)"""
                
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
    if not attachments:
        # Try to load from persistent disk cache first (survives restarts, avoids memory limits)
        cached_attachments = []
        try:
            cached_attachments = get_conversation_attachments(conversation_id)
        except Exception as cache_err:
            logger.warning(f"Failed to load cached attachments: {cache_err}")
        
        if cached_attachments:
            logger.info(f"Loading {len(cached_attachments)} attachment(s) from disk cache for follow-up")
            parts = []
            for cached_file in cached_attachments:
                fname = cached_file.get("name", "unknown")
                fcontent = cached_file.get("content", "")
                if fcontent:
                    # Include FULL cached content - no truncation for complete analysis
                    parts.append(f"[Previously uploaded: {fname}]\n{fcontent}")
                    logger.info(f"Loaded full cached content for {fname}: {len(fcontent)} chars")
                    # Content already in cache - no need to store in memory
            if parts:
                attachment_context = "\n\n" + "\n---\n".join(parts)
                attachment_texts_for_llm = parts
                logger.info(f"Loaded {len(parts)} cached file(s) with {sum(len(p) for p in parts)} total chars")
        elif file_storage:
            # Use cached attachments as primary source (file_storage is now cache-based)
            logger.info(f"Including {len(file_storage)} previously uploaded file(s) from cache")
            parts = []
            for stored_file in file_storage:
                fname = stored_file.get("name", "unknown")
                fcontent = stored_file.get("content", "")
                if fcontent:
                    # Include FULL cached content - no truncation for complete analysis  
                    parts.append(f"[Previously uploaded: {fname}]\n{fcontent}")
                    logger.info(f"Including full cached file {fname}: {len(fcontent)} chars")
            if parts:
                attachment_context = "\n\n" + "\n---\n".join(parts)
                attachment_texts_for_llm = parts
                logger.info(f"Loaded {len(parts)} stored file(s) with {sum(len(p) for p in parts)} total chars")

    # If user provided no instruction but sent attachments, default to summarization
    try:
        if (not (user_text and user_text.strip())) and attachments and attachment_context:
            plural = "s" if len(attachments) > 1 else ""
            user_text = f"Summarize the attached document{plural}."
            logger.info("No instruction provided; defaulting to summarization of attachments")
            # Update routing intent to reflect summarization context
            route["action"] = "respond_direct"
            route["should_search"] = False
    except Exception:
        pass

    # Skip external search/Graph for greetings or small-talk
    smalltalk = False
    try:
        smalltalk = is_smalltalk(user_text or "")
        if smalltalk:
            logger.info("Small-talk detected; responding directly without search/Graph")
            route["action"] = "respond_direct"
            route["should_search"] = False
    except Exception:
        pass

    # Force search when ALWAYS_CALL_AI_SEARCH is enabled (override router decision)
    try:
        if getattr(Config, "ALWAYS_CALL_AI_SEARCH", False) and action == "respond_direct" and not smalltalk:
            # Only force search if there's actual query content (not just attachments)
            has_query = bool((user_text or "").strip())
            is_attachment_only = (attachments and not has_query)
            
            if has_query and not is_attachment_only:
                logger.info("ALWAYS_CALL_AI_SEARCH enabled: overriding 'respond_direct' to 'search_documents'")
                action = "search_documents"  # Update local variable
                route["action"] = "search_documents"
                route["should_search"] = True
                # Use user text as query if no explicit query was set
                if not route.get("query"):
                    route["query"] = user_text.strip()
    except Exception as e:
        logger.error(f"Error applying ALWAYS_CALL_AI_SEARCH override: {e}")
        pass

    # Re-sync action variable with route after any overrides
    action = route.get("action", action)

    # Route based on intent
    if action == "refine_previous":
        logger.info("Refinement detected; using conversation memory")
    elif action == "search_documents":
        # Never skip search when the router decided to search.
        # Even with attachments and summarization language, proceed if any explicit query exists.
        try:
            explicit_query = (route.get("query") or "").strip()
            if attachments and explicit_query:
                logger.info("Attachments present with explicit query; proceeding with document search")
            elif attachments and not explicit_query:
                # No explicit query and attachments-only with generic summarization → respond direct
                user_text_lower = (user_text or "").lower()
                generic_phrases = {"this doc", "this document", "the doc", "the document"}
                if ("summarize" in user_text_lower) or (user_text_lower.strip() in generic_phrases):
                    logger.info("Attachments present with summarization intent and no explicit query; responding directly")
                    action = "respond_direct"
        except Exception:
            pass

    if action == "search_documents":
        q = route.get("query", user_text).strip()

        # If user attached files and is asking to summarize, prefer the attachments only.
        try:
            q_lower = q.lower()
            wants_attachment_summary = (
                attachments
                and attachment_context
                and (
                    "summarize" in q_lower
                    or "summary" in q_lower
                    or q_lower in {"summarize this", "summarize the document", "summarize the doc"}
                )
            )
            if wants_attachment_summary:
                logger.info("Attachments present with summarization intent; skipping search and responding with attachments only")
                action = "respond_direct"
        except Exception:
            pass

    if action == "search_documents":
        set_last_query(conversation_id, q)
        if q:
            # Send typing indicator for search operations
            await send_typing_indicator(ctx)
            
            # Increase aggregation breadth when ALWAYS_CALL_AI_SEARCH is enabled
            default_top_k = 5 if getattr(Config, "ALWAYS_CALL_AI_SEARCH", False) else 3
            top_k = int(route.get("top_k", default_top_k))
            
            # Initialize result containers before searching
            doc_entries = []
            sources_refs = []
            full_contents = []
            
            # STEP 0: Search cached attachments if no current attachments in context
            # This allows follow-up questions to access previously uploaded files
            # NOTE: Full content is preserved in cache (no truncation) to ensure all data is available
            if not attachment_context and not attachments:
                logger.info(f"No current attachments - searching cached attachments for: {q}")
                try:
                    cached_search_results = search_attachment_contents(conversation_id, q, limit=3)
                    if cached_search_results:
                        logger.info(f"Found {len(cached_search_results)} relevant cached attachment(s)")
                        cached_attachment_parts = []
                        for result in cached_search_results:
                            filename = result.get("filename", "Unknown")
                            snippet = result.get("content_snippet", "")
                            score = result.get("relevance_score", 0)
                            full_content = result.get("full_content", "")
                            content_size = len(full_content)
                            
                            logger.info(f"Including cached attachment: {filename} (relevance: {score}, size: {content_size:,} chars)")
                            # Add full content to context for thorough analysis
                            # Full content is available regardless of original file size
                            cached_attachment_parts.append(f"[Cached attachment: {filename}]\n{full_content}")
                        
                        # Add cached attachments to context
                        if cached_attachment_parts:
                            attachment_context = "\n\n" + "\n---\n".join(cached_attachment_parts)
                            attachment_texts_for_llm = cached_attachment_parts
                            total_cached_chars = sum(len(part) for part in cached_attachment_parts)
                            logger.info(f"Added {len(cached_attachment_parts)} cached attachment(s) to context ({total_cached_chars:,} chars total)")
                except Exception as cache_search_err:
                    logger.warning(f"Failed to search cached attachments: {cache_search_err}")
            
            # STEP 1: Search local document cache first (fastest)
            logger.info(f"Searching document cache for: {q}")
            try:
                scored = cache.search_cache_scored(q, user_id=cache_user_id, limit=top_k, include_shared=True)
            except Exception:
                scored = []
            cached_results = [r.get("doc", {}) for r in scored]
            top_score = max([int(r.get("score", 0)) for r in scored], default=0)
            logger.info(f"Document cache returned {len(cached_results)} results (top score={top_score}) for user_id={cache_user_id}")
            try:
                logger.debug(
                    "Cached result names: %s",
                    ", ".join([d.get("name", "(no-name)") for d in cached_results])
                )
            except Exception:
                pass
            
            # Filter out unrelated cached docs to avoid off-topic combines and speed up
            try:
                q_tokens = [t.lower() for t in (q or "").split() if len(t) > 2]
                if q_tokens and cached_results:
                    def _doc_matches(doc: dict) -> bool:
                        text = ((doc.get("name") or "") + " " + (doc.get("snippet") or doc.get("content") or "")).lower()
                        return any(tok in text for tok in q_tokens)
                    before = len(cached_results)
                    cached_results = [d for d in cached_results if _doc_matches(d)]
                    filtered = before - len(cached_results)
                    if filtered:
                        logger.info(f"Filtered out {filtered} cached docs unrelated to query '{q}'")
            except Exception:
                pass

            # Also search web cache alongside docs
            web_indexer = get_web_indexer()
            web_results = web_indexer.search_web_cache(q, limit=top_k)

            # STEP 2: Decide whether to call AI Search too (fallback or complement)
            ai_search_results = []
            call_ai_search = False
            decision_reasons = []
            
            # Check if ALWAYS_CALL_AI_SEARCH is enabled (force AI Search regardless of cache)
            if getattr(Config, "ALWAYS_CALL_AI_SEARCH", False):
                call_ai_search = True
                decision_reasons.append("ALWAYS_CALL_AI_SEARCH=true")
                logger.info("ALWAYS_CALL_AI_SEARCH is enabled; forcing AI Search")
            
            # Respect attachment-only offline mode
            try:
                if attachments and getattr(Config, "DISABLE_APIS_ON_ATTACHMENTS", False):
                    call_ai_search = False
                    decision_reasons.append("attachments_offline_mode")
                    logger.info("Attachments detected; external APIs disabled by config. Skipping AI Search/Graph.")
            except Exception:
                pass
            
            # Only apply heuristics if ALWAYS_CALL_AI_SEARCH is not set
            if not getattr(Config, "ALWAYS_CALL_AI_SEARCH", False):
                # If cache score is solid, skip external search entirely
                cache_is_solid = False
                try:
                    min_graph_skip = int(getattr(Config, "MIN_CACHED_SCORE_BEFORE_GRAPH", 55))
                    if cached_results and top_score >= min_graph_skip:
                        cache_is_solid = True
                        call_ai_search = False
                        decision_reasons.append(f"solid_cache({top_score}>={min_graph_skip})")
                except Exception:
                    pass

                if not cache_is_solid:
                    if not cached_results and not web_results:
                        call_ai_search = True
                        decision_reasons.append("no_cache_or_web")
                    try:
                        if top_score < int(config.MIN_CACHED_SCORE_BEFORE_AI):
                            call_ai_search = True
                            decision_reasons.append(f"low_score({top_score}<{int(config.MIN_CACHED_SCORE_BEFORE_AI)})")
                    except Exception:
                        pass
                    try:
                        tokens = [t.lower() for t in q.split() if len(t) > 3]
                        if cached_results and len(cached_results) < max(1, int(top_k)):
                            call_ai_search = True
                            decision_reasons.append("few_cached_results")
                        elif cached_results and tokens:
                            def _text_of(doc):
                                name = (doc.get("name") or "").lower()
                                snippet = (doc.get("snippet") or doc.get("content") or "").lower()
                                return name + " " + snippet
                            cache_hit = any(any(tok in _text_of(d) for tok in tokens) for d in cached_results)
                            if not cache_hit:
                                call_ai_search = True
                                decision_reasons.append("off_topic_cache")
                    except Exception:
                        pass

            logger.info(f"AI Search decision: call={call_ai_search} reasons={decision_reasons}")

            if call_ai_search:
                from knowledge_base import unified_search
                ai_search_results = await asyncio.to_thread(unified_search, q, top=top_k, user_id=cache_user_id)
                logger.info(f"✅ AI Search completed: {len(ai_search_results or [])} results returned")
                try:
                    if ai_search_results:
                        result_names = [d.get("name", "(no-name)") for d in ai_search_results]
                        logger.info(f"AI Search results: {', '.join(result_names[:3])}{'...' if len(result_names) > 3 else ''}")
                        
                        # Detailed logging of what AI search actually returned
                        logger.info("📋 AI Search Results Detail:")
                        for i, doc in enumerate(ai_search_results[:5], 1):  # Show first 5 results
                            name = doc.get("name", "(no-name)")
                            url = doc.get("url", doc.get("file_path", doc.get("webUrl", "(no-url)")))
                            score = doc.get("score", "N/A")
                            snippet = doc.get("snippet", doc.get("content", ""))[:150] + "..." if doc.get("snippet", doc.get("content", "")) else "(no content)"
                            source = "Graph" if doc.get("driveId") else "Cache/Local"
                            
                            logger.info(f"  [{i}] {name} (score: {score}, source: {source})")
                            logger.info(f"      URL: {url[:100]}{'...' if len(str(url)) > 100 else ''}")
                            logger.info(f"      Content: {snippet}")
                    else:
                        logger.info("AI Search returned no results")
                except Exception as e:
                    logger.warning(f"Error logging AI search details: {e}")
            else:
                # Heuristic: filename-style queries with attachments should prefer AI Search
                try:
                    q_lower = q.lower()
                    looks_filename = any(ext in q_lower for ext in (".pdf",".docx",".doc",".xlsx",".xls",".pptx",".ppt",".csv",".json",".xml"))
                    looks_docish = any(tok in q_lower for tok in ("form","report","invoice","contract"))
                    if attachments and (looks_filename or looks_docish) and not getattr(Config, "DISABLE_APIS_ON_ATTACHMENTS", False):
                        logger.info("Forcing AI Search for filename-style query with attachments")
                        from knowledge_base import unified_search
                        ai_search_results = await asyncio.to_thread(unified_search, q, top=top_k, user_id=cache_user_id)
                        decision_reasons.append("forced_filename_query_with_attachments")
                        logger.info(f"AI Search decision override: call=True reasons={decision_reasons}")
                        logger.info(f"✅ AI Search override completed: {len(ai_search_results or [])} results returned")
                        
                        # Detailed logging for override results too
                        try:
                            if ai_search_results:
                                logger.info("📋 AI Search Override Results Detail:")
                                for i, doc in enumerate(ai_search_results[:3], 1):  # Show first 3 results
                                    name = doc.get("name", "(no-name)")
                                    score = doc.get("score", "N/A")
                                    snippet = doc.get("snippet", doc.get("content", ""))[:100] + "..." if doc.get("snippet", doc.get("content", "")) else "(no content)"
                                    source = "Graph" if doc.get("driveId") else "Cache/Local"
                                    logger.info(f"  [{i}] {name} (score: {score}, source: {source}): {snippet}")
                        except Exception as e:
                            logger.warning(f"Error logging AI search override details: {e}")
                except Exception:
                    pass
                # Note: Graph results are NOT cached here - they will be cached AFTER
                # the LLM response is generated, and ONLY for documents that were actually used
            
            # Determine which results to use based on what was actually searched
            # Priority: AI search results (if called) > cached results
            if call_ai_search or ai_search_results:
                combined_doc_results = ai_search_results or []
                result_source = "AI Search"
                logger.info(f"Using AI Search results: {len(combined_doc_results)} documents")
                
                # Log relevance assessment for debugging
                if combined_doc_results and q:
                    query_terms = set(q.lower().split())
                    logger.info(f"🔍 Query terms for relevance check: {query_terms}")
                    for i, doc in enumerate(combined_doc_results[:3], 1):
                        name = doc.get("name", "").lower()
                        content = doc.get("snippet", doc.get("content", "")).lower()
                        matching_terms = [term for term in query_terms if term in name or term in content]
                        logger.info(f"  [{i}] {doc.get('name', '(no-name)')}: matching terms = {matching_terms if matching_terms else 'NONE'}")
            else:
                combined_doc_results = cached_results or []
                result_source = "Document Cache"
                logger.info(f"Using cached results: {len(combined_doc_results)} documents")
            
            # Ensure variables have default values
            if 'result_source' not in locals():
                result_source = "None"
            
            # Format combined results (both cache and AI search)
            if combined_doc_results:
                # Deduplicate by document name (case-insensitive) to avoid processing duplicates
                try:
                    seen_names = set()
                    deduped_results = []
                    for d in combined_doc_results:
                        name_key = (d.get("name") or d.get("title") or "").strip().lower()
                        if not name_key:
                            deduped_results.append(d)
                            continue
                        if name_key in seen_names:
                            continue
                        seen_names.add(name_key)
                        deduped_results.append(d)
                    if len(deduped_results) != len(combined_doc_results):
                        logger.info(f"Deduped documents by name: {len(combined_doc_results)} -> {len(deduped_results)}")
                    combined_doc_results = deduped_results
                except Exception:
                    pass
                # Cap to top 5 documents to keep processing fast
                if len(combined_doc_results) > 5:
                    combined_doc_results = combined_doc_results[:5]
                    logger.info("Capped combined document results to top 5")
                # CRITICAL: Fetch content for Graph results (check cache first to save time)
                # Live Graph search only returns metadata - we need actual document content
                max_extract = min(5, len(combined_doc_results))
                logger.info(f"Preparing content for top {max_extract} document(s)...")
                download_jobs = []
                graph_token = None
                download_sem = asyncio.Semaphore(3)

                async def _download_for_doc(doc: dict, name: str, web_url: str, drive_id: str, item_id: str):
                    nonlocal graph_token
                    async with download_sem:
                        if graph_token is None:
                            graph_token = await asyncio.to_thread(get_graph_token)
                        if not graph_token:
                            return None
                        return await asyncio.to_thread(download_and_extract_content, web_url, graph_token, name, drive_id, item_id)

                for idx_doc, doc in enumerate(combined_doc_results, 1):
                    if idx_doc > max_extract:
                        break
                    # Skip if we already have content (e.g., from cache search results)
                    if doc.get("content") and len(doc.get("content", "").strip()) > 50:
                        logger.info(f"Using existing content for: {doc.get('name', 'unknown')} ({len(doc.get('content', ''))} chars)")
                        continue
                    
                    # For Graph results without content, check cache first before downloading
                    if doc.get("_from_live_graph") or doc.get("driveId") or doc.get("itemId"):
                        try:
                            name = doc.get("name") or "Untitled"
                            web_url = doc.get("webUrl") or doc.get("url") or ""
                            drive_id = doc.get("driveId") or ""
                            item_id = doc.get("itemId") or ""

                            # OPTIMIZATION: Check cache first to avoid redundant downloads
                            doc_id = f"{drive_id}:{item_id}" if drive_id and item_id else web_url
                            cached_doc = None
                            try:
                                all_cached = cache.get_all_documents(cache_user_id, include_shared=True)
                                cached_doc = next((d for d in all_cached if d.get("id") == doc_id or d.get("url") == web_url or d.get("name") == name), None)
                            except Exception:
                                pass

                            if cached_doc and cached_doc.get("content") and len(cached_doc.get("content", "").strip()) > 50:
                                # Use cached content instead of downloading
                                doc["content"] = cached_doc["content"]
                                logger.info(f"✓ Using cached content for: {name} ({len(cached_doc['content'])} chars)")
                            else:
                                # Download content from Graph (not in cache or cache has no content)
                                task = asyncio.create_task(_download_for_doc(doc, name, web_url, drive_id, item_id))
                                download_jobs.append((doc, name, task))
                        except Exception as dl_err:
                            logger.warning(f"Error preparing content for {doc.get('name', 'unknown')}: {dl_err}")

                if download_jobs:
                    results = await asyncio.gather(*(t for _, _, t in download_jobs), return_exceptions=True)
                    for (doc, name, _task), result in zip(download_jobs, results):
                        if isinstance(result, Exception):
                            logger.warning(f"Failed to download content for: {name}")
                            continue
                        content = result or ""
                        if content and len(content.strip()) >= 10:
                            doc["content"] = content
                            logger.info(f"Downloaded content for: {name} ({len(content)} chars)")
                        else:
                            logger.warning(f"Failed to download content for: {name}")
                
                # Improve ordering: prioritize Azure Search relevance score and query token matches
                try:
                    q_tokens = [t.lower() for t in (q or "").split() if len(t) > 2]
                    def _rank(doc: dict) -> tuple:
                        score = float(doc.get("score") or 0)
                        text = ((doc.get("name") or "") + " " + (doc.get("snippet") or doc.get("content") or "")).lower()
                        match_hits = sum(1 for tok in q_tokens if tok in text)
                        # Tuple: (has_matches, score) so matches take precedence, then score
                        return (1 if match_hits > 0 else 0, score)
                    combined_doc_results.sort(key=_rank, reverse=True)
                except Exception:
                    pass
                logger.info(f"Formatting {len(combined_doc_results)} combined document results (cache + AI Search)")
                for idx, doc in enumerate(combined_doc_results, 1):
                    name = doc.get("name") or doc.get("title") or "Untitled"
                    url = doc.get("url") or doc.get("file_path") or doc.get("webUrl") or ""
                    content = doc.get("content", "")
                    # No truncation - use full content
                    snippet = doc.get("snippet", content if content else "")
                    from urllib.parse import quote
                    clean_url = quote(url, safe=':/?#[]@!$&\'()*+,;=')
                    
                    # Mark source for clarity
                    is_cached = doc in cached_results
                    source_label = "💾 Cached" if is_cached else "🔍 AI Search"
                    
                    doc_entries.append(
                        f"<div style=\"margin-bottom: 1.5rem;\">"
                        f"<h4 style=\"margin: 0.5rem 0; font-size: 1rem;\">[{idx}] {name} {source_label}</h4>"
                        f"<p style=\"margin: 0.25rem 0; font-size: 0.85rem; color: #666;\">"
                        f"<a href=\"{clean_url}\" style=\"text-decoration: none; color: #0078d4;\">View Document</a></p>"
                        f"<p style=\"margin: 0.5rem 0; font-size: 0.9rem; line-height: 1.4;\">{snippet if len(snippet) <= 500 else snippet[:500] + '…'}</p>"
                        f"</div>"
                    )
                    sources_refs.append(
                        f"<strong>[{idx}]</strong> "
                        f"<a href=\"{clean_url}\" style=\"color: #0078d4; text-decoration: none;\">{name}</a>"
                    )
                    full_contents.append({
                        "idx": idx,
                        "name": name,
                        "url": clean_url,
                        "content": content
                    })

                # Note: Web cache results will be processed separately and included alongside document results
                
                # Add combined results section
                citation_urls = [doc.get("url") or "" for doc in combined_doc_results]
                citation_example = " ".join([f"[{i+1}]({url})" for i, url in enumerate(citation_urls)])
                search_context += (
                    "\n\n<!-- combined-documents -->\n"
                    "<div style=\"font-size: 0.95rem; line-height: 1.5;\">"
                    "<h3 style=\"margin: 1rem 0 0.5rem 0; font-size: 1.1rem;\">📚 Combined Search Results (Cache + AI Search)</h3>"
                    + "".join(doc_entries) +
                    "<h4 style=\"margin: 1rem 0 0.5rem 0; font-size: 0.95rem;\">📌 Sources:</h4>"
                    "<ul style=\"margin: 0; padding-left: 1.5rem; font-size: 0.85rem;\">"
                    + "".join(f"<li>{ref}</li>" for ref in sources_refs) +
                    "</ul>"
                    "<p style=\"margin: 1rem 0 0; font-size: 0.85rem; font-style: italic; color: #666;\">"
                    "<strong>CITATION FORMAT:</strong> Use [1](URL) [2](URL) etc. "
                    f"Example: {citation_example}</p>"
                    "</div>"
                )
                # Add content without limits - no truncation
                used = 0
                for doc in full_contents:
                    raw = doc.get("content", "") or ""
                    # Use full content without any limits
                    search_context += (
                        f"\n\n<!-- full-document -->\n"
                        f"[FULL CONTENT OF DOCUMENT {doc['idx']}: {doc['name']}]\n{raw}\n[END OF DOCUMENT {doc['idx']}]\n"
                    )
            
            # Format Web cache results (if any)
            if web_results:
                logger.info(f"Formatting {len(web_results)} web cache results")
                web_entries = []
                web_refs = []
                for idx, page in enumerate(web_results, 1):
                    title = page.get("title") or page.get("url") or "Untitled"
                    url = page.get("url") or ""
                    content = page.get("content", "")
                    from urllib.parse import quote
                    clean_url = quote(url, safe=':/?#[]@!$&\'()*+,;=')
                    # No truncation - use full content
                    snippet = content
                    web_entries.append(
                        f"<div style=\"margin-bottom: 1.5rem;\">"
                        f"<h4 style=\"margin: 0.5rem 0; font-size: 1rem;\">[{idx}] {title} 🌐</h4>"
                        f"<p style=\"margin: 0.25rem 0; font-size: 0.85rem; color: #666;\">"
                        f"<a href=\"{clean_url}\" style=\"text-decoration: none; color: #0078d4;\">Open Page</a> (Web Cache)</p>"
                        f"<p style=\"margin: 0.5rem 0; font-size: 0.9rem; line-height: 1.4;\">{snippet}</p>"
                        f"</div>"
                    )
                    web_refs.append(
                        f"<strong>[{idx}]</strong> "
                        f"<a href=\"{clean_url}\" style=\"color: #0078d4; text-decoration: none;\">{title}</a>"
                    )
                citation_urls = [p.get("url") or "" for p in web_results]
                citation_example = " ".join([f"[{i+1}]({url})" for i, url in enumerate(citation_urls)])
                search_context += (
                    "\n\n<!-- web-cache -->\n"
                    "<div style=\"font-size: 0.95rem; line-height: 1.5;\">"
                    "<h3 style=\"margin: 1rem 0 0.5rem 0; font-size: 1.1rem;\">🌐 Web Results (Cached)</h3>"
                    + "".join(web_entries) +
                    "<h4 style=\"margin: 1rem 0 0.5rem 0; font-size: 0.95rem;\">📌 Sources:</h4>"
                    "<ul style=\"margin: 0; padding-left: 1.5rem; font-size: 0.85rem;\">"
                    + "".join(f"<li>{ref}</li>" for ref in web_refs) +
                    "</ul>"
                    "<p style=\"margin: 1rem 0 0; font-size: 0.85rem; font-style: italic; color: #666;\">"
                    "<strong>CITATION FORMAT:</strong> Use [1](URL) [2](URL) etc. "
                    f"Example: {citation_example}</p>"
                    "</div>"
                )

            # Format AI search results (if any)
            search_results = ai_search_results
            if search_results:
                doc_entries = []
                sources_refs = []
                full_contents = []
                for idx, doc in enumerate(search_results, 1):
                    name = doc.get("name") or doc.get("title") or "Untitled"
                    url = doc.get("file_path") or doc.get("url") or doc.get("webUrl") or ""
                    content = doc.get("content", "")
                    from urllib.parse import quote
                    clean_url = quote(url, safe=':/?#[]@!$&\'()*+,;=')
                    # No truncation - use full content
                    summary_content = content if content else "[No content extracted]"
                    doc_entries.append(
                        f"<div style=\"margin-bottom: 1.5rem;\">"
                        f"<h4 style=\"margin: 0.5rem 0; font-size: 1rem;\">[{idx}] {name} 📄</h4>"
                        f"<p style=\"margin: 0.25rem 0; font-size: 0.85rem; color: #666;\">"
                        f"<a href=\"{clean_url}\" style=\"text-decoration: none; color: #0078d4;\">View Document</a></p>"
                        f"<p style=\"margin: 0.5rem 0; font-size: 0.9rem; line-height: 1.4;\">{summary_content}</p>"
                        f"</div>"
                    )
                    sources_refs.append(
                        f"<strong>[{idx}]</strong> "
                        f"<a href=\"{clean_url}\" style=\"color: #0078d4; text-decoration: none;\">{name}</a>"
                    )
                    full_contents.append({
                        "idx": idx,
                        "name": name,
                        "url": clean_url,
                        "content": content
                    })
                citation_example = " ".join([f"[{i+1}]" for i in range(len(search_results))])  # AI search: no hyperlinks
                search_context += (
                    "\n\n<!-- ai-search -->\n"
                    "<div style=\"font-size: 0.95rem; line-height: 1.5;\">"
                    "<h3 style=\"margin: 1rem 0 0.5rem 0; font-size: 1.1rem;\">📄 Retrieved Documents (AI Search)</h3>"
                    + "".join(doc_entries) +
                    "<h4 style=\"margin: 1rem 0 0.5rem 0; font-size: 0.95rem;\">📌 Sources:</h4>"
                    "<ul style=\"margin: 0; padding-left: 1.5rem; font-size: 0.85rem;\">"
                    + "".join(f"<li>{ref}</li>" for ref in sources_refs) +
                    "</ul>"
                    "<p style=\"margin: 1rem 0 0; font-size: 0.85rem; font-style: italic; color: #666;\">"
                    "<strong>CITATION FORMAT:</strong> Use [1] [2] etc. (no hyperlinks for AI search)"
                    f"Example: {citation_example}</p>"
                    "</div>"
                )
            # Add trimmed AI Search content with budget limits
            if search_results:
                total_budget = int(getattr(Config, "MAX_TOTAL_CONTEXT_CHARS", 12000))
                per_doc_limit = int(getattr(Config, "MAX_DOC_CONTEXT_CHARS", 3000))
                used = 0
                for doc in full_contents:
                    if used >= total_budget:
                        break
                    raw = doc.get("content", "") or ""
                    chosen = raw[:per_doc_limit]
                    used += len(chosen)
                    search_context += (
                        f"\n\n<!-- full-document-content -->\n"
                        f"[FULL CONTENT OF DOCUMENT {doc['idx']}: {doc['name']}]\n{chosen}\n[END OF DOCUMENT {doc['idx']}]\n"
                    )

            # Final fallback: if all sources returned 0, provide a helpful message and avoid empty responses
            if not (cached_results or web_results or ai_search_results):
                search_context += (
                    "\n\n<!-- no-results -->\n"
                    + "<div style=\"font-size: 0.95rem; line-height: 1.5;\">"
                    + "<h3 style=\"margin: 1rem 0 0.5rem 0; font-size: 1.1rem;\">No results found</h3>"
                    + "<p>We exhaustively searched all available sources including:"
                    + "<ul style=\"margin: 0.5rem 0;\">"
                    + "<li>Local document cache</li>"
                    + "<li>Microsoft Graph API (OneDrive/SharePoint)</li>"
                    + "<li>Azure AI Search (indexed documents)</li>"
                    + "<li>Web search cache</li>"
                    + "</ul>"
                    + "No matching content was found for this query. Try rephrasing your question, including more specific terms, or check access permissions.</p>"
                    + "</div>"
                )
            
            # Only append references instruction if there are actual relevant external sources with content
            has_external_sources = (
                (combined_doc_results and result_source == "AI Search" and any(d.get("content") or d.get("snippet") for d in combined_doc_results)) or
                (web_results and any(w.get("content") for w in web_results))
                # Note: Only show references for AI search results and web results, not cached attachments
            )
            
            if has_external_sources:
                sources_searched_summary = []
                if combined_doc_results and result_source == "AI Search":
                    sources_searched_summary.append(f"AI Search (Azure): {len(combined_doc_results)} result(s)")
                elif combined_doc_results and result_source == "Document Cache":
                    sources_searched_summary.append(f"Document Cache: {len(combined_doc_results)} result(s)")
                if web_results:
                    sources_searched_summary.append(f"Web Cache: {len(web_results)} result(s)")
                if call_ai_search:
                    sources_searched_summary.append("Graph API: searched")
                
                search_context += (
                    "\n\n<!-- sources-searched-summary -->\n"
                    "[SOURCES SEARCHED - Include these in your References section]\n"
                    + "\n".join(f"- {s}" for s in sources_searched_summary)
                    + "\n[IMPORTANT: You MUST include a 'References:' section at the end of your response listing all sources with URLs]"
                )

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
    # Collect doc items for model (use cached + ai search snippets)
    model_doc_items = []
    for d in (cached_results or []):
        model_doc_items.append({
            "title": d.get("name") or "Untitled",
            "url": d.get("url") or "",
            "snippet": d.get("snippet") or d.get("content", "")
        })
    for d in (ai_search_results or []):
        model_doc_items.append({
            "title": d.get("name") or d.get("title") or "Untitled",
            "url": d.get("file_path") or d.get("url") or d.get("webUrl") or "",
            "snippet": d.get("content", "")
        })
    # Web plain text
    web_plain_text = ""
    if web_results:
        try:
            web_plain_text = "\n\n".join([(w.get("content") or "") for w in web_results if w.get("content")])
        except Exception:
            web_plain_text = ""

    # Send typing indicator before LLM processing
    await send_typing_indicator(ctx)
    
    llm_input, llm_log = build_llm_input(
        user_text=user_text or "",
        attachment_texts=attachment_texts,
        doc_items=model_doc_items,
        web_text=web_plain_text,
        personalization=personalization,
        memory_text=""  # Not extracting SDK memory; enforce budgets via prompt only
    )
    try:
        logger.info(f"LLM input sizes: {llm_log['sizes']} | est_tokens={llm_log['estimated_tokens']} | actions={llm_log['truncation_actions']} | docs={llm_log['doc_count']}")
    except Exception:
        pass

    # CALCULATION INTERCEPTOR: Detect calculation requests and use Python instead of LLM
    # This overcomes fundamental LLM arithmetic limitations
    calculation_result = None
    try:
        # Check if user wants a calculation and we have file data
        is_calc, calc_type = detect_calculation_intent(user_text or "")
        
        if is_calc and file_storage:
            # Calculator mode: process cached CSV files for comparison/aggregation
            csv_files = []
            for stored_file in file_storage:
                fname = stored_file.get("name", "").lower()
                if fname.endswith('.csv') or 'csv' in fname:
                    csv_files.append({
                        "name": stored_file.get("name", "Unknown"),
                        "content": stored_file.get("content", "")
                    })
            
            logger.info(f"Calculator: Found {len(csv_files)} CSV files for calculation")
            
            if len(csv_files) >= 2:
                # Multi-file calculation
                logger.info(f"Calculator: Multi-file mode - processing {len(csv_files)} files")
                calculation_result = process_multi_file_calculation(user_text, csv_files)
                if calculation_result:
                    logger.info(f"Calculator: Multi-file calculation successful - bypassing LLM")
            elif len(csv_files) == 1:
                # Single file calculation
                logger.info(f"Calculator: Single-file mode - '{csv_files[0]['name']}'")
                calculation_result = process_calculation_request(
                    user_text, 
                    csv_files[0]["content"], 
                    csv_files[0]["name"]
                )
                if calculation_result:
                    logger.info(f"Calculator: Single-file calculation successful - bypassing LLM")
            
            # Fallback to current attachments if no stored CSV files
            if not calculation_result and attachment_texts:
                for att_text in attachment_texts:
                    if att_text and len(att_text) > 100:
                        source_name = file_storage[-1].get("name", "Uploaded Document") if file_storage else "Uploaded Document"
                        logger.info(f"Calculator: Using current attachment ({len(att_text)} chars)")
                        calculation_result = process_calculation_request(user_text, att_text, source_name)
                        if calculation_result:
                            break
    except Exception as calc_err:
        logger.warning(f"Calculator error (falling back to LLM): {calc_err}")
        calculation_result = None

    # If calculation was successful, return it directly without LLM
    if calculation_result:
        ctx.stream.emit(calculation_result)
        logger.info("Calculation response sent (bypassed LLM)")
        
        # Save conversation memory
        try:
            memory = memory_for(conversation_id)
            _save_conversation_memory(conversation_id, memory)
        except Exception:
            pass
        return

    # Skip secondary token budget enforcement - build_llm_input already handles truncation
    # Azure OpenAI will handle final token limits gracefully
    # The build_llm_input function already truncated content appropriately

    chat_prompt = ChatPrompt(model)
    
    # Throttle streaming to prevent Bot Framework 429 errors
    last_emit_time = 0
    chunk_buffer = []
    MIN_CHUNK_INTERVAL = config.STREAM_CHUNK_INTERVAL  # From config (default 300ms)
    
    async def throttled_emit(chunk: str):
        """Emit chunks with rate limiting to prevent Bot Framework 429 errors"""
        nonlocal last_emit_time, chunk_buffer
        
        chunk_buffer.append(chunk)
        current_time = time.time()
        time_since_last = current_time - last_emit_time
        
        # If enough time has passed, emit buffered chunks
        if time_since_last >= MIN_CHUNK_INTERVAL:
            if chunk_buffer:
                combined = "".join(chunk_buffer)
                ctx.stream.emit(combined)
                chunk_buffer = []
                last_emit_time = current_time
        # Otherwise, buffer chunks and they'll be emitted in next interval or at end
    
    # Use Teams SDK built-in streaming with retry logic and throttling
    try:
        # Send final typing indicator before generating response
        await send_typing_indicator(ctx)
        
        async def make_chat_call():
            async with llm_semaphore:
                return await chat_prompt.send(
                    input=llm_input,
                memory=memory_for(conversation_id),
                instructions=BASE_INSTRUCTIONS,
                on_chunk=throttled_emit,
            )
        
        chat_result = await call_llm_with_retry(make_chat_call)
        
        # Emit any remaining buffered chunks
        if chunk_buffer:
            combined = "".join(chunk_buffer)
            ctx.stream.emit(combined)
            chunk_buffer = []
        
        # Log response for debugging
        try:
            response_text = str(chat_result).strip() if chat_result else "(empty)"
            logger.info(f"Chat response completed | Length: {len(response_text)} chars | Preview: {response_text[:100]}...")
        except Exception:
            logger.info("Chat response completed")
        
        # CACHE DOCUMENTS POST-RESPONSE: Only cache documents from live Graph search that were used
        # Save conversation memory after response for instant recovery on next turn
        try:
            memory = memory_for(conversation_id)
            _save_conversation_memory(conversation_id, memory)
        except Exception as mem_err:
            logger.debug(f"Failed to save conversation memory: {mem_err}")
        
        # This ensures we don't cache all searched documents, only the ones that provided value
        try:
            # Check if we have any Graph results to potentially cache
            graph_docs_to_cache = [d for d in (ai_search_results or []) if d.get("_from_live_graph")]
            
            if graph_docs_to_cache:
                logger.info(f"Post-response caching: Found {len(graph_docs_to_cache)} Graph documents from live search")
                
                # Simple heuristic: Cache all returned documents from Graph since they were shown to user
                # In a more sophisticated implementation, we could parse the response to see which citations were used
                token = await asyncio.to_thread(get_graph_token)
                if token and cache_user_id:
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
                        doc, content = result
                        try:
                            name = doc.get("name") or doc.get("title") or "Untitled"
                            web_url = doc.get("webUrl") or doc.get("url") or ""
                            drive_id = doc.get("driveId") or ""
                            item_id = doc.get("itemId") or ""
                            
                            # Only cache if content is valid
                            if content and len(content.strip()) >= 10 and "[Unable to download" not in content and "[Error extracting" not in content:
                                # Use composite id for stability
                                doc_id = f"{drive_id}:{item_id}" if drive_id and item_id else (web_url or name)
                                
                                # For CSV files, chunk the content for better search granularity
                                is_csv = name.lower().endswith('.csv')
                                if is_csv:
                                    chunks = chunk_csv_for_cache(content, name)
                                    for chunk_id, chunk_content in chunks:
                                        if _is_personal_url(web_url):
                                            cache.add_document(f"{doc_id}:{chunk_id}", f"{name} (chunk)", web_url, chunk_content, user_id=cache_user_id, metadata={"source":"graph_search_used", "is_csv_chunk": True, "chunk_id": chunk_id})
                                        else:
                                            cache.add_shared_document(f"{doc_id}:{chunk_id}", f"{name} (chunk)", web_url, chunk_content, metadata={"source":"graph_search_used", "is_csv_chunk": True, "chunk_id": chunk_id})
                                else:
                                    # Non-CSV files cached normally
                                    if _is_personal_url(web_url):
                                        cache.add_document(doc_id, name, web_url, content, user_id=cache_user_id, metadata={"source":"graph_search_used"})
                                    else:
                                        cache.add_shared_document(doc_id, name, web_url, content, metadata={"source":"graph_search_used"})
                                
                                cached_count += 1
                                logger.info(f"Cached document: {name} (from Graph live search)")
                        except Exception as cache_err:
                            logger.warning(f"Failed to cache document {doc.get('name', 'unknown')}: {cache_err}")
                    
                    if cached_count > 0:
                        logger.info(f"✓ Post-response caching completed: {cached_count}/{len(graph_docs_to_cache)} documents cached")
                    else:
                        logger.info("Post-response caching: No documents were cached (all failed or already cached)")
                else:
                    logger.warning("Post-response caching skipped: Graph token or user ID unavailable")
        except Exception as post_cache_err:
            logger.error(f"Post-response caching error: {post_cache_err}", exc_info=False)
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg:
            logger.error(f"Rate limit error (Bot Framework API): {e}")
            await ctx.send(MessageActivityInput(text="⚠️ The bot is sending messages too quickly. Please wait a moment and try again.").add_ai_generated())
        else:
            logger.error(f"Chat error: {e}")
            await ctx.send(MessageActivityInput(text="Sorry, I encountered an error processing your request. Please try again in a moment.").add_ai_generated())
        return


# ---------------------------
# Event handlers
# ---------------------------
async def send_welcome_message(ctx: ActivityContext):
    """Send welcome message for new users or when requested"""
    try:
        welcome_message = """👋 **Welcome to SwopeAI!**

I'm your intelligent assistant powered by AI. Here's what I can help you with:

📄 **Document Analysis**
• Upload and analyze PDFs, Word docs, Excel files, and more
• Extract information and summarize content
• Compare multiple documents

🔍 **Smart Search**
• Search your SharePoint and OneDrive files
• Find information across your organization's documents
• Get answers from indexed web content

💬 **Natural Conversations**
• Ask questions in plain English
• Get detailed explanations with source citations
• Access current date/time information

🏥 **Swope Health Information**
• Learn about services, locations, and healthcare offerings
• Get organization-specific information

**Ready to get started?** Try asking me something like:
• "What can you help me with?"
• "Tell me about Swope Health"
• "Search my SharePoint files"

Or simply upload a document and ask me to analyze it! 📎"""

        await ctx.send(MessageActivityInput(text=welcome_message).add_ai_generated())
        logger.info("Welcome message sent to user")
        
    except Exception as e:
        logger.error(f"Error sending welcome message: {e}", exc_info=True)
        # Fallback simple welcome message
        try:
            await ctx.send(MessageActivityInput(text="👋 Hello! I'm SwopeAI, your intelligent assistant. How can I help you today?").add_ai_generated())
        except Exception as fallback_error:
            logger.error(f"Failed to send fallback welcome message: {fallback_error}")

@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]):
    try:
        logger.info("=" * 60)
        logger.info(f"MESSAGE RECEIVED | Text: {ctx.activity.text[:100] if ctx.activity.text else 'None'}")
        logger.info(f"From: {getattr(ctx.activity.from_, 'name', 'Unknown')} | Conv: {ctx.activity.conversation.id if ctx.activity.conversation else 'Unknown'}")
        logger.info("=" * 60)
        
        # Handle welcome for first-time users or specific welcome commands
        user_text = (ctx.activity.text or "").strip().lower()
        if user_text in ["hello", "hi", "start", "welcome", "help"]:
            # This might be a first interaction, consider showing welcome
            await send_welcome_message(ctx)
            return  # Don't process further for welcome commands
        
        await handle_stateful_conversation(model, ctx)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Message handling error: {error_msg}", exc_info=True)
        
        # Provide user-friendly error messages based on error type
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            user_message = "⚠️ I'm experiencing a temporary connection issue. Please try your message again in a moment."
        elif "token" in error_msg.lower() or "auth" in error_msg.lower():
            user_message = "⚠️ There was an authentication issue. Please try again. If this persists, the bot may need to be restarted."
        elif "openai" in error_msg.lower() or "429" in error_msg:
            user_message = "⚠️ The AI service is temporarily busy. Please wait a moment and try again."
        elif "graph" in error_msg.lower():
            user_message = "⚠️ I had trouble accessing Microsoft 365 resources. Please try again."
        else:
            user_message = f"⚠️ Sorry, I encountered an unexpected error. Please try again.\n\n_Error: {error_msg[:100]}_"
        
        try:
            await ctx.send(MessageActivityInput(text=user_message).add_ai_generated())
        except Exception as send_error:
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
                user_message = "⚠️ I'm experiencing connectivity issues. Please try your message again."
            else:
                user_message = "⚠️ Something went wrong. Please try again in a moment."
            
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


def check_web_indexing_dependencies():
    """Check if required web indexing dependencies are available"""
    try:
        import aiohttp
        logger.info("✓ aiohttp is installed")
    except ImportError:
        logger.error("✗ aiohttp is NOT installed - web indexing will not work. Run: pip install aiohttp")
        return False
    
    try:
        from bs4 import BeautifulSoup
        logger.info("✓ beautifulsoup4 is installed")
    except ImportError:
        logger.error("✗ beautifulsoup4 is NOT installed - web indexing will not work. Run: pip install beautifulsoup4")
        return False
    
    return True


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
        logger.info(f"Configured TENANT_ID: {config.APP_TENANTID or 'not set'}")
        logger.info(f"Configured CLIENT_ID: {config.APP_ID or 'not set'}")
        logger.info(f"Configured SENDER_UPN: {config.SENDER_UPN or 'not set'}")
        logger.info("Token strategy: Graph app-only via OAuth2 client credentials (.default scope). No Bot Framework UserToken API.")
    except Exception:
        pass
    
    # Check web indexing dependencies first
    logger.info("Checking web indexing dependencies...")
    deps_ok = check_web_indexing_dependencies()
    
    # Preload document cache into memory for instant access
    logger.info("Preloading document cache into memory...")
    try:
        cache = get_cache()
        all_cached_docs = cache.get_all_documents(None, include_shared=True)
        total_content_size = sum(len(d.get("content", "")) for d in (all_cached_docs or []))
        logger.info(f"✓ Cache preloaded: {len(all_cached_docs or [])} documents ({total_content_size / 1024 / 1024:.2f} MB)")
    except Exception as cache_load_err:
        logger.warning(f"Failed to preload cache: {cache_load_err}")
    
    # Start web indexing in background (don't await - let it run parallel to app)
    try:
        # Create web indexing tasks but don't block on them
        logger.info("Initializing web indexer...")
        web_indexer = get_web_indexer()
        logger.info("✓ Web indexer singleton created")
        
        external_sources = Config.EXTERNAL_WEB_SOURCES or ""
        logger.info(f"EXTERNAL_WEB_SOURCES config: {external_sources}")
        
        if external_sources and deps_ok:
            urls = [url.strip() for url in external_sources.split(",") if url.strip()]
            logger.info(f"Initiating web indexing for {len(urls)} sources: {urls}")
            
            for url in urls:
                try:
                    # Check if domain is already indexed and completed or failed
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc or urlparse(url).path
                    cache = web_indexer.cache.get("websites", {}).get(domain, {})
                    
                    if cache.get("status") == "completed" and cache.get("pages"):
                        logger.info(f"Domain {domain} already fully indexed ({len(cache['pages'])} pages). Skipping re-indexing.")
                        continue
                    
                    if cache.get("status") == "failed":
                        logger.info(f"Domain {domain} previously marked as failed. Skipping crawl.")
                        continue
                    
                    logger.info(f"Creating indexing task for: {url}")
                    task = asyncio.create_task(
                        web_indexer.crawl_website(
                            url,
                            max_pages=Config.WEB_CRAWL_MAX_PAGES,
                            max_depth=Config.WEB_CRAWL_MAX_DEPTH
                        )
                    )
                    add_background_task(task, f"web_indexing_{domain}")
                    logger.info(f"✓ Created background indexing task for: {url}")
                except Exception as e:
                    logger.error(f"✗ Failed to create indexing task for {url}: {e}", exc_info=True)
        elif not deps_ok:
            logger.error("✗ Web indexing dependencies missing - web crawling disabled")
        else:
            logger.info("No external web sources configured for indexing")
    except Exception as e:
        logger.error(f"✗ Error initializing web indexing: {e}", exc_info=True)

    # Background SharePoint/OneDrive crawling DISABLED
    # All searches are now purely live via Graph API
    # Cache is only populated from documents actually used in responses
    logger.info("SharePoint/OneDrive background crawling: DISABLED (live search only)")
    
    # Start the Teams app (this will run indefinitely)
    logger.info("Starting Teams AI app...")
    logger.info("=" * 60)
    await app.start()


# Entry point
# ---------------------------
if __name__ == "__main__":
    asyncio.run(startup())
