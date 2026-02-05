# =====================================================
# AI Search (Azure Cognitive Search) Integration
# =====================================================
import os
import tempfile
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

def _map_search_doc(raw: dict) -> dict:
    """Normalize Azure Search doc fields to common keys and derive a snippet.

    Prefer `content`/`text`/`chunk` when available. Otherwise, use semantic
    `@search.captions` or simple `@search.highlights` to populate a short snippet
    so results can be surfaced even when the index doesn't make raw content
    retrievable.
    """
    try:
        name = (
            raw.get("name") or raw.get("title") or raw.get("metadata_storage_name") or raw.get("file_name") or raw.get("filename")
        )
        url = (
            raw.get("file_path") or raw.get("url") or raw.get("metadata_storage_path") or raw.get("webUrl")
        )

        # Base content fields
        content = raw.get("content") or raw.get("text") or raw.get("chunk") or ""

        # Derive snippet from captions/highlights when content is missing
        snippet = content
        try:
            captions = raw.get("@search.captions") or []
            if not snippet and isinstance(captions, list) and captions:
                # captions: [{"text": "...", "highlights": ["..."]}]
                cap_texts = []
                for c in captions[:3]:
                    t = (c.get("text") or "").strip()
                    if t:
                        cap_texts.append(t)
                snippet = " \n".join(cap_texts).strip()
        except Exception:
            pass

        try:
            highlights = raw.get("@search.highlights") or {}
            if not snippet and isinstance(highlights, dict) and highlights:
                # Common highlight keys: content, text, chunk, metadata_storage_name
                hl_keys = ("content", "text", "chunk")
                hl_texts = []
                for k in hl_keys:
                    vals = highlights.get(k) or []
                    if isinstance(vals, list):
                        for v in vals[:3]:
                            s = (v or "").strip()
                            if s:
                                hl_texts.append(s)
                if hl_texts:
                    snippet = " \n".join(hl_texts).strip()
        except Exception:
            pass

        return {
            "id": raw.get("id"),
            "name": name or "Untitled",
            "file_path": url or "",
            "url": url or "",
            "content": snippet or "",
            "file_type": raw.get("file_type"),
            "upload_date": raw.get("upload_date"),
            "score": raw.get("@search.score"),
        }
    except Exception:
        return {"name": "Untitled", "file_path": "", "url": "", "content": ""}

def search_documents(query: str, top: int = 5) -> list:
    """Search Azure Cognitive Search using the exact semantic-only body requested."""
    endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
    # Ensure correct index name; default to 'swope-vector-documents' if unset
    index_name = os.getenv("AZURE_SEARCH_INDEX") or "swope-vector-documents"
    api_key = os.getenv("AZURE_SEARCH_KEY")
    # API version
    api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2023-10-01-Preview")
    if not endpoint or not index_name or not api_key:
        logger.error("Missing Azure Cognitive Search configuration.")
        return []
    
    # Preprocess query to improve search accuracy
    # Remove or simplify special characters and path-like syntax
    cleaned_query = query.strip()
    # Remove backslashes and path separators
    cleaned_query = cleaned_query.replace("\\", " ").replace("/", " ")
    # Remove extra parentheses and special chars that confuse Azure Search
    cleaned_query = cleaned_query.replace("(", "").replace(")", "").replace("-", " ")
    # Collapse multiple spaces
    cleaned_query = " ".join(cleaned_query.split())
    
    logger.info(f"Query preprocessing: '{query}' → '{cleaned_query}'")
    
    url = f"{endpoint}/indexes/{index_name}/docs/search"
    params = {"api-version": api_version}
    headers = {"Content-Type": "application/json", "api-key": api_key}

    # Exact semantic-only body as requested
    sem_cfg = (os.getenv("AZURE_SEARCH_SEMANTIC_CONFIG") or "semantic-config").strip()
    
    # IMPORTANT: Azure Semantic Search requires a non-empty base 'search' text.
    # Using '*' with queryType='semantic' returns HTTP 400.
    # Therefore, always pass the cleaned query string.
    search_term = cleaned_query
    
    exact_body = {
        "search": search_term,  # Non-empty query required for semantic ranking
        "count": True,
        "queryType": "semantic",
        "semanticConfiguration": sem_cfg,
        "captions": "extractive",
        "answers": "extractive|count-3",
        "top": min(top * 10, 50),  # Request more results for better semantic ranking
        "queryLanguage": "en-us"  # Improve semantic understanding
    }
    
    # Log final body for diagnostics
    logger.info(f"Azure Search semantic body prepared: {exact_body}")

    try:
        logger.info(f"Azure Search semantic request: index={index_name} body={exact_body}")
        resp = requests.post(url, params=params, headers=headers, json=exact_body, timeout=30)
        if resp.status_code == 200:
            data = resp.json() or {}
            items = data.get("value", []) or []
            answers = data.get("@search.answers") or []
            if items:
                logger.info(f"Azure Search (semantic) returned {len(items)} results")
                mapped = [_map_search_doc(d) for d in items[:top]]
                # Prepend a synthetic semantic answer result if available
                try:
                    if isinstance(answers, list) and answers:
                        ans = answers[0]
                        ans_text = (ans.get("text") or "").strip()
                        if ans_text and items:
                            first = items[0]
                            mapped.insert(0, {
                                "id": "semantic-answer",
                                "name": first.get("name") or first.get("title") or "Answer",
                                "file_path": first.get("file_path") or first.get("url") or first.get("metadata_storage_path") or "",
                                "url": first.get("file_path") or first.get("url") or first.get("metadata_storage_path") or "",
                                "content": ans_text,
                                "file_type": "semantic-answer",
                                "score": ans.get("score")
                            })
                except Exception:
                    pass
                return mapped
        else:
            logger.warning(f"Azure Search (semantic) error: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Azure Search (semantic) request failed: {e}")

    logger.info("Azure Search returned 0 results across semantic pass")
    return []

# =====================================================
# Unified Search API
# =====================================================
def unified_search(query: str, top: int = 5, user_context: bool = True, user_id: Optional[str] = None) -> list:
    """Search using local cache first, then live Graph search for OneDrive/SharePoint.
    
    Search flow:
    - Cache: Searches previously crawled SharePoint/OneDrive docs (fastest)
    - Live Graph API: Real-time search of OneDrive/SharePoint when cache is empty or has low results
    - AI Search: Optional indexed document search (if configured)
    
    Args:
        query: Search query
        top: Max results to return
        user_context: Whether to use user context (always True for security)
        user_id: User's ID for personalized results and access control
    
    Returns:
        Combined list of search results with deduplication
    """
    # Ensure user profile is cached for better experience
    if user_id:
        ensure_user_profile_cached(user_id)
    
    all_results = []
    seen_ids = set()
    cache_has_results = False
    cache_has_no_results = False
    
    # Step 1: Try cache first (SharePoint/OneDrive documents already crawled)
    if user_id:
        cache = get_cache()
        cache_results = cache.search_cache_scored(query, user_id=user_id, limit=top)
        if cache_results:
            cache_has_results = True
            first_item = cache_results[0]
            if isinstance(first_item, dict):
                top_score = first_item.get("score", 0)
            else:
                top_score = first_item[1] if len(first_item) > 1 else 0
            logger.info(f"📦 Cache returned {len(cache_results)} results (top score={top_score}) for '{query}'")
            
            # Add cache results to the combined list
            for item in cache_results:
                if isinstance(item, dict):
                    doc = item.get("doc", {})
                else:
                    doc = item[0] if item else {}
                doc_id = doc.get("id") or doc.get("file_path")
                if doc_id and doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    all_results.append(doc)
            
            # If cache has high relevance, skip live Graph/AI search
            min_score_for_graph_skip = getattr(config, "MIN_CACHED_SCORE_BEFORE_GRAPH", 55)
            if top_score >= min_score_for_graph_skip:
                logger.info(
                    f"✓ Cache has high relevance results (score={top_score}), "
                    f"skipping live Graph/AI search (threshold={min_score_for_graph_skip})"
                )
                return all_results
            else:
                logger.info(
                    f"⚠️ Cache results below threshold (score={top_score} < {min_score_for_graph_skip}), "
                    "will perform live Graph search"
                )
                # Continue to Graph search below
        else:
            cache_has_no_results = True
            logger.info(f"📦 Cache returned 0 results for '{query}' - live Graph search REQUIRED")
    
    # Step 2: Perform live Graph search if cache is empty OR has low-score results
    # Check if we should call Graph based on cache quality
    should_call_graph = cache_has_no_results or not cache_has_results
    if cache_has_results and all_results:
        # We have cache results - check if score is good enough
        first_item = cache_results[0]
        if isinstance(first_item, dict):
            top_score = first_item.get("score", 0)
        else:
            top_score = first_item[1] if len(first_item) > 1 else 0
        min_score_for_graph_skip = getattr(config, "MIN_CACHED_SCORE_BEFORE_GRAPH", 55)
        if top_score < min_score_for_graph_skip:
            should_call_graph = True
    
    if should_call_graph:
        logger.info("🔍 Performing live Graph API search for OneDrive/SharePoint")
        token = get_graph_token()
        if token:
            try:
                graph_data = search_sharepoint(query, token, user_context=user_context)
                graph_results = graph_data.get("results", [])
                
                if graph_results:
                    logger.info(f"✓ Live Graph search returned {len(graph_results)} results")
                    # Add Graph results to combined list (deduped)
                    # Check cache first and populate content if available to save time
                    for doc in graph_results[:top]:  # Limit to top N
                        doc_id = doc.get("id") or doc.get("webUrl") or doc.get("file_path")
                        if doc_id and doc_id not in seen_ids:
                            seen_ids.add(doc_id)
                            # Mark as from Graph for tracking
                            doc["_from_live_graph"] = True
                            
                            # OPTIMIZATION: Check if this document is already cached with content
                            try:
                                web_url = doc.get("webUrl") or ""
                                drive_id = doc.get("driveId") or ""
                                item_id = doc.get("itemId") or ""
                                name = doc.get("name") or ""
                                composite_id = f"{drive_id}:{item_id}" if drive_id and item_id else web_url
                                
                                # Try to find in cache by multiple identifiers
                                all_cached = cache.get_all_documents(user_id, include_shared=True)
                                cached_doc = next((d for d in all_cached 
                                                 if d.get("id") == composite_id 
                                                 or d.get("url") == web_url 
                                                 or (d.get("name") == name and name)), None)
                                
                                if cached_doc and cached_doc.get("content") and len(cached_doc.get("content", "").strip()) > 50:
                                    # Use cached content - no download needed!
                                    doc["content"] = cached_doc["content"]
                                    doc["_from_cache"] = True  # Mark as cache-populated
                                    logger.info(f"✓ Populated Graph result with cached content: {name} ({len(cached_doc['content'])} chars)")
                            except Exception as cache_err:
                                logger.debug(f"Cache lookup failed for {doc.get('name', 'unknown')}: {cache_err}")
                            
                            all_results.append(doc)
                    # If Graph returned results, return them immediately (no combining with other sources)
                    return all_results
                else:
                    logger.info(f"⚠️ Live Graph search returned no results for '{query}'")
                    
                    # IMPORTANT: Clear low-relevance cache results when Graph search fails
                    # This ensures AI Search can be attempted instead of returning irrelevant cached docs
                    if cache_has_results and all_results:
                        # Calculate relevance score for cache clearing decision
                        try:
                            relevance_threshold = 15  # Minimum score to keep when Graph fails
                            should_clear_cache = False
                            top_score = "unknown"
                            
                            # Check if we have the original cache results to examine their scores
                            if 'cache_results' in locals() and cache_results and len(cache_results) > 0:
                                first_item = cache_results[0] 
                                if isinstance(first_item, dict):
                                    top_score = first_item.get("score", 0)
                                else:
                                    top_score = first_item[1] if len(first_item) > 1 else 0
                            else:
                                # Fallback: Check the score in the all_results directly
                                for result in all_results:
                                    if isinstance(result, dict) and 'score' in result:
                                        top_score = result['score']
                                        break
                                if top_score == "unknown":
                                    top_score = 0  # Default to low score
                            
                            # Decision: clear cache if score is too low when Graph fails
                            if isinstance(top_score, (int, float)) and top_score < relevance_threshold:
                                should_clear_cache = True
                            elif top_score == "unknown":
                                should_clear_cache = True  # Clear if we can't determine relevance
                            
                            if should_clear_cache:
                                logger.info(f"🗑️ Clearing low-relevance cache results (score={top_score} < {relevance_threshold}) since Graph found nothing relevant")  
                                all_results = []  # Clear cache results to allow AI Search
                                seen_ids = set()  # Reset seen IDs  
                                cache_has_results = False  # Update flag
                            else:
                                logger.info(f"✓ Keeping cache results (score={top_score} >= {relevance_threshold}) even though Graph found nothing")
                                
                        except Exception as clear_err:
                            logger.warning(f"Error in cache clearing logic: {clear_err}, proceeding with cache results")
                            # Leave cache results as-is if we can't determine their quality
            except Exception as e:
                logger.error(f"❌ Live Graph search error: {e}")
        else:
            logger.warning("⚠️ Graph token unavailable for live search")
    
    # Step 3: Always try AI Search (Azure Cognitive Search) if cache + Graph returned no results
    # This ensures we search everywhere before saying nothing was found
    if not all_results:
        logger.info(f"🔎 Performing AI Search (Azure Cognitive Search) as final comprehensive search")
        ai_results = search_documents(query, top=top)
        if ai_results:
            logger.info(f"✓ AI Search returned {len(ai_results)} results")
            for doc in ai_results:
                doc_id = doc.get("id") or doc.get("file_path")
                if doc_id and doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    all_results.append(doc)
        else:
            logger.info(f"⚠️ AI Search returned no results")
    
    # Step 4: Web search fallback if nothing found anywhere else (if configured)
    if not all_results and getattr(config, "ENABLE_WEB_SEARCH_FALLBACK", True):
        logger.info(f"🌐 Performing web search as final fallback")
        try:
            from web_indexer import get_web_indexer
            indexer = get_web_indexer()
            web_results = indexer.search_web_cache(query, limit=3)
            if web_results:
                logger.info(f"✓ Web search returned {len(web_results)} results")
                for doc in web_results:
                    doc_id = doc.get("id") or doc.get("url") or doc.get("title")
                    if doc_id and doc_id not in seen_ids:
                        seen_ids.add(doc_id)
                        doc["_from_web"] = True
                        all_results.append(doc)
            else:
                logger.info(f"⚠️ Web search returned no results")
        except Exception as e:
            logger.debug(f"Web search failed: {e}")
    
    # Return final results
    if all_results:
        logger.info(f"✓ Returning {len(all_results)} results from {len(seen_ids)} unique sources")
        return all_results
    
    logger.info(f"❌ No results found in ANY source (cache, live Graph, AI Search, or web) for '{query}'")
    return []
"""
Knowledge Base - Graph API, Search, and Document Processing (FIXED)

Key fixes:
- Proper Graph shareId encoding: u!<base64url(url)>
- Graph fallback triggered on more than 401/403 (e.g., 404, 302, HTML/empty response)
- Consistent use of config.GRAPH_TIMEOUT
- sendMail fixed for app-only auth: /users/{SENDER_UPN}/sendMail
- Better logging and error diagnostics
"""

import io
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import base64
import tempfile
import json
import os
import time
from urllib.parse import urlparse, unquote
from typing import Optional
import re
from config import Config
from document_cache import get_cache

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
config = Config()

# Create a session with connection pooling to prevent socket exhaustion
# This is crucial for long-running bots that make many HTTP requests
_http_session = None

def get_http_session() -> requests.Session:
    """Get a reusable HTTP session with connection pooling and retry logic."""
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        
        # Configure retry strategy for transient failures
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
        )
        
        # Configure connection pooling
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,  # Number of connection pools
            pool_maxsize=20,      # Max connections per pool
            pool_block=False,     # Don't block when pool is full
        )
        
        _http_session.mount("http://", adapter)
        _http_session.mount("https://", adapter)
        
        # Set default timeout and headers
        _http_session.headers.update({
            "Connection": "keep-alive",
        })
        
        logger.info("HTTP session created with connection pooling")
    
    return _http_session


def session_get(url, **kwargs):
    """Make a GET request using the connection-pooled session."""
    return get_http_session().get(url, **kwargs)


def session_post(url, **kwargs):
    """Make a POST request using the connection-pooled session."""
    return get_http_session().post(url, **kwargs)


# User profile cache to store display names and UPNs
_USER_PROFILE_CACHE = {}
_USER_PROFILES_CACHE_FILE = os.path.join(os.path.dirname(__file__), "user_profiles_cache.json")


def _retry_request(func, max_retries=3, initial_delay=1.0, backoff_factor=2.0):
    """Retry a request with exponential backoff on timeout errors."""
    delay = initial_delay
    last_error = None
    
    for attempt in range(max_retries):
        try:
            return func()
        except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt < max_retries - 1:
                logger.warning(f"Request timeout (attempt {attempt + 1}/{max_retries}). Retrying in {delay}s: {str(e)[:100]}")
                time.sleep(delay)
                delay *= backoff_factor
            else:
                logger.error(f"Request failed after {max_retries} attempts: {str(e)[:100]}")
    
    raise last_error


def _load_profiles_from_disk() -> dict:
    """Load user profiles from disk cache."""
    try:
        if os.path.exists(_USER_PROFILES_CACHE_FILE):
            with open(_USER_PROFILES_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load profiles from disk: {e}")
    return {}


def _save_profiles_to_disk(profiles: dict) -> None:
    """Save user profiles to disk cache."""
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(_USER_PROFILES_CACHE_FILE), exist_ok=True)
        
        # Write directly (simpler than atomic, less chance of Windows locking issues)
        with open(_USER_PROFILES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save profiles to disk: {e}")


def _get_profile_from_cache(user_id: str) -> Optional[dict]:
    """Get profile from memory or disk cache."""
    # Check memory first
    if user_id in _USER_PROFILE_CACHE:
        return _USER_PROFILE_CACHE[user_id]
    
    # Check disk cache
    disk_cache = _load_profiles_from_disk()
    if user_id in disk_cache:
        # Load into memory for faster access
        _USER_PROFILE_CACHE[user_id] = disk_cache[user_id]
        return disk_cache[user_id]
    
    return None


def _save_profile_to_cache(user_id: str, profile: dict) -> None:
    """Save profile to both memory and disk cache."""
    # Save to memory
    _USER_PROFILE_CACHE[user_id] = profile
    
    # Save to disk
    disk_cache = _load_profiles_from_disk()
    disk_cache[user_id] = profile
    _save_profiles_to_disk(disk_cache)

SUPPORTED_DOC_EXTENSIONS = {
    ".docx",
    ".doc",
    ".pdf",
    ".xlsx",
    ".xls",
    ".csv",
    ".txt",
    ".pptx",
    ".ppt",
    ".json",
    ".xml",
}
GRAPH_CRAWL_MAX_ITEMS_PER_DRIVE = int(os.environ.get("GRAPH_CRAWL_MAX_ITEMS_PER_DRIVE", "300"))
GRAPH_CRAWL_MAX_DEPTH = int(os.environ.get("GRAPH_CRAWL_MAX_DEPTH", "4"))
# Maximum file size for download (50 MB default - increased to handle larger documents)
GRAPH_CRAWL_MAX_FILE_BYTES = int(os.environ.get("GRAPH_CRAWL_MAX_FILE_BYTES", "52428800"))  # 50 MB

# Optional document processing imports
pypdf = None
Document = None
load_workbook = None
Image = None
Presentation = None
xlrd = None
textract = None
try:
    import pypdf
except ImportError:
    pass
try:
    from docx import Document
except ImportError:
    pass
try:
    from openpyxl import load_workbook
except ImportError:
    pass
try:
    from PIL import Image
except ImportError:
    pass
try:
    from pptx import Presentation
except ImportError:
    pass
try:
    import xlrd
except ImportError:
    pass
try:
    import textract
except ImportError:
    pass


# =====================================================
# Helpers
# =====================================================
def _is_probably_html(resp: requests.Response) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        return True
    # Sometimes SharePoint returns HTML login page with 200
    body = (resp.content[:200] or b"").lower()
    return b"<html" in body or b"<!doctype html" in body


def _should_attempt_graph(resp: Optional[requests.Response]) -> bool:
    """Decide if we should try Graph-auth download."""
    if resp is None:
        return True

    # Common failures
    if resp.status_code in (401, 403, 404, 410):
        return True

    # Redirects often indicate a pre-auth link that needs cookies/auth
    if resp.status_code in (301, 302, 303, 307, 308):
        return True

    # 200 but HTML/login page or empty content -> treat as failure
    if resp.status_code == 200:
        if len(resp.content or b"") == 0:
            return True
        if _is_probably_html(resp):
            return True

    return False


def _graph_share_id_from_url(url: str) -> str:
    """
    Graph expects shareId in the form: u!base64url(url)
    """
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"u!{encoded}"


def _log_http(prefix: str, label: str, resp: requests.Response) -> None:
    try:
        snippet = resp.text[:200] if resp.text else ""
    except Exception:
        snippet = "<non-text-response>"
    logger.info(
        f"{prefix}{label} HTTP {resp.status_code} | "
        f"Content-Type={resp.headers.get('Content-Type','')} | "
        f"Bytes={len(resp.content or b'')}"
    )
    if resp.status_code >= 400:
        logger.warning(f"{prefix}{label} error body snippet: {snippet}")


def _graph_download_via_path(content_url: str, headers: dict, prefix: str) -> Optional[requests.Response]:
    """
    Attempt to download a file via Graph using site/drive path resolution.
    Works best for classic SharePoint/OneDrive URLs with a resolvable site path.
    """
    try:
        parsed = urlparse(content_url)
        hostname = parsed.netloc
        server_path = unquote(parsed.path).lstrip("/")
        segments = [s for s in server_path.split("/") if s]
        if not segments:
            logger.info(f"{prefix}Graph path resolve: no path segments")
            return None

        # Handle:
        # /sites/<siteName>/...
        # /personal/<user>_<domain>_com/...
        if segments[0] in ("sites", "personal") and len(segments) >= 2:
            site_server_path = "/".join(segments[:2])
            file_segments = segments[2:]
        else:
            site_server_path = segments[0]
            file_segments = segments[1:]

        file_rel_path = "/".join(file_segments)

        # Resolve site id
        site_url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/{site_server_path}"
        logger.info(f"{prefix}Resolving site via Graph: {site_url}")
        site_resp = requests.get(site_url, headers=headers, timeout=config.GRAPH_TIMEOUT)
        _log_http(prefix, "Site resolve", site_resp)
        if site_resp.status_code != 200:
            return None

        site_id = site_resp.json().get("id")
        if not site_id:
            logger.error(f"{prefix}Site resolve returned no id")
            return None

        # Try default drive root path
        drive_content_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{file_rel_path}:/content"
        logger.info(f"{prefix}Downloading via Graph drive path: {drive_content_url}")
        drive_resp = requests.get(drive_content_url, headers=headers, timeout=config.GRAPH_TIMEOUT)
        _log_http(prefix, "Drive path download", drive_resp)
        return drive_resp
    except Exception as e:
        logger.error(f"{prefix}Graph path resolution error: {e}", exc_info=True)
        return None


def _is_supported_document(name: str) -> bool:
    """Check if filename is a supported document type for indexing."""
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in SUPPORTED_DOC_EXTENSIONS)


def _is_personal_url(url: str) -> bool:
    """Heuristically detect personal OneDrive URLs to avoid sharing them globally."""
    if not url:
        return False
    url_lower = url.lower()
    return "/personal/" in url_lower or "my.sharepoint.com/personal" in url_lower


def _resolve_site_id(site_url: str, headers: dict) -> Optional[str]:
    """Resolve a SharePoint site URL to a site ID."""
    try:
        parsed = urlparse(site_url)
        hostname = parsed.netloc
        path = parsed.path.rstrip("/") or "/"
        resolve_url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:{path}"
        resp = requests.get(resolve_url, headers=headers, timeout=config.GRAPH_TIMEOUT)
        _log_http("", "Site resolve", resp)
        if resp.status_code == 200:
            return resp.json().get("id")
        logger.warning(f"Failed to resolve site id for {site_url}: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Site resolve error for {site_url}: {e}", exc_info=True)
    return None


def _extract_pptx_text_fallback(content: bytes) -> str:
    """Fallback PPTX text extraction using ZIP + XML parsing (avoids python-pptx rId issues)."""
    try:
        import zipfile
        import xml.etree.ElementTree as ET

        texts = []
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            slide_files = [n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
            for name in sorted(slide_files):
                try:
                    xml_bytes = zf.read(name)
                    root = ET.fromstring(xml_bytes)
                    # Collect all text runs (<a:t>) regardless of namespace prefix
                    for elem in root.iter():
                        if elem.tag.endswith("}t") and elem.text:
                            texts.append(elem.text)
                except Exception:
                    continue
        combined = "\n".join(t.strip() for t in texts if t and t.strip()).strip()
        return combined
    except Exception:
        return ""


def _textract_legacy_office(content: bytes, extension: str, display_name: str) -> str:
    """Extract text from legacy Office formats (.ppt, .doc, .xls) using textract if available."""
    if textract is None:
        return ""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{extension}") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        extracted = textract.process(tmp_path, extension=extension)
        if extracted:
            return extracted.decode("utf-8", errors="ignore").strip()
    except Exception as e:
        logger.warning(f"Legacy extraction failed for {display_name}: {e}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    return ""


def _extract_xls_with_xlrd(content: bytes) -> str:
    """Extract text from legacy .xls files with full data, statistics, and per-person breakdown."""
    if xlrd is None:
        return ""
    try:
        wb = xlrd.open_workbook(file_contents=content)
        sheets_text = []
        total_data_rows = 0
        all_column_stats = {}
        
        for sheet in wb.sheets():
            all_rows = []
            max_cols = 0
            
            # Collect ALL rows (no limits)
            for rowx in range(sheet.nrows):
                row = sheet.row_values(rowx)
                formatted_row = [_format_cell_value(cell) for cell in row]
                all_rows.append(formatted_row)
                max_cols = max(max_cols, len(formatted_row))
            
            if not all_rows:
                continue
            
            # Normalize row lengths
            for row in all_rows:
                while len(row) < max_cols:
                    row.append("")
            
            # Detect header
            has_header = False
            header_row = [f"Col{i+1}" for i in range(max_cols)]
            if all_rows:
                first_row = all_rows[0]
                non_empty = [c for c in first_row if c]
                if non_empty:
                    text_cells = sum(1 for c in non_empty if not _is_numeric(c))
                    has_header = text_cells >= len(non_empty) * 0.4
                    if has_header:
                        header_row = first_row
            
            # Get data rows (excluding header if present)
            data_rows = all_rows[1:] if has_header else all_rows
            
            # Compute statistics for numeric columns
            sheet_stats = {}
            for col_idx, col_name in enumerate(header_row):
                numeric_values = []
                for row in data_rows:
                    if col_idx < len(row):
                        cell_val = row[col_idx].strip()
                        try:
                            clean_val = cell_val.replace(",", "").replace("$", "").replace("%", "").strip()
                            if clean_val:
                                num = float(clean_val)
                                numeric_values.append(num)
                        except (ValueError, TypeError):
                            pass
                
                if len(numeric_values) >= max(1, len(data_rows) * 0.3):
                    total = sum(numeric_values)
                    avg = total / len(numeric_values) if numeric_values else 0
                    min_val = min(numeric_values) if numeric_values else 0
                    max_val = max(numeric_values) if numeric_values else 0
                    sheet_stats[col_name] = {"sum": total, "avg": avg, "min": min_val, "max": max_val, "count": len(numeric_values)}
                    if col_name in all_column_stats:
                        all_column_stats[col_name]["sum"] += total
                        all_column_stats[col_name]["count"] += len(numeric_values)
                        all_column_stats[col_name]["min"] = min(all_column_stats[col_name]["min"], min_val)
                        all_column_stats[col_name]["max"] = max(all_column_stats[col_name]["max"], max_val)
                    else:
                        all_column_stats[col_name] = {"sum": total, "count": len(numeric_values), "min": min_val, "max": max_val}
            
            # DYNAMIC: Detect identifier column and create breakdowns for ALL numeric columns
            identifier_col_idx = None
            numeric_col_indices = []
            
            for col_idx, col_name in enumerate(header_row):
                text_values = []
                numeric_count = 0
                for row in data_rows[:min(100, len(data_rows))]:
                    if col_idx < len(row):
                        val = row[col_idx].strip()
                        if val:
                            try:
                                float(val.replace(",", "").replace("$", "").replace("€", "").replace("K", "000").replace("M", "000000"))
                                numeric_count += 1
                            except:
                                text_values.append(val)
                
                if len(text_values) > numeric_count and identifier_col_idx is None:
                    unique_ratio = len(set(text_values)) / max(1, len(text_values))
                    if unique_ratio > 0.5:
                        identifier_col_idx = col_idx
                elif numeric_count > len(text_values) * 0.5:
                    numeric_col_indices.append(col_idx)
            
            # Create dynamic breakdown
            dynamic_breakdowns = {}
            if identifier_col_idx is not None and numeric_col_indices:
                for num_col_idx in numeric_col_indices[:5]:
                    col_name = header_row[num_col_idx] if num_col_idx < len(header_row) else f"Col{num_col_idx}"
                    breakdown = {}
                    for row in data_rows:
                        if identifier_col_idx < len(row) and num_col_idx < len(row):
                            identifier = row[identifier_col_idx].strip()
                            val_str = row[num_col_idx].strip()
                            if identifier and val_str:
                                try:
                                    clean_val = val_str.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
                                    multiplier = 1
                                    if clean_val.endswith("K"):
                                        clean_val = clean_val[:-1]
                                        multiplier = 1000
                                    elif clean_val.endswith("M"):
                                        clean_val = clean_val[:-1]
                                        multiplier = 1000000
                                    num_val = float(clean_val) * multiplier
                                    if identifier not in breakdown:
                                        breakdown[identifier] = {"total": 0, "count": 0}
                                    breakdown[identifier]["total"] += num_val
                                    breakdown[identifier]["count"] += 1
                                except (ValueError, TypeError):
                                    pass
                    if breakdown:
                        dynamic_breakdowns[col_name] = breakdown
            
            rows_text = []
            
            # Add sheet statistics
            if sheet_stats:
                rows_text.append("📊 **COLUMN STATISTICS:**")
                for col_name, stats in sheet_stats.items():
                    rows_text.append(f"  • **{col_name}**: SUM={stats['sum']:,.2f}, AVG={stats['avg']:,.2f}, MIN={stats['min']:,.2f}, MAX={stats['max']:,.2f}, COUNT={stats['count']}")
                rows_text.append("")
            
            # Add dynamic breakdowns
            if dynamic_breakdowns:
                id_col_name = header_row[identifier_col_idx] if identifier_col_idx < len(header_row) else "Item"
                rows_text.append(f"📊 **BREAKDOWN BY {id_col_name.upper()}:**")
                for col_name, breakdown in dynamic_breakdowns.items():
                    if breakdown:
                        sorted_items = sorted(breakdown.items(), key=lambda x: x[1]["total"], reverse=True)
                        rows_text.append(f"\n  **{col_name} - Top 10:**")
                        for item, data in sorted_items[:10]:
                            rows_text.append(f"    • {item}: {data['total']:,.2f}")
                        if len(sorted_items) > 10:
                            rows_text.append(f"  **{col_name} - Bottom 5:**")
                            for item, data in sorted_items[-5:]:
                                rows_text.append(f"    • {item}: {data['total']:,.2f}")
                rows_text.append("")
            
            # Add column headers and sample rows
            rows_text.append(f"**Column Headers:** {', '.join(header_row)}")
            rows_text.append(f"**Rows:** {len(data_rows)}")
            rows_text.append("")
            rows_text.append("**SAMPLE DATA (First 20 rows):**")
            for idx, row in enumerate(data_rows[:20], 1):
                if any(cell.strip() for cell in row):
                    row_parts = [f"{hdr}={val}" for hdr, val in zip(header_row, row) if val.strip()]
                    rows_text.append(f"Row {idx}: " + " | ".join(row_parts))
                    total_data_rows += 1
            
            if len(data_rows) > 25:
                rows_text.append(f"\n... ({len(data_rows) - 25} rows omitted) ...")
            
            if rows_text:
                sheet_header = f"=== Sheet: {sheet.name} ({len(data_rows)} data rows) ===\n"
                sheets_text.append(sheet_header + "\n".join(rows_text))
        
        return "\n\n".join(sheets_text)
    except Exception as e:
        logger.error(f"Error extracting .xls content: {e}")
        return ""


def _format_cell_value(cell) -> str:
    """Format a cell value for AI readability - NO truncation, full content preserved."""
    if cell is None:
        return ""
    if isinstance(cell, float):
        # Check if it's actually an integer
        if cell == int(cell):
            return str(int(cell))
        # Format decimals nicely
        return f"{cell:.4f}".rstrip('0').rstrip('.')
    if isinstance(cell, (int, bool)):
        return str(cell)
    # Convert to string and clean whitespace (preserve full content)
    cell_str = str(cell).strip()
    # Replace problematic characters that could break table structure
    cell_str = cell_str.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    # Collapse multiple spaces
    while '  ' in cell_str:
        cell_str = cell_str.replace('  ', ' ')
    return cell_str


def _extract_xlsx_with_structure(content: bytes, display_name: str) -> str:
    """Extract Excel content with full data, automatic statistics, and AI-optimized formatting.
    
    Format optimized for LLM comprehension:
    - Clear sheet separation
    - Header row clearly marked
    - Automatic statistics for numeric columns (SUM, AVG, MIN, MAX)
    - Data rows numbered for reference
    - Full content preserved (no truncation)
    - Clean column separation with pipes
    """
    if load_workbook is None:
        return f"📊 Excel: {display_name}\n\n(Install openpyxl to extract content.)"
    
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
        try:
            wb = load_workbook(io.BytesIO(content), data_only=True)
        except Exception as e:
            logger.error(f"Error loading Excel workbook: {e}")
            return f"📊 Excel: {display_name}\n\n❌ Error loading workbook: {str(e)}"
        
        sheets_text = []
        total_data_rows = 0
        all_column_stats = {}  # Aggregate stats across all sheets
        
        for sheet in wb.worksheets:
            # Collect ALL non-empty rows (no limits)
            all_rows = []
            max_cols = 0
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None and str(cell).strip() != "" for cell in row):
                    formatted_row = [_format_cell_value(cell) for cell in row]
                    all_rows.append(formatted_row)
                    max_cols = max(max_cols, len(formatted_row))
            
            if not all_rows:
                continue
            
            # Normalize row lengths for consistent table structure
            for row in all_rows:
                while len(row) < max_cols:
                    row.append("")
            
            # Detect if first row is a header (more text than numbers)
            has_header = False
            header_row = [f"Col{i+1}" for i in range(max_cols)]
            if all_rows:
                first_row = all_rows[0]
                non_empty = [c for c in first_row if c]
                if non_empty:
                    text_cells = sum(1 for c in non_empty if not _is_numeric(c))
                    has_header = text_cells >= len(non_empty) * 0.4  # At least 40% text
                    if has_header:
                        header_row = first_row
            
            # Get data rows (excluding header if present)
            data_rows = all_rows[1:] if has_header else all_rows
            
            # Compute automatic statistics for numeric columns
            sheet_stats = {}
            for col_idx, col_name in enumerate(header_row):
                numeric_values = []
                for row in data_rows:
                    if col_idx < len(row):
                        cell_val = row[col_idx].strip()
                        try:
                            clean_val = cell_val.replace(",", "").replace("$", "").replace("%", "").strip()
                            if clean_val:
                                num = float(clean_val)
                                numeric_values.append(num)
                        except (ValueError, TypeError):
                            pass
                
                if len(numeric_values) >= max(1, len(data_rows) * 0.3):
                    total = sum(numeric_values)
                    avg = total / len(numeric_values) if numeric_values else 0
                    min_val = min(numeric_values) if numeric_values else 0
                    max_val = max(numeric_values) if numeric_values else 0
                    sheet_stats[col_name] = {
                        "sum": total,
                        "avg": avg,
                        "min": min_val,
                        "max": max_val,
                        "count": len(numeric_values)
                    }
                    # Aggregate across sheets
                    if col_name in all_column_stats:
                        all_column_stats[col_name]["sum"] += total
                        all_column_stats[col_name]["count"] += len(numeric_values)
                        all_column_stats[col_name]["min"] = min(all_column_stats[col_name]["min"], min_val)
                        all_column_stats[col_name]["max"] = max(all_column_stats[col_name]["max"], max_val)
                    else:
                        all_column_stats[col_name] = {
                            "sum": total, "count": len(numeric_values),
                            "min": min_val, "max": max_val
                        }
            
            # DYNAMIC: Detect identifier column and create breakdowns for ALL numeric columns
            identifier_col_idx = None
            numeric_col_indices = []
            
            for col_idx, col_name in enumerate(header_row):
                text_values = []
                numeric_count = 0
                for row in data_rows[:min(100, len(data_rows))]:
                    if col_idx < len(row):
                        val = row[col_idx].strip()
                        if val:
                            try:
                                float(val.replace(",", "").replace("$", "").replace("€", "").replace("K", "000").replace("M", "000000"))
                                numeric_count += 1
                            except:
                                text_values.append(val)
                
                if len(text_values) > numeric_count and identifier_col_idx is None:
                    unique_ratio = len(set(text_values)) / max(1, len(text_values))
                    if unique_ratio > 0.5:
                        identifier_col_idx = col_idx
                elif numeric_count > len(text_values) * 0.5:
                    numeric_col_indices.append(col_idx)
            
            # Create dynamic breakdown
            dynamic_breakdowns = {}
            if identifier_col_idx is not None and numeric_col_indices:
                for num_col_idx in numeric_col_indices[:5]:
                    col_name = header_row[num_col_idx] if num_col_idx < len(header_row) else f"Col{num_col_idx}"
                    breakdown = {}
                    for row in data_rows:
                        if identifier_col_idx < len(row) and num_col_idx < len(row):
                            identifier = row[identifier_col_idx].strip()
                            val_str = row[num_col_idx].strip()
                            if identifier and val_str:
                                try:
                                    clean_val = val_str.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
                                    multiplier = 1
                                    if clean_val.endswith("K"):
                                        clean_val = clean_val[:-1]
                                        multiplier = 1000
                                    elif clean_val.endswith("M"):
                                        clean_val = clean_val[:-1]
                                        multiplier = 1000000
                                    num_val = float(clean_val) * multiplier
                                    if identifier not in breakdown:
                                        breakdown[identifier] = {"total": 0, "count": 0}
                                    breakdown[identifier]["total"] += num_val
                                    breakdown[identifier]["count"] += 1
                                except (ValueError, TypeError):
                                    pass
                    if breakdown:
                        dynamic_breakdowns[col_name] = breakdown
            
            # Build AI-optimized structured output
            rows_text = []
            
            # Add sheet statistics if available
            if sheet_stats:
                rows_text.append("📊 **COLUMN STATISTICS:**")
                for col_name, stats in sheet_stats.items():
                    rows_text.append(f"  • **{col_name}**: SUM={stats['sum']:,.2f}, AVG={stats['avg']:,.2f}, MIN={stats['min']:,.2f}, MAX={stats['max']:,.2f}, COUNT={stats['count']}")
                rows_text.append("")
            
            # Add dynamic breakdowns
            if dynamic_breakdowns:
                id_col_name = header_row[identifier_col_idx] if identifier_col_idx < len(header_row) else "Item"
                rows_text.append(f"📊 **BREAKDOWN BY {id_col_name.upper()}:**")
                for col_name, breakdown in dynamic_breakdowns.items():
                    if breakdown:
                        sorted_items = sorted(breakdown.items(), key=lambda x: x[1]["total"], reverse=True)
                        rows_text.append(f"\n  **{col_name} - Top 10:**")
                        for item, data in sorted_items[:10]:
                            rows_text.append(f"    • {item}: {data['total']:,.2f}")
                        if len(sorted_items) > 10:
                            rows_text.append(f"  **{col_name} - Bottom 5:**")
                            for item, data in sorted_items[-5:]:
                                rows_text.append(f"    • {item}: {data['total']:,.2f}")
                rows_text.append("")
            
            # Add column headers and sample rows
            rows_text.append(f"**Column Headers:** {', '.join(header_row)}")
            rows_text.append(f"**Rows:** {len(data_rows)}")
            rows_text.append("")
            rows_text.append("**SAMPLE DATA (First 20 rows):**")
            for idx, row in enumerate(data_rows[:20], 1):
                if any(cell.strip() for cell in row):
                    row_parts = [f"{hdr}={val}" for hdr, val in zip(header_row, row) if val.strip()]
                    rows_text.append(f"Row {idx}: " + " | ".join(row_parts))
                    total_data_rows += 1
            
            if len(data_rows) > 25:
                rows_text.append(f"\n... ({len(data_rows) - 25} rows omitted) ...")
            
            if rows_text:
                sheet_header = f"=== Sheet: {sheet.title} ({len(data_rows)} data rows) ===\n"
                sheets_text.append(sheet_header + "\n".join(rows_text))
        
        if not sheets_text:
            return f"📊 Excel: {display_name}\n\n[No data found in workbook]"
        
        # Add summary header with aggregate statistics
        summary = f"📊 Excel File: {display_name}\n"
        summary += f"**Total Sheets:** {len(wb.worksheets)} | **Total Data Rows:** {total_data_rows}\n\n"
        
        # Add aggregate statistics across all sheets
        if all_column_stats:
            summary += "**📊 AUTOMATIC COLUMN STATISTICS (ALL SHEETS):**\n"
            for col_name, stats in all_column_stats.items():
                avg = stats["sum"] / stats["count"] if stats["count"] > 0 else 0
                summary += f"  • **{col_name}**: SUM={stats['sum']:.2f}, AVG={avg:.2f}, MIN={stats['min']:.2f}, MAX={stats['max']:.2f}, COUNT={stats['count']}\n"
            summary += "\n"
        
        summary += "=" * 60 + "\n\n"
        
        return summary + "\n\n".join(sheets_text)


def _is_numeric(value: str) -> bool:
    """Check if a string value is numeric."""
    if not value:
        return False
    try:
        float(value.replace(",", "").replace("$", "").replace("%", ""))
        return True
    except ValueError:
        return False


def _extract_csv_with_structure(content: bytes, display_name: str) -> str:
    """Extract CSV content with full data, automatic statistics, and AI-optimized formatting.
    
    Format optimized for LLM comprehension:
    - Header row clearly marked
    - Automatic statistics for numeric columns (SUM, AVG, MIN, MAX)
    - Data rows numbered for reference
    - Full content preserved (no truncation)
    - Clean column separation
    """
    import csv as csv_module
    
    try:
        # Try to detect encoding
        text = None
        for encoding in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        
        if text is None:
            return f"📄 CSV: {display_name}\n\n❌ Unable to decode file content"
        
        # Parse CSV - extract ALL rows
        reader = csv_module.reader(io.StringIO(text))
        all_rows = []
        max_cols = 0
        for row in reader:
            formatted_row = [_format_cell_value(cell) for cell in row]
            all_rows.append(formatted_row)
            max_cols = max(max_cols, len(formatted_row))
        
        if not all_rows:
            return f"📄 CSV: {display_name}\n\n[Empty file]"
        
        # Normalize row lengths
        for row in all_rows:
            while len(row) < max_cols:
                row.append("")
        
        # Detect header (first row with mostly text)
        has_header = False
        header_row = [f"Col{i+1}" for i in range(max_cols)]
        if all_rows:
            first_row = all_rows[0]
            non_empty = [c for c in first_row if c]
            if non_empty:
                text_cells = sum(1 for c in non_empty if not _is_numeric(c))
                has_header = text_cells >= len(non_empty) * 0.4
                if has_header:
                    header_row = first_row
        
        # Get data rows (excluding header if present)
        data_rows = all_rows[1:] if has_header else all_rows
        total_data_rows = len(data_rows)
        
        # Compute automatic statistics for numeric columns
        column_stats = {}
        for col_idx, col_name in enumerate(header_row):
            numeric_values = []
            for row in data_rows:
                if col_idx < len(row):
                    cell_val = row[col_idx].strip()
                    try:
                        # Remove common formatting
                        clean_val = cell_val.replace(",", "").replace("$", "").replace("%", "").strip()
                        if clean_val:
                            num = float(clean_val)
                            numeric_values.append(num)
                    except (ValueError, TypeError):
                        pass
            
            # If more than 30% of rows have numeric values, compute stats
            if len(numeric_values) >= max(1, total_data_rows * 0.3):
                total = sum(numeric_values)
                avg = total / len(numeric_values) if numeric_values else 0
                min_val = min(numeric_values) if numeric_values else 0
                max_val = max(numeric_values) if numeric_values else 0
                column_stats[col_name] = {
                    "sum": total,
                    "avg": avg,
                    "min": min_val,
                    "max": max_val,
                    "count": len(numeric_values)
                }
        
        # Detect name/person column and hours column for per-person breakdown
        name_col_idx = None
        hours_col_idx = None
        name_keywords = ['fullname', 'name', 'employee', 'staff', 'person', 'user', 'member', 'player']
        hours_keywords = ['hours', 'hrs', 'time', 'duration', 'amount', 'total', 'wage', 'salary', 'value', 'price', 'cost', 'fee', 'pay', 'earnings', 'income']
        for idx, hdr in enumerate(header_row):
            hdr_lower = hdr.lower()
            if any(kw in hdr_lower for kw in name_keywords):
                name_col_idx = idx
            if any(kw in hdr_lower for kw in hours_keywords):
                hours_col_idx = idx
        
        # Compute per-person breakdown if we have both columns
        person_stats = {}
        if name_col_idx is not None and hours_col_idx is not None:
            for row in data_rows:
                if name_col_idx < len(row) and hours_col_idx < len(row):
                    person = row[name_col_idx].strip()
                    hours_str = row[hours_col_idx].strip()
                    if person and hours_str:
                        try:
                            hours = float(hours_str.replace(",", ""))
                            if person not in person_stats:
                                person_stats[person] = {"hours": 0, "entries": 0}
                            person_stats[person]["hours"] += hours
                            person_stats[person]["entries"] += 1
                        except ValueError:
                            pass
        
        # Build summary header
        summary = f"📄 CSV File: {display_name}\n"
        summary += f"**Total Data Rows:** {total_data_rows}\n"
        summary += f"**Columns ({max_cols}):** {', '.join(header_row)}\n\n"
        
        # Add automatic statistics for numeric columns
        if column_stats:
            summary += "**📊 AUTOMATIC COLUMN STATISTICS:**\n"
            for col_name, stats in column_stats.items():
                summary += f"  • **{col_name}**: "
                summary += f"SUM={stats['sum']:.2f}, "
                summary += f"AVG={stats['avg']:.2f}, "
                summary += f"MIN={stats['min']:.2f}, "
                summary += f"MAX={stats['max']:.2f}, "
                summary += f"COUNT={stats['count']}\n"
            summary += "\n"
        
        # Add per-person breakdown if available
        if person_stats:
            summary += "**👥 HOURS BY PERSON (Pre-computed):**\n"
            # Sort by hours descending
            sorted_persons = sorted(person_stats.items(), key=lambda x: x[1]["hours"], reverse=True)
            for person, stats in sorted_persons:
                summary += f"  • **{person}**: {stats['hours']:.2f} hours ({stats['entries']} entries)\n"
            summary += "\n"
        
        summary += "=" * 60 + "\n\n"
        
        # Build data in compact format (more efficient for LLM processing)
        summary += "**FULL DATA (Row# | Values):**\n"
        summary += "-" * 60 + "\n"
        
        # Add ALL data rows with key=value format (no truncation)
        for idx, row in enumerate(data_rows, 1):
            # Format: Row# | col1=val1 | col2=val2 | ...
            row_parts = []
            for i, (hdr, val) in enumerate(zip(header_row, row)):
                val_clean = val.strip() if val else ""
                if val_clean:  # Only include non-empty values
                    row_parts.append(f"{hdr}={val_clean}")
            summary += f"Row {idx}: " + " | ".join(row_parts) + "\n"
        
        return summary
    
    except Exception as e:
        logger.error(f"Error extracting CSV content: {e}")
        return f"📄 CSV: {display_name}\n\n❌ Error: {str(e)}"


def _list_site_drives(site_id: str, headers: dict) -> list[dict]:
    """List drives for a site."""
    try:
        url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives?$select=id,name,driveType"
        resp = requests.get(url, headers=headers, timeout=config.GRAPH_TIMEOUT)
        _log_http("", "List site drives", resp)
        if resp.status_code != 200:
            return []
        data = resp.json() or {}
        return data.get("value", []) or []
    except Exception as e:
        logger.error(f"Error listing drives for site {site_id}: {e}", exc_info=True)
        return []


def _should_skip_file(item: dict) -> bool:
    size = item.get("size")
    try:
        if size is not None and int(size) > GRAPH_CRAWL_MAX_FILE_BYTES:
            return True
    except Exception:
        return False
    return False
def _make_drive_doc_id(drive_id: str, item_id: str) -> str:
    return f"{drive_id}:{item_id}"

# Token cache to prevent excessive token requests and connection timeouts
_graph_token_cache = {
    "token": None,
    "expires_at": 0,  # Unix timestamp
}
_TOKEN_CACHE_BUFFER_SECONDS = 300  # Refresh token 5 minutes before expiry

def get_graph_token_app_only() -> Optional[str]:
    """Acquire an app-only Graph token via Client Credentials or Managed Identity.
    
    Tokens are cached in memory and reused until 5 minutes before expiry to prevent
    connection exhaustion and timeouts from frequent token requests.
    """
    import time
    global _graph_token_cache
    
    # Check if cached token is still valid
    current_time = time.time()
    if _graph_token_cache["token"] and _graph_token_cache["expires_at"] > current_time + _TOKEN_CACHE_BUFFER_SECONDS:
        return _graph_token_cache["token"]
    
    session = get_http_session()
    
    try:
        tenant_id = config.GRAPH_TENANT_ID or config.APP_TENANTID
        client_id = config.GRAPH_CLIENT_ID or config.APP_ID
        client_secret = config.GRAPH_CLIENT_SECRET or config.APP_PASSWORD

        # Prefer client credentials if available
        if tenant_id and client_id and client_secret:
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            data = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            }
            resp = session.post(token_url, data=data, timeout=config.GRAPH_TIMEOUT)
            if resp.status_code == 200:
                token_data = resp.json() or {}
                tok = token_data.get("access_token")
                expires_in = token_data.get("expires_in", 3600)  # Default 1 hour
                if tok:
                    # Cache the token with expiry time
                    _graph_token_cache["token"] = tok
                    _graph_token_cache["expires_at"] = current_time + expires_in
                    logger.info(f"Graph token acquired and cached (expires in {expires_in}s)")
                    return tok
            logger.error(f"App-only token failure (client creds): HTTP {resp.status_code} {resp.text[:200]}")

        # Managed Identity (IMDS)
        try:
            imds = "http://169.254.169.254/metadata/identity/oauth2/token"
            params = {"api-version": "2018-02-01", "resource": "https://graph.microsoft.com/"}
            headers = {"Metadata": "true"}
            resp = requests.get(imds, params=params, headers=headers, timeout=config.GRAPH_TIMEOUT)
            if resp.status_code == 200:
                tok = (resp.json() or {}).get("access_token")
                if tok:
                    return tok
            logger.error(f"Managed Identity token failure (IMDS): HTTP {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Managed Identity (IMDS) exception: {e}")

        return None
    except Exception as e:
        logger.error(f"App-only token exception: {e}", exc_info=True)
        return None


def get_graph_token_obo(user_assertion: str) -> Optional[str]:
    """OBO is disabled; return None to enforce app-only usage."""
    return None


# Back-compat: keep old name for existing callers
def get_graph_token() -> Optional[str]:
    return get_graph_token_app_only()


_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")

def _looks_like_guid(value: str) -> bool:
    try:
        if not value or len(value) < 30:
            return False
        return bool(_GUID_RE.match(value))
    except Exception:
        return False


def get_user_profile(user_id: str, user_assertion: Optional[str] = None) -> Optional[dict]:
    """Get user profile from Microsoft Graph using app-only permissions.

    Policy (Option A):
    - OBO/delegated flow is disabled; ignore user_assertion.
    - Use app-only token to call /users/{idOrUserPrincipalName}.
      Supports both GUID AAD object IDs and UPN/email (contains '@').
    - Results are cached in memory for the session.
    """
    try:
        if not user_id:
            logger.warning("Graph profile: no user_id provided")
            return None
            
        # Check cache first (memory + disk)
        cached = _get_profile_from_cache(user_id)
        if cached:
            logger.info(f"User profile cache hit for {user_id[:min(8, len(user_id))]}...")
            return cached

        headers = {"Content-Type": "application/json"}

        # OBO disabled by policy; always use app-only when possible

        # Accept GUID or UPN/email
        looks_guid = _looks_like_guid(user_id or "")
        looks_upn = isinstance(user_id, str) and ("@" in user_id)
        
        if not (looks_guid or looks_upn):
            logger.warning(f"Graph profile: user_id '{user_id[:min(20, len(user_id))]}...' is neither GUID nor UPN/email")
            return None
            
        token = get_graph_token_app_only()
        if not token:
            logger.error("Graph profile: failed to acquire app-only token")
            return None
            
        headers["Authorization"] = f"Bearer {token}"
        endpoint = f"https://graph.microsoft.com/v1.0/users/{user_id}?$select=id,displayName,mail,userPrincipalName,jobTitle,givenName"
        logger.info(f"Graph profile: method=app-only endpoint=/users/{{idOrUPN}} for {user_id[:min(8, len(user_id))]}...")
        resp = session_get(endpoint, headers=headers, timeout=config.GRAPH_TIMEOUT)
        _log_http("", "Get profile (/users/{idOrUPN})", resp)
        
        if resp.status_code == 200:
            profile = resp.json() or {}
            result = {
                "id": profile.get("id"),
                "displayName": profile.get("displayName"),
                "givenName": profile.get("givenName"),
                "mail": profile.get("mail") or profile.get("userPrincipalName"),
                "userPrincipalName": profile.get("userPrincipalName"),
                "jobTitle": profile.get("jobTitle"),
            }
            # Cache the result (memory + disk)
            _save_profile_to_cache(user_id, result)
            logger.info(f"Cached profile for user: {result.get('displayName')} ({result.get('userPrincipalName')})")
            return result
        else:
            logger.warning(f"Graph profile: HTTP {resp.status_code} for user_id {user_id[:min(8, len(user_id))]}...")
            return None

    except Exception as e:
        logger.error(f"Profile fetch error for user_id '{user_id[:min(20, len(user_id)) if user_id else 0]}...': {e}", exc_info=True)
        return None


def get_user_display_name(user_id: str, fallback_name: str = None) -> str:
    """Get user's display name for friendly greetings.
    
    Args:
        user_id: User's AAD object ID or UPN
        fallback_name: Optional display name from Teams if Graph profile doesn't have one
    
    Returns:
        Display name or given name, falls back to provided name or 'there'
    """
    try:
        if not user_id:
            logger.warning("get_user_display_name: no user_id provided")
            return fallback_name or "there"
            
        profile = get_user_profile(user_id)
        if profile:
            # Prefer given name for informal greeting, fallback to display name, then fallback_name from Teams
            display = profile.get("givenName") or profile.get("displayName") or fallback_name or "there"
            logger.info(f"Display name resolved: '{display}' for user_id={user_id[:min(8, len(user_id))]}...")
            return display
        
        logger.warning(f"get_user_display_name: no profile found for user_id={user_id[:min(20, len(user_id))]}...")
        return fallback_name or "there"
    except Exception as e:
        logger.error(f"Error getting display name for user_id={user_id[:min(20, len(user_id)) if user_id else 0]}...: {e}")
        return fallback_name or "there"


def ensure_user_profile_cached(user_id: str) -> bool:
    """Ensure user profile is fetched and cached at the start of conversation.
    
    Args:
        user_id: User's AAD object ID or UPN
    
    Returns:
        True if profile was successfully cached, False otherwise
    """
    try:
        profile = get_user_profile(user_id)
        return profile is not None
    except Exception:
        return False


# =====================================================
# Document Processing
# =====================================================
def process_document(attachment, corr_id: Optional[str] = None) -> str:
    """Download attachment content and extract text. Uses Graph when needed."""
    prefix = f"[{corr_id}] " if corr_id else ""

    try:
        display_name = getattr(attachment, "name", None) or "attachment"
        logger.info(f"{prefix}Processing document: {display_name}")

        content_type = getattr(attachment, "content_type", "") or ""
        if content_type:
            logger.info(f"{prefix}Attachment content_type: {content_type}")

        # Extract content url from Teams attachment payload
        content_info = None
        content_url = None

        if hasattr(attachment, "content") and attachment.content:
            if isinstance(attachment.content, dict):
                content_info = attachment.content
            elif isinstance(attachment.content, str):
                s = attachment.content.strip()
                if s.startswith("{"):
                    try:
                        content_info = json.loads(s)
                    except Exception:
                        content_info = None

        # Prefer Teams-provided pre-auth downloadUrl if present
        if content_info and content_info.get("downloadUrl"):
            content_url = content_info.get("downloadUrl")
            logger.info(f"{prefix}Using Teams-provided downloadUrl")

        if not content_url and getattr(attachment, "content_url", None):
            content_url = attachment.content_url

        if not content_url and content_info and content_info.get("contentUrl"):
            content_url = content_info.get("contentUrl")

        if not content_url:
            logger.warning(f"{prefix}No content URL for {display_name}")
            return (
                f"❌ Cannot access {display_name} (type: {content_type or 'unknown'}). "
                "No download URL provided. Try re-uploading directly in chat."
            )

        # 1) Try direct download (fast path)
        logger.info(f"{prefix}Direct download attempt: {content_url[:120]}...")
        direct_resp = None
        try:
            direct_resp = requests.get(content_url, timeout=30, allow_redirects=False)
            _log_http(prefix, "Direct download", direct_resp)
        except Exception as e:
            logger.warning(f"{prefix}Direct download exception: {e}")

        # 2) If direct is suspicious/fails, use Graph
        resp = direct_resp
        if _should_attempt_graph(direct_resp):
            logger.info(f"{prefix}Attempting Graph-auth download...")
            token = get_graph_token()
            if not token:
                return (
                    f"❌ Could not get Graph token to download {display_name}. "
                    "Check Graph client id/secret/tenant and permissions."
                )

            headers = {"Authorization": f"Bearer {token}"}

            # If URL already a Graph URL, retry with auth header
            if "graph.microsoft.com" in content_url:
                logger.info(f"{prefix}Retrying Graph URL with bearer token...")
                resp = requests.get(content_url, headers=headers, timeout=config.GRAPH_TIMEOUT, allow_redirects=True)
                _log_http(prefix, "Graph direct", resp)
            else:
                # Try shares API
                share_id = _graph_share_id_from_url(content_url)
                shares_url = f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem/content"
                logger.info(f"{prefix}Retrying via Graph shares API: {shares_url}")
                resp = requests.get(shares_url, headers=headers, timeout=config.GRAPH_TIMEOUT, allow_redirects=True)
                _log_http(prefix, "Graph shares", resp)

                # If shares fails, try path-based resolution
                if resp.status_code in (400, 404):
                    alt_resp = _graph_download_via_path(content_url, headers, prefix)
                    if alt_resp is not None:
                        resp = alt_resp

        if resp is None:
            return f"❌ Failed to download {display_name}: no response."

        try:
            resp.raise_for_status()
        except requests.HTTPError:
            status = resp.status_code
            if status in (401, 403):
                return (
                    f"❌ Access denied for {display_name} (HTTP {status}). "
                    "Ensure app has *application* permissions: Files.Read.All, Sites.Read.All "
                    "and admin consent is granted."
                )
            return f"❌ Failed to download {display_name} (HTTP {status})."

        content = resp.content or b""
        if len(content) == 0:
            return f"❌ Downloaded 0 bytes for {display_name}. (Likely an auth/redirect issue.)"

        file_name = display_name.lower()
        logger.info(f"{prefix}Downloaded {len(content)} bytes for {display_name}")

        # PDF
        if file_name.endswith(".pdf"):
            if pypdf is None:
                return f"📄 PDF: {display_name}\n\n(Install pypdf to extract text.)"
            reader = pypdf.PdfReader(io.BytesIO(content))
            text_parts = []
            for page in reader.pages:
                t = page.extract_text() or ""
                if t.strip():
                    text_parts.append(t)
            text = "\n".join(text_parts)
            return f"📄 PDF: {display_name}\n\n{text}"

        # Word
        if file_name.endswith((".docx", ".doc")): 
            if Document is None:
                return f"📝 Word: {display_name}\n\n(Install python-docx to extract text.)"
            
            # python-docx only works with .docx (Office Open XML format)
            # Old .doc files (Word 97-2003) are binary format and won't work
            if file_name.endswith(".doc") and not file_name.endswith(".docx"):
                # Try to detect if it's actually a .docx misnamed as .doc
                try:
                    doc = Document(io.BytesIO(content))
                    text = "\n".join(p.text for p in doc.paragraphs if p.text and p.text.strip())
                    return f"📝 Word: {display_name}\n\n{text}"
                except Exception as e:
                    logger.warning(f"Cannot extract old .doc format: {display_name}")
                    return (
                        f"📝 Word (Legacy): {display_name}\n\n"
                        f"⚠️ This file is in old Word 97-2003 format (.doc).\n"
                        f"Please convert to .docx format or re-save as .docx to extract content.\n"
                        f"File size: {len(content)} bytes"
                    )
            
            # .docx files
            try:
                doc = Document(io.BytesIO(content))
                text = "\n".join(p.text for p in doc.paragraphs if p.text and p.text.strip())
                return f"📝 Word: {display_name}\n\n{text}"
            except Exception as e:
                logger.error(f"Error extracting Word document: {e}")
                return f"📝 Word: {display_name}\n\n❌ Error extracting content: {str(e)}"

        # Excel - use improved extraction with structure
        if file_name.endswith((".xlsx", ".xls")):
            if file_name.endswith(".xls") and not file_name.endswith(".xlsx"):
                xls_text = _extract_xls_with_xlrd(content)
                if xls_text:
                    return f"📊 Excel: {display_name}\n\n{xls_text}"
                if xlrd is None:
                    return f"📊 Excel: {display_name}\n\n(Install xlrd==1.2.0 to extract legacy .xls content.)"
                return f"📊 Excel: {display_name}\n\n❌ Error extracting legacy .xls content."
            # Use improved .xlsx extraction
            return _extract_xlsx_with_structure(content, display_name)

        # CSV files
        if file_name.endswith(".csv"):
            return _extract_csv_with_structure(content, display_name)

        # PowerPoint
        if file_name.endswith((".pptx", ".ppt")):
            if file_name.endswith(".ppt") and not file_name.endswith(".pptx"):
                if content[:4] == b"PK\x03\x04":
                    try:
                        if Presentation is None:
                            return f"📽️ PowerPoint: {display_name}\n\n(Install python-pptx to extract content.)"
                        prs = Presentation(io.BytesIO(content))
                        slides_text = []
                        for slide in prs.slides:
                            parts = []
                            for shape in slide.shapes:
                                try:
                                    if hasattr(shape, "text"):
                                        txt = (shape.text or "").strip()
                                        if txt:
                                            parts.append(txt)
                                except Exception:
                                    continue
                            if parts:
                                slides_text.append("\n".join(parts))
                        combined = "\n\n".join(slides_text).strip()
                        return combined if combined else "[No extractable text found in slides]"
                    except Exception as e:
                        logger.warning(f"PowerPoint extraction error for {display_name}: {e}")
                legacy_text = _textract_legacy_office(content, "ppt", display_name)
                if legacy_text:
                    return legacy_text
                if textract is None:
                    return "[Legacy .ppt detected. Install textract to extract content.]"
                return "[Legacy .ppt detected but extraction failed.]"
            if Presentation is None:
                return f"📽️ PowerPoint: {display_name}\n\n(Install python-pptx to extract content.)"
            try:
                prs = Presentation(io.BytesIO(content))
                slides_text = []
                for slide in prs.slides:
                    parts = []
                    for shape in slide.shapes:
                        try:
                            if hasattr(shape, "text"):
                                txt = (shape.text or "").strip()
                                if txt:
                                    parts.append(txt)
                        except Exception:
                            continue
                    if parts:
                        slides_text.append("\n".join(parts))
                combined = "\n\n".join(slides_text).strip()
                return combined if combined else "[No extractable text found in slides]"
            except Exception as e:
                logger.warning(f"PowerPoint extraction error for {display_name}: {e}")
                if file_name.endswith(".pptx"):
                    fallback_text = _extract_pptx_text_fallback(content)
                    if fallback_text:
                        return fallback_text
                return f"[PowerPoint file - content extraction failed: {str(e)}]"

        # Images
        if file_name.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp")):
            if Image is None:
                return f"🖼️ Image: {display_name}\n\n(Install pillow to inspect image metadata.)"
            img = Image.open(io.BytesIO(content))
            return f"🖼️ Image: {display_name} ({img.width}x{img.height}px, {img.format})"

        return f"📎 File: {display_name} ({len(content)} bytes)"

    except Exception as e:
        logger.error(f"{prefix}Error processing document: {e}", exc_info=True)
        return f"❌ Error processing {getattr(attachment,'name','attachment')}: {str(e)}"


# =====================================================
# Fuzzy Search Helpers
# =====================================================
def _fuzzy_match(word1: str, word2: str, threshold: float = 0.7) -> bool:
    """
    Check if two words are similar enough using Levenshtein-like comparison.
    Returns True if similarity >= threshold.
    """
    w1, w2 = word1.lower(), word2.lower()
    if w1 == w2:
        return True
    if w1 in w2 or w2 in w1:
        return True
    
    # Simple Levenshtein distance ratio
    len1, len2 = len(w1), len(w2)
    if max(len1, len2) == 0:
        return True
    
    # Quick rejection for very different lengths
    if abs(len1 - len2) > max(len1, len2) * 0.4:
        return False
    
    # Calculate edit distance (simplified)
    if len1 < len2:
        w1, w2 = w2, w1
        len1, len2 = len2, len1
    
    previous_row = range(len2 + 1)
    for i, c1 in enumerate(w1):
        current_row = [i + 1]
        for j, c2 in enumerate(w2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    
    distance = previous_row[-1]
    similarity = 1 - (distance / max(len1, len2))
    return similarity >= threshold


def _expand_query_with_fuzzy(query: str) -> str:
    """
    Expand search query with common misspelling corrections.
    Returns enhanced query for better Graph search results.
    """
    # Common misspellings/typos dictionary
    corrections = {
        'nirobi': 'nairobi',
        'niarobi': 'nairobi',
        'narobi': 'nairobi',
        'agentcon': 'agentcon',  # Keep as-is, might be intentional
        'docuemnt': 'document',
        'documnet': 'document',
        'meetting': 'meeting',
        'meetign': 'meeting',
        'porject': 'project',
        'prject': 'project',
        'repotr': 'report',
        'reprot': 'report',
        'summry': 'summary',
        'sumarry': 'summary',
        'anlysis': 'analysis',
        'anlaysis': 'analysis',
        'presenation': 'presentation',
        'presentaiton': 'presentation',
    }
    
    words = query.split()
    expanded_words = []
    
    for word in words:
        word_lower = word.lower()
        # Check direct correction
        if word_lower in corrections:
            corrected = corrections[word_lower]
            if corrected != word_lower:
                logger.info(f"Spelling correction: '{word}' -> '{corrected}'")
                # Add both original and corrected for broader search
                expanded_words.append(f"({word} OR {corrected})")
            else:
                expanded_words.append(word)
        else:
            expanded_words.append(word)
    
    return " ".join(expanded_words)


def _fuzzy_term_match(term: str, text: str, threshold: float = 0.7) -> bool:
    """
    Check if a search term fuzzy-matches any word in the text.
    """
    text_words = text.lower().split()
    term_lower = term.lower()
    
    for text_word in text_words:
        # Clean punctuation
        clean_word = ''.join(c for c in text_word if c.isalnum())
        if _fuzzy_match(term_lower, clean_word, threshold):
            return True
    return False


# =====================================================
# Graph API - SharePoint Search
# =====================================================
def search_sharepoint(query: str, token: str, user_context: bool = True) -> dict:
    """
    Search SharePoint/OneDrive using Microsoft Graph Search API.
    Includes fuzzy matching for typo tolerance.
    
    Args:
        query: Search query string
        token: Graph access token (delegated or app-only)
        user_context: If True (default), token is delegated and respects user permissions.
                     If False, token is app-only and may access more than user can see.
    
    Returns:
        Dictionary with search results including webUrls for downloading
    """
    try:
        auth_type = "delegated (user-context)" if user_context else "app-only"
        logger.info(f"Searching SharePoint with {auth_type} token")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Region handling: default to 'US' per Graph error guidance; allow override via env
        region = os.getenv("GRAPH_SEARCH_REGION", "US").strip().upper() or "US"
        
        # Expand query with spelling corrections for better matches
        expanded_query = _expand_query_with_fuzzy(query)
        logger.info(f"Search query expanded: '{query}' -> '{expanded_query}'")

        search_body = {
            "requests": [
                {
                    "entityTypes": ["driveItem", "listItem", "site"],
                    "query": {"queryString": expanded_query},
                    "from": 0,
                    "size": 15,  # Increased to allow fuzzy filtering
                    # Explicit region for app-only queries
                    "region": region,
                }
            ]
        }

        # Use retry logic with exponential backoff for Graph search
        def _search_graph():
            return requests.post(
                "https://graph.microsoft.com/v1.0/search/query",
                headers=headers,
                json=search_body,
                timeout=config.GRAPH_TIMEOUT,
            )
        
        try:
            resp = _retry_request(_search_graph, max_retries=3, initial_delay=2.0)
        except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as e:
            logger.error(f"SharePoint search timeout after retries: {str(e)[:100]}")
            return {"error": f"SharePoint search timed out: {str(e)[:50]}"}

        _log_http("", "Graph search", resp)

        # Retry once on transient 5xx errors (Graph sometimes returns 500/503)
        if resp.status_code in (500, 502, 503, 504):
            logger.warning(f"Graph search transient error HTTP {resp.status_code}; retrying once...")
            try:
                resp = _retry_request(_search_graph, max_retries=2, initial_delay=2.0)
                _log_http("", "Graph search (retry)", resp)
            except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as e:
                logger.error(f"SharePoint search retry timeout: {str(e)[:100]}")
                return {"error": f"SharePoint search timed out: {str(e)[:50]}"}

        if resp.status_code != 200:
            # Retry once with US if server indicates only US is valid
            try:
                body_txt = resp.text or ""
            except Exception:
                body_txt = ""
            if "Only valid regions are US" in body_txt and region != "US":
                logger.warning("Graph search region rejected; retrying with region=US")
                search_body["requests"][0]["region"] = "US"
                
                def _search_graph_us():
                    return requests.post(
                        "https://graph.microsoft.com/v1.0/search/query",
                        headers=headers,
                        json=search_body,
                        timeout=config.GRAPH_TIMEOUT,
                    )
                
                try:
                    resp = _retry_request(_search_graph_us, max_retries=3, initial_delay=2.0)
                except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as e:
                    logger.error(f"SharePoint search US retry timeout: {str(e)[:100]}")
                    return {"error": f"SharePoint search timed out: {str(e)[:50]}"}
                
                _log_http("", "Graph search (retry US)", resp)
                if resp.status_code != 200:
                    return {"error": f"SharePoint search failed (HTTP {resp.status_code})"}
            else:
                return {"error": f"SharePoint search failed (HTTP {resp.status_code})"}

        data = resp.json()
        results = []
        
        # Extract query keywords for relevance filtering
        query_terms = [term.lower().strip() for term in query.split() if len(term.strip()) > 2]
        logger.info(f"Graph search query terms for filtering: {query_terms}")

        for block in data.get("value", []):
            for container in block.get("hitsContainers", []) or []:
                for hit in container.get("hits", []) or []:
                    resource = hit.get("resource", {}) or {}
                    name = resource.get("name") or resource.get("title") or "Untitled"
                    summary = (hit.get("summary") or "").strip()
                    
                    # Calculate relevance score based on keyword matches
                    searchable_text = (name + " " + summary).lower()
                    matches = sum(1 for term in query_terms if term in searchable_text)
                    relevance_score = matches / len(query_terms) if query_terms else 0
                    
                    # Only include documents with at least 30% keyword match
                    if relevance_score >= 0.3 or not query_terms:
                        results.append({
                            "name": name,
                            "webUrl": resource.get("webUrl", ""),
                            "summary": summary,
                            "driveId": resource.get("parentReference", {}).get("driveId", ""),
                            "itemId": resource.get("id", ""),
                            "relevance_score": relevance_score,
                        })
                    else:
                        logger.info(f"Filtered out low-relevance result: {name} (score={relevance_score:.2f})")
        
        # Sort by relevance score (highest first)
        results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        
        logger.info(f"Graph search: {len(results)} relevant results after filtering (from {len([h for b in data.get('value', []) for c in b.get('hitsContainers', []) for h in c.get('hits', [])])} raw results)")
        
        return {"results": results, "count": len(results)}

    except Exception as e:
        logger.error(f"SharePoint search error: {e}", exc_info=True)
        return {"error": str(e)}


def download_and_extract_content(url: str, token: str, file_name: str, drive_id: str = "", item_id: str = "") -> str:
    """
    Download a file from SharePoint/OneDrive and extract its text content.
    
    Args:
        url: Web URL of the file
        token: Graph API token
        file_name: Name of the file
        drive_id: Drive ID from search result
        item_id: Item ID from search result
    
    Returns:
        Extracted text content
    """
    try:
        headers = {"Authorization": f"Bearer {token}"}
        
        # Use Graph API directly with drive/item IDs if available
        if drive_id and item_id:
            graph_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content"
            logger.info(f"Downloading via Graph API: {graph_url}")
            resp = requests.get(graph_url, headers=headers, timeout=30)
        else:
            # Fallback to share ID approach
            logger.info(f"Downloading content from: {url}")
            resp = requests.get(url, headers=headers, timeout=30)
            
            # If unauthorized or HTML viewer page, try Graph shares/path
            if resp.status_code in (401, 403) or _is_probably_html(resp):
                logger.warning(f"Direct fetch is unauthorized or HTML viewer; attempting Graph shares/path")
                share_id = _graph_share_id_from_url(url)
                graph_url = f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem/content"
                resp = requests.get(graph_url, headers=headers, timeout=30)
                if resp.status_code in (400, 404) or _is_probably_html(resp):
                    alt_resp = _graph_download_via_path(url, headers, "")
                    if alt_resp is not None:
                        resp = alt_resp
        
        if resp.status_code != 200:
            return f"[Unable to download: HTTP {resp.status_code} - Check Files.Read.All permission]"
        
        content = resp.content
        file_name_lower = file_name.lower()

        # If we still received HTML, avoid parsing as a binary document
        try:
            if _is_probably_html(resp):
                return f"[Content appears to be an HTML viewer page, not raw file: {file_name}]"
        except Exception:
            pass
        
        # Extract text based on file type
        if file_name_lower.endswith(".pdf") and pypdf:
            reader = pypdf.PdfReader(io.BytesIO(content))
            text_parts = []
            for page in reader.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    # Some PDFs trigger internal parser errors (e.g., missing 'bbox'); skip page
                    t = ""
                if t.strip():
                    text_parts.append(t)
            extracted = "\n".join(text_parts)
            logger.info(f"Extracted {len(extracted)} chars from {len(reader.pages)} pages: {file_name}")
            return extracted
        
        elif file_name_lower.endswith((".docx", ".doc")):
            if file_name_lower.endswith(".doc") and not file_name_lower.endswith(".docx"):
                # Try python-docx first (works if misnamed .docx)
                try:
                    if Document:
                        doc = Document(io.BytesIO(content))
                        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                except Exception:
                    pass
                
                # Real legacy .doc - try textract
                from simple_file_handler import _textract_legacy_office
                legacy_text = _textract_legacy_office(content, "doc", file_name)
                if legacy_text:
                    return legacy_text
                if textract is None:
                    return "[Legacy Word .doc detected. Install textract to extract content.]"
                return "[Legacy Word .doc detected but extraction failed.]"
            
            # .docx files
            if Document is None:
                return "[Word .docx detected but python-docx is not installed.]"
            doc = Document(io.BytesIO(content))
            extracted = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            logger.info(f"Extracted {len(extracted)} chars from {len(doc.paragraphs)} paragraphs: {file_name}")
            return extracted
        
        elif file_name_lower.endswith((".xlsx", ".xls")):
            if file_name_lower.endswith(".xls"):
                xls_text = _extract_xls_with_xlrd(content)
                if xls_text:
                    logger.info(f"Extracted {len(xls_text)} chars from legacy .xls: {file_name}")
                    return xls_text
                if xlrd is None:
                    return "[Legacy Excel .xls detected. Install xlrd==1.2.0 to extract content.]"
                return "[Legacy Excel .xls detected but extraction failed.]"
            if not load_workbook:
                return "[Excel .xlsx detected but openpyxl is not installed.]"
            wb = load_workbook(io.BytesIO(content), data_only=True)
            sheets_text = []
            total_rows = 0
            for sheet in wb.worksheets:
                rows = []
                sheet_rows = 0
                # Capture ALL rows - no limits
                for row in sheet.iter_rows(values_only=True):
                    if any(cell is not None and str(cell).strip() for cell in row):
                        rows.append(" | ".join(str(cell or "") for cell in row))
                        sheet_rows += 1
                if rows:
                    sheets_text.append(f"Sheet: {sheet.title}\n" + "\n".join(rows))
                    total_rows += sheet_rows
            extracted = "\n\n".join(sheets_text)
            logger.info(f"Extracted {len(extracted)} chars from {total_rows} rows across {len(wb.worksheets)} sheets: {file_name}")
            return extracted

        elif file_name_lower.endswith((".pptx", ".ppt")):
            if file_name_lower.endswith(".ppt") and not file_name_lower.endswith(".pptx"):
                # Check if it's actually a .pptx file (ZIP signature)
                if content[:4] == b"PK\x03\x04":
                    try:
                        if Presentation is None:
                            return "[PowerPoint .pptx detected but python-pptx is not installed.]"
                        prs = Presentation(io.BytesIO(content))
                        slides_text = []
                        for slide in prs.slides:
                            parts = []
                            for shape in slide.shapes:
                                try:
                                    if hasattr(shape, "text"):
                                        txt = (shape.text or "").strip()
                                        if txt:
                                            parts.append(txt)
                                except Exception:
                                    continue
                            if parts:
                                slides_text.append("\n".join(parts))
                        combined = "\n\n".join(slides_text).strip()
                        return combined if combined else "[No extractable text found in slides]"
                    except Exception as e:
                        logger.warning(f"PowerPoint extraction error for {file_name}: {e}")
                
                # Real legacy .ppt - try textract
                from simple_file_handler import _textract_legacy_office
                legacy_text = _textract_legacy_office(content, "ppt", file_name)
                if legacy_text:
                    return legacy_text
                if textract is None:
                    return "[Legacy PowerPoint .ppt detected. Install textract to extract content.]"
                return "[Legacy PowerPoint .ppt detected but extraction failed.]"
            
            # .pptx files - standard extraction
            if Presentation is None:
                return "[PowerPoint .pptx detected but python-pptx is not installed.]"
            try:
                prs = Presentation(io.BytesIO(content))
                slides_text = []
                for slide in prs.slides:
                    parts = []
                    for shape in slide.shapes:
                        try:
                            if hasattr(shape, "text"):
                                txt = (shape.text or "").strip()
                                if txt:
                                    parts.append(txt)
                        except Exception:
                            continue
                    if parts:
                        slides_text.append("\n".join(parts))
                combined = "\n\n".join(slides_text).strip()
                return combined if combined else "[No extractable text found in slides]"
            except Exception as e:
                logger.warning(f"PowerPoint extraction error for {file_name}: {e}")
                if file_name_lower.endswith(".pptx"):
                    fallback_text = _extract_pptx_text_fallback(content)
                    if fallback_text:
                        return fallback_text
                return f"[PowerPoint file - content extraction failed: {str(e)}]"
        
        elif file_name_lower.endswith(".txt"):
            extracted = content.decode("utf-8", errors="ignore")
            logger.info(f"Extracted {len(extracted)} chars from text file: {file_name}")
            return extracted

        elif file_name_lower.endswith((".csv", ".json", ".xml")):
            try:
                extracted = content.decode("utf-8", errors="ignore")
                logger.info(f"Extracted {len(extracted)} chars from {file_name_lower.split('.')[-1].upper()} file: {file_name}")
                return extracted
            except Exception:
                return "[Unable to decode file as UTF-8 text]"

        elif file_name_lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp")) and 'Image' in globals() and Image is not None:
            try:
                img = Image.open(io.BytesIO(content))
                return f"[Image: {file_name} {img.width}x{img.height}px, {img.format}]"
            except Exception:
                return f"[Image file detected but failed to read basic metadata]"
        
        else:
            return f"[{file_name}: Content extraction not supported for this file type]"
    
    except Exception as e:
        logger.error(f"Error extracting content from {file_name}: {e}")
        return f"[Error extracting content: {str(e)}]"


# =====================================================
# Graph API - Send Email (FIXED for app-only)
# =====================================================
def send_email(to: str, subject: str, body: str, token: str, user_context: bool = True, sender_upn: Optional[str] = None) -> str:
    """
    Send email via Microsoft Graph.
    
    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body (plain text)
        token: Graph access token (delegated or app-only)
        user_context: If True (default), uses /me/sendMail (delegated token).
        sender_upn: Sender's UPN (required only for app-only mode)
    """
    try:
        auth_type = "delegated (user-context)" if user_context else "app-only"
        logger.info(f"Sending email with {auth_type} token")
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        email_body = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": "true",
        }

        # Use appropriate endpoint based on token type
        if user_context:
            # Delegated token: send as authenticated user
            endpoint = "https://graph.microsoft.com/v1.0/me/sendMail"
        else:
            # App-only token: requires explicit sender UPN
            sender = sender_upn or getattr(config, "SENDER_UPN", None)
            if not sender:
                return (
                    "\n\n❌ Email not configured: set SENDER_UPN in config/env. "
                    "App-only sendMail requires /users/{SENDER_UPN}/sendMail."
                )
            endpoint = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

        resp = requests.post(
            endpoint,
            headers=headers,
            json=email_body,
            timeout=config.GRAPH_TIMEOUT,
        )

        _log_http("", "Send mail", resp)

        if resp.status_code == 202:
            return f"\n\n✅ Email sent successfully to {to}!"
        return f"\n\n❌ Failed to send email (HTTP {resp.status_code})."

    except Exception as e:
        logger.error(f"Email error: {e}", exc_info=True)
        return f"\n\n❌ Email error: {str(e)}"


# =====================================================
# Graph API - Full Crawl and Caching
# =====================================================
def _crawl_drive_items(
    drive_id: str,
    token: str,
    owner_user_id: str,
    treat_as_shared: bool,
    max_items: int,
    max_depth: int,
    stats: dict,
):
    """Breadth-first crawl of a drive and cache supported documents."""
    headers = {"Authorization": f"Bearer {token}"}
    cache = get_cache()
    queue: list[tuple[str, int]] = [("root", 0)]
    
    # Track items processed in this crawl session to avoid re-indexing duplicates
    # within the same crawl (in case same item appears in multiple places)
    seen_in_crawl = set()

    processed = 0
    while queue and processed < max_items:
        current_item, depth = queue.pop(0)
        page_url = (
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{current_item}/children"
            "?$select=id,name,folder,file,parentReference,webUrl,size,lastModifiedDateTime"
        )

        while page_url and processed < max_items:
            resp = requests.get(page_url, headers=headers, timeout=config.GRAPH_TIMEOUT)
            _log_http("", "Drive list", resp)
            if resp.status_code != 200:
                stats["errors"] += 1
                break

            data = resp.json() or {}
            for item in data.get("value", []) or []:
                # Recurse into folders up to max_depth
                if "folder" in item:
                    if depth < max_depth:
                        queue.append((item.get("id"), depth + 1))
                    continue

                # Only process files we support
                if "file" not in item:
                    continue

                name = item.get("name", "")
                if not _is_supported_document(name):
                    stats["skipped"] += 1
                    continue

                if _should_skip_file(item):
                    stats["skipped"] += 1
                    continue

                item_id = item.get("id", "")
                doc_id = _make_drive_doc_id(drive_id, item_id)
                web_url = item.get("webUrl", "")
                
                # Skip if already processed in this crawl session
                if doc_id in seen_in_crawl:
                    stats["skipped"] += 1
                    continue
                seen_in_crawl.add(doc_id)

                if treat_as_shared:
                    if cache.has_shared_document(doc_id):
                        stats["skipped"] += 1
                        continue
                else:
                    if cache.has_document(doc_id, user_id=owner_user_id):
                        stats["skipped"] += 1
                        continue

                content = download_and_extract_content(web_url, token, name, drive_id, item_id)
                if not content or len(content.strip()) < 10:
                    stats["skipped"] += 1
                    continue

                error_indicators = [
                    "[Error extracting",
                    "[Unable to download",
                    "Content extraction not supported",
                    "Check Files.Read.All permission",
                    "File is not a zip file",
                ]
                if any(indicator in content for indicator in error_indicators):
                    stats["skipped"] += 1
                    continue

                metadata = {
                    "drive_id": drive_id,
                    "item_id": item_id,
                    "last_modified": item.get("lastModifiedDateTime"),
                    "path": item.get("parentReference", {}).get("path", ""),
                }

                # SECURITY: All documents are user-specific (no shared cache)
                if treat_as_shared:
                    # Even "shared" SharePoint docs are tagged per-user for access control
                    cache.add_document(doc_id, name, web_url, content, user_id=owner_user_id, metadata=metadata)
                    stats["shared_indexed"] += 1
                else:
                    # Personal drive documents
                    cache.add_document(doc_id, name, web_url, content, user_id=owner_user_id, metadata=metadata)
                    stats["personal_indexed"] += 1

                processed += 1
                if processed >= max_items:
                    break

            page_url = data.get("@odata.nextLink")


def crawl_accessible_documents(
    user_id: str,
    include_personal: bool = True,
    include_sites: bool = True,
    max_items_per_drive: Optional[int] = None,
    max_depth: Optional[int] = None,
    user_display_name: str = None,
) -> dict:
    """Background crawl to index OneDrive/SharePoint documents for faster cache searches.

    NOTE: This is for BACKGROUND INDEXING only. For fresh results, use live Graph search.
    - Crawling builds a local cache for faster subsequent searches
    - Live Graph search (search_sharepoint) provides real-time results without waiting for crawls
    - Web crawling (web_indexer) is SEPARATE and only for external websites

    CRITICAL SECURITY:
    - Personal drive items are cached with user_id tag (user-specific access)
    - SharePoint site libraries are cached with user_id tag (user sees only what they request)
    - NO shared cache - all documents are user-specific to prevent unauthorized access
    - User profile is cached at the start for personalized greetings
    
    Args:
        user_id: User's AAD object ID or UPN (REQUIRED)
        include_personal: If True, crawl user's personal OneDrive
        include_sites: If True, crawl configured SharePoint sites
        max_items_per_drive: Max items to index per drive
        max_depth: Max folder depth to crawl
    
    Returns:
        Statistics dictionary with indexed counts
    """

    if not user_id:
        logger.error("Crawl aborted: user_id is required for security reasons")
        return {"error": "user_id is required for security reasons"}

    # Special handling for shared crawl (startup crawl with no real user)
    is_shared_crawl = (user_id == "shared")
    
    # Ensure user profile is cached first for personalized experience (skip for shared crawl)
    display_name = "shared access"  # Default for shared crawl
    if not is_shared_crawl:
        try:
            profile_cached = ensure_user_profile_cached(user_id)
            if not profile_cached:
                logger.warning(f"Could not cache user profile for {user_id[:min(20, len(user_id))]}... - profile fetch may have failed")
        except Exception as e:
            logger.error(f"Error ensuring profile cache for {user_id[:min(20, len(user_id))]}...: {e}")
        
        # Use provided display name as fallback if Graph profile doesn't have it
        display_name = get_user_display_name(user_id, fallback_name=user_display_name)
    
    logger.info(f"Starting document crawl for {'shared libraries' if is_shared_crawl else f'user: {display_name}'} ({user_id[:min(8, len(user_id))]}...)")

    token = get_graph_token()
    if not token:
        logger.error("Crawl aborted: Graph token unavailable")
        return {"error": "Graph token unavailable"}

    headers = {"Authorization": f"Bearer {token}"}
    max_items = max_items_per_drive or GRAPH_CRAWL_MAX_ITEMS_PER_DRIVE
    depth_limit = max_depth or GRAPH_CRAWL_MAX_DEPTH

    stats = {
        "user_id": user_id,
        "user_display_name": display_name,
        "personal_indexed": 0,
        "sharepoint_indexed": 0,
        "skipped": 0,
        "errors": 0,
    }

    # Crawl personal OneDrive (always user-specific, skip for shared crawl)
    if include_personal and user_id and not is_shared_crawl:
        try:
            drive_resp = requests.get(
                f"https://graph.microsoft.com/v1.0/users/{user_id}/drive",
                headers=headers,
                timeout=config.GRAPH_TIMEOUT,
            )
            _log_http("", "User drive", drive_resp)
            if drive_resp.status_code == 200:
                drive_id = drive_resp.json().get("id")
                if drive_id:
                    logger.info(f"Crawling personal OneDrive for {display_name}...")
                    _crawl_drive_items(
                        drive_id,
                        token,
                        owner_user_id=user_id,
                        treat_as_shared=False,
                        max_items=max_items,
                        max_depth=depth_limit,
                        stats=stats,
                    )
            else:
                logger.warning(f"Could not access drive for {display_name}: HTTP {drive_resp.status_code}")
        except Exception as e:
            logger.error(f"Error crawling personal drive for {display_name}: {e}", exc_info=True)
            stats["errors"] += 1

    # Crawl SPECIFIC SharePoint libraries (user-tagged, not shared)
    if include_sites:
        site_urls = config.get_sharepoint_sites_list()
        crawl_label = "shared libraries" if is_shared_crawl else display_name
        logger.info(f"Crawling {len(site_urls)} configured SharePoint site(s) for {crawl_label}...")
        
        for site_url in site_urls:
            site_id = _resolve_site_id(site_url, headers)
            if not site_id:
                logger.warning(f"Could not resolve site: {site_url}")
                stats["errors"] += 1
                continue

            # Get only document libraries from the site
            drives = _list_site_drives(site_id, headers)
            logger.info(f"Found {len(drives)} drive(s) at {site_url}")
            
            for drive in drives:
                drive_id = drive.get("id")
                drive_name = drive.get("name", "Unknown")
                drive_type = drive.get("driveType", "unknown")
                
                if not drive_id:
                    continue
                
                # Focus on document libraries (driveType: documentLibrary)
                crawl_label = "shared libraries" if is_shared_crawl else display_name
                logger.info(f"Crawling SharePoint library '{drive_name}' (type: {drive_type}) for {crawl_label}")
                _crawl_drive_items(
                    drive_id,
                    token,
                    owner_user_id=user_id,
                    treat_as_shared=False,  # Changed: treat as user-specific, not shared
                    max_items=max_items,
                    max_depth=depth_limit,
                    stats=stats,
                )

    # Update stat naming for clarity
    stats["sharepoint_indexed"] = stats.pop("shared_indexed", 0)
    
    crawl_label = "shared libraries" if is_shared_crawl else display_name
    logger.info(
        f"Crawl complete for {crawl_label}: "
        f"{stats['personal_indexed']} personal, "
        f"{stats['sharepoint_indexed']} SharePoint, "
        f"{stats['skipped']} skipped, "
        f"{stats['errors']} errors"
    )

    return stats
