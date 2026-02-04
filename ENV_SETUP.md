# .env.dev Example - Azure Cognitive Search Configuration

## Complete Environment Variables for AI Search + File Download

```bash
# ============================================
# AZURE COGNITIVE SEARCH (PRIMARY - AI Search)
# ============================================
AZURE_SEARCH_ENDPOINT=https://your-search-service.search.windows.net
AZURE_SEARCH_KEY=your-admin-api-key-here
AZURE_SEARCH_INDEX=documents

# ============================================
# MICROSOFT GRAPH API (FALLBACK - Search + Download)
# ============================================
GRAPH_CLIENT_ID=your-graph-client-id
GRAPH_CLIENT_SECRET=your-graph-client-secret
GRAPH_TENANT_ID=your-tenant-id

# ============================================
# BOT CONFIGURATION
# ============================================
BOT_ID=your-bot-id
BOT_PASSWORD=your-bot-password
TEAMS_APP_ID=your-teams-app-id
APP_TENANTID=your-tenant-id
APP_TYPE=MultiTenant

# ============================================
# OPTIONAL: Azure OpenAI (Image Vision - disabled by default)
# ============================================
# AZURE_OPENAI_API_KEY=your-openai-key
# AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
# AZURE_OPENAI_MODEL_DEPLOYMENT_NAME=gpt-4o
# ENABLE_IMAGE_VISION=false

# ============================================
# OPTIONAL: Performance Tuning
# ============================================
GRAPH_TIMEOUT=10
CRAWL_WORKERS=4
ATTACHMENT_DOWNLOAD_TIMEOUT=30
```

---

## Step-by-Step Setup

### 1. Get Azure Cognitive Search Credentials

#### Option A: Azure CLI
```bash
# Get endpoint
az search service show \
  --name your-search-service \
  --resource-group your-resource-group \
  --query "endpoint" \
  --output tsv

# Get admin API key
az search admin-key show \
  --service-name your-search-service \
  --resource-group your-resource-group \
  --query "primaryKey" \
  --output tsv
```

#### Option B: Azure Portal
1. Go to your Azure Cognitive Search service
2. Click **Overview** → Copy the **URL** (this is your endpoint)
3. Click **Keys** → Copy **Primary admin key**

**Example values:**
- ENDPOINT: `https://my-search.search.windows.net`
- KEY: `0A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P` (64-char string)

### 2. Get Microsoft Graph Credentials

These are usually already configured, but if needed:

```bash
# Using Azure CLI to get existing credentials
az ad app show --id <your-app-id> --query "appId"
```

Or in Azure Portal:
1. Go to **Azure AD** → **App registrations**
2. Find your bot's app registration
3. Copy **Application (client) ID** → `GRAPH_CLIENT_ID`
4. Go to **Certificates & secrets** → Copy a **Client Secret** → `GRAPH_CLIENT_SECRET`
5. Go to **Overview** → Copy **Directory (tenant) ID** → `GRAPH_TENANT_ID`

### 3. Create Search Index

**Using REST API:**
```bash
curl -X POST https://your-search-service.search.windows.net/indexes \
  -H "Content-Type: application/json" \
  -H "api-key: your-admin-key" \
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

### 4. Update .env.dev File

```bash
# Copy values from steps 1-2 above
cp .env.example .env.dev

# Edit .env.dev and add:
AZURE_SEARCH_ENDPOINT=https://your-search-service.search.windows.net
AZURE_SEARCH_KEY=your-admin-api-key
AZURE_SEARCH_INDEX=documents

GRAPH_CLIENT_ID=your-client-id
GRAPH_CLIENT_SECRET=your-client-secret
GRAPH_TENANT_ID=your-tenant-id
```

### 5. Verify Configuration

```bash
# Test Azure Search connectivity
curl https://your-search-service.search.windows.net/indexes/documents/stats \
  -H "api-key: your-admin-key"

# Should return: {"@odata.context":"...","count":0}
```

### 6. Restart Bot

```bash
# Stop existing bot
ctrl+c

# Start bot (search enabled)
python src/app.py
```

---

## Quick Reference

| Variable | Example | Where to Find |
|----------|---------|---------------|
| `AZURE_SEARCH_ENDPOINT` | `https://my-search.search.windows.net` | Azure Search → Overview → URL |
| `AZURE_SEARCH_KEY` | `0A1B2C3D...` (64 chars) | Azure Search → Keys → Primary key |
| `AZURE_SEARCH_INDEX` | `documents` | Create via REST API (see above) |
| `GRAPH_CLIENT_ID` | UUID format | Azure AD → App registrations → App ID |
| `GRAPH_CLIENT_SECRET` | `~abc-def_GHI...` | Azure AD → Certificates & secrets |
| `GRAPH_TENANT_ID` | UUID format | Azure AD → Properties → Tenant ID |
| `BOT_ID` | UUID format | From previous bot registration |
| `BOT_PASSWORD` | (64 chars) | From previous bot registration |

---

## Validation Checklist

After updating `.env.dev`:

- [ ] `AZURE_SEARCH_ENDPOINT` is not empty and ends with `.search.windows.net`
- [ ] `AZURE_SEARCH_KEY` is exactly 64 characters (copy from portal, not abbreviated)
- [ ] `AZURE_SEARCH_INDEX` = `documents` (exact match)
- [ ] `GRAPH_CLIENT_ID` is UUID format (36 chars with hyphens)
- [ ] `GRAPH_CLIENT_SECRET` is not empty
- [ ] `GRAPH_TENANT_ID` is UUID format
- [ ] All keys copied exactly (no typos)
- [ ] No trailing whitespace on lines
- [ ] File is saved

**Test connectivity:**
```bash
python -c "
import requests
import os
from dotenv import load_dotenv

load_dotenv('.env.dev')

endpoint = os.getenv('AZURE_SEARCH_ENDPOINT')
key = os.getenv('AZURE_SEARCH_KEY')

headers = {'api-key': key}
url = f'{endpoint}/indexes/documents/stats'

resp = requests.get(url, headers=headers)
print(f'Status: {resp.status_code}')
print(f'Response: {resp.json()}')
"
```

Expected output:
```
Status: 200
Response: {'@odata.context': '...', 'count': 0}
```

---

## Common Issues & Fixes

### Issue: "403 Forbidden"
**Cause:** Wrong API key (query key instead of admin key)  
**Fix:** Use **Primary admin key** from "Keys" section, not query key

### Issue: "Index not found"
**Cause:** Index not created yet  
**Fix:** Create index using REST API call above

### Issue: "Connection timeout"
**Cause:** Wrong endpoint URL  
**Fix:** Verify endpoint includes `.search.windows.net`

### Issue: "Search working but not indexing"
**Cause:** Graph credentials invalid  
**Fix:** Verify `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_TENANT_ID` are correct

### Issue: "Documents still not searchable after upload"
**Cause:** Index not created or wrong name  
**Fix:** Create index named exactly `documents`

---

## What Happens After Setup

1. **User uploads file** → Bot downloads from Teams/OneDrive
2. **Bot extracts content** → Text extracted from PDF/Word/Excel
3. **Bot indexes in AI search** → Content sent to Azure Cognitive Search (automatic)
4. **Document is searchable** → User can now query it
5. **User searches** → Bot queries Azure Search (fast) → Falls back to Graph if needed

---

## Support

If you need help:
1. Check logs for error messages (look for 🔍, ❌, ⚠️)
2. Verify credentials are copied exactly from Azure Portal
3. Make sure search index exists and is named `documents`
4. Restart bot after updating `.env.dev`

See `SEARCH_CONFIG.md` for detailed setup instructions.
