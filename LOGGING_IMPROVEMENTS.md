# Clean, Organized Logging System

## Overview
Complete logging refactor to provide clean, categorized, and easy-to-read output with proper visual organization and removal of irrelevant noise.

## New SearchLogger Class

Added centralized logging utility with consistent formatting:

```python
class SearchLogger:
    """Centralized logging with clean categorization"""
    
    # Section headers
    SearchLogger.section(title)           # Major section divider
    
    # Query operations
    SearchLogger.query_received()         # Incoming query
    
    # Search source results
    SearchLogger.search_source()          # Results from each source
    
    # Document listings
    SearchLogger.document()               # Individual document details
    
    # Filtering & errors
    SearchLogger.filtered()               # Silently removed items (DEBUG only)
    SearchLogger.error()                  # Error messages
    SearchLogger.warning()                # Warning messages
    SearchLogger.summary()                # Summary statistics
```

---

## Before vs After: Log Output Examples

### ❌ BEFORE (Messy, Verbose, Hard to Parse)

```
DEBUG: unified_search called with user_upn='john@company.com', user_id='user123'
🔵 Starting OneDrive-specific search for 'salary report' (user: john@company.com)
OneDrive search query: '(salary report) AND (driveType:personal)'
❌ Skipping inaccessible document: taxes_2023.pdf (owner: jane_smith) URL: https://...
❌ Skipping personal OneDrive document due to missing user_upn: other_doc.xlsx...
❌ File type filtered: document.zip (not in supported extensions)
OneDrive: Filtered out unsupported file: archive.rar
OneDrive: Filtered out non-personal URL: sharepoint_file.docx
OneDrive: Filtered out inaccessible document: restricted.pdf
✅ OneDrive search returned 3 documents for 'salary report'
Searching SharePoint with delegated (user-context) token
Search query expanded: 'salary report' -> '(salary report) OR (salarireport) OR (salary.docx) OR ...'
Graph search returned 8 results (delegated)
Populated Graph result with cached content: report_2024.pdf (truncated to 4096 chars)
Filtered out low-relevance result: unrelated_doc.docx (score=0.23)
Keeping 2 low-relevance Graph result(s) as fallback
✅ Graph search complete: 5 results after filtering (out of 12 raw)
  🔴 Document type filtered: 5 unsupported files
  🔒 Permission pre-filtered: 2 inaccessible documents
  📊 Relevance filtered: 0 low-relevance documents
🔎 Performing AI Search (Azure Cognitive Search) for comprehensive knowledge base coverage
✓ AI Search returned 2 results
🌐 Performing web search for comprehensive knowledge base coverage
⚠️ Web search returned no results
📊 SUMMARY: Unified
```

### ✅ AFTER (Clean, Organized, Relevant Only)

```
============================================================
📋 DOCUMENT SEARCH: 'salary report'
============================================================

  ✅ Cache: 0 results | Query: 'salary report'
  ✅ OneDrive: 3 result(s) | Query: 'salary report'
    📄 salary_report_2024.pdf | Type: pdf | Score: 0.95
    📄 compensation_review.docx | Type: docx | Score: 0.88
    📄 budget_report.xlsx | Type: xlsx | Score: 0.72
    ... and 0 more
  ✅ Graph: 5 result(s) | Query: 'salary report'
    📄 Annual_Salary_Survey.pdf | Type: pdf | Score: 0.92
    📄 Payroll_Report_Q4.pdf | Type: pdf | Score: 0.85
    📄 Compensation_Analysis.docx | Type: docx | Score: 0.78
    📄 Financial_Summary.xlsx | Type: xlsx | Score: 0.65
    ... and 1 more
  ✅ AI Search: 2 result(s) | Query: 'salary report'
  ⚪ Web Search: 0 results | Query: 'salary report'

✓ Returning 10 results from 10 unique sources
```

---

## Key Improvements

### 1. **Removed Verbose Filtering Logs**

**Before:**
```
❌ Skipping inaccessible document: taxes_2023.pdf (owner: jane_smith) URL: https://...
OneDrive: Filtered out non-personal URL: sharepoint_file.docx
OneDrive: Filtered out unsupported file: archive.rar
Filtered out low-relevance result: unrelated_doc.docx (score=0.23)
```

**After:**
```
[SILENT - only technical debugging logs when DEBUG level]
```

**Benefit:** Cleaner logs showing only what matters - the results

---

### 2. **Organized Section Headers**

**Before:**
```
🔵 Starting OneDrive-specific search for 'salary report' (user: john@company.com)
Searching SharePoint with delegated (user-context) token
🔎 Performing AI Search (Azure Cognitive Search) for comprehensive knowledge base coverage
```

**After:**
```
============================================================
📋 DOCUMENT SEARCH: 'salary report'
============================================================
```

**Benefit:** Instantly clear what's happening with visual boundary

---

### 3. **Consistent Result Formatting**

**Before:**
```
✅ OneDrive search returned 3 documents for 'salary report'
✓ Live Graph search returned 8 results (delegated)
✓ AI Search returned 2 results
⚠️ Web search returned no results
```

**After:**
```
  ✅ OneDrive: 3 result(s) | Query: 'salary report'
  ✅ Graph: 5 result(s) | Query: 'salary report'
  ✅ AI Search: 2 result(s) | Query: 'salary report'
  ⚪ Web Search: 0 results | Query: 'salary report'
```

**Benefit:** Uniform format, easy to scan and compare

---

### 4. **Document Detail Display**

**Before:**
```
Populated Graph result with cached content: report_2024.pdf (truncated to 4096 chars)
```

**After:**
```
    📄 report_2024.pdf | Type: pdf | Score: 0.92
    📄 compensation_review.docx | Type: docx | Score: 0.88
    📄 budget_summary.xlsx | Type: xlsx | Score: 0.75
    📄 salary_survey.txt | Type: txt | Score: 0.68
    ... and 2 more
```

**Benefit:** Quick visual scan of what files were found, their type, and relevance

---

### 5. **Silent Error Handling**

**Before:**
```
Token acquisition failed, retrying (1/2)...
Request timeout (attempt 1/3). Retrying in 2.0s: Connection timed out
File type filtering error: some_error
```

**After:**
```
[Errors only shown when critical]
❌ ERROR: Could not acquire Graph token - cannot search
❌ ERROR: Graph search timed out
⚠️  OneDrive search failed (HTTP 503)
```

**Benefit:** Only relevant errors shown; transient retries are silent

---

## Logging Categories

### 📊 Information Levels

```
INFO Level (Default - most useful):
  • Section headers (DOCUMENT SEARCH: ...)
  • Search results from each source
  • Document listings
  • Summaries
  • Important warnings/errors

DEBUG Level (Detail - filtered operations):
  • Individual filtering decisions
  • Cache lookups
  • Request details
  • Technical diagnostics
```

### 🎯 Search Source Symbols

```
✅ - Source had results
⚪ - Source had no results (normal)
⚠️  - Warning (transient error, retrying)
❌ - Error (critical failure)
📄 - Document found
→  - List continuation
```

---

## Output Examples by Operation

### Query Reception
```
🔍 QUERY: 'my cv' | User: john@company.com
```

### Source Results
```
  ✅ OneDrive: 2 result(s) | Query: 'my cv'
    📄 My_CV_2024.pdf | Type: pdf | Score: 0.99
    📄 Resume_John_Smith.docx | Type: docx | Score: 0.97
  ✅ Graph: 1 result(s) | Query: 'my cv'
    📄 CV_Current.pdf | Type: pdf | Score: 0.95
```

### Summary
```
✓ Returning 3 results from 2 unique sources
========================================================== (line break)
```

### Errors (Only When Needed)
```
⚠️  OneDrive search failed, continuing with general search
❌ ERROR: Could not acquire Graph token - cannot search
```

---

## Configuration

No additional configuration needed. Works immediately with:

```python
import logging

# Get standard logger
logger = logging.getLogger(__name__)

# Set to INFO for clean output (default)
# Set to DEBUG for detailed filtering logs
logging.basicConfig(level=logging.INFO)
```

---

## Removed Verbosity

The following noisy logs have been removed:

| Old Log | Reason | Result |
|---------|--------|--------|
| `❌ Skipping personal OneDrive document due to missing user_upn...` | Irrelevant internal detail | Silent filtering |
| `❌ Skipping inaccessible document: xyz (owner: ...)` | Too verbose for normal operation | Silent filtering |
| `Query pipeline: '...' → enhanced → '...'` | Unnecessary detail | Removed |
| `Token acquisition error: ...` | Transient issue, auto-retried | Silent |
| `Request timeout (attempt 1/3). Retrying...` | Noisy retry logic | Silent |
| `Populated Graph result with cached content: ... (truncated to 4096 chars)` | Too technical | Only shown on success |

---

## Testing the New Logging

### Run a Search and See Clean Output

```python
from knowledge_base import unified_search

results = unified_search(
    query="salary report",
    user_upn="john@company.com",
    user_id="user123",
    user_assertion="token_here"
)
```

**Clean Output You'll See:**
```
============================================================
📋 DOCUMENT SEARCH: 'salary report'
============================================================

  ✅ OneDrive: 3 result(s) | Query: 'salary report'
    📄 Compensation_Report.pdf | Type: pdf | Score: 0.95
    📄 Salary_Survey_2024.xlsx | Type: xlsx | Score: 0.88
    📄 Budget_Review.docx | Type: docx | Score: 0.72

  ✅ Graph: 5 result(s) | Query: 'salary report'
    📄 Annual_Report.pdf | Type: pdf | Score: 0.92
     ... and 4 more

✓ Returning 8 results from 2 unique sources
```

---

## SearchLogger API Reference

```python
# Section Management
SearchLogger.section("TITLE")              # Print major divider with title

# Query Logging
SearchLogger.query_received("query")       # Log incoming query request
SearchLogger.query_received("query", "user@company.com")  # With user info

# Search Results
SearchLogger.search_source("OneDrive", 5, "salary report")  # Results from source
SearchLogger.search_source("Graph", 0)     # No results variant

# Individual Documents
SearchLogger.document("Report.pdf")        # Simple document
SearchLogger.document("Report.pdf", doc_type="pdf")  # With type
SearchLogger.document("Report.pdf", doc_type="pdf", score=0.95)  # With score

# Filtering (DEBUG level only)
SearchLogger.filtered("unsupported file type", "archive.zip")

# Errors & Warnings
SearchLogger.error("Something failed")     # Error message
SearchLogger.warning("Something might be wrong")  # Warning

# Summary
SearchLogger.summary("Search Results", {
    "Total Results": 15,
    "OneDrive": 5,
    "SharePoint": 8,
    "Web": 2
})
```

---

## Migration Notes

- **100% Backward Compatible**: All search functionality unchanged
- **Drop-in Replacement**: No code changes needed in app.py
- **Immediate Use**: Works with existing code
- **No Configuration**: Uses standard Python logging

---

## Future Enhancements

- JSON structured logging for parsing
- Log level configuration via environment variables
- Performance metrics logging
- Search analytics/trending
- User interaction analytics
