"""
Attachment Cache - Persistent storage for file attachment contents.
Caches full attachment contents to disk so follow-up questions can access 
the data without re-downloading or hitting memory limits.
"""

import json
import os
import hashlib
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Cache file path - stored alongside other cache files
ATTACHMENT_CACHE_PATH = os.path.join(os.path.dirname(__file__), "attachment_cache.json")

# In-memory cache for fast access
_attachment_cache: dict = {}

# Cache settings
MAX_CACHE_SIZE_MB = 200  # Maximum total cache size in MB (increased for full content preservation)
MAX_CONTENT_SIZE_MB = 50  # Maximum size per attachment in MB (increased to handle large files)
MAX_CONTENT_SIZE_CHARS = 10000000  # Maximum characters per attachment (~10M chars = ~2.5M tokens) - effectively unlimited for most files
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


def _load_cache() -> dict:
    """Load the attachment cache from disk."""
    global _attachment_cache
    
    try:
        if not os.path.exists(ATTACHMENT_CACHE_PATH):
            _attachment_cache = {"attachments": {}, "metadata": {"version": 1}}
            return _attachment_cache
        
        with open(ATTACHMENT_CACHE_PATH, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                _attachment_cache = {"attachments": {}, "metadata": {"version": 1}}
                return _attachment_cache
            
            data = json.loads(content)
            if not isinstance(data, dict):
                data = {"attachments": {}, "metadata": {"version": 1}}
            
            # Ensure required structure
            if "attachments" not in data:
                data["attachments"] = {}
            if "metadata" not in data:
                data["metadata"] = {"version": 1}
            
            _attachment_cache = data
            return _attachment_cache
            
    except Exception as e:
        logger.error(f"Failed to load attachment cache: {e}")
        _attachment_cache = {"attachments": {}, "metadata": {"version": 1}}
        return _attachment_cache


def _save_cache() -> bool:
    """Save the attachment cache to disk atomically."""
    global _attachment_cache
    
    try:
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


def cache_attachment(conversation_id: str, filename: str, content: str) -> bool:
    """
    Cache an attachment's full content for later retrieval.
    Implements size limits to prevent memory issues.
    
    Args:
        conversation_id: The conversation this attachment belongs to
        filename: The attachment filename
        content: The full extracted text content
        
    Returns:
        True if cached successfully, False otherwise
    """
    if not conversation_id or not filename or not content:
        return False
    
    # Check character limit first (faster than byte size)
    if len(content) > MAX_CONTENT_SIZE_CHARS:
        logger.warning(f"Attachment '{filename}' content too long ({len(content):,} chars > {MAX_CONTENT_SIZE_CHARS:,})")
        logger.warning(f"Attachment exceeds maximum - this file is extremely large and may cause issues")
        # For extremely large files (> 10M chars), we still need a hard limit
        truncated_content = content[:MAX_CONTENT_SIZE_CHARS]
        truncated_content += f"\n\n⚠️ [CACHED CONTENT TRUNCATED - Original: {len(content):,} chars, Cached: {MAX_CONTENT_SIZE_CHARS:,} chars]\n"
        truncated_content += "[Note: This file is exceptionally large. Consider splitting it into smaller files for better results.]"
        content = truncated_content
        logger.info(f"Truncated extremely large file {filename}: {len(content):,} chars")
    
    # Check byte size limit for storage
    content_size_mb = len(content.encode('utf-8', errors='ignore')) / (1024 * 1024)
    if content_size_mb > MAX_CONTENT_SIZE_MB:
        logger.warning(f"Attachment '{filename}' too large to cache ({content_size_mb:.1f}MB > {MAX_CONTENT_SIZE_MB}MB)")
        return False
    
    try:
        # Load current cache
        cache = _load_cache()
        
        # Generate content hash for deduplication
        content_hash = _calculate_content_hash(content)
        attachment_id = _generate_attachment_id(conversation_id, filename, content_hash)
        
        # Store the attachment
        cache["attachments"][attachment_id] = {
            "conversation_id": conversation_id,
            "filename": filename,
            "content": content,
            "content_hash": content_hash,
            "content_length": len(content),
            "cached_at": datetime.now().isoformat(),
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
            logger.info(f"Cached attachment '{filename}' for conversation {conversation_id[:8]}... ({len(content):,} chars, {size_mb:.2f} MB) - FULL content preserved")
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Failed to cache attachment '{filename}': {e}")
        return False


def get_cached_attachment(conversation_id: str, filename: str) -> Optional[str]:
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
                logger.info(f"Retrieved cached attachment '{filename}' ({att_data.get('content_length', 0)} chars)")
                return att_data.get("content")
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to retrieve cached attachment '{filename}': {e}")
        return None


def search_attachment_contents(conversation_id: str, query: str, limit: int = 5) -> list[dict]:
    """
    Search within cached attachment contents for a conversation.
    
    Args:
        conversation_id: The conversation ID
        query: The search query
        limit: Maximum number of results to return
        
    Returns:
        List of dicts with 'filename', 'content_snippet', 'relevance_score' keys
    """
    try:
        if not query or not conversation_id:
            return []
            
        cache = _load_cache()
        query_lower = query.lower()
        results = []
        
        # Search through all attachments for this conversation
        for att_id, att_data in cache.get("attachments", {}).items():
            if att_data.get("conversation_id") == conversation_id:
                content = att_data.get("content", "")
                filename = att_data.get("filename", "unknown")
                
                if not content:
                    continue
                    
                content_lower = content.lower()
                
                # Simple relevance scoring based on query term matches
                query_terms = query_lower.split()
                match_count = 0
                content_matches = []
                
                for term in query_terms:
                    if term in content_lower:
                        match_count += content_lower.count(term)
                        # Find ALL occurrences and extract context around each match
                        # This ensures we capture matches throughout the document, not just the first
                        idx = 0
                        while idx < len(content_lower) and len(content_matches) < 10:  # Limit to 10 snippets per file
                            idx = content_lower.find(term, idx)
                            if idx == -1:
                                break
                            # Extract snippet around the match (400 chars before/after for better context)
                            snippet_start = max(0, idx - 400)
                            snippet_end = min(len(content), idx + len(term) + 400)
                            snippet = content[snippet_start:snippet_end].strip()
                            if snippet and snippet not in content_matches:
                                content_matches.append(snippet)
                            idx += len(term)
                
                if match_count > 0:
                    # Calculate relevance score
                    content_length = len(content)
                    relevance_score = (match_count * 100) / max(1, content_length / 1000)  # Normalized score
                    
                    results.append({
                        "filename": filename,
                        "content_snippet": " ... ".join(content_matches[:3]),  # Best 3 snippets
                        "relevance_score": round(relevance_score, 2),
                        "match_count": match_count,
                        "cached_at": att_data.get("cached_at"),
                        "full_content": content  # Include full content for further processing
                    })
        
        # Sort by relevance score (highest first)
        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        
        # Limit results
        limited_results = results[:limit]
        
        if limited_results:
            logger.info(f"Found {len(limited_results)} cached attachment(s) matching '{query}' in conversation {conversation_id[:8]}...")
        
        return limited_results
        
    except Exception as e:
        logger.error(f"Failed to search attachment contents: {e}")
        return []


def get_conversation_attachments(conversation_id: str, include_content: bool = True) -> list[dict]:
    """
    Get all cached attachments for a conversation.
    
    Args:
        conversation_id: The conversation ID
        include_content: If False, returns only metadata (faster for checks)
        
    Returns:
        List of dicts with 'filename' and optionally 'content' keys
    """
    try:
        cache = _load_cache()
        attachments = []
        
        # Find all attachments for this conversation
        for att_id, att_data in cache.get("attachments", {}).items():
            if att_data.get("conversation_id") == conversation_id:
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
            "max_size_mb": MAX_CACHE_SIZE_MB,
        }
        
    except Exception as e:
        logger.error(f"Failed to get cache stats: {e}")
        return {"error": str(e)}
