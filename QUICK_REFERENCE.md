# Quick Reference: Security & Query Handling

## 📋 One-Page Summary

### Document Security: 4-Layer Verification ✅

When documents are cached after an LLM response:

```
1️⃣ Was document shown to user?
   └─ Check: Is it in combined_doc_results?
   └─ No? → Don't cache

2️⃣ Can user access it?
   └─ Check: is_url_accessible_by_user(url, email)
   └─ No? → Don't cache

3️⃣ Does user have identity for personal docs?
   └─ Check: Is it in /personal/ path?
   └─ No email? → Don't cache

4️⃣ Is it from fresh Graph search?
   └─ Check: _from_graph flag
   └─ From cache already? → Don't re-cache
```

**Result:** Only verified, user-accessible documents enter cache.

---

### Query Routing: LLM-Based (95% Search Mandate) ✅

```
User Query
    ↓
Is it bot self-knowledge?
    ├─ Yes → respond_direct (no search)
    └─ No → Send to LLM Router
              ↓
        LLM Decides:
        ├─ Greeting/small talk? → respond_direct
        ├─ Format/reformat request? → refine_previous
        ├─ Everything else (~95%)? → search_documents ← SEARCH!
        └─ Unclear? → search_documents (default)
```

**Key:** LLM sees instruction `"NEVER HALLUCINATE - ALWAYS SEARCH FIRST"`

---

### Hallucination Prevention ✅

| Risk | Prevention | Result |
|------|-----------|--------|
| LLM guesses when uncertain | Router mandates search (~95%) | Ask documents first |
| Math is wrong | Use pandas computation | Actual values, not estimates |
| Names invented | verify_names_in_data() | Only real names used |
| Bad docs in cache | 4-layer security | Only good docs cached |
| Data truncated loses info | Full content for calc, truncated for chat | Accurate calculations |

---

### Complex Query Handling ✅

**Multi-File Calculation:**
```
User: "Grand total across all files?"
  ↓
Pattern match: "grand total across files" ✓
  ↓
Load 3 CSVs → 3 pandas DataFrames
  ↓
df1.sum() + df2.sum() + df3.sum() = $2,847,392.45
  ↓
Verify names in data (no hallucination)
  ↓
Response: Exact value
```

**Follow-Up on Cached Attachments:**
```
Turn 1: Upload sales.csv → cached to disk
Turn 2: No attachment, ask "filter by Q4?"
  ↓
System: Load cached sales.csv (full content!)
  ↓
Answer from complete data (not truncated memory)
```

**Multi-File Aggregation:**
```
User uploads 3 files
  ↓
System: aggregate_tabular_files()
  ↓
Cross-file comparison inserted
  ↓
LLM sees aggregated analysis
```

---

## 🔐 Security Guarantees

| Guarantee | Mechanism | Location |
|-----------|-----------|----------|
| **Only verified docs cached** | 4-layer security checks | app.py:4230-4295 |
| **User isolation** | user_id in cache metadata | app.py:4388-4413 |
| **No permission leaks** | is_url_accessible_by_user() | knowledge_base.py |
| **Personal docs protected** | Email required check | app.py:4270-4274 |
| **Calculations accurate** | Python execution + verification | data_calculator.py |
| **Names from data** | verify_names_in_data() | data_calculator.py |
| **No stale data** | Fresh search each query | app.py:3200+ |
| **Filtered docs never cached** | graph_docs_to_cache only has safe items | app.py:4290 |

---

## 🎯 Router Rules

### `search_documents` (~95% of queries)

**USE WHEN:**
- "Tell me about X" (any X subject)
- "Who is X" / "What is X"
- "Find X" / "Search for X"
- ANY organizational question (staff, services, departments)
- Document analysis, comparison, summary
- Research requests
- ANY question with searchable nouns/names/topics
- Data, statistics, reports, information requests
- ANY question where answer isn't immediately obvious

### `respond_direct` (rare)

**USE ONLY FOR:**
- Pure greetings: "hello", "hi", "good morning"
- Farewells: "bye", "thanks"
- Basic math: "2+2", "15% of 200" (with no context)
- Bot self-knowledge: "what can you do", "how do you work"

### `refine_previous` (rare)

**USE ONLY FOR:**
- Reformatting: "make shorter", "bullet points", "summarize"
- No new information needed

### `clarify` (NEVER USED)

- If unclear → extract terms and search instead

---

## 📊 Data Verification Functions

| Function | Purpose | Returns |
|----------|---------|---------|
| `verify_names_in_data(names, df)` | Check if names exist in dataset | (valid_names, invalid_names) |
| `extract_unique_categorical_values(df)` | List actual values in each column | dict[col: [values]] |
| `detect_calculation_intent(text)` | Pattern match for math queries | (is_calc, calc_type) |
| `process_calculation_request(...)` | Execute pandas math operation | computed_value or error |
| `process_multi_file_calculation(...)` | Aggregate across files | combined_result |

---

## 🔄 Request Lifecycle

```
1. User message → 2. Fast bot-knowledge check
                  ↓ (if no match)
              3. LLM router decision
                  ↓
              4a. respond_direct → Send response
              4b. search_documents:
                  ├─ Enhance query (expand "my X")
                  ├─ Search all sources (cache+SharePoint+OneDrive+AI+Web)
                  ├─ Check: Calculation intent?
                  │  ├─ YES → pandas compute + verify
                  │  └─ NO → LLM response with citations
                  ├─ Filter & verify 4-layer security
                  └─ Cache safe documents
                  ↓
              5. Send response + memory saved
                  ↓
              6. Follow-up handled with cached attachments
```

---

## 🚨 Error Handling

| Scenario | Handling | Result |
|----------|----------|--------|
| Memory error on file | Catch, suggest splitting | User knows why |
| Calculation fails | Note missing columns explicitly | Not guessed |
| Permission denied | Don't cache, log access attempt | Audit trail |
| Timeout on search | Continue safely without data | User not blocked |
| LLM router parse fails | Fallback to respond_direct | Safe default |
| Group chat 405 error | Return troubleshooting guide | User self-help |

---

## 📈 Key Metrics

- **Search mandate:** ~95% of queries trigger search
- **Cache verification layers:** 4 independent checks
- **Parallel search concurrency:** Max 8 concurrent (rate limit respectful)
- **CSV chunking:** Preserve security metadata per chunk
- **Query deduplication:** Prevents duplicate searches
- **Security audit frequency:** Every 10 cache operations
- **User isolation:** user_id on every cached document
- **Fallback behavior:** Always favor search over guessing

---

## 💡 Common Scenarios

### Scenario: User searches for "my performance review"

```
Router Decision: search_documents ✓
Query Enhancement: "my performance review" 
                → "John Smith performance review"
Scope: graph (all sources)
Result: Finds personal document, expands identity
```

### Scenario: User uploads budget.xlsx and asks "what's the total?"

```
Calculation Detected: ✓ (pattern match)
Path: Data Calculator NOT LLM
Process: df.sum() for numeric columns
Verify: All column names real ✓
Result: Exact value "$847,392.14" NOT "approximately"
```

### Scenario: Multiple files uploaded, "compare across them"

```
Files: sales_q1.csv, sales_q2.csv, sales_q3.csv
Aggregation: Auto-triggered ✓
Result: Comparative analysis prepended
Cache: All 3 stored (follow-up questions work)
```

### Scenario: Follow-up question two turns later

```
Turn 1: Upload data.csv → Answer question → Cached
Turn 2: No new file, ask "break down by region?"
System: Check if cached attachments exist
        ↓ YES → Load full data.csv
        ↓ Answer from complete cached content
Result: Works without re-uploading
```

---

## ✅ Security Checklist

- [ ] Documents filtered before reaching cache ✓
- [ ] 4-layer verification implemented ✓
- [ ] User isolation in cache metadata ✓
- [ ] Personal documents protected ✓
- [ ] Calculations verified against data ✓
- [ ] Names checked before using ✓
- [ ] Router favors search (~95%) ✓
- [ ] Hallucination risks addressed ✓
- [ ] Error handling fail-safe ✓
- [ ] Audit trail for security events ✓

---

## 🔗 Related Documents

- Main analysis: [SECURITY_AND_QUERY_HANDLING_ANALYSIS.md](SECURITY_AND_QUERY_HANDLING_ANALYSIS.md)
- Code locations documented with line numbers
- Function references with file paths
- Visual flow diagrams showing complete paths

---

## ⚡ At a Glance

**Document Security:** ✅ Verified → Cached
**Query Routing:** ✅ LLM decides (95% search)
**Hallucination:** ✅ Search first or compute
**Calculations:** ✅ Pandas, not estimates
**Names:** ✅ From data, not invented
**Errors:** ✅ Fail-safe, user-friendly

