# Search Scope & Result Limits - Implementation Guide

## Problem Summary

**Issue 1: Azure Search returned 50 results but only 4 were used**
- Azure Search semantic returned 50 results, but `top_k` parameter limited results to 3 (default)
- Cache adds 1 result (if exists), totaling 4 results returned
- Only 3 docs made it to LLM input context due to token limits

**Issue 2: User wants to specify search location**
- No way to say "search only in SharePoint" or "search only in OneDrive" or "search only in AI Search"
- System always follows fallback chain: cache → AI Search → Graph

## Solutions Implemented

### 1. **Increased Default Top-K from 3 to 5**
- **File**: [src/app.py](src/app.py#L1562)
- **Change**: `default_top_k = 5` (was 3)
- **Impact**: System returns up to 5 results per source instead of 3, providing more comprehensive results

### 2. **Added Scope-Specific Search Function**
- **File**: [src/knowledge_base.py](src/knowledge_base.py#L162)
- **New Function**: `scoped_search(query, scope, top, user_context, user_id)`
- **Supported Scopes**:
  - `'all'` (default): cache → AI Search → Graph (full three-tier)
  - `'cache'` or `'local'`: Only search local cached documents
  - `'ai-search'`: Only Azure Cognitive Search
  - `'graph'`, `'sharepoint'`, `'onedrive'`: Only Graph API SharePoint/OneDrive

### 3. **Enhanced Router Prompt**
- **File**: [src/app.py](src/app.py#L995)
- **Change**: Router now detects scope preferences in user queries
- **Detection Rules**:
  - "in sharepoint" → scope='sharepoint'
  - "in onedrive" → scope='onedrive'
  - "ai search" or "using ai" → scope='ai-search'
  - "attached file" or "this document" → scope='cache'

### 4. **Updated Search Logic in Handler**
- **File**: [src/app.py](src/app.py#L1610)
- **Change**: Uses `scoped_search()` when user specifies a scope, otherwise uses full `unified_search()`

## How to Use

### Example Queries

**Search Only in SharePoint:**
```
"search for policies in sharepoint"
```
→ Routes directly to Graph API SharePoint search, skips cache & AI Search

**Search Only in OneDrive:**
```
"find documents in onedrive"
```
→ Routes directly to Graph API OneDrive search

**Search Only in AI Search:**
```
"search using ai for employee data"
```
→ Routes directly to Azure Cognitive Search, skips cache & Graph

**Search Only Attached File:**
```
"summarize this document"
(with file attachment)
```
→ Routes to cache (local), skips cloud searches

**Full Search (Default):**
```
"tell me about Christian Wilson"
```
→ Follows fallback chain: cache → AI Search → Graph

## Configuration

### Environment Variables

```env
# Max results to return per query (increased from 3 to 5)
# Affects: top_k parameter in search functions
DEFAULT_TOP_K=5

# Result limits (adjust if needed)
MAX_DOCS=5          # Max docs to include in LLM context
MAX_DOC_CONTEXT_CHARS=3000
MAX_TOTAL_CONTEXT_CHARS=12000
```

## Technical Details

### Search Result Flow

#### Before (top_k=3):
```
Azure Search: 50 results
  ↓ (limited by top_k=3)
AI Search returns: 4 combined (cache=1 + AI=3)
  ↓
LLM Input: 3 docs (some filtered by token budget)
```

#### After (top_k=5):
```
Azure Search: 50 results
  ↓ (limited by top_k=5)
AI Search returns: 5+ combined (cache=N + AI=5)
  ↓
LLM Input: 3-5 docs (more comprehensive before filtering)
```

### Scope-Aware Search Logic

```python
# User specifies scope in query
if scope in ("sharepoint", "onedrive", "ai-search", "cache"):
    results = scoped_search(query, scope=scope, top=5, user_id=user_id)
else:
    # Default: full three-tier fallback
    results = unified_search(query, top=5, user_id=user_id)
```

### Azure Search Result Filtering

The 50 results returned by Azure Search semantic are:
1. **Semantic ranked**: Results scored by semantic relevance
2. **Top-N limited**: Only top `top` results extracted (`top=5`)
3. **Deduplicated**: Compared with cache results to avoid duplicates
4. **Context-limited**: Final LLM input filtered by token budget (MAX_PROMPT_TOKENS_APPROX=3500)

## Logging

Enable detailed logging to see search flow:

```python
# Typical log output with new features:
| Scoped search: query='Christian Wilson' scope='all' top=5
| Cache returned 1 results (top score=10)
| Cache-only search returned 1 results
| Cache results have low relevance (score=10), will combine with AI Search
| Azure Search semantic returned 50 results
| AI Search returned 5 results for 'Christian  Wilson'
| Returning 5 combined results from all sources (cache + AI Search)
| Formatting 5 combined document results
| LLM input sizes: {...docs=5...}
```

## Testing

### Test Case 1: Search with Scope Specification
```
User: "search for policies in sharepoint"
Expected: Only Graph API searches (no cache, no AI Search)
Verify: Logs show "Scoped search: scope='sharepoint'" and "Graph-only search returned X results"
```

### Test Case 2: Default Full Search
```
User: "tell me about Christian Wilson"
Expected: cache → AI Search → Graph fallback chain
Verify: Logs show all three sources attempted
```

### Test Case 3: More Results Returned
```
User: "search for employee records"
Expected: 5 results instead of 3
Verify: Logs show "Returning 5 combined results" and LLM receives more docs
```

## Troubleshooting

**Q: Why are only 3 docs in LLM input if top_k=5?**
A: Token budget limits—MAX_PROMPT_TOKENS_APPROX=3500 may exclude lower-ranked docs after context is built.

**Q: User says "search in SharePoint" but still gets OneDrive results?**
A: Router may not detect scope if phrasing is ambiguous. Check logs for "Scoped search:" message.

**Q: Only 4 results returned instead of 5?**
A: Deduplication—if cache doc and AI result have same file_path, only 1 is kept.

## Files Modified

- [src/app.py](src/app.py#L1562) - Increased `default_top_k`, updated router prompt, integrated `scoped_search()`
- [src/knowledge_base.py](src/knowledge_base.py#L162) - Added `scoped_search()` function

