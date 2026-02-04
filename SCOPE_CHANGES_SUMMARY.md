# Quick Summary: Search Scope & Result Limits

## What Changed

### 1. **More Results Returned (top_k: 3 → 5)**
   - System now returns up to 5 results instead of 3
   - Provides more comprehensive search results
   - File: `src/app.py` line 1562

### 2. **Scope-Specific Search**
   - Users can now specify where to search:
     - `"search in sharepoint"` → Only SharePoint
     - `"search in onedrive"` → Only OneDrive  
     - `"ai search for..."` → Only Azure Cognitive Search
     - `"summarize [file]"` → Only local cached file
   - Files: `src/knowledge_base.py` (new `scoped_search()` function), `src/app.py` (router prompt update)

### 3. **Why Azure Search Returned 50 but Only 4 Used**
   - **Azure Search**: Returns ranked semantic results (up to 1000)
   - **top_k parameter**: Limits to top 5 results
   - **Cache + dedup**: Adds cache result (if exists) = 4-5 total
   - **Token budget**: LLM input limited by MAX_PROMPT_TOKENS_APPROX=3500 → final docs = 3

## Files Modified

| File | Changes | Lines |
|------|---------|-------|
| `src/app.py` | Enhanced router prompt + increased default top_k + integrated scoped_search | 995-1015, 1562, 1610-1625 |
| `src/knowledge_base.py` | Added `scoped_search()` function | 162-226 |

## Testing

```python
# Test 1: More results
"search for employee data"
# Logs should show: "Returning 5 combined results from all sources"

# Test 2: Scope-specific search
"find policies in sharepoint"
# Logs should show: "Scoped search: scope='sharepoint'" + "Graph-only search returned X results"

# Test 3: Attached file only
"summarize [PDF attachment]"
# Logs should show: "Cache-only search returned X results"
```

## Key Benefits

✅ More comprehensive results (5 instead of 3)  
✅ User control over search location  
✅ Faster searches when scope is specified (skips unnecessary tiers)  
✅ Same fallback chain if no scope specified  
✅ Backward compatible (existing queries unaffected)

---

**Next Step**: Run bot with updated code and test with attachment + scope queries.
