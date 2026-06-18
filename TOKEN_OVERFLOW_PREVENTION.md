# Token Overflow Prevention Implementation Summary

## Objective
Prevent token overflow by limiting content injected into LLM prompts to ensure stable, cost-effective operations.

## Implementation Date
February 20, 2026

## Changes Implemented

### 1️⃣ Global Token/Context Limits (config.py)
**Location:** [src/config.py](src/config.py)

Added new constants to control LLM context/token budget:
```python
# === LLM Context/Snippet/Attachment Limits (Token Overflow Protection) ===
MAX_LLM_CONTEXT_CHARS = 12000
MAX_DOC_SNIPPET_CHARS = 1500
MAX_ATTACH_SNIPPET_CHARS = 2000
MAX_TOTAL_CONTEXT_CHARS = 15000
```

### 2️⃣ Safe Truncation Utility
**Location:** [src/utils/truncation.py](src/utils/truncation.py) (NEW FILE)

Created reusable truncation function:
```python
def safe_truncate(text: str, limit: int) -> str:
    """Safely truncate text to a character limit, ending at a word boundary if possible."""
```

Features:
- Truncates to character limit
- Attempts to end at word boundaries to avoid cutting words
- Returns empty string for None/empty input
- Adds "..." suffix to indicate truncation

### 3️⃣ Enforce Snippet Policy in unified_search()
**Location:** [src/knowledge_base.py](src/knowledge_base.py)

Updated cache content population to use safe truncation:
```python
from src.utils.truncation import safe_truncate
from src.config import MAX_DOC_SNIPPET_CHARS

doc["content"] = safe_truncate(
    cached_doc.get("content", ""),
    MAX_DOC_SNIPPET_CHARS
)
```

### 4️⃣ Limit Graph/Search Result Content
**Location:** [src/knowledge_base.py](src/knowledge_base.py)

Applied truncation to all search result sources before appending to `all_results`:
- Cache results (from cache_buffer)
- AI Search results
- Web search results
- Graph API results (already handled in #3)

All results now truncated to `MAX_DOC_SNIPPET_CHARS` (1500 chars) before inclusion in prompt.

### 5️⃣ Protect Attachment Content for LLM
**Location:** [src/attachment_cache.py](src/attachment_cache.py)

#### Updated Storage Limits:
```python
MAX_CONTENT_SIZE_CHARS = 5_000_000  # storage ok (reduced from 50M)
MAX_LLM_EXPOSURE_CHARS = 2000       # chat safety (NEW)
```

#### Updated get_content_for_llm_conversation():
Applied `safe_truncate()` to all content paths:
- Summary content
- Preview content
- Relevant chunks
- Full content fallback

All chat mode responses now capped at `MAX_LLM_EXPOSURE_CHARS` (2000 chars).

**Calculation mode** still returns full content when needed.

### 6️⃣ Final Context Budget Enforcement
**Location:** [src/utils/context_budget.py](src/utils/context_budget.py) (NEW FILE)

Created budget enforcement utility:
```python
def enforce_context_budget(context_blocks: list[str], max_chars: int = None) -> str:
    """Enforce context budget by combining blocks until limit is reached."""
```

**Integration in app.py:**
Added safety check in `build_llm_input()`:
```python
if len(prompt) > MAX_FINAL_CONTEXT:
    logger.warning(f"Prompt exceeds MAX_TOTAL_CONTEXT_CHARS...")
    prompt_parts = prompt.split("\n\n")
    prompt = enforce_context_budget(prompt_parts, max_chars=MAX_FINAL_CONTEXT)
```

### 7️⃣ Reduce Parallel Search Explosion
**Location:** [src/app.py](src/app.py) - `perform_parallel_searches()`

Added hard limits to prevent excessive concurrent searches:
```python
# Apply limits to prevent token overflow
top_k = min(top_k, 3)
max_concurrent = min(max_concurrent, 4)
```

**Effect:**
- Maximum 3 results per query (was unlimited)
- Maximum 4 concurrent searches (was 8)
- Reduces total context volume from parallel searches

### 8️⃣ Log Context Size for Diagnostics
**Location:** [src/app.py](src/app.py) - `build_llm_input()`

Added logging after prompt construction:
```python
logger.info(f"Context size (chars): {len(prompt)}, estimated tokens: {log_info['estimated_tokens']}")
```

Helps diagnose:
- Token overflow issues
- Truncation effectiveness
- Budget adherence

## Expected Results

✅ **Stable prompts** - No more token overflow crashes  
✅ **No token overflow** - All content properly truncated before LLM call  
✅ **Faster responses** - Less content = faster processing  
✅ **Lower Azure OpenAI cost** - Fewer tokens = lower API costs  
✅ **Better user experience** - Consistent, reliable responses  
✅ **Diagnostic visibility** - Logging helps troubleshoot issues  

## Configuration

All limits can be adjusted via environment variables or by modifying [src/config.py](src/config.py):

| Constant | Default | Purpose |
|----------|---------|---------|
| `MAX_LLM_CONTEXT_CHARS` | 12000 | Max chars for entire LLM context |
| `MAX_DOC_SNIPPET_CHARS` | 1500 | Max chars per document snippet |
| `MAX_ATTACH_SNIPPET_CHARS` | 2000 | Max chars for attachment content |
| `MAX_TOTAL_CONTEXT_CHARS` | 15000 | Final budget cap |

## Testing Recommendations

1. **Test with large attachments** - Verify truncation works
2. **Test with many search results** - Verify parallel search limits
3. **Monitor logs** - Check "Context size (chars)" messages
4. **Check response quality** - Ensure truncation doesn't harm answers
5. **Monitor Azure costs** - Should see reduction in token usage

## Files Modified

- [src/config.py](src/config.py) - Added constants
- [src/utils/truncation.py](src/utils/truncation.py) - NEW FILE
- [src/utils/context_budget.py](src/utils/context_budget.py) - NEW FILE
- [src/knowledge_base.py](src/knowledge_base.py) - Applied truncation to search results
- [src/attachment_cache.py](src/attachment_cache.py) - Updated limits and truncation
- [src/app.py](src/app.py) - Added logging, parallel search limits, budget enforcement

## Rollback Instructions

If issues occur:
1. Increase limits in [src/config.py](src/config.py)
2. Set `MAX_DOC_SNIPPET_CHARS` to 30000 (original)
3. Set `MAX_TOTAL_CONTEXT_CHARS` to 100000 (original)
4. Remove `top_k = min(top_k, 3)` line from `perform_parallel_searches()`

## Next Steps

1. Deploy to test environment
2. Monitor logs for "Context size (chars)" entries
3. Verify no token overflow errors occur
4. Measure response time improvements
5. Track Azure OpenAI cost reduction
6. Fine-tune limits based on actual usage patterns
