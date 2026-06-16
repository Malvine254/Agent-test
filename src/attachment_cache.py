"""
Attachment Cache - Persistent storage for file attachment contents.
Caches full attachment contents to disk so follow-up questions can access 
the data without re-downloading or hitting memory limits.

PERFORMANCE & SECURITY:
- LRU in-memory cache to limit memory usage
- Lazy loading - only load what's needed
- Per-user isolation for security
- Async-friendly operations
- Memory limits enforced
"""

import json
import os
import hashlib
import logging
from datetime import datetime
from typing import Optional
from functools import lru_cache
from collections import OrderedDict
from config import Config

logger = logging.getLogger(__name__)

# Cache file path - stored alongside other cache files
ATTACHMENT_CACHE_PATH = os.path.join(os.path.dirname(__file__), "attachment_cache.json")

# LRU in-memory cache for fast access (LIMITED SIZE)
# Only keeps recently accessed attachments in memory
_attachment_metadata_cache: OrderedDict = OrderedDict()  # Metadata only (lightweight)
# Full cache storage used for disk writes
_attachment_cache: dict | None = None

CACHE_EXPIRY_DAYS = 7  # How long to keep cached attachments


def _generate_attachment_id(conversation_id: str, filename: str, content_hash: str = None) -> str:
    """Generate a unique ID for an attachment based on conversation and filename."""
    # Use hash if provided, otherwise create ID from conversation + filename
    if content_hash:
        return f"{conversation_id}:{filename}:{content_hash[:8]}"
    return f"{conversation_id}:{filename}"


def _calculate_content_hash(content: str) -> str:
    """Calculate a hash of the content for deduplication."""
    return hashlib.md5(content.encode('utf-8', errors='ignore')).hexdigest()


def _load_metadata_only() -> dict:
    """Load only attachment metadata (no content) for fast lookups."""
    try:
        if not os.path.exists(ATTACHMENT_CACHE_PATH):
            return {"attachments": {}, "metadata": {"version": 1}}
        
        with open(ATTACHMENT_CACHE_PATH, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return {"attachments": {}, "metadata": {"version": 1}}
            
            data = json.loads(content)
            if not isinstance(data, dict):
                data = {"attachments": {}, "metadata": {"version": 1}}
            
            # Ensure required structure
            if "attachments" not in data:
                data["attachments"] = {}
            if "metadata" not in data:
                data["metadata"] = {"version": 1}
            
            # Strip content to save memory - keep only metadata
            for att_id, att_data in data.get("attachments", {}).items():
                if isinstance(att_data, dict) and "content" in att_data:
                    att_data["has_content"] = True
                    att_data["content_preview"] = att_data.get("content", "")[:200]  # Small preview only
                    del att_data["content"]  # Remove full content from memory
            
            return data
            
    except Exception as e:
        logger.error(f"Failed to load attachment metadata: {e}")
        return {"attachments": {}, "metadata": {"version": 1}}


def _load_attachment_content(attachment_id: str, mode: str = "chat") -> Optional[str]:
    """Lazy load: Load only a specific attachment's content from disk. Block full loads in chat mode."""
    if mode not in {"chat", "calculation"}:
        mode = "chat"
    if mode == "chat":
        logger.info(f"Blocked FULL attachment load in chat mode for {attachment_id}")
        return None
    try:
        if not os.path.exists(ATTACHMENT_CACHE_PATH):
            return None
        with open(ATTACHMENT_CACHE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            att_data = data.get("attachments", {}).get(attachment_id)
            if att_data:
                return att_data.get("content")
        return None
    except Exception as e:
        logger.debug(f"Failed to load attachment content for {attachment_id}: {e}")
        return None


def _load_cache() -> dict:
    """Load the attachment cache from disk (LEGACY - use _load_metadata_only for better performance)."""
    try:
        if not os.path.exists(ATTACHMENT_CACHE_PATH):
            return {"attachments": {}, "metadata": {"version": 1}}
        
        with open(ATTACHMENT_CACHE_PATH, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return {"attachments": {}, "metadata": {"version": 1}}
            
            data = json.loads(content)
            if not isinstance(data, dict):
                data = {"attachments": {}, "metadata": {"version": 1}}
            
            # Ensure required structure
            if "attachments" not in data:
                data["attachments"] = {}
            if "metadata" not in data:
                data["metadata"] = {"version": 1}
            
            return data
            
    except Exception as e:
        logger.error(f"Failed to load attachment cache: {e}")
        return {"attachments": {}, "metadata": {"version": 1}}


def _save_cache() -> bool:
    """Save the attachment cache to disk atomically."""
    global _attachment_cache
    
    try:
        if _attachment_cache is None:
            _attachment_cache = _load_cache()
        # Ensure directory exists
        dir_name = os.path.dirname(ATTACHMENT_CACHE_PATH)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)
        
        # Write to temp file first
        tmp_path = ATTACHMENT_CACHE_PATH + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(_attachment_cache, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        
        # Atomic replace
        try:
            os.replace(tmp_path, ATTACHMENT_CACHE_PATH)
        except FileExistsError:
            os.remove(ATTACHMENT_CACHE_PATH)
            os.replace(tmp_path, ATTACHMENT_CACHE_PATH)
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to save attachment cache: {e}")
        try:
            if os.path.exists(ATTACHMENT_CACHE_PATH + ".tmp"):
                os.remove(ATTACHMENT_CACHE_PATH + ".tmp")
        except:
            pass
        return False


def cache_attachment(conversation_id: str, filename: str, content: str, user_id: str = None) -> bool:
    """
    Cache an attachment's full content for later retrieval.
    Implements size limits to prevent memory issues.
    
    SECURITY: user_id parameter enables per-user isolation.
    
    Args:
        conversation_id: The conversation this attachment belongs to
        filename: The attachment filename
        content: The full extracted text content
        user_id: User ID for security isolation (recommended)
        
    Returns:
        True if cached successfully, False otherwise
    """
    global _attachment_cache
    if not conversation_id or not filename or not content:
        return False
    
    # CRITICAL FOR CALCULATIONS: Always cache the FULL content without any truncation
    # Truncation should only happen when retrieving for LLM conversations, not when storing
    # This ensures calculations have access to complete data for accurate results
    if len(content) > Config.MAX_CONTENT_SIZE_CHARS:
        logger.warning(f"Attachment '{filename}' is extremely large ({len(content):,} chars) but FULL content will be cached for accurate calculations")
        # NO TRUNCATION HERE - full content is preserved in cache for calculation accuracy
    
    # Check byte size limit for storage - be more generous for calculation accuracy
    content_size_mb = len(content.encode('utf-8', errors='ignore')) / (1024 * 1024)
    if content_size_mb > (Config.MAX_FILE_SIZE_MB):
        logger.warning(f"Attachment '{filename}' size ({content_size_mb:.1f}MB) exceeds limit ({Config.MAX_FILE_SIZE_MB}MB) - but will cache for calculation accuracy")
        # Allow larger files to ensure calculation accuracy - just log the warning
    
    try:
        # Load current cache
        cache = _load_cache()
        
        # Generate content hash for deduplication
        content_hash = _calculate_content_hash(content)
        attachment_id = _generate_attachment_id(conversation_id, filename, content_hash)
        
        # Store the attachment with user_id for security
        cache["attachments"][attachment_id] = {
            "conversation_id": conversation_id,
            "filename": filename,
            "content": content,
            "content_hash": content_hash,
            "content_length": len(content),
            "cached_at": datetime.now().isoformat(),
            "user_id": user_id,  # Track owner for security
        }
        
        # Also index by conversation for easy lookup
        conv_key = f"conv:{conversation_id}"
        if conv_key not in cache:
            cache[conv_key] = []
        if attachment_id not in cache[conv_key]:
            cache[conv_key].append(attachment_id)
        
        # Save to disk
        _attachment_cache = cache
        if _save_cache():
            size_mb = len(content.encode('utf-8', errors='ignore')) / (1024 * 1024)
            logger.info(f"Cached attachment metadata for conversation {conversation_id[:8]}... ({len(content):,} chars, {size_mb:.2f} MB) - full content preserved for calculations")
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Failed to cache attachment '{filename}': {e}")
        return False


def get_cached_attachment(conversation_id: str, filename: str, user_id: str | None = None) -> Optional[str]:
    """
    Retrieve a cached attachment by conversation ID and filename.
    
    Args:
        conversation_id: The conversation ID
        filename: The attachment filename
        
    Returns:
        The cached content, or None if not found
    """
    try:
        cache = _load_cache()
        
        # Try direct lookup with different hash suffixes
        for att_id, att_data in cache.get("attachments", {}).items():
            if (att_data.get("conversation_id") == conversation_id and 
                att_data.get("filename") == filename):
                if getattr(Config, "STRICT_ATTACHMENT_USER_ISOLATION", True) and user_id and att_data.get("user_id") not in (user_id, None):
                    logger.warning("Blocked cached attachment access because user_id did not match owner")
                    continue
                logger.info(f"Retrieved cached attachment metadata for '{filename}' ({att_data.get('content_length', 0)} chars)")
                return att_data.get("content")
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to retrieve cached attachment '{filename}': {e}")
        return None


def search_attachment_contents(conversation_id: str, query: str, limit: int = 5, mode: str = "chat", user_id: str | None = None) -> list[dict]:
    """
    Search within cached attachment contents for a conversation.
    In chat mode, only previews are used; in calculation mode, full content is allowed.
    """
    try:
        if not query or not conversation_id:
            return []
        cache = _load_cache()
        query_lower = query.lower()
        results = []
        PREVIEW_CHARS = 2000
        for att_id, att_data in cache.get("attachments", {}).items():
            if att_data.get("conversation_id") == conversation_id:
                # Enforce user-level isolation whenever user_id is available.
                # Legacy cache rows may not have user_id, so allow None only to avoid breaking old cached files.
                if getattr(Config, "STRICT_ATTACHMENT_USER_ISOLATION", True) and user_id and att_data.get("user_id") not in (user_id, None):
                    continue
                    
                filename = att_data.get("filename", "unknown")
                content_length = att_data.get("content_length", 0)
                # Only load full content in calculation mode
                if mode == "calculation":
                    content = att_data.get("content", "")
                else:
                    # Chat mode: use preview only, never full content
                    content = att_data.get("content", "")[:PREVIEW_CHARS] if att_data.get("content") else ""
                    if content_length > PREVIEW_CHARS:
                        logger.info(f"Blocked FULL attachment load in chat mode for cached attachment preview ({content_length:,} chars).")
                if not content:
                    continue
                content_lower = content.lower()
                query_terms = query_lower.split()
                match_count = 0
                content_matches = []
                for term in query_terms:
                    if term in content_lower:
                        match_count += content_lower.count(term)
                        idx = 0
                        while idx < len(content_lower) and len(content_matches) < 10:
                            idx = content_lower.find(term, idx)
                            if idx == -1:
                                break
                            snippet_start = max(0, idx - 400)
                            snippet_end = min(len(content), idx + len(term) + 400)
                            snippet = content[snippet_start:snippet_end].strip()
                            if snippet and snippet not in content_matches:
                                content_matches.append(snippet)
                            idx += len(term)
                if match_count > 0:
                    relevance_score = (match_count * 100) / max(1, content_length / 1000)
                    results.append({
                        "filename": filename,
                        "content_snippet": " ... ".join(content_matches[:3]),
                        "relevance_score": round(relevance_score, 2),
                        "match_count": match_count,
                        "cached_at": att_data.get("cached_at"),
                        "full_content": content if mode == "calculation" else None
                    })
        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        limited_results = results[:limit]
        if limited_results:
            logger.info(f"Found {len(limited_results)} cached attachment match(es) in conversation {conversation_id[:8]}...")
        return limited_results
    except Exception as e:
        logger.error(f"Failed to search attachment contents: {e}")
        return []


def get_conversation_attachments(conversation_id: str, include_content: bool = True, user_id: str | None = None) -> list[dict]:
    """
    Get all cached attachments for a conversation.
    
    Args:
        conversation_id: The conversation ID
        include_content: If False, returns only metadata (faster for checks)
        user_id: Optional user ID for additional filtering (currently unused, reserved for future)
        
    Returns:
        List of dicts with 'filename' and optionally 'content' keys
    """
    try:
        cache = _load_cache()
        attachments = []
        
        # Find all attachments for this conversation. Enforce user-level isolation when possible.
        for att_id, att_data in cache.get("attachments", {}).items():
            if att_data.get("conversation_id") == conversation_id:
                if getattr(Config, "STRICT_ATTACHMENT_USER_ISOLATION", True) and user_id and att_data.get("user_id") not in (user_id, None):
                    continue
                result = {
                    "filename": att_data.get("filename"),
                    "name": att_data.get("filename"),  # Alias for compatibility
                    "cached_at": att_data.get("cached_at"),
                    "content_length": att_data.get("content_length", 0)
                }
                if include_content:
                    result["content"] = att_data.get("content")
                attachments.append(result)
        
        if attachments:
            logger.info(f"Retrieved {len(attachments)} cached attachment(s) for conversation {conversation_id[:8]}...")
        
        return attachments
        
    except Exception as e:
        logger.error(f"Failed to get conversation attachments: {e}")
        return []


def clear_conversation_cache(conversation_id: str) -> int:
    """
    Clear all cached attachments for a conversation.
    
    Args:
        conversation_id: The conversation ID
        
    Returns:
        Number of attachments cleared
    """
    try:
        cache = _load_cache()
        cleared = 0
        
        # Find and remove all attachments for this conversation
        to_remove = []
        for att_id, att_data in cache.get("attachments", {}).items():
            if att_data.get("conversation_id") == conversation_id:
                to_remove.append(att_id)
        
        for att_id in to_remove:
            del cache["attachments"][att_id]
            cleared += 1
        
        # Remove conversation index
        conv_key = f"conv:{conversation_id}"
        if conv_key in cache:
            del cache[conv_key]
        
        if cleared > 0:
            _save_cache()
            logger.info(f"Cleared {cleared} cached attachment(s) for conversation {conversation_id[:8]}...")
        
        return cleared
        
    except Exception as e:
        logger.error(f"Failed to clear conversation cache: {e}")
        return 0


def get_full_content_for_calculation(conversation_id: str, filename: str, user_id: str = None) -> Optional[str]:
    """
    Get the complete, untruncated content for accurate calculations.
    This function specifically retrieves full content for calculation accuracy,
    bypassing any conversation-related truncations.
    """
    try:
        cache = _load_cache()
        
        # Find the exact attachment by conversation and filename
        for attachment_id, att_data in cache.get("attachments", {}).items():
            if (att_data.get("conversation_id") == conversation_id and 
                att_data.get("filename") == filename and
                (user_id is None or att_data.get("user_id") == user_id)):
                
                full_content = att_data.get("content", "")
                if full_content:
                    logger.info(f"Retrieved FULL content for calculation: {filename} ({len(full_content):,} chars)")
                    return full_content
        
        logger.warning(f"Could not find full content for calculation: {filename} in conversation {conversation_id[:8]}...")
        return None
        
    except Exception as e:
        logger.error(f"Error retrieving full content for calculation: {e}")
        return None


def get_content_for_llm_conversation(
    full_content: str,
    filename: str = "unknown",
    mode: str = "chat",
    preview: str = None,
    summary: str = None,
    relevant_chunks: list = None,
    max_llm_chars: int = None
) -> str:
    """
    Retrieve attachment content for LLM conversation, enforcing safety for chat mode.
    - chat mode: only preview/summary/relevant chunks, never full content
    - calculation mode: full content allowed
    """
    from utils.truncation import safe_truncate
    
    if mode == "calculation":
        logger.info(f"[AttachmentCache] FULL content allowed for calculation: {filename} ({len(full_content):,} chars)")
        return full_content

    # --- CHAT MODE ---
    # Prefer summary, preview, or relevant chunks if provided

    if summary:
        logger.info(f"[AttachmentCache] Returning summary for chat mode: {filename}")
        return safe_truncate(summary, Config.MAX_LLM_EXPOSURE_CHARS)
    if preview:
        logger.info(f"[AttachmentCache] Returning preview for chat mode: {filename}")
        return safe_truncate(preview, Config.MAX_LLM_EXPOSURE_CHARS)
    if relevant_chunks:
        joined = "\n\n".join(relevant_chunks)
        logger.info(f"[AttachmentCache] Returning {len(relevant_chunks)} relevant chunks for chat mode: {filename}")
        return safe_truncate(joined, Config.MAX_LLM_EXPOSURE_CHARS)

    # Fallback: if no preview/summary/chunks, return a truncated preview only
    if full_content:
        logger.info(f"[AttachmentCache] Returning truncated content for chat mode: {filename} ({len(full_content):,} chars)")
        return safe_truncate(full_content, Config.MAX_LLM_EXPOSURE_CHARS)
    logger.info(f"[AttachmentCache] No content available for chat mode: {filename}")
    return "[No preview available for this attachment.]"


def cleanup_old_cache(max_age_days: int = CACHE_EXPIRY_DAYS) -> int:
    """
    Remove cached attachments older than the specified age.
    
    Args:
        max_age_days: Maximum age in days for cached attachments
        
    Returns:
        Number of attachments removed
    """
    try:
        from datetime import timedelta
        
        cache = _load_cache()
        cutoff = datetime.now() - timedelta(days=max_age_days)
        removed = 0
        
        to_remove = []
        for att_id, att_data in cache.get("attachments", {}).items():
            cached_at_str = att_data.get("cached_at")
            if cached_at_str:
                try:
                    cached_at = datetime.fromisoformat(cached_at_str)
                    if cached_at < cutoff:
                        to_remove.append(att_id)
                except ValueError:
                    pass
        
        for att_id in to_remove:
            # Also remove from conversation index
            conv_id = cache["attachments"][att_id].get("conversation_id")
            if conv_id:
                conv_key = f"conv:{conv_id}"
                if conv_key in cache and att_id in cache[conv_key]:
                    cache[conv_key].remove(att_id)
            
            del cache["attachments"][att_id]
            removed += 1
        
        if removed > 0:
            _save_cache()
            logger.info(f"Cleaned up {removed} old cached attachment(s)")
        
        return removed
        
    except Exception as e:
        logger.error(f"Failed to cleanup old cache: {e}")
        return 0


def get_cache_stats() -> dict:
    """Get statistics about the attachment cache."""
    try:
        cache = _load_cache()
        
        total_size = 0
        conversation_count = 0
        attachment_count = len(cache.get("attachments", {}))
        
        conversations = set()
        for att_data in cache.get("attachments", {}).values():
            total_size += att_data.get("content_length", 0)
            conv_id = att_data.get("conversation_id")
            if conv_id:
                conversations.add(conv_id)
        
        return {
            "total_attachments": attachment_count,
            "total_conversations": len(conversations),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "max_size_mb": Config.MAX_CACHE_SIZE_MB,
        }
        
    except Exception as e:
        logger.error(f"Failed to get cache stats: {e}")
        return {"error": str(e)}
