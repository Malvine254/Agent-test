# Deep-Dive Audit — Armely / Mela Teams SharePoint AI Assistant

**Date:** 2026-06-16
**Scope:** Architecture, correctness, dead code, memory model, security/data-access, and readiness for deployment to an **external client**.
**Method:** Static read of `src/` (16k LOC), live tests through the running bot, live Microsoft Graph + Azure AI Search queries.

> TL;DR — The retrieval engine (Azure AI Search over SharePoint) works and produces grounded, cited answers. But the product has **three blocking issues** before any external-client deployment: **(1) no per-user access control — it overshares**, **(2) a router that defaults to *not* searching**, and **(3) live secrets in the working tree + a single monolith with substantial dead/duplicate code.** None are hard to fix; they need to be done deliberately.

---

## 1. Architecture (as actually wired today)

Two systems share one process:

### A. Background indexer (SharePoint → Azure AI Search)
`startup()` → `search/ai_search_worker.py` runs on boot and every `SHAREPOINT_INDEX_POLL_SECONDS` (900s).
Per pass (`search/ai_search_ingestion.py`): enumerate **every document library** in each `SHAREPOINT_SITES` site via **app-only Graph** (`sharepoint/graph_client.py`) → download → extract text (`sharepoint/sharepoint_reader.py`, supports pdf/docx/txt/csv/xlsx/pptx) → checksum (incremental) → chunk (~6k chars, 800 overlap) → embed (`text-embedding-3-small`, 1536-d) → upsert chunks into the `sharepoint-documents` index (`search/ai_search_index.py`).
**Status:** Working. Index currently holds **2,285 chunks**.

### B. Chat request flow (per Teams message)
`@app.on_message → handle_message → handle_stateful_conversation` (the ~3,200-line orchestrator in `app.py`):
1. Extract text / attachments / identity.
2. **Route** (two keyword-gate layers: `app.py` fast-router + `smart_router.py`).
3. Retrieve: Azure AI Search hybrid (vector + semantic), with keyword and live-Graph fallbacks (`search/ai_search_retriever.py`); uploaded-file path is separate.
4. Build prompt under token budgets (`utils/context_budget.py`), grounding guard (`grounding_guard.py`).
5. Azure OpenAI `gpt-4.1` → stream answer + citations back to Teams.

**Identity model:** **app-only** client-credential tokens only (no delegated/per-user sign-in). This single fact drives most of the security findings below.

---

## 2. What's working
- ✅ SharePoint→AI Search indexing (incremental, checksum-based).
- ✅ Hybrid retrieval with graceful fallback chain; returns real docs for content queries.
- ✅ Grounded answers with inline SharePoint citations (verified live).
- ✅ Uploaded-file Q&A path (separate from the index).
- ✅ Streaming, typing indicators, rate-limit awareness.
- ✅ Local testing path (Agents Playground, no-auth launcher).

## 3. What's NOT working / bugs

| # | Issue | Evidence | Impact |
|---|---|---|---|
| B1 | **Router defaults to *not* searching.** Org questions only search if they hit a hard-coded keyword list (`document/file/policy/report…`). "Summarize the IDSR **guideline**", "Daily Brief AI **spec**", any doc named by title/acronym → routed `respond_direct` → empty "no matching source content". | `app.py:2725` `_is_org_or_document_request`; `smart_router.py:339` `org_or_doc_markers`; live logs | **High** — most natural user phrasings fail. Channel-independent (Teams identical). |
| B2 | **"List documents / what do you have" hits a dead path.** Title-listing reads the legacy `document_cache` (empty in current design), not the AI Search index. Always answers "no documents". | `app.py` title-list branch; live test | High — discovery questions never work. |
| B3 | **Indexer crawls junk.** Office lock files (`~$*.docx`), `.msixbundle`, `.zip` get attempted; cause "File is not a zip file"/timeouts. | indexing logs | Low/med — noise, wasted Graph calls, occasional stalls. |
| B4 | **Log mojibake.** Emoji/Unicode render as `â€¦`/`ðŸ”’` (Windows cp1252 stream). | all logs | Low — cosmetic, but hurts debuggability. |
| B5 | **Conversation memory lost on restart & not shared across instances** (in-process dict). | `app.py:1882` | Med — see §4; breaks horizontal scaling and loses context on redeploy. |

---

## 4. Memory model

### Short-term (per conversation)
- **`conversation_store: dict[conversation_id → ListMemory]`** (`app.py:1882`) — the live working memory. **In-process only**: lost on restart, **not shared across scaled instances**. Trimmed to `MAX_MEMORY_TURNS` turns — **currently `1`** in `.env.local`/`.env` (i.e. ~2 messages), which is very short and undermines follow-ups.
- **Conversation summary** (`update_conversation_summary`, capped ~1,800 chars) — also in-process.
- **`last_sources`** retained for follow-up reuse — in-process.

### Long-term (persisted to disk)
- **`user_profiles_cache.json`** — name/email per AAD object id.
- **Attachment cache** (`attachment_cache.py`, `src/cache/`) — uploaded file content, **partitioned by `user_id`**, with TTL cleanup.
- **`document_cache.json`** — legacy SharePoint cache, largely unused now.
- **Azure AI Search index** — the real durable knowledge store (external, persistent).

### Gaps / recommendations
- No **durable** conversation memory (restart/scale loses it) → move to shared store (Azure Table/Cosmos/Redis) keyed by conversation id, or the SDK's storage abstraction.
- `MAX_MEMORY_TURNS=1` is too aggressive → raise to ~4–6.
- No **per-user long-term memory** (preferences, prior topics across sessions) — fine for v1, but a candidate for the "make it intelligent" goal.
- **`memory_store.py` is dead** (see §6) — there's a cleaner `MemoryStore` with TTL that nobody imports; either adopt it (and back it with durable storage) or delete it.

---

## 5. Security & data-access — **the critical section**

### 5.1 🔴 No per-user access control → the bot overshares
- The indexer reads documents with an **app-only Graph token** (`GRAPH_CLIENT_ID`), i.e. with the *application's* tenant-wide permissions — **not the asking user's**.
- The index schema **has `acl_users`/`acl_groups` fields, but they are written empty**: `"acl_users": []`, `"acl_groups": []` (`search/ai_search_ingestion.py:95-96`).
- The retriever applies **no security filter** — `filter_expr` is always `""` (`search/ai_search_retriever.py`).
- The only access check, `is_url_accessible_by_user` (`knowledge_base.py:1328`), **returns `True` for every `/sites/` URL** (line 1380) and only blocks *other users'* personal OneDrive. It also runs only on the legacy Graph/cache caching path, **not** on the AI Search answer path.

**Net effect:** **Any user who can message the bot can read the content of ANY indexed document** from the configured libraries, regardless of their actual SharePoint permissions. A user with zero access to "Surveillance documents" still gets its contents (and citations) in chat. **This is a hard blocker for an external client** and likely violates the customer's data-governance expectations.

**Fix options (in order of fidelity):**
1. **Security trimming at query time** — capture each document's SharePoint permissions during indexing into `acl_users`/`acl_groups`, resolve the asking user's id + group memberships (delegated token or Graph lookup), and add an OData `filter` on every search. This is the correct model.
2. **Delegated (on-behalf-of) Graph search** instead of app-only, so Graph itself trims to the user's permissions (loses the pre-indexed speed; heavier auth).
3. **Interim guardrail** — restrict the bot to libraries explicitly designated "everyone in tenant can read", and state that limitation contractually. Cheapest, least safe.

### 5.2 Secrets & tenancy
- `.env` files are correctly **gitignored**, but **live secrets sit in the working tree** (`CLIENT_SECRET`, `GRAPH_CLIENT_SECRET`, `AZURE_OPENAI_API_KEY`, `AZURE_SEARCH_ADMIN_KEY`). They have also been surfaced in this working session. **Rotate all of them before external deployment** and source them from **Azure Key Vault / App Service settings**, never files.
- The search client uses the **admin key** (read/write) for query operations; queries should use a **query-only key** (`AZURE_SEARCH_QUERY_KEY` exists but the retriever path should be verified to use it).
- Bot is **MultiTenant** — confirm the external client's tenant boundary and whether the bot should be single-tenant for them.

### 5.3 Other
- `ALLOW_CACHE_USER_INFERENCE=false` (good — don't infer identity from cached docs).
- Group chats: memory is keyed by `conversation_id` (shared among participants) — acceptable, but combined with 5.1 means group oversharing too.
- No PII handling / audit logging of who-asked-what-and-got-which-doc — likely required for an external client.

---

## 6. Dead code & duplication

The repo has a **monolith + several abandoned refactors running in parallel**. `app.py` does NOT import the `teams/`, `routing/`, `retrieval/`, `prompts/`, or `cache/` packages.

**Confirmed dead (0 live imports) — safe to remove after a quick verify:**
- `memory_store.py`
- `context_budget.py` (root) — live code uses `utils/context_budget.py`
- `truncation.py` (root) — live code uses `utils/truncation.py`
- `prompt_builder.py` (root) and `prompts/prompt_builder.py`
- `routing/` package (`router.py` wrapper, `route_models.py`) — note: **root `router.py` IS live** (used by `smart_router.py`); only the `routing/` *package* is dead
- `retrieval/` package (`ai_search_retriever.py`, `upload_retriever.py`, and the `retrieval_service.py` re-export)
- `cache/attachment_cache.py` — live code uses root `attachment_cache.py`
- `teams/` package (`handlers.py`, `responses.py`, `typing.py`)

**Partially live / legacy (audit before trimming):**
- `knowledge_base.py` (3,860 LOC) — mostly legacy cache+Graph search, but still provides `unified_search`, `is_url_accessible_by_user`, `get_graph_token*`, and attachment-download helpers. Untangle carefully.
- `document_cache.py` — only referenced by `knowledge_base.py`; legacy.
- `retrieval_service.py` + `grounding_guard.py` — a **cleaner abstraction that's only partially wired** (grounding guard is called; the `RetrievalResult` path is not the hot path). Decide: adopt it as the single retrieval API, or remove.

**Maintainability:** `app.py` at **5,759 lines** is the central risk — routing, memory, prompt-building, retrieval orchestration, caching, and security checks are all interleaved. This is why bugs B1/B2 are hard to reason about.

---

## 7. External-client deployment checklist
- [ ] **P0** Implement per-user security trimming (§5.1) — or contractually scope to all-tenant-readable libraries.
- [ ] **P0** Rotate all secrets; move to Key Vault / App Settings; use query-only search key.
- [ ] **P0** Fix routing default-to-search (B1) and listing path (B2).
- [ ] **P1** Durable, shared conversation memory (B5); raise `MAX_MEMORY_TURNS`.
- [ ] **P1** Skip junk files in indexer (B3); add audit logging.
- [ ] **P1** Confirm bot endpoint/registration + tenancy for the client tenant.
- [ ] **P2** Remove dead code; split `app.py`; fix log encoding (B4).
- [ ] **P2** Add automated tests around routing + retrieval + security trimming.

---

## 8. Proposed implementation order (safe, incremental)
Each step is independently verifiable in the Playground before moving on:

1. **Routing fix (B1+B2)** — invert the default so substantive, non-small-talk questions search AI Search; route listing questions to an index-backed enumeration. *Lowest risk, highest immediate value; unblocks testing.*
2. **Security trimming (5.1)** — the big one. Populate ACLs at index time + filter at query time. Build behind a feature flag, test with two identities.
3. **Memory durability + turns (4/B5).**
4. **Secret hygiene + config for client tenant (5.2).**
5. **Dead-code removal + `app.py` decomposition (6).** Last, once behavior is locked by tests.
6. **Indexer hygiene + logging + encoding (B3/B4).**

> Recommendation: do **#1 first** (small, safe, immediately makes the bot usable), then tackle **#2** as its own focused effort since it's the true gate for an external client.
