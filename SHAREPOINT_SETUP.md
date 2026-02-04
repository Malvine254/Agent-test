# SharePoint Library Configuration Guide

## Quick Setup

### 1. Add Your SharePoint Sites to .env

Edit your `.env.local` (or `.env.dev`) file and configure the `SHAREPOINT_SITES` variable:

```env
# SharePoint Libraries Configuration
# Add your SharePoint site URLs (comma-separated, no spaces around commas)
SHAREPOINT_SITES=https://yourtenant.sharepoint.com/sites/YourTeamSite,https://yourtenant.sharepoint.com/sites/Documents

# Example with real tenant:
# SHAREPOINT_SITES=https://contoso.sharepoint.com/sites/Marketing,https://contoso.sharepoint.com/sites/HR,https://contoso.sharepoint.com/sites/Engineering
```

### 2. How It Works

When a user starts a conversation:
1. The bot **caches their user profile** (name, email, UPN)
2. The bot **crawls their personal OneDrive** and tags documents with their user ID
3. The bot **crawls the configured SharePoint sites** and tags documents with their user ID
4. All documents are **user-specific** - no shared cache for security
5. The bot greets users by their **first name** from the cached profile

### 3. Finding Your SharePoint Site URLs

#### Method 1: From SharePoint
1. Navigate to your SharePoint site in a browser
2. Copy the URL from the address bar
3. Use the base site URL (e.g., `https://yourtenant.sharepoint.com/sites/SiteName`)

#### Method 2: From Teams
1. Open a Teams channel that has a SharePoint tab
2. Click "Open in SharePoint"
3. Copy the URL (remove everything after `/sites/SiteName`)

### 4. Examples

```env
# Single site
SHAREPOINT_SITES=https://contoso.sharepoint.com/sites/Engineering

# Multiple sites (comma-separated, NO SPACES)
SHAREPOINT_SITES=https://contoso.sharepoint.com/sites/Marketing,https://contoso.sharepoint.com/sites/HR,https://contoso.sharepoint.com/sites/Sales

# Multiple sites (with line break for readability - will still work)
SHAREPOINT_SITES=https://contoso.sharepoint.com/sites/Marketing,\
https://contoso.sharepoint.com/sites/HR,\
https://contoso.sharepoint.com/sites/Sales
```

### 5. What Gets Crawled

For each SharePoint site, the bot will:
- ✅ Crawl **all document libraries** (Documents, Shared Documents, etc.)
- ✅ Process supported file types: .docx, .doc, .pdf, .xlsx, .xls, .csv, .txt, .pptx, .ppt, .json, .xml
- ✅ Extract text content from documents
- ✅ Tag all documents with the requesting user's ID
- ✅ Respect folder depth limits (default: 4 levels deep)
- ✅ Respect size limits (default: 300 items per drive, max 10MB per file)

### 6. Required Permissions

Ensure your Azure App Registration has these **Application Permissions** (not delegated):
- ✅ `Files.Read.All` - Read all files in SharePoint and OneDrive
- ✅ `Sites.Read.All` - Read all SharePoint sites
- ✅ `User.Read.All` - Read user profiles (for name/UPN caching)

**Important:** Admin consent must be granted for these permissions!

### 7. Environment Variables Reference

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `SHAREPOINT_SITES` | No | Comma-separated SharePoint site URLs | `https://tenant.sharepoint.com/sites/Site1,https://tenant.sharepoint.com/sites/Site2` |
| `GRAPH_CLIENT_ID` | Yes | Azure App ID with Graph permissions | `c944b55d-4632-42f1-b27e-4cd9745218de` |
| `GRAPH_CLIENT_SECRET` | Yes | Azure App secret | `z9k8Q~G9WoQq~...` |
| `GRAPH_TENANT_ID` | Yes | Azure AD tenant ID | `588cadf4-9902-4465-86c0-8bcf04f4f102` |
| `GRAPH_CRAWL_MAX_ITEMS_PER_DRIVE` | No | Max items to index per library (default: 300) | `500` |
| `GRAPH_CRAWL_MAX_DEPTH` | No | Max folder depth (default: 4) | `6` |
| `GRAPH_CRAWL_MAX_FILE_BYTES` | No | Max file size in bytes (default: 10MB) | `20971520` |

### 8. Testing

After configuration:
1. Save your `.env.local` file
2. Restart the bot
3. Send a message in Teams
4. The bot should:
   - Greet you by your first name
   - Have access to documents from your OneDrive
   - Have access to documents from configured SharePoint sites
   - All tagged with your user ID for security

### 9. Logs to Watch For

```
INFO:knowledge_base:Cached profile for user: John Smith (john.smith@contoso.com)
INFO:knowledge_base:Starting document crawl for user: John (abc123...)
INFO:knowledge_base:Crawling personal OneDrive for John...
INFO:knowledge_base:Crawling 2 configured SharePoint site(s) for John...
INFO:knowledge_base:Found 3 drive(s) at https://contoso.sharepoint.com/sites/Marketing
INFO:knowledge_base:Crawling SharePoint library 'Documents' (type: documentLibrary) for John
INFO:knowledge_base:Crawl complete for John: 15 personal, 47 SharePoint, 8 skipped, 0 errors
```

### 10. Troubleshooting

| Issue | Solution |
|-------|----------|
| "Graph token unavailable" | Check `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_TENANT_ID` |
| "HTTP 403" on sites | Ensure app has `Sites.Read.All` and `Files.Read.All` with admin consent |
| "Could not resolve site" | Check SharePoint URL format (must be base site URL) |
| Bot doesn't use user's name | Check `User.Read.All` permission is granted |
| No documents indexed | Check that sites have document libraries with supported file types |

### 11. Security Notes

🔒 **User-Specific Access Control:**
- Every document is tagged with the user's ID who requested the crawl
- NO shared document cache - each user has their own view
- Personal OneDrive documents are always user-specific
- SharePoint documents are tagged per-user even if from shared sites

🔒 **Profile Privacy:**
- User profiles (name, email, UPN) are cached in memory only
- Cache is cleared when the bot restarts
- Only used for personalized greetings and logging

## Configuration File Locations

- **Local Development:** `env/.env.local`
- **Playground:** `env/.env.playground`  
- **Production:** Azure App Service → Configuration → Application Settings

---

**Need Help?** Check the logs for detailed error messages and Graph API responses.
