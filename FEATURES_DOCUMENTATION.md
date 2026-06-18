# Teams SharePoint AI Assistant - Features Documentation

## Overview

The Teams SharePoint AI Assistant is a Microsoft Teams bot that helps users search, summarize, review, and work with organizational SharePoint documents. It is designed to be SharePoint-first, fast with cached content, source-grounded, and aware of conversation follow-ups.

## Core Features

### 1. Microsoft Teams Chat Assistant

- Runs inside Microsoft Teams as a conversational bot.
- Supports one-on-one and group/channel conversations.
- Sends typing indicators quickly while processing requests.
- Provides concise workplace-style responses.

### 2. SharePoint-First Knowledge Retrieval

- Uses configured SharePoint sites as the main organizational knowledge source.
- Searches cached SharePoint documents first for faster answers.
- Uses live Microsoft Graph SharePoint search/listing as fallback when needed.
- Keeps OneDrive, Azure AI Search, and web search disabled by default unless enabled through environment variables.

### 3. Local SharePoint Document Cache

- Stores extracted document text and metadata locally.
- Speeds up repeated document questions and summaries.
- Supports shared organizational documents.
- Preserves document title, URL, content, source metadata, and visibility.
- Allows title listing and document hydration from cache.

### 4. Document Search And Ranking

- Prioritizes exact title matches.
- Boosts strong title matches above weak body-text matches.
- Ignores helper words such as "can", "you", "please", "summarize", "document", and "file" during relevance scoring.
- Avoids unrelated documents when a strong title match exists.

### 5. Full Document Summary Mode

- Detects summary and overview requests.
- Uses larger bounded context windows for exact or strong title matches.
- Prefers cached full document content over snippets.
- Adds truncation notes when only part of a document is included.
- Avoids claiming no additional content exists when a document was truncated.

### 6. Follow-Up Question Handling

- Tracks previous source documents and recent conversation history.
- Routes clear follow-ups to previous context instead of starting a new search.
- Handles follow-ups such as:
  - "What should I add to the document?"
  - "Tell me more about it."
  - "I meant the employee handbook."
  - "Give me the URL for each."
- Reuses previous document content and metadata where possible.

### 7. Document Review And Recommendations

- Supports document-improvement questions.
- Can suggest additions, gaps, and improvements based on included document content.
- Cites the source document when describing what it already contains.
- Labels recommendations clearly as recommendations rather than source facts.

### 8. Document Title Listing

- Lists available cached SharePoint document titles directly from metadata.
- Does not rely on the LLM to infer titles from content snippets.
- Supports requests such as:
  - "List top 10 document titles."
  - "Show available documents."
  - "Show more."
- Supports pagination for large libraries.

### 9. Listed Document Summary With URLs

- Remembers the last shown title batch.
- Summarizes those exact listed titles when the user asks for short summaries and URLs.
- Uses cached document content to create compact summaries.
- Includes available document URLs.

### 10. Uploaded File Support

- Processes uploaded Teams attachments.
- Supports common document and data file formats.
- Stores uploaded file content for follow-up questions.
- Prioritizes uploaded files over external search when the user asks about an uploaded file.

### 11. Source Citations

- Requires citations for organizational facts.
- Uses inline source references for retrieved SharePoint documents.
- Avoids citing documents that were not actually used.
- Avoids inventing organizational facts when source content is unavailable.

### 12. Context And Token Budget Controls

- Compresses and caps document content before sending it to the LLM.
- Applies per-document and total-context limits.
- Uses larger limits for document summaries where appropriate.
- Applies a final token gatekeeper before model calls.

### 13. Optional Data Sources

The assistant supports optional data sources, but they are disabled by default for focused SharePoint-first behavior:

- OneDrive search
- Azure AI Search
- External web search/cache

These can be re-enabled later through environment variables.

### 14. Logging And Diagnostics

Logs include useful operational events such as:

- Routing decisions
- Follow-up detection
- Search scope
- Selected primary document
- Content passed to the LLM
- Truncation status
- Cache search results
- Graph/listing failures

## Feature Configuration Summary

| Feature | Main Setting |
| --- | --- |
| SharePoint search | `ENABLE_SHAREPOINT_SEARCH=true` |
| SharePoint cache | `ENABLE_SHAREPOINT_CACHE=true` |
| Cache-first behavior | `SHAREPOINT_CACHE_FIRST=true` |
| SharePoint-only mode | `DATA_SOURCE_MODE=sharepoint` |
| OneDrive search | `ENABLE_ONEDRIVE_SEARCH=false` by default |
| Azure AI Search | `ENABLE_AI_SEARCH=false` by default |
| Web search | `ENABLE_WEB_SEARCH=false` by default |
| Startup SharePoint crawl | `ENABLE_SHAREPOINT_STARTUP_CRAWL=true` |
| Summary content window | `SUMMARY_PRIMARY_DOC_CHARS`, `SUMMARY_DOC_SNIPPET_CHARS`, `SUMMARY_TOTAL_CONTEXT_CHARS` |
| Prompt safety limits | `MAX_PROMPT_CHARS`, `MAX_DOCS`, `MAX_DOC_SNIPPET_CHARS`, `MAX_TOTAL_CONTEXT_CHARS` |

## Packaging Note

This features documentation is part of the project handover/deployment documentation package.

It should not be inserted into the Microsoft Teams app package zip. The Teams app package should contain the Teams manifest and required icon assets only. Documentation should be distributed alongside the deployment package, stored in the project repository, or uploaded to the customer handover SharePoint location.

## Acceptance Criteria For Features

- Exact document-title questions return the intended document first.
- Summary requests include enough document content to be useful.
- Follow-up questions do not trigger unnecessary new searches.
- Title listing returns real cached SharePoint titles.
- Title listing pagination works with "show more".
- Listed-title summary requests summarize the last listed batch.
- Organizational facts are cited.
- Optional data sources remain disabled unless explicitly enabled.
