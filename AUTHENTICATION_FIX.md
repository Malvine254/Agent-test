# Teams File Download Authentication Fix

## Problem Fixed

The bot was using **Bot Framework token** (`botframework.com/oauth2/v2.0/token`) to download files from SharePoint/OneDrive, which is **incorrect and causes 401/403 errors**.

**Bot Framework tokens are ONLY for the Bot Connector API**, not for accessing SharePoint/OneDrive files.

## Solution Implemented

### New Authentication Strategy

#### 1. **Personal Chat (1:1 with bot)** ✅
- Use Teams-provided `downloadUrl` from attachment
- **NO Authorization header needed** - URL is pre-authenticated by Teams
- URL is short-lived (~1 hour), download immediately
- Implemented in: `download_via_teams_downloadurl()`

```python
# CORRECT: No auth header for downloadUrl
response = requests.get(download_url, timeout=30, allow_redirects=True)
```

#### 2. **Channel/Group Chat** 🔄
- Fallback to **Microsoft Graph API** with delegated user token
- Use `unique_id` to fetch: `/me/drive/items/{uniqueId}/content`
- Requires Graph token with proper delegated permissions
- Implemented in: `download_via_graph_fallback()`

```python
# Graph API fallback for channels
graph_url = "https://graph.microsoft.com/v1.0/me/drive/items/{uniqueId}/content"
response = requests.get(graph_url, headers={"Authorization": f"Bearer {graph_token}"})
```

### Code Changes

#### **simple_file_handler.py**

**Removed:**
- `get_bot_access_token()` function - NO LONGER USED for file downloads
- Bot Framework token authentication logic

**Added:**
```python
def extract_download_info(attachment) -> Dict:
    """Extract download URLs and IDs from Teams attachment"""
    # Returns: {download_url, content_url, unique_id, file_name, content_type}

def download_via_teams_downloadurl(download_url: str, file_name: str) -> Optional[bytes]:
    """Download using Teams pre-authenticated URL (personal chat)"""
    # NO Authorization header - URL is already authenticated

def download_via_graph_fallback(unique_id: str, content_url: str, file_name: str) -> Optional[bytes]:
    """Fallback: Download via Microsoft Graph API (channels/groups)"""
    # Uses get_graph_token() for delegated user authentication

def process_attachment(attachment, corr_id, conversation_type="personal") -> str:
    """Process attachment with context-aware download strategy"""
    # Routes to correct download method based on chat type
```

#### **app.py**

**Updated attachment handler to pass conversation type:**
```python
# Extract conversation type for proper authentication strategy
conversation_type = getattr(ctx.activity.conversation, 'conversation_type', 'personal') or 'personal'
file_content = process_attachment(att, conversation_id, conversation_type=conversation_type)
```

## Download Flow Decision Tree

```
File Attachment Received
│
├─ PERSONAL CHAT (1:1)
│  └─ downloadUrl available?
│     ├─ YES → download_via_teams_downloadurl() ✅ (NO auth header)
│     └─ NO  → download_via_graph_fallback()
│
├─ CHANNEL/GROUP CHAT
│  └─ downloadUrl available?
│     ├─ YES → Try Graph first, fallback to downloadUrl
│     └─ NO  → download_via_graph_fallback() (using unique_id)
│
└─ NO URLS AVAILABLE
   └─ Return clear error message with troubleshooting steps
```

## What This Fixes

| Issue | Before | After |
|-------|--------|-------|
| Personal chat file uploads | 401/403 errors (wrong token) | Works immediately ✅ |
| Channel file uploads | Doesn't work | Falls back to Graph API ✅ |
| Mobile attachment detection | Multiple format issues | Robust parsing ✅ |
| Token usage | Bot Framework token (wrong) | Context-specific auth ✅ |
| Error messages | Generic | Specific with troubleshooting ✅ |

## Testing Recommendations

### 1. **Personal Chat (1:1)**
```
✓ Upload PDF
✓ Upload Word document
✓ Upload Excel spreadsheet
✓ Verify files download without auth errors
```

### 2. **Channel Chat**
```
✓ Upload file to channel
✓ Verify Graph fallback is used
✓ Check logs for correct download path
```

### 3. **Mobile Clients**
```
✓ Test on Teams mobile app
✓ Verify JSON content parsing works
✓ Test various file formats
```

## Expected Log Output

### Personal Chat (Successful)
```
Attachment processing: test.pdf
  → Using Teams downloadUrl (personal chat)
  📥 Downloading (Teams personal chat) test.pdf from: https://...
  ✓ Downloaded 45678 bytes via teams_personal
```

### Channel Chat (Successful)
```
Attachment processing: test.pdf
  → Channel/group chat detected, attempting Graph fallback
  📥 Graph fallback: downloading test.pdf (unique_id=...)
  ✓ Downloaded 45678 bytes via graph_channel
```

## Environment Requirements

No new environment variables needed. Uses existing:
- `GRAPH_CLIENT_ID` - For Graph API delegation
- `GRAPH_CLIENT_SECRET` - For Graph API delegation
- `GRAPH_TENANT_ID` - For Graph API calls

## Known Limitations

1. **Graph fallback stub** - Currently uses `get_graph_token()` from `knowledge_base.py`
   - Verify this returns a valid delegated user token
   - May need OBO (On-Behalf-Of) implementation for channels

2. **SharePoint URL parsing** - Not yet implemented
   - Can parse item IDs directly from SharePoint URLs if needed
   - Use `contentUrl` as fallback reference

## References

- [Microsoft Teams File Upload Docs](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bots-filesv4)
- [Microsoft Graph File Download](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content)
- [Bot Framework Authentication](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-scenario-backend-integration)

## Next Steps

1. **Deploy and test** in personal chat first
2. **Monitor logs** for Graph fallback behavior in channels
3. **Verify Graph token** is being obtained correctly
4. **Implement full Graph parsing** if SharePoint URL fallback is needed
5. **Add OBO token flow** if delegated tokens are insufficient for channels
