# Teams SharePoint AI Assistant - Project Overview and Requirements

## Purpose

This project is a Microsoft Teams AI assistant that helps users find, understand, summarize, and work with organizational documents. The assistant is optimized for SharePoint as the primary knowledge source and uses cached SharePoint document content for fast responses, with live Microsoft Graph search/listing as a fallback when needed.

The assistant is designed for workplace use inside Microsoft Teams. It answers from trusted organizational sources, cites source documents, handles document follow-up questions, and avoids using unrelated sources when a prior document or prior answer already provides enough context.

## What The Project Does

- Runs as a Microsoft Teams bot using the Microsoft Teams AI SDK.
- Uses Azure OpenAI for routing, summarization, document Q&A, and response generation.
- Searches configured SharePoint libraries and local SharePoint document cache.
- Caches SharePoint document text locally for faster repeated access.
- Supports common file types such as Word, PDF, PowerPoint, Excel, CSV, text, JSON, and XML.
- Handles uploaded files in the current Teams conversation.
- Remembers recent conversation context and previously used source documents.
- Supports follow-up questions without re-searching when prior context is enough.
- Lists available cached SharePoint document titles with pagination.
- Summarizes previously listed document titles with URLs.
- Uses citations for organizational facts and source-grounded answers.

## Primary Data Source Strategy

SharePoint is the main data source.

The intended production mode is:

- `DATA_SOURCE_MODE=sharepoint`
- `ENABLE_SHAREPOINT_SEARCH=true`
- `ENABLE_SHAREPOINT_CACHE=true`
- `SHAREPOINT_CACHE_FIRST=true`
- `ENABLE_ONEDRIVE_SEARCH=false`
- `ENABLE_AI_SEARCH=false`
- `ENABLE_WEB_SEARCH=false`

OneDrive, Azure AI Search, and web search can be re-enabled later through environment variables, but they are disabled by default to keep answers focused, faster, and easier to validate.

## Core User Workflows

### Document Search And Q&A

Users can ask questions such as:

- "What is the vacation policy?"
- "Summarize the employee handbook."
- "What does this implementation guide cover?"

Expected behavior:

- Search cached SharePoint documents first.
- Use live SharePoint Graph search only when cache results are missing or weak.
- Rank exact title matches above weak body matches.
- Hydrate cached documents before prompt building.
- Cite the source document used in the answer.

### Document Summary

For summary requests, the assistant should pass enough content from the matched document into the LLM prompt.

Expected behavior:

- Detect summary/overview requests.
- Prefer full cached document content over snippets.
- Allow larger context windows for exact or strong title matches.
- Avoid unrelated documents when a strong title match exists.
- If content is truncated, state that the summary is based on the included portion.

### Follow-Up Questions

Users can ask follow-ups such as:

- "What should I add to the document?"
- "Tell me more about it."
- "I meant the employee handbook."
- "Give me the URL for each."

Expected behavior:

- Reuse previous source documents and conversation history.
- Do not run a new SharePoint search unless the user explicitly asks for a new search.
- Preserve previous document metadata such as title, URL, included content size, and truncation status.

### Document Title Listing

Users can ask:

- "List top 10 document titles."
- "Show available documents."
- "Show more."

Expected behavior:

- List cached SharePoint document titles directly from metadata.
- Do not ask the LLM to infer titles from snippets.
- Paginate large libraries per conversation.
- Preserve the last shown batch so follow-ups can summarize those exact titles.

## Architecture

### Main Components

- `src/app.py`: Teams bot entry point, routing, conversation handling, prompt building, title listing, and response flow.
- `src/smart_router.py`: LLM-assisted routing with deterministic safety gates for small talk, follow-ups, attachments, and SharePoint search.
- `src/document_cache.py`: Local SharePoint document cache, scoring, ranking, hydration, shared/user cache handling, and cache statistics.
- `src/knowledge_base.py`: Microsoft Graph and SharePoint integration, live search/listing, document download, and text extraction.
- `src/context_budget.py` and `src/utils/context_budget.py`: Context compression and token budget enforcement.
- `src/instructions.txt`: Assistant behavior, citation, formatting, and source-use rules.
- `env/.env.local`, `env/.env.dev`, and related env files: Runtime configuration.

### High-Level Request Flow

1. Teams sends a message activity to the bot.
2. The bot extracts user text, attachments, identity, and conversation context.
3. Routing decides whether to answer directly, use previous context, process attachments, list titles, or search SharePoint.
4. If searching, cache is checked first and live Graph fallback is used only when needed.
5. Retrieved documents are hydrated from cache where possible.
6. Prompt context is built with token limits and citation rules.
7. Azure OpenAI generates the answer.
8. The bot streams or sends the response back into Teams.

## Functional Requirements

- The assistant must answer Teams messages reliably in one-on-one and group/chat contexts.
- The assistant must use SharePoint as the primary organizational source.
- The assistant must cache SharePoint document content for fast access.
- The assistant must rank exact and strong title matches above weak content matches.
- The assistant must detect and preserve follow-up context.
- The assistant must not search again for clear follow-up questions.
- The assistant must provide source citations for organizational facts.
- The assistant must support document listing and pagination.
- The assistant must summarize listed documents with URLs when requested.
- The assistant must handle uploaded documents and cached attachments.
- The assistant must enforce prompt and content size limits to avoid token overflow.
- The assistant must avoid inventing organizational facts when no source content supports them.

## Non-Functional Requirements

- Responses should be fast, especially for cached SharePoint content.
- Typing indicators should appear quickly and continue during long operations.
- Search and document extraction should run off the event loop where possible.
- The bot should degrade gracefully when Graph, cache, or LLM calls fail.
- Logs should show routing decisions, selected sources, content sizes, truncation, and cache behavior.
- The code should keep optional data sources disabled unless explicitly enabled.
- Secrets must not be logged or included in documentation.

## Required Configuration

The following environment variables are required or strongly recommended:

- `BOT_ID`: Microsoft bot application/client ID.
- `SECRET_BOT_PASSWORD` or `BOT_PASSWORD`: Bot client secret.
- `TENANT_ID` or `TEAMS_APP_TENANT_ID`: Microsoft Entra tenant ID.
- `AZURE_OPENAI_API_KEY`: Azure OpenAI key.
- `AZURE_OPENAI_ENDPOINT`: Azure OpenAI endpoint.
- `AZURE_OPENAI_MODEL_DEPLOYMENT_NAME`: Azure OpenAI deployment name.
- `GRAPH_CLIENT_ID`: App registration used for Microsoft Graph.
- `GRAPH_CLIENT_SECRET`: Graph app secret.
- `GRAPH_TENANT_ID`: Tenant used for Graph auth.
- `SHAREPOINT_SITES`: Comma-separated SharePoint site URLs.
- `DATA_SOURCE_MODE`: Recommended value is `sharepoint`.
- `ENABLE_SHAREPOINT_SEARCH`: Recommended value is `true`.
- `ENABLE_SHAREPOINT_CACHE`: Recommended value is `true`.
- `SHAREPOINT_CACHE_FIRST`: Recommended value is `true`.

Optional source toggles:

- `ENABLE_ONEDRIVE_SEARCH`
- `ENABLE_AI_SEARCH`
- `ENABLE_WEB_SEARCH`

Recommended defaults keep these optional sources set to `false`.

## Microsoft Graph Requirements

The Graph app registration must be allowed to access configured SharePoint content. Depending on the chosen security model, this usually requires Microsoft Graph application permissions such as:

- `Sites.Read.All` or a more restrictive Sites.Selected setup.
- `Files.Read.All` if file access requires it.
- Admin consent for application permissions.

For least privilege, prefer restricting the app to the required SharePoint sites rather than tenant-wide file access.

## Security Requirements

- Do not expose secrets in logs, responses, or documentation.
- Keep user-specific cache partitions isolated.
- Treat shared SharePoint cache entries as organizational only when they have SharePoint metadata or shared visibility.
- Do not use personal OneDrive documents from another user.
- Do not cite sources that were not used in the answer.
- Do not invent organizational facts.
- Keep optional data sources disabled unless explicitly required.

## Performance Requirements

- Cached SharePoint results should be returned before live Graph fallback.
- Follow-up questions should avoid search and reuse prior context.
- Large documents should be capped and compressed before prompt submission.
- Exact document-summary matches should allow larger content windows than normal Q&A.
- Prompt building should log included character counts and truncation status.

## Current Implementation Notes

- The assistant stores previous source documents in memory per conversation.
- Follow-up routing uses previous source names and recent history to avoid unnecessary search.
- Document title listing now uses cache metadata directly and supports pagination.
- Listed-title summary follow-ups use the last shown title batch and cached document text.
- The bot currently favors SharePoint and cached document retrieval over OneDrive, AI Search, or web search.

## Deployment Notes

The project can run locally through the Teams Toolkit/dev tunnel flow or be deployed to Azure App Service. Production deployment should configure environment variables through Azure Application Settings rather than checked-in env files.

Before deployment:

- Verify bot registration credentials.
- Verify Azure OpenAI deployment and quota.
- Verify Graph app permissions and admin consent.
- Verify `SHAREPOINT_SITES` points to the intended libraries/sites.
- Confirm optional sources are disabled unless required.
- Compile-check Python files.
- Test Teams message handling, SharePoint search, document summary, title listing, pagination, and follow-ups.

## Acceptance Criteria

- "Summarize employee handbook" selects `employee handbook.docx` when available.
- Strong title matches exclude unrelated documents.
- Follow-up questions about a previous document do not start a new search.
- "List top 10 document titles" returns ten cached SharePoint titles when available.
- "Show more" returns the next page of titles.
- "Give me a short summary in each and URL to each" summarizes the last listed batch.
- Organizational facts include citations.
- Truncated summaries disclose that only included content was used.
- Python compile checks pass for changed files.
