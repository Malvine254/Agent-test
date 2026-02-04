# Azure Cognitive Search Configuration

## Overview

The bot now uses **Azure Cognitive Search** as the primary method for document knowledge. When users upload files, they are automatically indexed and become searchable through AI-powered search queries.

**Search Priority:**
1. **Azure Cognitive Search** (PRIMARY - Fast, indexed documents)
2. **Microsoft Graph API** (FALLBACK - Real-time, if search unavailable)
3. **Graph shares API** (FILE DOWNLOAD - For channel/group files)

---

## Environment Variables Required

### Azure Cognitive Search (PRIMARY)

```bash
# Azure Cognitive Search endpoint
AZURE_SEARCH_ENDPOINT=https://<service-name>.search.windows.net

# Azure Cognitive Search admin/query key
AZURE_SEARCH_KEY=<admin-or-query-key>

# Index name (default: "documents")
AZURE_SEARCH_INDEX=swope-vector-documents
```

**How to get these:**

1. **Create Azure Search Service** (if not exists):
   ```bash
   az search service create \
     --name <service-name> \
     --resource-group <resource-group> \
     --sku standard
   ```

2. **Get Endpoint:**
   ```bash
   az search service show \
     --name <service-name> \
     --resource-group <resource-group> \
     --query "endpoint"
   ```

3. **Get API Key:**
   ```bash
   az search admin-key show \
     --service-name <service-name> \
     --resource-group <resource-group> \
     --query "primaryKey"
   ```

4. **Create Index:**
   See [Create Search Index](#create-search-index) section

### Microsoft Graph API (FALLBACK)

```bash
# Graph API credentials (app-only authentication)
GRAPH_CLIENT_ID=<client-id>
GRAPH_CLIENT_SECRET=<client-secret>
GRAPH_TENANT_ID=<tenant-id>
```

These are typically already configured for your bot.

### Azure Managed Identity (FALLBACK)

If using Managed Identity instead of Client Secret:

```bash
APP_ID=<user-assigned-identity-client-id>
APP_TYPE=UserAssignedMsi
```

---

## Create Search Index

### Option 1: Azure Portal

1. Go to Azure Cognitive Search service
2. Click **Indexes** → **Create new index**
3. Name: `documents`
4. Add fields:

| Field Name | Type | Retrievable | Searchable | Filterable |
|-----------|------|-------------|-----------|-----------|
| id | Edm.String | ✓ | ✓ | ✓ |
| name | Edm.String | ✓ | ✓ | ✓ |
| content | Edm.String | ✓ | ✓ | ✗ |
| file_path | Edm.String | ✓ | ✓ | ✗ |
| file_type | Edm.String | ✓ | ✓ | ✓ |
| upload_date | Edm.DateTimeOffset | ✓ | ✓ | ✓ |

5. Save index

### Option 2: REST API

```bash
curl -X POST https://<service>.search.windows.net/indexes \
  -H "Content-Type: application/json" \
  -H "api-key: <admin-key>" \
  -d '{
    "name": "documents",
    "fields": [
      {"name": "id", "type": "Edm.String", "key": true, "retrievable": true, "searchable": true, "filterable": true},
      {"name": "name", "type": "Edm.String", "retrievable": true, "searchable": true, "filterable": true},
      {"name": "content", "type": "Edm.String", "retrievable": true, "searchable": true},
      {"name": "file_path", "type": "Edm.String", "retrievable": true, "searchable": true},
      {"name": "file_type", "type": "Edm.String", "retrievable": true, "searchable": true, "filterable": true},
      {"name": "upload_date", "type": "Edm.DateTimeOffset", "retrievable": true, "filterable": true}
    ]
  }'
```

### Option 3: Python Script

```python
import requests
import json

service_name = "your-service"
index_name = "documents"
api_key = "your-admin-key"

endpoint = f"https://{service_name}.search.windows.net/indexes"

index_def = {
    "name": index_name,
    "fields": [
        {"name": "id", "type": "Edm.String", "key": True, "retrievable": True, "searchable": True, "filterable": True},
        {"name": "name", "type": "Edm.String", "retrievable": True, "searchable": True, "filterable": True},
        {"name": "content", "type": "Edm.String", "retrievable": True, "searchable": True},
        {"name": "file_path", "type": "Edm.String", "retrievable": True, "searchable": True},
        {"name": "file_type", "type": "Edm.String", "retrievable": True, "searchable": True, "filterable": True},
        {"name": "upload_date", "type": "Edm.DateTimeOffset", "retrievable": True, "filterable": True}
    ]
}

headers = {
    "Content-Type": "application/json",
    "api-key": api_key
}

response = requests.post(endpoint, headers=headers, json=index_def)
print(response.status_code, response.text)
```

---

## Optional: Enable Vector + Semantic Search (Recommended)

Add a vector field to support hybrid retrieval and configure a semantic settings block so the service can return better captions/highlights.

### Updated Index Schema (vector aware)

```json
{
  "name": "documents",
  "fields": [
    {"name": "id", "type": "Edm.String", "key": true, "retrievable": true, "searchable": true, "filterable": true},
    {"name": "name", "type": "Edm.String", "retrievable": true, "searchable": true, "filterable": true},
    {"name": "content", "type": "Edm.String", "retrievable": true, "searchable": true},
    {"name": "content_vector", "type": "Collection(Edm.Single)", "retrievable": true, "searchable": true, "dimensions": 1536, "vectorSearchConfiguration": "default"},
    {"name": "file_path", "type": "Edm.String", "retrievable": true, "searchable": true},
    {"name": "file_type", "type": "Edm.String", "retrievable": true, "searchable": true, "filterable": true},
    {"name": "upload_date", "type": "Edm.DateTimeOffset", "retrievable": true, "filterable": true}
  ],
  "vectorSearch": {
    "algorithms": [{"name": "default", "kind": "hnsw"}],
    "profiles": [{"name": "default", "algorithm": "default"}]
  },
  "semantic": {
    "configurations": [
      {
        "name": "default",
        "prioritizedFields": {
          "titleField": {"fieldName": "name"},
          "prioritizedContentFields": [{"fieldName": "content"}]
        }
      }
    ]
  }
}
```

### Notes
- `content_vector` uses 1536 dims (OpenAI text-embedding-3-small). Adjust if your embedder differs.
- Keep `vectorSearchConfiguration` matching the profile name under `vectorSearch.profiles`.
- If semantic ranker is enabled on the service, set `queryType=semantic` and `semanticConfiguration=default` in queries.
- Hybrid search flow: send both `vector` and `search` text in the same request (e.g., `/docs/search?api-version=2023-11-01` with `vectorQueries`).

---

## How It Works

### Document Upload & Indexing

```
User uploads file (Word, PDF, Excel, etc.)
           ↓
Download from Teams/OneDrive/SharePoint
           ↓
Extract text content
           ↓
Index in Azure Cognitive Search (INDEXED)
           ↓
Document now searchable
```

### Document Search

```
User asks question: "Find information about X"
           ↓
Query Azure Cognitive Search (PRIMARY) ← FAST, indexed docs
           ↓
If no results, fallback to Graph API search ← SLOWER, real-time
           ↓
Return results with highlighting
```

---

## Search Features

### Text Search
- Simple search: `azure deployment`
- Phrases: `"deployment pipeline"`

### Field Filtering
- By file type: `file_type eq 'pdf'`
- By upload date: `upload_date gt 2024-01-01`

### Highlighting
Search results include highlighted matching text (marked with `<mark>` tags)

---

## Configuration in .env.dev

```bash
# === Azure Cognitive Search (PRIMARY) ===
AZURE_SEARCH_ENDPOINT=https://your-service.search.windows.net
AZURE_SEARCH_KEY=<admin-key>
AZURE_SEARCH_INDEX=swope-vector-documents

# === Microsoft Graph API (FALLBACK) ===
GRAPH_CLIENT_ID=<client-id>
GRAPH_CLIENT_SECRET=<client-secret>
GRAPH_TENANT_ID=<tenant-id>

# === Bot Configuration ===
BOT_ID=<bot-id>
BOT_PASSWORD=<bot-password>
TEAMS_APP_ID=<app-id>
```

---

## Testing

### Upload and Index Document
1. Upload PDF/Word file to bot in Teams (1:1 chat)
2. Bot extracts content
3. Document is automatically indexed in Azure Search
4. Check logs: `✓ Indexed document in AI search`

### Search Documents
1. Ask bot: "Search for X"
2. Bot searches Azure Cognitive Search first
3. Returns matching documents with highlights
4. If no results, Graph fallback is tried

### Verify Index
```bash
curl https://<service>.search.windows.net/indexes/documents/stats \
  -H "api-key: <admin-key>"
```

Should return document count.

---

## Fallback Behavior

### If Azure Search Not Configured
- Indexing skipped (silently, no error)
- Search falls back to Graph API
- Documents still searchable via Graph

### If Graph API Not Configured
- File downloads still work (Teams downloadUrl)
- Channel/group files can't be accessed
- Search falls back to cached documents

### If Both Unavailable
- File uploads still work
- Search returns error message with helpful guidance

---

## Performance Notes

- **First search:** May be slow (cold start)
- **Subsequent searches:** Fast (cached results)
- **Index size limit:** 512 MB per index (upgrade tier if needed)
- **Document size limit:** 16 MB per document
- **Content limit:** 100 KB per indexed document (snippets stored)

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Search not configured" | Add `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_KEY` |
| "Index not found" | Create index named `documents` (see above) |
| "No documents found" | Upload files first (they're indexed automatically) |
| "403 Forbidden" | Check API key is valid (use admin key, not query key) |
| "Slow searches" | Delete old index and recreate (reset index) |

---

## Cost Estimation

**Azure Cognitive Search Pricing:**
- **Free tier:** 1 index, 50 MB storage (development)
- **Basic tier:** $75/month, 2 GB storage
- **Standard tier:** $250/month, 25 GB storage

**Recommendation:**
- **Development:** Use Free tier
- **Production:** Use Basic or Standard tier based on document volume

---

## Next Steps

1. ✅ Deploy Azure Cognitive Search service
2. ✅ Create `documents` index with schema above
3. ✅ Add environment variables to `.env.dev`
4. ✅ Restart bot
5. ✅ Upload documents and test indexing
6. ✅ Test search queries

Documents will now be automatically indexed and searchable!
