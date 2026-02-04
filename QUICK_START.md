# ⚡ Quick Start - AI Search Integration (5 minutes)

## TL;DR - What Happened

Your bot now automatically indexes uploaded files in **Azure Cognitive Search** and searches them when users ask questions.

**Search Priority:**
1. 🎯 Azure Cognitive Search (PRIMARY - fast, indexed)
2. 📊 Microsoft Graph API (FALLBACK - if search unavailable)
3. 📥 Graph shares API (FILE DOWNLOAD - channel files)

---

## ⚙️ Setup (5 Steps)

### Step 1: Create Azure Search Service

```bash
az search service create \
  --name my-search \
  --resource-group my-rg \
  --sku free
```

Or use Azure Portal → Create resource → Cognitive Search

### Step 2: Get Credentials

**Endpoint:**
- Azure Portal → Your search service → Overview → Copy URL
- Example: `https://my-search.search.windows.net`

**API Key:**
- Azure Portal → Keys → Copy **Primary admin key**
- Example: `0A1B2C3D...` (64 characters)

### Step 3: Create Index

```bash
# Replace with your values
SERVICE="my-search"
KEY="your-admin-key"

curl -X POST https://${SERVICE}.search.windows.net/indexes \
  -H "Content-Type: application/json" \
  -H "api-key: ${KEY}" \
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

### Step 4: Update `.env.dev`

Add these lines:

```bash
AZURE_SEARCH_ENDPOINT=https://my-search.search.windows.net
AZURE_SEARCH_KEY=your-64-char-admin-key
AZURE_SEARCH_INDEX=documents
```

### Step 5: Restart Bot

```bash
# Stop (Ctrl+C) and restart
python src/app.py
```

---

## ✅ Test It (2 Steps)

### Test 1: Upload & Index

1. Open Teams
2. Start 1:1 chat with bot
3. Upload a PDF or Word document
4. Check logs for: `✓ Indexed document in AI search`
5. Verify in Azure Portal: Cognitive Search → indexes → documents → check count increased

### Test 2: Search

1. Ask bot: "Find information about [word-from-document]"
2. Bot returns matching documents with highlighted text
3. Done! 🎉

---

## 📋 What You Need

```
✓ Azure subscription
✓ Azure Cognitive Search service (Free or Basic)
✓ Graph API credentials (usually already have)
✓ 5 minutes to configure
```

---

## 🔍 How It Works

```
Upload File          Extract Content        Index in AI Search
     ↓                      ↓                        ↓
[Doc.pdf] → Download → "Text from doc" → Azure Search
                                              ↓
                                        Searchable Now!
                                              ↓
                                    User: "Find X"
                                              ↓
                                    Bot searches Azure
                                              ↓
                                    "Found in Doc.pdf"
```

---

## 🚨 Common Issues

| Error | Fix |
|-------|-----|
| "Search not configured" | Add `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_KEY` to `.env.dev` |
| "Index not found" | Create index (see Step 3 above) |
| "403 Forbidden" | Make sure you're using **Admin** key, not Query key |
| "No documents found" | Upload files first (they're auto-indexed) |

---

## 📚 Full Docs

- 📖 [AI_SEARCH_IMPLEMENTATION.md](AI_SEARCH_IMPLEMENTATION.md) - Complete guide
- 🔧 [SEARCH_CONFIG.md](SEARCH_CONFIG.md) - Detailed setup
- 🌍 [ENV_SETUP.md](ENV_SETUP.md) - Environment variables

---

## ✨ What Changed

### New Code in Bot

```python
# When file uploaded:
_index_document_to_search(doc_id, name, url, content)

# When user searches:
search_knowledge_base(query)
  ├─ Azure Cognitive Search (PRIMARY)
  └─ Graph API (FALLBACK)
```

### New Environment Variables

```bash
AZURE_SEARCH_ENDPOINT=...
AZURE_SEARCH_KEY=...
AZURE_SEARCH_INDEX=documents
```

### New Files

- `src/knowledge_base.py`: Added `search_documents()`, `index_document()`
- `src/simple_file_handler.py`: Added `search_knowledge_base()`, `_index_document_to_search()`

---

## 🎯 Next Steps

- [ ] Create Azure Search service
- [ ] Get endpoint + API key
- [ ] Create `documents` index
- [ ] Add to `.env.dev`
- [ ] Restart bot
- [ ] Upload test file
- [ ] Search for content

Done! Your bot now has AI-powered search. 🚀

---

## Cost

- **Free tier:** $0/month (50 MB, good for dev)
- **Basic tier:** $75/month (2 GB, good for prod)
- **Per-search:** $0.001 per search (minimal)

---

## Support

Any issues? Check:
1. Logs for error messages
2. [SEARCH_CONFIG.md](SEARCH_CONFIG.md) troubleshooting section
3. Azure Portal → Search service → Monitoring

That's it! You're done. Your bot now searches documents automatically. ✨
