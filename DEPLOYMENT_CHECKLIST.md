# ✅ Deployment Checklist - AI Search Integration

## Pre-Deployment Review

### Code Changes
- [x] `src/knowledge_base.py` - Added search functions
- [x] `src/simple_file_handler.py` - Added indexing + search
- [x] Performance optimizations applied
- [x] Syntax errors: **NONE** ✓
- [x] Import statements updated ✓

### Documentation Created
- [x] QUICK_START.md (5-minute setup)
- [x] AI_SEARCH_IMPLEMENTATION.md (full guide)
- [x] SEARCH_CONFIG.md (detailed setup)
- [x] ENV_SETUP.md (credentials guide)
- [x] AI_SEARCH_SUMMARY.md (technical summary)
- [x] IMPLEMENTATION_SUMMARY.md (what changed)
- [x] This checklist

---

## Phase 1: Azure Setup (15 minutes)

### Create Search Service
- [ ] Open Azure Portal: https://portal.azure.com
- [ ] Click **Create a resource**
- [ ] Search for **"Cognitive Search"**
- [ ] Click **Create**
- [ ] Fill in:
  - Name: `my-search` (or your name)
  - Region: Same as bot's region
  - SKU: **Free** (for dev) or **Basic** (for prod)
- [ ] Click **Review + create** → **Create**
- [ ] Wait for deployment (2-3 minutes)

### Get Credentials
- [ ] In Azure Portal, go to your search service
- [ ] Copy **URL** from Overview → This is your **ENDPOINT**
  - Example: `https://my-search.search.windows.net`
- [ ] Click **Keys** on left menu
- [ ] Copy **Primary admin key** → This is your **KEY**
  - Should be exactly 64 characters

### Create Index
- [ ] Open terminal/PowerShell
- [ ] Run curl command from [SEARCH_CONFIG.md](SEARCH_CONFIG.md) or [ENV_SETUP.md](ENV_SETUP.md)
- [ ] Replace `SERVICE_NAME` and `ADMIN_KEY` with your values
- [ ] Verify response: HTTP 201 or 200 (success)

---

## Phase 2: Bot Configuration (10 minutes)

### Update Environment Variables
- [ ] Open `.env.dev` file
- [ ] Add these lines:
  ```bash
  AZURE_SEARCH_ENDPOINT=https://your-service.search.windows.net
  AZURE_SEARCH_KEY=your-64-char-admin-key
  AZURE_SEARCH_INDEX=documents
  ```
- [ ] Verify values are copied exactly (no spaces, no typos)
- [ ] Verify file is saved
- [ ] Check that Graph credentials are still valid:
  - [ ] `GRAPH_CLIENT_ID` not empty
  - [ ] `GRAPH_CLIENT_SECRET` not empty
  - [ ] `GRAPH_TENANT_ID` not empty

### Restart Bot
- [ ] Stop running bot (Ctrl+C)
- [ ] Wait 2-3 seconds
- [ ] Start bot: `python src/app.py`
- [ ] Check logs for no errors
- [ ] Confirm bot started successfully

---

## Phase 3: Testing (15 minutes)

### Test 1: File Upload & Indexing
- [ ] Open Teams
- [ ] Start 1:1 chat with bot
- [ ] Upload a test file (PDF or Word doc)
- [ ] Watch logs for: `✓ Indexed document in AI search`
- [ ] Check Azure Portal:
  - Search service → Indexes → documents → Check **Document count** increased
- [ ] If count increased: **PASS** ✓
- [ ] If count didn't increase: Check logs for errors

### Test 2: Document Search
- [ ] In Teams chat, ask: `"Search for [keyword from your document]"`
- [ ] Bot should return matching documents
- [ ] Check results include:
  - [ ] File name shown
  - [ ] Content snippet shown
  - [ ] Matching text highlighted (or marked)
- [ ] If results appear: **PASS** ✓
- [ ] If no results: Try keyword from actual file content

### Test 3: Multiple Documents
- [ ] Upload 2-3 more files (different types: PDF, Word, Excel)
- [ ] Check logs for indexing messages
- [ ] Search for different keywords
- [ ] Verify results include all uploaded documents
- [ ] If all searchable: **PASS** ✓

### Test 4: Fallback (Optional)
- [ ] Temporarily remove `AZURE_SEARCH_KEY` from `.env.dev`
- [ ] Restart bot
- [ ] Try searching
- [ ] Bot should fall back to Graph API
- [ ] Results should still work (slower)
- [ ] If fallback works: **PASS** ✓
- [ ] Restore `AZURE_SEARCH_KEY` afterward

---

## Phase 4: Validation

### Code Review
- [ ] All new functions present:
  - [x] `search_documents()`
  - [x] `index_document()`
  - [x] `search_knowledge_base()`
  - [x] `_index_document_to_search()`
  - [x] `search_graph_documents()`
- [ ] No syntax errors in bot code
- [ ] Bot starts without errors
- [ ] No error on import statements

### Environment Variables
- [ ] `AZURE_SEARCH_ENDPOINT` = URL (ends with `.search.windows.net`)
- [ ] `AZURE_SEARCH_KEY` = Exactly 64 characters
- [ ] `AZURE_SEARCH_INDEX` = `documents` (exact match)
- [ ] `GRAPH_CLIENT_ID` = UUID format or non-empty
- [ ] `GRAPH_CLIENT_SECRET` = Non-empty
- [ ] `GRAPH_TENANT_ID` = UUID format or non-empty

### Azure Search
- [ ] Service exists and is **Running** state
- [ ] Index named **documents** exists
- [ ] Index has correct schema (6 fields)
- [ ] Index starts with **0 documents** (before upload)

### Logging
- [ ] Upload file → see `✓ Indexed document in AI search`
- [ ] Search query → see `🔍 Searching documents` or fallback message
- [ ] Graph fallback → see appropriate fallback messages
- [ ] No error logs related to search

---

## Phase 5: Performance Verification

### Speed Test
- [ ] First search: Should complete in 2-3 seconds
- [ ] Second search (same query): Should be <1 second
- [ ] Document extraction: Should be 2-5 seconds
- [ ] Indexing: Should be silent, 1-2 seconds

### Scalability Test
- [ ] Upload 10 documents
- [ ] Index document count should be 10
- [ ] Search should return relevant results
- [ ] No timeout or performance issues

---

## Phase 6: Documentation Review

- [ ] [QUICK_START.md](QUICK_START.md) is clear and follows workflow
- [ ] [ENV_SETUP.md](ENV_SETUP.md) has correct credential locations
- [ ] [SEARCH_CONFIG.md](SEARCH_CONFIG.md) has troubleshooting guide
- [ ] All files reference each other appropriately

---

## Phase 7: Production Readiness

### Security
- [ ] API keys stored in `.env.dev` (not in code)
- [ ] Admin key used for indexing only
- [ ] Query key would be used for production clients (not implemented here)
- [ ] Graph credentials haven't changed
- [ ] No secrets in logs or error messages

### Availability
- [ ] Azure Search service has SLA
- [ ] Graph API fallback is working
- [ ] Both services can be accessed from bot location
- [ ] Network connectivity verified

### Monitoring
- [ ] Logs show clear messages (✓, ❌, 🔍, ⚠️)
- [ ] Error messages are user-friendly
- [ ] Document count trackable in Azure Portal
- [ ] Search latency measurable

---

## GO/NO-GO Decision

### Go if all of these are true:
- [ ] All code changes deployed without errors
- [ ] All environment variables configured correctly
- [ ] File upload test passed (indexing verified)
- [ ] Document search test passed (results received)
- [ ] No critical errors in logs
- [ ] Performance is acceptable (<3 sec for first search)
- [ ] Azure Search service is running and healthy

### No-Go if any of these are true:
- [ ] Code has syntax errors
- [ ] Environment variables missing or wrong
- [ ] File upload doesn't trigger indexing
- [ ] Search returns no results when documents exist
- [ ] Azure Search service is not responding
- [ ] Search times out (>10 seconds)
- [ ] Critical errors in logs

---

## Rollback Plan (if needed)

### If Search Service Fails
1. Comment out `AZURE_SEARCH_KEY` in `.env.dev`
2. Restart bot
3. Bot falls back to Graph API
4. No user-facing changes

### If Indexing Causes Issues
1. Remove `_index_document_to_search()` call from `process_attachment()`
2. Indexing stops but file extraction still works
3. Users can still upload files, but not searchable

### If Whole Feature Fails
1. Restore previous `.env.dev`
2. Restart bot
3. All functionality reverts to pre-search state

---

## Success Criteria

✅ **Feature is ready for production when:**

1. File upload works and documents are indexed
2. Search returns results from Azure Cognitive Search
3. Fallback to Graph API works if search unavailable
4. No critical errors in logs
5. Response times are acceptable (<3 seconds first search)
6. Documentation is clear and complete

✅ **Feature is complete when:**

1. All tests pass
2. Documentation is reviewed and approved
3. Team is trained on new search functionality
4. Monitoring and alerting is configured
5. Rollback procedure is documented and tested

---

## Sign-Off

- [ ] Deployment: **[Date]** **[By]**
- [ ] Testing: **[Date]** **[By]**
- [ ] Approval: **[Date]** **[By]**

---

## Support Contacts

**Questions about setup:** See [SEARCH_CONFIG.md](SEARCH_CONFIG.md) or [ENV_SETUP.md](ENV_SETUP.md)  
**Technical issues:** Check [AI_SEARCH_IMPLEMENTATION.md](AI_SEARCH_IMPLEMENTATION.md) troubleshooting  
**Monitoring:** Azure Portal → Cognitive Search → Monitoring

---

## Post-Deployment

Once deployed, monitor:
- [ ] Document indexing happening (check logs daily)
- [ ] Search queries completing successfully
- [ ] No spike in error rates
- [ ] Azure Search quota usage
- [ ] User feedback on search accuracy

---

**Last Updated:** January 14, 2026  
**Status:** Ready for Deployment ✅
