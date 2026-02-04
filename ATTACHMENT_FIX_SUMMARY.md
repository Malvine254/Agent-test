# Teams Bot Attachment Fix - Summary

## Problem
Attachments were not being downloaded properly - files were inaccessible even though they appeared to be uploaded in Teams.

## Root Cause
Based on Microsoft Teams documentation (https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bots-filesv4):

1. **How Teams file attachments work:**
   - When a user uploads a file in Teams, it's first uploaded to their **OneDrive for Business**
   - The bot receives an attachment with:
     - `contentType`: `"application/vnd.microsoft.teams.file.download.info"`
     - `content.downloadUrl`: Pre-authenticated OneDrive URL (expires quickly, ~1 hour)
     - `contentUrl`: Permanent SharePoint URL
   
2. **Authentication requirement:**
   - Bot must use its **Microsoft App Credentials** access token
   - Token obtained via OAuth 2.0 client credentials flow
   - Scope: `https://api.botframework.com/.default`
   - Required for both downloadUrl and contentUrl

3. **Previous issues:**
   - Code was trying downloadUrl without authentication first
   - Didn't always use bot token for contentUrl fallback
   - Error messages didn't explain the OneDrive upload delay

## Solution Implemented

### 1. Fixed Authentication (`simple_file_handler.py`)
- **Always** obtain bot's access token before downloading
- Use token for all download attempts (both downloadUrl and contentUrl)
- Proper fallback: try downloadUrl first, then contentUrl if it fails
- Better error handling with specific HTTP status codes

### 2. Improved User Feedback (`app.py`)
Enhanced error messages to explain:
- Files need 10-30 seconds to upload to OneDrive
- Use paperclip button instead of drag-and-drop
- Desktop/Web Teams work better than mobile app
- File size limits (250 MB recommended)
- Link to official documentation

### 3. Better Logging
- Log each step of the download process
- Show which URL type is being used
- Display HTTP response codes and headers
- Track authentication success/failure

## Key Changes

### `simple_file_handler.py`
```python
# BEFORE: Try without token first
if download_url:
    resp = requests.get(download_url, timeout=30)

# AFTER: Always use bot token
bot_token = get_bot_access_token()
if not bot_token:
    return "❌ Bot authentication failed..."
headers["Authorization"] = f"Bearer {bot_token}"
resp = requests.get(download_url, timeout=30, headers=headers)
```

### `app.py`
```python
# Enhanced error message with troubleshooting steps
error_msg = "⚠️ I detected file attachment(s) but couldn't access them:\\n\\n"
error_msg += "**How to fix this:**\\n\\n"
error_msg += "1. **Wait 10-30 seconds** after uploading...\\n"
error_msg += "2. **Use the paperclip/attach button**...\\n"
error_msg += "3. **Use Desktop or Web Teams**...\\n"
```

## Manifest Configuration

Already properly configured in `appPackage/manifest.json`:
```json
{
  "bots": [{
    "supportsFiles": true,  // ✓ Required for file handling
    "scopes": ["team", "groupChat", "personal"]
  }]
}
```

## Testing Checklist

- [x] Verify bot credentials (APP_ID, APP_PASSWORD) are set
- [ ] Test file upload from **Desktop Teams**
- [ ] Test file upload from **Web Teams**  
- [ ] Test file upload from **Mobile Teams** (known limitations)
- [ ] Verify files process after 10-30 second delay
- [ ] Test different file types (PDF, Word, Excel, images, text)
- [ ] Verify error messages are clear and helpful
- [ ] Check logs for proper authentication flow

## Monitoring

Watch for these log messages:
```
✓ Successfully obtained bot access token
✓ Downloaded X bytes from OneDrive  
✓ Successfully processed attachment: filename.pdf
```

Error patterns to watch:
- `HTTP 401/403` → Authentication/permission issues
- `HTTP 404` → URL expired or file deleted
- `No download URL found` → Attachment structure issue

## References

- [Send and receive files using bot](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bots-filesv4)
- [Bot file upload sample](https://github.com/OfficeDev/Microsoft-Teams-Samples/tree/main/samples/bot-file-upload)
- [Bot Framework attachments](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-add-media-attachments)

## Mobile App Limitations

**Known Issue:** Mobile Teams apps (iOS/Android) sometimes structure attachments differently than desktop/web.

**Workaround:** Code now checks multiple possible attachment properties and provides clear error messages directing users to desktop/web Teams.
