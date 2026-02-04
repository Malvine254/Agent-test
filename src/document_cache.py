import os
import json
import logging
from difflib import SequenceMatcher
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from collections import defaultdict

class DocumentCache:
    def __init__(self, cache_file: str = "document_cache.json"):
        self.cache_file = cache_file
        self.cache = self._load_cache()
        self._word_index = {}  # Fast lookup: word -> list of doc_ids containing it

    def _atomic_write_json(self, path: str, obj: dict) -> None:
        """Atomically write JSON: write to a temp file then replace target."""
        try:
            dir_name = os.path.dirname(path)
            if dir_name and not os.path.exists(dir_name):
                try:
                    os.makedirs(dir_name, exist_ok=True)
                except OSError as e:
                    logger.warning(f"Could not create directory {dir_name}: {e}. Attempting direct write.")
                    # Fallback: try writing directly if directory creation fails
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(obj or {}, f, indent=2, ensure_ascii=False)
                    return
            
            tmp_path = path + ".tmp"
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(obj or {}, f, indent=2, ensure_ascii=False)
                    f.flush()
                    try:
                        import os as _os
                        _os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(tmp_path, path)
            except Exception as tmp_err:
                logger.warning(f"Atomic write (temp file) failed for {path}: {tmp_err}. Attempting direct write.")
                # Fallback: write directly if atomic write fails
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(obj or {}, f, indent=2, ensure_ascii=False)
                # Clean up temp file if it exists
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Atomic write failed for {path}: {e}")

    def _ensure_roots(self) -> None:
        """Ensure core cache sections exist for users and shared documents."""
        if "users" not in self.cache:
            self.cache["users"] = {}
        if "shared" not in self.cache:
            self.cache["shared"] = {"documents": {}}
    
    def _load_cache(self) -> Dict:
        """Load cache from JSON file"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if not content:
                        logger.warning("Cache file is empty, initializing new cache")
                        self.cache = {"users": {}, "shared": {"documents": {}}, "last_updated": None}
                        return self.cache
                    
                    data = json.loads(content)
                    # Migrate old format to new user-specific format
                    if "documents" in data and "users" not in data:
                        logger.info("Migrating cache to user-specific format")
                        data = {"users": {}, "shared": {"documents": {}}, "last_updated": data.get("last_updated")}

                    self.cache = data
                    self._ensure_roots()
                    return self.cache
            except json.JSONDecodeError as e:
                logger.error(f"Cache file corrupted (JSON error): {e}. Starting fresh.")
                self.cache = {"users": {}, "shared": {"documents": {}}, "last_updated": None}
                return self.cache
            except Exception as e:
                logger.error(f"Error loading cache: {e}")
                self.cache = {"users": {}, "shared": {"documents": {}}, "last_updated": None}
                return self.cache
        self.cache = {"users": {}, "shared": {"documents": {}}, "last_updated": None}
        return self.cache
    
    def _save_cache(self):
        """Save cache to JSON file"""
        try:
            # Optional: allow manual edits by disabling persistence via env flag
            disable = os.environ.get("DISABLE_CACHE_PERSIST", "false").strip().lower() in ("1", "true", "yes")
            if disable:
                logger.info("Cache persistence disabled by env (DISABLE_CACHE_PERSIST). Skipping save.")
                return
            self._ensure_roots()
            self.cache["last_updated"] = datetime.now().isoformat()
            self._atomic_write_json(self.cache_file, self.cache)
        except Exception as e:
            logger.error(f"Error saving cache: {e}")
    
    def _get_user_cache(self, user_id: str) -> Dict:
        """Get or create user-specific cache"""
        if "users" not in self.cache:
            self.cache["users"] = {}
        
        if user_id not in self.cache["users"]:
            self.cache["users"][user_id] = {"documents": {}}
        
        return self.cache["users"][user_id]["documents"]

    def _get_shared_cache(self) -> Dict:
        """Get or create shared (common) cache accessible to all users."""
        self._ensure_roots()
        return self.cache["shared"]["documents"]
    
    def get_document(self, doc_id: str, user_id: str) -> Optional[Dict]:
        """Get a specific document from user's cache"""
        user_docs = self._get_user_cache(user_id)
        return user_docs.get(doc_id)
    
    def add_document(self, doc_id: str, name: str, url: str, content: str, user_id: str, metadata: Dict = None):
        """Add or update a document in user's cache (skips if already exists)"""
        user_docs = self._get_user_cache(user_id)
        
        # Skip if document already exists
        if doc_id in user_docs:
            logger.debug(f"Document {doc_id} already cached for user {user_id[:8]}..., skipping")
            return
        
        user_docs[doc_id] = {
            "id": doc_id,
            "name": name,
            "url": url,
            "content": content,
            "metadata": metadata or {},
            "cached_at": datetime.now().isoformat(),
            "user_id": user_id,
            "visibility": "user"
        }
        self._save_cache()
        logger.info(f"Cached document for user {user_id[:8]}...: {name}")

    def add_shared_document(self, doc_id: str, name: str, url: str, content: str, metadata: Dict = None):
        """Add or update a document in the shared cache (visible to all users). Skips if already exists."""
        shared_docs = self._get_shared_cache()
        
        # Skip if document already exists
        if doc_id in shared_docs:
            logger.debug(f"Shared document {doc_id} already cached, skipping")
            return
        
        shared_docs[doc_id] = {
            "id": doc_id,
            "name": name,
            "url": url,
            "content": content,
            "metadata": metadata or {},
            "cached_at": datetime.now().isoformat(),
            "visibility": "shared"
        }
        self._save_cache()
        logger.info(f"Cached shared document: {name}")
    
    def _best_snippet(self, content: str, query: str, window: int = 240) -> str:
        """Return a short snippet around the best match for the query.
        
        Prioritizes finding the actual query term in the content for context.
        """
        if not content:
            return ""
        
        content_lower = content.lower()
        query_lower = query.lower()
        
        # 1. Try exact phrase match first
        idx = content_lower.find(query_lower)
        if idx != -1:
            start = max(0, idx - window // 3)
            end = min(len(content), idx + len(query) + window - (idx - start))
            return content[start:end].strip()
        
        # 2. Try each word of the query separately
        words = query_lower.split()
        for word in words:
            if len(word) > 2:  # Only search for meaningful words
                idx = content_lower.find(word)
                if idx != -1:
                    start = max(0, idx - window // 3)
                    end = min(len(content), idx + len(word) + window - (idx - start))
                    return content[start:end].strip()
        
        # 3. Fallback: return first window-sized chunk
        return content[:window].strip()

    def _semantic_ratio(self, query: str, content: str) -> float:
        """Lightweight semantic similarity via sequence matching on paragraphs."""
        if not content:
            return 0.0

        content_segments = [seg.strip() for seg in content.split("\n\n") if seg.strip()]
        if not content_segments:
            content_segments = [content.strip()]

        # Limit segments for performance
        content_segments = content_segments[:8]
        query_lower = query.lower()

        best = 0.0
        for seg in content_segments:
            ratio = SequenceMatcher(None, query_lower, seg.lower()).ratio()
            if ratio > best:
                best = ratio
        return best

    def _score_document(self, doc: Dict, query: str) -> int:
        """Calculate relevance score for a document based on query.
        
        Scoring hierarchy (precision-first):
        - Phrase match in title: +50
        - Title startswith query: +20
        - Phrase match in content: +25
        - Word boundary match: +14 per word (in title), +10 per word (in content)
        - Fuzzy match in title/content: +15 to +30 (typo tolerance)
        - Partial word match: +4 per word
        - Semantic similarity: +0 to +8
        """
        score = 0
        name_lower = doc.get("name", "").lower()
        content_lower = doc.get("content", "").lower()
        query_lower = query.lower()
        
        # Early exit for empty query
        if not query_lower.strip():
            return 0
        
        # 1. Phrase matches (exact substring) - highest priority
        if query_lower in name_lower:
            score += 50
        if name_lower.startswith(query_lower):
            score += 20
        if query_lower in content_lower:
            score += 25
        
        # 2. Word-level matches with word boundaries (handles multi-word queries)
        words = query_lower.split()
        
        for word in words:
            if not word:
                continue
            
            # Check word boundaries in title (stronger signal)
            if f" {word} " in f" {name_lower} " or name_lower.startswith(word) or name_lower.endswith(word):
                score += 14
            elif word in name_lower:
                # Partial match in title (e.g., "doc" in "document")
                score += 4
            
            # Check word boundaries in content (weaker signal but important)
            if f" {word} " in f" {content_lower} ":
                score += 10
            elif word in content_lower:
                # Partial match in content - includes names like "edgar" in text
                score += 4
        
        # 3. Fuzzy matching for typo tolerance (apply when relevance is still modest)
        if score < 50:  # Boost near-misses (e.g., 'pto document' vs 'PTO_File.xlsx')
            # Check if query is similar to filename (typo tolerance)
            name_words = name_lower.split('_')  # Split on underscore for file names like "pto_file.xlsx"
            name_words.extend(name_lower.replace('_', ' ').split())  # Also split on spaces
            
            for name_word in name_words:
                if len(name_word) >= 3:  # Only fuzzy match words with 3+ chars
                    similarity = SequenceMatcher(None, query_lower, name_word).ratio()
                    if similarity >= 0.8:  # 80% similar (handles 1-2 char typos)
                        score += 30
                    elif similarity >= 0.7:  # 70% similar
                        score += 20
                    elif similarity >= 0.6:  # 60% similar
                        score += 15
            
            # Also check word-level fuzzy matching for multi-word queries
            for word in words:
                if len(word) >= 3:
                    for name_word in name_words:
                        if len(name_word) >= 3:
                            similarity = SequenceMatcher(None, word, name_word).ratio()
                            if similarity >= 0.8:
                                score += 12  # Per-word fuzzy bonus
                            elif similarity >= 0.7:
                                score += 8
        
        # 3. Semantic similarity boost (lightweight "semantic" matching)
        # This helps when words differ but meaning overlaps (typos or paraphrase).
        semantic_score = self._semantic_ratio(query, doc.get("content", ""))
        if semantic_score >= 0.35:
            score += int(semantic_score * 40)  # stronger boost for semantically close text
        else:
            score += int(semantic_score * 8)   # mild boost for weaker matches
        
        return score

    def _build_word_index(self, docs_map: Dict) -> Dict:
        """Build fast word->document lookup index for current query set.
        
        Returns dict mapping words to list of doc_ids containing them.
        Only indexes words >= 2 chars for performance.
        """
        word_index = defaultdict(list)
        
        for doc_id, doc in docs_map.items():
            # Extract words from title and content
            text = f"{doc.get('name', '')} {doc.get('content', '')}".lower()
            words = set()
            
            # Split into words, keeping only 2+ char words
            for word in text.split():
                # Strip punctuation
                word = word.strip('.,!?;:\'"()[]{}')
                if len(word) >= 2:
                    words.add(word)
            
            # Add all words for this doc
            for word in words:
                word_index[word].append(doc_id)
        
        return dict(word_index)

    def _is_personal_url(self, url: str) -> bool:
        """Detect OneDrive personal URLs (restrict to user-only)."""
        return (
            "my.sharepoint.com/personal" in url
            or "/personal/" in url
        )

    def search_cache(self, query: str, user_id: Optional[str], limit: int = 10, include_shared: bool = True) -> List[Dict]:
        """Fast search using indexed word lookup + scoring.

        Strategy:
        1. Use word index to pre-filter candidate documents (faster)
        2. Score only candidates that match query words
        3. Return top-N by relevance score
        
        Security:
        - User can only search their own documents (user_id partition)
        - Shared documents (if enabled) are accessible to all
        - Personal OneDrive docs from other users are NEVER accessible
        """
        if not query or not query.strip():
            return []
        
        # Build unique set of candidate docs (dedupe by doc_id)
        docs_map = {}
        
        # SECURITY: Only include current user's documents
        if user_id:
            user_docs = self._get_user_cache(user_id)
            for doc_id, doc in user_docs.items():
                # Double-check document belongs to this user
                doc_owner = doc.get("user_id", "")
                if doc_owner and doc_owner != user_id:
                    logger.warning(f"SECURITY: Skipping doc {doc_id} - owner mismatch (expected {user_id[:8]}..., got {doc_owner[:8]}...)")
                    continue
                docs_map[doc_id] = doc
            logger.debug(f"User cache: {len(docs_map)} docs for user {user_id[:8]}...")
        else:
            logger.warning("SECURITY: No user_id provided for cache search - returning empty results")
            return []
        
        # SECURITY: Only include explicitly shared documents if enabled
        if include_shared:
            shared_docs = self._get_shared_cache()
            for doc_id, doc in shared_docs.items():
                # Verify document is marked as shared visibility
                if doc.get("visibility") != "shared":
                    logger.warning(f"SECURITY: Skipping doc {doc_id} - not explicitly shared")
                    continue
                # Don't override user-specific documents with shared ones
                if doc_id not in docs_map:
                    docs_map[doc_id] = doc
            logger.debug(f"Added {len(shared_docs)} shared docs to search pool")

        # SECURITY NOTE: We do NOT include documents from other users' caches
        # All SharePoint and OneDrive documents are user-specific for privacy
        logger.debug(f"Total searchable docs: {len(docs_map)} (user: {user_id[:8]}...)")

        # Build word index for fast lookup
        word_index = self._build_word_index(docs_map)
        
        # Get candidate doc IDs by checking which have matching words
        query_lower = query.lower()
        query_words = query_lower.split()
        candidate_ids = set()
        
        # Check for phrase match first (fastest)
        for doc_id, doc in docs_map.items():
            if query_lower in doc.get("name", "").lower() or query_lower in doc.get("content", "").lower():
                candidate_ids.add(doc_id)
        
        # Then check for word matches
        for word in query_words:
            word_clean = word.strip('.,!?;:\'"()[]{}')
            if len(word_clean) >= 2 and word_clean in word_index:
                candidate_ids.update(word_index[word_clean])
        
        # If no candidates found, score all docs anyway (fallback for partial matches)
        if not candidate_ids:
            candidate_ids = set(docs_map.keys())
        
        # Score all candidates and collect results
        results = []
        for doc_id in candidate_ids:
            if doc_id not in docs_map:
                continue
            
            doc = docs_map[doc_id]
            score = self._score_document(doc, query)
            
            if score > 0:
                snippet = self._best_snippet(doc["content"], query)
                results.append({
                    "doc": {**doc, "snippet": snippet},
                    "score": score
                })
        
        # Sort by relevance score (highest first)
        results.sort(key=lambda x: x["score"], reverse=True)
        
        # Dedupe results by doc id (keep highest scoring instance)
        unique_docs = []
        seen_ids = set()
        for r in results:
            rid = r["doc"].get("id") or r["doc"].get("url")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            unique_docs.append(r["doc"])
        
        return unique_docs[:limit]

    def search_cache_scored(self, query: str, user_id: Optional[str], limit: int = 10, include_shared: bool = True) -> List[Dict]:
        """Same as search_cache but returns scored entries: [{ 'doc': {...}, 'score': int }].
        
        Security:
        - User can only search their own documents (user_id partition)
        - Shared documents (if enabled) are accessible to all
        - Personal OneDrive docs from other users are NEVER accessible
        """
        if not query or not query.strip():
            return []

        # Build unique set of candidate docs (dedupe by doc_id)
        docs_map = {}
        
        # SECURITY: Only include current user's documents
        if user_id:
            user_docs = self._get_user_cache(user_id)
            for doc_id, doc in user_docs.items():
                # Double-check document belongs to this user
                doc_owner = doc.get("user_id", "")
                if doc_owner and doc_owner != user_id:
                    logger.warning(f"SECURITY: Skipping doc {doc_id} - owner mismatch")
                    continue
                docs_map[doc_id] = doc
        else:
            logger.warning("SECURITY: No user_id provided for scored cache search - returning empty results")
            return []
        
        # SECURITY: Only include explicitly shared documents if enabled
        if include_shared:
            for doc_id, doc in self._get_shared_cache().items():
                # Verify document is marked as shared visibility
                if doc.get("visibility") != "shared":
                    continue
                # Don't override user-specific documents with shared ones
                if doc_id not in docs_map:
                    docs_map[doc_id] = doc

        # SECURITY NOTE: We do NOT include documents from other users' caches

        word_index = self._build_word_index(docs_map)
        query_lower = query.lower()
        query_words = query_lower.split()
        candidate_ids = set()
        for doc_id, doc in docs_map.items():
            if query_lower in doc.get("name", "").lower() or query_lower in doc.get("content", "").lower():
                candidate_ids.add(doc_id)
        for word in query_words:
            word_clean = word.strip('.,!?;:\'"()[]{}')
            if len(word_clean) >= 2 and word_clean in word_index:
                candidate_ids.update(word_index[word_clean])
        if not candidate_ids:
            candidate_ids = set(docs_map.keys())

        results = []
        for doc_id in candidate_ids:
            if doc_id not in docs_map:
                continue
            doc = docs_map[doc_id]
            score = self._score_document(doc, query)
            if score > 0:
                snippet = self._best_snippet(doc.get("content", ""), query)
                results.append({
                    "doc": {**doc, "snippet": snippet},
                    "score": score
                })
        results.sort(key=lambda x: x["score"], reverse=True)
        # Deduplicate by doc id/url while keeping score
        unique_scored = []
        seen_ids = set()
        for r in results:
            rid = r["doc"].get("id") or r["doc"].get("url")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            unique_scored.append(r)
        return unique_scored[:limit]
    
    def has_document(self, doc_id: str, user_id: str) -> bool:
        """Check if document exists in user's cache"""
        user_docs = self._get_user_cache(user_id)
        return doc_id in user_docs

    def has_shared_document(self, doc_id: str) -> bool:
        """Check if document exists in shared cache"""
        shared_docs = self._get_shared_cache()
        return doc_id in shared_docs

    def get_shared_document(self, doc_id: str) -> Optional[Dict]:
        shared_docs = self._get_shared_cache()
        return shared_docs.get(doc_id)
    
    def get_all_documents(self, user_id: str, include_shared: bool = True) -> List[Dict]:
        """Get all cached documents for a user and, optionally, shared docs."""
        docs = list(self._get_user_cache(user_id).values())
        if include_shared:
            docs.extend(self._get_shared_cache().values())
        return docs

    def get_all_shared_documents(self) -> List[Dict]:
        """Return all shared documents."""
        return list(self._get_shared_cache().values())
    
    def clear_user_cache(self, user_id: str):
        """Clear cached documents for a specific user"""
        if "users" in self.cache and user_id in self.cache["users"]:
            self.cache["users"][user_id] = {"documents": {}}
            self._save_cache()
            logger.info(f"Cache cleared for user {user_id[:8]}...")
    
    def clear_cache(self):
        """Clear all cached documents (admin function)"""
        self.cache = {"users": {}, "shared": {"documents": {}}, "last_updated": None}
        self._save_cache()
        logger.info("All cache cleared")

    def clear_shared_cache(self):
        """Clear only the shared document cache."""
        self._ensure_roots()
        self.cache["shared"] = {"documents": {}}
        self._save_cache()
        logger.info("Shared cache cleared")

# Singleton instance
_cache_instance = None

def get_cache() -> DocumentCache:
    """Get singleton cache instance"""
    global _cache_instance
    if _cache_instance is None:
        cache_dir = os.path.dirname(__file__)
        cache_file = os.path.join(cache_dir, "document_cache.json")
        _cache_instance = DocumentCache(cache_file)
    return _cache_instance
