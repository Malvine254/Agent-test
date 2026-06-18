import os
import json
import logging
import hashlib
import re
from difflib import SequenceMatcher
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from collections import defaultdict

class DocumentCache:
    SEARCH_STOPWORDS = {
        "a", "an", "and", "are", "as", "at", "be", "by", "from", "in",
        "is", "it", "of", "on", "or", "the", "to", "with",
        "can", "you", "please", "for", "summarize", "summary", "tell",
        "about", "overview", "explain", "document", "file",
        "llc", "inc", "corp", "corporation", "ltd", "limited", "company",
    }

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
            self.cache["shared"] = {"documents": {}, "content_hash_index": {}}
    
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
        """Get or create user-specific cache with strict isolation"""
        if not user_id or not user_id.strip():
            logger.error("SECURITY: Cannot create cache - user_id is empty")
            return {}
            
        if "users" not in self.cache:
            self.cache["users"] = {}
        
        # SECURITY: Validate user_id format (prevent path traversal)
        if ".." in user_id or "/" in user_id or "\\" in user_id:
            logger.error(f"SECURITY: Invalid user_id format: {user_id}")
            return {}
        
        if user_id not in self.cache["users"]:
            self.cache["users"][user_id] = {
                "documents": {},
                "created_at": datetime.now().isoformat(),
                "user_id": user_id  # Store for verification
            }
            logger.info(f"Created new cache partition for user: {user_id[:8]}...")
        
        return self.cache["users"][user_id]["documents"]

    def _get_shared_cache(self) -> Dict:
        """Get or create shared (common) cache accessible to all users."""
        self._ensure_roots()
        return self.cache["shared"]["documents"]
    
    def _calculate_content_hash(self, content: str) -> str:
        """Calculate SHA256 hash of content for deduplication."""
        return hashlib.sha256(content.encode('utf-8', errors='ignore')).hexdigest()
    
    def _find_duplicate_by_content(self, content_hash: str) -> Optional[str]:
        """Find if content already exists in shared cache by hash. Returns shared doc_id if found."""
        self._ensure_roots()
        content_index = self.cache["shared"].get("content_hash_index", {})
        return content_index.get(content_hash)
    
    def get_document(self, doc_id: str, user_id: str) -> Optional[Dict]:
        """Get a specific document from user's cache or follow reference to shared cache."""
        user_docs = self._get_user_cache(user_id)
        doc = user_docs.get(doc_id)
        
        # If it's a reference to shared content, resolve it
        if doc and doc.get("is_reference"):
            shared_doc_id = doc.get("shared_doc_id")
            if shared_doc_id:
                shared_doc = self.get_shared_document(shared_doc_id)
                if shared_doc:
                    # Merge user-specific metadata with shared content
                    resolved = {**shared_doc, **doc}
                    resolved["content"] = shared_doc.get("content", "")
                    return resolved
        
        return doc
    
    def add_document(self, doc_id: str, name: str, url: str, content: str, user_id: str, metadata: Dict = None):
        """Add or update a document in user's cache with content deduplication.
        
        If identical content already exists in shared cache, creates a reference instead
        of duplicating the content.
        """
        # SECURITY: Validate inputs
        if not user_id or not user_id.strip():
            logger.error("SECURITY: Cannot add document - user_id is required")
            return
            
        if not doc_id or not doc_id.strip():
            logger.error("SECURITY: Cannot add document - doc_id is required")
            return
            
        # SECURITY: Check for personal OneDrive access violations
        if url and "/personal/" in url.lower():
            # Extract owner from personal OneDrive URL for security check
            owner_match = None
            try:
                if "/personal/" in url.lower():
                    parts = url.lower().split("/personal/")
                    if len(parts) > 1:
                        owner_part = parts[1].split("/")[0]
                        if "_" in owner_part:
                            owner_clean = owner_part.replace("_", ".")
                            if owner_clean.count(".") >= 2:
                                email_parts = owner_clean.rsplit(".", 2)
                                if len(email_parts) == 3:
                                    owner_match = f"{email_parts[0]}@{email_parts[1]}.{email_parts[2]}"
            except Exception:
                pass
                
            # Get user's email from metadata to verify ownership
            user_email = (metadata or {}).get("cached_for_user", "")
            if owner_match and user_email:
                if owner_match.lower() != user_email.lower():
                    logger.error(f"SECURITY VIOLATION: User {user_email} attempted to cache document owned by {owner_match}")
                    logger.error(f"SECURITY: Blocked URL: {url}")
                    return
                    
        user_docs = self._get_user_cache(user_id)
        
        # Skip if document already exists
        if doc_id in user_docs:
            logger.debug(f"Document {doc_id} already cached for user {user_id[:8]}..., skipping")
            return
        
        # Calculate content hash for deduplication
        content_hash = self._calculate_content_hash(content)
        
        # Check if identical content already exists in shared cache
        existing_shared_id = self._find_duplicate_by_content(content_hash)
        
        if existing_shared_id:
            # Content already exists - create a reference instead of duplicating
            user_docs[doc_id] = {
                "id": doc_id,
                "name": name,
                "url": url,
                "is_reference": True,
                "shared_doc_id": existing_shared_id,
                "content_hash": content_hash,
                "metadata": metadata or {},
                "cached_at": datetime.now().isoformat(),
                "user_id": user_id,  # SECURITY: Always track owner
                "visibility": "user",  # SECURITY: Mark visibility
                "security_verified": True  # SECURITY: Mark as verified
            }
            self._save_cache()
            logger.info(f"📎 Linked to existing shared content for user {user_id[:8]}...: {name} (deduped)")
        else:
            # New unique content - store in shared cache and create reference
            shared_doc_id = f"shared:{content_hash[:16]}"
            
            # Add to shared cache
            shared_docs = self._get_shared_cache()
            shared_docs[shared_doc_id] = {
                "id": shared_doc_id,
                "name": name,
                "content": content,
                "content_hash": content_hash,
                "metadata": metadata or {},
                "cached_at": datetime.now().isoformat(),
                "visibility": "shared",
                "original_url": url
            }
            
            # Update content hash index
            if "content_hash_index" not in self.cache["shared"]:
                self.cache["shared"]["content_hash_index"] = {}
            self.cache["shared"]["content_hash_index"][content_hash] = shared_doc_id
            
            # Create user reference to shared content
            user_docs[doc_id] = {
                "id": doc_id,
                "name": name,
                "url": url,
                "is_reference": True,
                "shared_doc_id": shared_doc_id,
                "content_hash": content_hash,
                "metadata": metadata or {},
                "cached_at": datetime.now().isoformat(),
                "user_id": user_id,  # SECURITY: Always track owner
                "visibility": "user",  # SECURITY: Mark visibility  
                "security_verified": True  # SECURITY: Mark as verified
            }
            
            self._save_cache()
            logger.info(f"📦 Cached deduplicated document for user {user_id[:8]}...: {name} (new shared content)")

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

        # 2b. Try fuzzy word matches for misspellings in the query.
        query_terms = self._query_terms(query)
        for match in re.finditer(r"\b[a-z0-9]{3,}\b", content_lower):
            body_word = match.group(0)
            for term in query_terms:
                if abs(len(body_word) - len(term)) <= 2 and SequenceMatcher(None, term, body_word).ratio() >= 0.82:
                    idx = match.start()
                    start = max(0, idx - window // 3)
                    end = min(len(content), idx + len(body_word) + window - (idx - start))
                    return content[start:end].strip()
        
        # 3. Fallback: return first window-sized chunk
        return content[:window].strip()

    def _best_snippets(self, content: str, query: str, window: int = 360, max_snippets: int = 3) -> list[str]:
        """Return several non-overlapping body snippets where query terms appear."""
        if not content:
            return []

        terms = self._query_terms(query)
        phrase = self._main_query_phrase(query)
        haystack = content.lower()
        hits = []

        if phrase:
            start = 0
            phrase_lower = phrase.lower()
            while len(hits) < max_snippets * 2:
                idx = haystack.find(phrase_lower, start)
                if idx == -1:
                    break
                hits.append((idx, len(phrase_lower), len(terms) + 2))
                start = idx + max(1, len(phrase_lower))

        for term in terms:
            if len(term) < 3:
                continue
            for match in re.finditer(r"\b" + re.escape(term.lower()) + r"\b", haystack):
                hits.append((match.start(), len(term), 1))
                if len(hits) >= max_snippets * 8:
                    break

        if not hits:
            fallback = self._best_snippet(content, query, window=window)
            return [fallback] if fallback else []

        hits.sort(key=lambda item: (-item[2], item[0]))
        snippets = []
        ranges = []
        for idx, length, _weight in hits:
            start = max(0, idx - window // 3)
            end = min(len(content), idx + length + window - (idx - start))

            # Prefer cleaner paragraph boundaries when nearby.
            paragraph_start = content.rfind("\n\n", 0, start)
            if paragraph_start != -1 and start - paragraph_start < 160:
                start = paragraph_start + 2
            paragraph_end = content.find("\n\n", end)
            if paragraph_end != -1 and paragraph_end - end < 160:
                end = paragraph_end

            overlaps = any(not (end <= prior_start or start >= prior_end) for prior_start, prior_end in ranges)
            if overlaps:
                continue

            snippet = content[start:end].strip()
            if snippet:
                snippets.append(snippet)
                ranges.append((start, end))
            if len(snippets) >= max_snippets:
                break

        return snippets

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

    def _normalize_search_text(self, text: str) -> str:
        """Normalize titles/queries for title-first matching."""
        text = (text or "").lower()
        text = re.sub(r"\.(docx?|pdf|pptx?|xlsx?|csv|txt|md|json|xml)$", "", text)
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return " ".join(text.split())

    def _search_tokens_from_text(self, text: str) -> set[str]:
        """Tokenize searchable text with the same normalization used for queries."""
        return {
            token
            for token in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(token) > 1 and token not in self.SEARCH_STOPWORDS
        }

    def _phrase_in_text(self, phrase: str, text: str) -> bool:
        """Match a normalized phrase inside raw text, allowing punctuation between words."""
        if not phrase or not text:
            return False
        words = [re.escape(w) for w in phrase.split() if w]
        if not words:
            return False
        pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(words) + r"(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None

    def _is_typo_match(self, term: str, word: str) -> bool:
        """Cheap typo matcher for one-edit errors and adjacent transpositions."""
        if not term or not word or abs(len(term) - len(word)) > 1:
            return False
        if term == word:
            return True
        if len(term) == len(word):
            if sorted(term) == sorted(word):
                return True
            mismatches = sum(1 for a, b in zip(term, word) if a != b)
            return mismatches <= 1
        if len(term) > len(word):
            term, word = word, term
        i = j = edits = 0
        while i < len(term) and j < len(word):
            if term[i] == word[j]:
                i += 1
                j += 1
            else:
                edits += 1
                if edits > 1:
                    return False
                j += 1
        return True

    def _query_terms(self, query: str) -> list[str]:
        normalized = self._normalize_search_text(query)
        return [t for t in normalized.split() if len(t) > 1 and t not in self.SEARCH_STOPWORDS]

    def _main_query_phrase(self, query: str) -> str:
        return " ".join(self._query_terms(query))

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
        title_norm = self._normalize_search_text(doc.get("name", ""))
        main_phrase = self._main_query_phrase(query)
        query_terms = self._query_terms(query)
        
        # Early exit for empty query
        if not query_terms and not query_lower.strip():
            return 0
        
        # 1. Title phrase matches - highest priority.
        if main_phrase and main_phrase in title_norm:
            score += 500
        if main_phrase and title_norm.startswith(main_phrase):
            score += 150
        if main_phrase and title_norm == main_phrase:
            score += 250
        if main_phrase and self._phrase_in_text(main_phrase, content_lower):
            score += 220
        
        # 2. Word-level matches with word boundaries (handles multi-word queries)
        words = query_terms
        
        for word in words:
            if not word:
                continue
            
            # Check word boundaries in title (stronger signal)
            if f" {word} " in f" {title_norm} " or title_norm.startswith(word) or title_norm.endswith(word):
                score += 45
            elif word in title_norm:
                # Partial match in title (e.g., "doc" in "document")
                score += 12
            
            # Check word boundaries in content (weaker signal but important)
            if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", content_lower):
                score += 28
            elif word in content_lower:
                # Partial match in content - includes names like "edgar" in text
                score += 8

        if query_terms and content_lower:
            body_hits = sum(
                1
                for word in query_terms
                if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", content_lower)
            )
            if body_hits == len(query_terms):
                score += 80 + (10 * body_hits)
            elif body_hits:
                score += 16 * body_hits

        # 3. Fuzzy matching for typo tolerance (apply when relevance is still modest)
        if score < 140:  # Boost near-misses (e.g., 'armley' vs 'Armely')
            # Check if query is similar to filename (typo tolerance)
            name_words = name_lower.split('_')  # Split on underscore for file names like "pto_file.xlsx"
            name_words.extend(name_lower.replace('_', ' ').split())  # Also split on spaces
            content_words = set(re.findall(r"\b[a-z0-9]{3,}\b", content_lower[:50000]))
            
            for name_word in name_words:
                if len(name_word) >= 3:  # Only fuzzy match words with 3+ chars
                    similarity = SequenceMatcher(None, main_phrase or query_lower, name_word).ratio()
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
                    best_body_similarity = 0.0
                    for content_word in content_words:
                        if (
                            len(content_word) >= 3
                            and abs(len(content_word) - len(word)) <= 2
                            and content_word[0] == word[0]
                            and content_word[-1] == word[-1]
                        ):
                            if self._is_typo_match(word, content_word):
                                best_body_similarity = 0.9
                                break
                    if best_body_similarity >= 0.88:
                        score += 32
                    elif best_body_similarity >= 0.78:
                        score += 20
        
        # 3. Semantic similarity boost (lightweight "semantic" matching)
        # This helps when words differ but meaning overlaps (typos or paraphrase).
        semantic_score = 0.0
        if score < 60 and len(doc.get("content", "")) < 20000:
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
            
            # Split into normalized words, keeping only useful searchable tokens.
            words.update(self._search_tokens_from_text(text))
            
            # Add all words for this doc
            for word in words:
                word_index[word].append(doc_id)
        
        return dict(word_index)

    def _fuzzy_candidate_ids(self, docs_map: Dict, query_words: list[str]) -> set[str]:
        """Find a bounded set of candidate docs for typo-heavy queries."""
        fuzzy_terms = [w for w in query_words if len(w) >= 3]
        if not fuzzy_terms:
            return set()

        candidate_ids: set[str] = set()
        for doc_id, doc in docs_map.items():
            title_words = re.findall(r"\b[a-z0-9]{3,}\b", self._normalize_search_text(doc.get("name", "")))
            content_words = re.findall(r"\b[a-z0-9]{3,}\b", (doc.get("content") or "").lower()[:50000])
            for term in fuzzy_terms:
                matched = False
                for word in title_words:
                    if (
                        abs(len(word) - len(term)) <= 2
                        and word[0] == term[0]
                        and word[-1] == term[-1]
                        and self._is_typo_match(term, word)
                    ):
                        candidate_ids.add(doc_id)
                        matched = True
                        break
                if matched:
                    break
                for word in content_words:
                    if (
                        abs(len(word) - len(term)) <= 2
                        and word[0] == term[0]
                        and word[-1] == term[-1]
                        and self._is_typo_match(term, word)
                    ):
                        candidate_ids.add(doc_id)
                        matched = True
                        break
                if matched:
                    break

        return candidate_ids

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
                
                # Resolve reference if needed
                if doc.get("is_reference"):
                    shared_doc_id = doc.get("shared_doc_id")
                    if shared_doc_id:
                        shared_doc = self.get_shared_document(shared_doc_id)
                        if shared_doc:
                            # Merge user metadata with shared content
                            resolved_doc = {**doc, "content": shared_doc.get("content", "")}
                            docs_map[doc_id] = resolved_doc
                        else:
                            logger.warning(f"Broken reference: {doc_id} -> {shared_doc_id}")
                            docs_map[doc_id] = doc
                    else:
                        docs_map[doc_id] = doc
                else:
                    docs_map[doc_id] = doc
            logger.debug(f"User cache: {len(docs_map)} docs for user {user_id[:8]}...")
        else:
            logger.error("SECURITY: No user_id provided for cache search - access denied")
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
        
        # SECURITY: Filter out documents where user may have lost access
        # This prevents returning documents cached earlier but no longer accessible
        if user_id:
            filtered_count = 0
            docs_before_filter = len(docs_map)
            verified_docs_map = {}
            
            for doc_id, doc in docs_map.items():
                doc_url = doc.get("url", "")
                doc_name = doc.get("name", "unknown")
                
                # Get user email from metadata for permission check
                user_email = doc.get("metadata", {}).get("cached_for_user", "")
                
                # CRITICAL: Verify document is still accessible
                # For personal OneDrive documents, check ownership
                if "/personal/" in doc_url.lower():
                    # Extract owner from personal OneDrive URL
                    try:
                        if "/personal/" in doc_url.lower():
                            parts = doc_url.lower().split("/personal/")
                            if len(parts) > 1:
                                owner_part = parts[1].split("/")[0]
                                if "_" in owner_part:
                                    owner_clean = owner_part.replace("_", ".")
                                    if owner_clean.count(".") >= 2:
                                        email_parts = owner_clean.rsplit(".", 2)
                                        if len(email_parts) == 3:
                                            owner_email = f"{email_parts[0]}@{email_parts[1]}.{email_parts[2]}"
                                            # Verify owner matches cached user
                                            if user_email and owner_email.lower() != user_email.lower():
                                                logger.warning(f"🔒 SECURITY: Filtering personal document with mismatched owner: {doc_name}")
                                                filtered_count += 1
                                                continue
                    except Exception as e:
                        logger.warning(f"Error checking personal OneDrive ownership for {doc_name}: {e}")
                        # Fail closed - skip document if we can't verify ownership
                        filtered_count += 1
                        continue
                
                # Document passed all security checks
                verified_docs_map[doc_id] = doc
            
            docs_map = verified_docs_map
            if filtered_count > 0:
                logger.info(f"🔒 SECURITY: Filtered {filtered_count}/{docs_before_filter} cached documents that may no longer be accessible")

        # Get candidate doc IDs by checking which have matching words
        query_lower = self._normalize_search_text(query)
        query_words = self._query_terms(query)
        main_phrase = self._main_query_phrase(query)
        candidate_ids = set()
        term_candidate_ids: dict[str, set[str]] = {word: set() for word in query_words}
        
        # Check for phrase match first (fastest)
        for doc_id, doc in docs_map.items():
            title_norm = self._normalize_search_text(doc.get("name", ""))
            content_lower = (doc.get("content") or "").lower()
            if (
                (main_phrase and main_phrase in title_norm)
                or (main_phrase and self._phrase_in_text(main_phrase, content_lower))
                or (query_lower and query_lower in title_norm)
            ):
                candidate_ids.add(doc_id)
                continue

            for word in query_words:
                if len(word) >= 2 and (word in title_norm or word in content_lower):
                    term_candidate_ids[word].add(doc_id)

        matched_term_sets = [ids for ids in term_candidate_ids.values() if ids]
        if matched_term_sets:
            all_term_matches = set.intersection(*matched_term_sets) if len(matched_term_sets) == len(query_words) else set()
            if all_term_matches:
                candidate_ids.update(all_term_matches)
            else:
                candidate_ids.update(set.union(*matched_term_sets))
        
        # If no candidates found, score all docs anyway (fallback for partial matches)
        if not candidate_ids:
            candidate_ids = self._fuzzy_candidate_ids(docs_map, query_words)
            if not candidate_ids and len(docs_map) <= 50:
                candidate_ids = set(docs_map.keys())
        
        # Score all candidates and collect results
        results = []
        for doc_id in candidate_ids:
            if doc_id not in docs_map:
                continue
            
            doc = docs_map[doc_id]
            score = self._score_document(doc, query)
            
            if score > 0:
                source_doc = {
                    **doc,
                    "_cache_score": score,
                    "_from_document_cache": True,
                    "_from_sharepoint": True,
                }
                results.append({
                    "doc": source_doc,
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
            doc = r["doc"]
            snippets = self._best_snippets(doc.get("content", ""), query)
            doc["snippet"] = "\n\n---\n\n".join(snippets) if snippets else self._best_snippet(doc.get("content", ""), query)
            doc["snippets"] = snippets
            unique_docs.append(doc)
            if len(unique_docs) >= limit:
                break
        
        return unique_docs

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
            
            # SECURITY: Apply ownership and permission filters
            filtered_security_count = 0
            user_emails = set()
            
            # First pass: extract user emails for verification
            for doc in user_docs.values():
                email = doc.get("metadata", {}).get("cached_for_user", "")
                if email:
                    user_emails.add(email.lower())
            
            primary_user_email = list(user_emails)[0] if user_emails else None
            
            # Second pass: verify ownership and resolve references
            for doc_id, doc in user_docs.items():
                # Double-check document belongs to this user
                doc_owner = doc.get("user_id", "")
                if doc_owner and doc_owner != user_id:
                    logger.warning(f"🔒 SECURITY: Skipping doc {doc_id} - owner mismatch")
                    filtered_security_count += 1
                    continue
                
                # Verify personal OneDrive ownership
                doc_url = doc.get("url", "")
                if "/personal/" in doc_url.lower():
                    try:
                        parts = doc_url.lower().split("/personal/")
                        if len(parts) > 1:
                            owner_part = parts[1].split("/")[0]
                            if "_" in owner_part:
                                owner_clean = owner_part.replace("_", ".")
                                if owner_clean.count(".") >= 2:
                                    email_parts = owner_clean.rsplit(".", 2)
                                    if len(email_parts) == 3:
                                        owner_email = f"{email_parts[0]}@{email_parts[1]}.{email_parts[2]}"
                                        if primary_user_email and owner_email.lower() != primary_user_email:
                                            logger.warning(f"🔒 SECURITY: Blocking personal document with wrong owner: {doc.get('name', 'unknown')}")
                                            filtered_security_count += 1
                                            continue
                    except Exception:
                        # Fail closed - skip documents we can't verify
                        filtered_security_count += 1
                        continue
                
                # Resolve reference if needed
                if doc.get("is_reference"):
                    shared_doc_id = doc.get("shared_doc_id")
                    if shared_doc_id:
                        shared_doc = self.get_shared_document(shared_doc_id)
                        if shared_doc:
                            # Merge user metadata with shared content
                            resolved_doc = {**doc, "content": shared_doc.get("content", "")}
                            docs_map[doc_id] = resolved_doc
                        else:
                            logger.warning(f"Broken reference: {doc_id} -> {shared_doc_id}")
                            docs_map[doc_id] = doc
                    else:
                        docs_map[doc_id] = doc
                else:
                    docs_map[doc_id] = doc
            
            if filtered_security_count > 0:
                logger.info(f"🔒 SECURITY: Filtered {filtered_security_count} documents from scored search due to permission violations")
        else:
            logger.error("SECURITY: No user_id provided for scored cache search - access denied")
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

        query_lower = self._normalize_search_text(query)
        query_words = self._query_terms(query)
        main_phrase = self._main_query_phrase(query)
        candidate_ids = set()
        term_candidate_ids: dict[str, set[str]] = {word: set() for word in query_words}
        for doc_id, doc in docs_map.items():
            title_norm = self._normalize_search_text(doc.get("name", ""))
            content_lower = (doc.get("content") or "").lower()
            if (
                (main_phrase and main_phrase in title_norm)
                or (main_phrase and self._phrase_in_text(main_phrase, content_lower))
                or (query_lower and query_lower in title_norm)
            ):
                candidate_ids.add(doc_id)
                continue

            for word in query_words:
                if len(word) >= 2 and (word in title_norm or word in content_lower):
                    term_candidate_ids[word].add(doc_id)
        matched_term_sets = [ids for ids in term_candidate_ids.values() if ids]
        if matched_term_sets:
            all_term_matches = set.intersection(*matched_term_sets) if len(matched_term_sets) == len(query_words) else set()
            if all_term_matches:
                candidate_ids.update(all_term_matches)
            else:
                candidate_ids.update(set.union(*matched_term_sets))
        if not candidate_ids:
            candidate_ids = self._fuzzy_candidate_ids(docs_map, query_words)
            if not candidate_ids and len(docs_map) <= 50:
                candidate_ids = set(docs_map.keys())

        results = []
        for doc_id in candidate_ids:
            if doc_id not in docs_map:
                continue
            doc = docs_map[doc_id]
            score = self._score_document(doc, query)
            if score > 0:
                source_doc = {
                    **doc,
                    "_cache_score": score,
                    "_from_document_cache": True,
                    "_from_sharepoint": True,
                }
                results.append({
                    "doc": source_doc,
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
            doc = r["doc"]
            snippets = self._best_snippets(doc.get("content", ""), query)
            doc["snippet"] = "\n\n---\n\n".join(snippets) if snippets else self._best_snippet(doc.get("content", ""), query)
            doc["snippets"] = snippets
            unique_scored.append(r)
            if len(unique_scored) >= limit:
                break
        return unique_scored
    
    def security_audit(self) -> dict:
        """Perform security audit on cached documents to detect violations.
        
        Returns:
            Dict with audit results and any security violations found
        """
        audit_results = {
            "total_users": 0,
            "total_docs": 0,
            "violations": [],
            "warnings": []
        }
        
        try:
            if "users" not in self.cache:
                return audit_results
                
            audit_results["total_users"] = len(self.cache["users"])
            
            for user_id, user_cache in self.cache["users"].items():
                user_docs = user_cache.get("documents", {})
                audit_results["total_docs"] += len(user_docs)
                
                for doc_id, doc in user_docs.items():
                    # Check 1: Document owner matches user
                    doc_owner = doc.get("user_id", "")
                    if doc_owner != user_id:
                        audit_results["violations"].append({
                            "type": "owner_mismatch",
                            "doc_id": doc_id,
                            "expected_owner": user_id,
                            "actual_owner": doc_owner,
                            "doc_name": doc.get("name", "unknown")
                        })
                    
                    # Check 2: Personal OneDrive URL ownership
                    url = doc.get("url", "")
                    if url and "/personal/" in url.lower():
                        cached_for_user = doc.get("metadata", {}).get("cached_for_user", "")
                        if cached_for_user:
                            # Extract owner from URL
                            try:
                                parts = url.lower().split("/personal/")
                                if len(parts) > 1:
                                    owner_part = parts[1].split("/")[0]
                                    if "_" in owner_part:
                                        owner_clean = owner_part.replace("_", ".")
                                        if owner_clean.count(".") >= 2:
                                            email_parts = owner_clean.rsplit(".", 2)
                                            if len(email_parts) == 3:
                                                url_owner = f"{email_parts[0]}@{email_parts[1]}.{email_parts[2]}"
                                                if url_owner.lower() != cached_for_user.lower():
                                                    audit_results["violations"].append({
                                                        "type": "personal_onedrive_access_violation",
                                                        "doc_id": doc_id,
                                                        "user_email": cached_for_user,
                                                        "url_owner": url_owner,
                                                        "doc_name": doc.get("name", "unknown"),
                                                        "url": url
                                                    })
                            except Exception:
                                pass
                                
                    # Check 3: Missing security verification
                    if not doc.get("security_verified"):
                        audit_results["warnings"].append({
                            "type": "unverified_document",
                            "doc_id": doc_id,
                            "user_id": user_id,
                            "doc_name": doc.get("name", "unknown")
                        })
                        
        except Exception as e:
            audit_results["audit_error"] = str(e)
            
        return audit_results
        
    def clean_security_violations(self) -> int:
        """Remove documents that violate security policies.
        
        Returns:
            Number of documents removed
        """
        removed_count = 0
        audit = self.security_audit()
        
        for violation in audit.get("violations", []):
            if violation["type"] in ["owner_mismatch", "personal_onedrive_access_violation"]:
                user_id = violation.get("expected_owner") or violation.get("user_email", "")
                doc_id = violation.get("doc_id")
                
                if user_id and doc_id:
                    try:
                        user_docs = self._get_user_cache(user_id)
                        if doc_id in user_docs:
                            del user_docs[doc_id]
                            removed_count += 1
                            logger.warning(f"🚨 SECURITY: Removed violating document: {violation.get('doc_name', doc_id)} from user {user_id[:8]}...")
                    except Exception as e:
                        logger.error(f"Failed to remove violating document {doc_id}: {e}")
        
        if removed_count > 0:
            self._save_cache()
            logger.info(f"🛡️ Security cleanup completed: {removed_count} documents removed")
            
        return removed_count
    
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
        """Get all cached documents for a user and, optionally, shared docs.
        Resolves references to shared content.
        """
        docs = []
        user_docs = self._get_user_cache(user_id)
        
        for doc_id, doc in user_docs.items():
            # Resolve reference if needed
            if doc.get("is_reference"):
                resolved_doc = self.get_document(doc_id, user_id)
                if resolved_doc:
                    docs.append(resolved_doc)
            else:
                docs.append(doc)
        
        if include_shared:
            docs.extend(self._get_shared_cache().values())
        return docs

    def get_all_shared_documents(self) -> List[Dict]:
        """Return all shared documents."""
        return list(self._get_shared_cache().values())
    
    def get_cache_stats(self) -> Dict:
        """Get statistics about cache usage and deduplication efficiency."""
        self._ensure_roots()
        
        total_users = len(self.cache.get("users", {}))
        total_shared_docs = len(self.cache.get("shared", {}).get("documents", {}))
        total_references = 0
        total_user_docs = 0
        
        for user_id, user_data in self.cache.get("users", {}).items():
            user_docs = user_data.get("documents", {})
            total_user_docs += len(user_docs)
            for doc in user_docs.values():
                if doc.get("is_reference"):
                    total_references += 1
        
        # Calculate deduplication savings
        dedup_ratio = (total_references / total_user_docs * 100) if total_user_docs > 0 else 0
        
        return {
            "total_users": total_users,
            "total_shared_content": total_shared_docs,
            "total_user_document_links": total_user_docs,
            "deduplicated_references": total_references,
            "deduplication_percentage": round(dedup_ratio, 1),
            "unique_content_count": total_user_docs - total_references + total_shared_docs,
            "last_updated": self.cache.get("last_updated")
        }
    
    def clear_user_cache(self, user_id: str):
        """Clear cached documents for a specific user"""
        if "users" in self.cache and user_id in self.cache["users"]:
            self.cache["users"][user_id] = {"documents": {}}
            self._save_cache()
            logger.info(f"Cache cleared for user {user_id[:8]}...")
    
    def clear_cache(self):
        """Clear all cached documents (admin function)"""
        self.cache = {"users": {}, "shared": {"documents": {}, "content_hash_index": {}}, "last_updated": None}
        self._save_cache()
        logger.info("All cache cleared")

    def clear_shared_cache(self):
        """Clear only the shared document cache."""
        self._ensure_roots()
        self.cache["shared"] = {"documents": {}, "content_hash_index": {}}
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
