# Graph Shares API Fix for Channel/Group File Downloads

## Summary
Fixed the Graph fallback implementation in `simple_file_handler.py` to properly download files from channel/group chats using the SharePoint "shares" API instead of guessing drive item paths.

## Changes Made

### 1. Updated `download_via_graph_fallback()` Function (Line 143)

**Old Approach (BROKEN):**
- Tried to use `/me/drive/items/{unique_id}/content`
- Did not work for channel files (which live in SharePoint site drives, not user's personal drive)
- `unique_id` from Teams is not always a proper drive item ID

**New Approach (CORRECT):**
Uses the Graph "shares" API to reliably resolve SharePoint URLs:

1. **Encode SharePoint URL into shareId:**
   ```python
   shareId = "u!" + base64.urlsafe_b64encode(contentUrl.encode("utf-8")).decode("utf-8").rstrip("=")
   ```

2. **Resolve shareId to get drive + item IDs:**
   ```
   GET https://graph.microsoft.com/v1.0/shares/{shareId}/driveItem?$select=id,parentReference
   ```
   - Returns: `id` (itemId) and `parentReference.driveId`

3. **Download file using resolved IDs:**
   ```
   GET https://graph.microsoft.com/v1.0/drives/{driveId}/items/{itemId}/content
   ```

**Why This Works:**
- The shares API is the official way to resolve SharePoint URLs
- Automatically finds the correct drive (site drive for channels, personal for OneDrive)
- Avoids "wrong drive" errors from guessing
- Works with both app-only and delegated tokens

### 2. Updated `process_attachment()` Download Strategy (Line 340-375)

**New Logic Flow:**

| Condition | Action | Result |
|-----------|--------|--------|
| `downloadUrl` exists | Download with `requests.get()` NO auth header | ✅ Works in personal chat |
| Channel/group + `contentUrl` exists | Use Graph shares fallback | ✅ Works if token available |
| Channel/group + no `contentUrl` | Return user message | ❌ Clear message, no 403 error |
| Personal/unknown + no `downloadUrl` | Return "upload in progress" message | ⚠️ Likely upload still happening |

**Key Change:**
```python
elif conversation_type in ("channel", "groupchat") and content_url:
    # Use Graph shares fallback for channel files with contentUrl
    file_content = download_via_graph_fallback(content_url, display_name)
    download_method = "graph_shares"
```

### 3. Fixed Default Conversation Type (Line 310)

**Before:**
```python
conversation_type = conversation_type or "personal"
```
Problem: Hides detection failures by defaulting to personal

**After:**
```python
conversation_type = conversation_type or "unknown"
```
Benefit: Logs will clearly show when conversation type couldn't be determined, making debugging easier

## Enhanced Logging

The Graph fallback now logs each step:

1. **Shares API resolution:**
   ```
   [ID] Resolving SharePoint URL via Graph shares API...
   [ID] Shares API response: HTTP 200
   [ID] ✓ Resolved: driveId=..., itemId=...
   ```

2. **File download:**
   ```
   [ID] Downloading from: /drives/.../items/...
   [ID] Download response: HTTP 200
   [ID] ✓ Downloaded file via Graph shares (12345 bytes)
   ```

3. **Error handling with specific messages:**
   ```
   [ID] ❌ Graph shares resolution failed: HTTP 403
   [ID] → Insufficient permissions to access this file (Files.Read.All or Sites.Read.All required)
   ```

## Token Requirements

The Graph fallback requires a valid Graph token via `knowledge_base.get_graph_token()`:

- **For App-Only Token:** Azure app registration needs `Files.Read.All` and `Sites.Read.All` permissions with admin consent
- **For Delegated Token:** Would use On-Behalf-Of (OBO) flow with user's consent
- **Missing Token:** Returns user-friendly message instead of failing silently

## Testing Checklist

### Personal Chat (1:1)
- [ ] Upload PDF, Word, Excel files
- [ ] Verify: Files download via `teams_downloadurl` (no auth header)
- [ ] Verify logs: `download_method=teams_downloadurl`
- [ ] Verify content extracted correctly

### Channel Chat with App Token
- [ ] Upload file to channel
- [ ] Verify: If Graph token available, attempts Graph shares fallback
- [ ] Verify logs: `Resolving SharePoint URL via Graph shares API`
- [ ] If successful: `download_method=graph_shares`, content extracted
- [ ] If missing permissions: Clear error message about `Files.Read.All`

### Channel Chat without Graph Token
- [ ] Upload file to channel
- [ ] Verify: Returns user-friendly message (not 403 error)
- [ ] Message indicates to upload in 1:1 chat instead
- [ ] Verify logs: `Cannot obtain Graph token`

### Group Chat
- [ ] Same as channel testing (group files also in SharePoint)

## Benefits

✅ **Fixed Channel File Downloads** - No longer tries impossible `/me/drive/` paths
✅ **Better Error Messages** - Users understand why channel files can't be downloaded
✅ **Transparent Fallback** - Logs show exactly which method (Teams URL vs Graph) was used
✅ **Proper SharePoint Resolution** - Uses official shares API, not URL parsing
✅ **Clear Permission Errors** - Distinguishes 403 (permission) from 404 (deleted file)

## Future Enhancements

1. **Delegated Token Support:** Implement OBO flow for delegated user consent
2. **Channel User Download:** Allow users to download channel files by signing in with their own credentials
3. **Caching:** Store resolved driveId/itemId mappings to reduce Graph API calls
4. **Retry Logic:** Automatic retry on transient failures (timeouts, 429)
