# Teams Bot File Attachments - Mobile vs Desktop

## Overview
File attachments work differently between Teams desktop/web and mobile apps. This guide explains the differences and how our bot handles them.

## Attachment Structure Differences

### Desktop/Web Client
```json
{
  "contentType": "application/vnd.microsoft.teams.file.download.info",
  "contentUrl": "https://contoso.sharepoint.com/...",
  "name": "file_example.pdf",
  "content": {
    "downloadUrl": "https://download.link",
    "uniqueId": "1150D938-8870-4044-9F2C-5BBDEBA70C9D",
    "fileType": "pdf",
    "etag": "123"
  }
}
```

**Key Features:**
- ✅ Has proper `contentType` 
- ✅ `content.downloadUrl` is pre-authenticated OneDrive URL
- ✅ Works reliably with bot token authentication
- ✅ File uploads immediately to OneDrive

### Mobile Client (iOS/Android)
```json
{
  "contentType": "application/vnd.microsoft.teams.file.download.info",
  "contentUrl": "https://contoso.sharepoint.com/...",
  "name": "file_example.pdf",
  "content": "JSON_STRING_OR_OBJECT"
}
```

**Potential Issues:**
- ⚠️ May have different attachment structure
- ⚠️ Content might be JSON string instead of object
- ⚠️ Upload to OneDrive may be slower
- ⚠️ Pre-authenticated URLs may expire faster
- ⚠️ Network connectivity more variable

## How Our Bot Handles Both

### 1. **Flexible Detection** ([app.py](src/app.py))
Our bot accepts attachments if ANY of these are true:
```python
- Has Teams file content type
- Has a file name
- Has content.downloadUrl
- Has contentUrl with SharePoint/download URL
```

### 2. **Multiple URL Sources** ([simple_file_handler.py](src/simple_file_handler.py))
The bot checks for URLs in this order:
1. `content.downloadUrl` (desktop primary)
2. `content.contentUrl` (fallback)
3. Direct `contentUrl` attribute
4. `downloadUrl` as direct attribute (mobile)
5. Parse `content` as JSON string (mobile format)
6. Search all URL-like properties

### 3. **Bot Authentication**
Always uses bot's access token:
```python
token = get_bot_access_token()  # OAuth 2.0 client credentials
headers = {"Authorization": f"Bearer {token}"}
```

## Troubleshooting Mobile Issues

### Issue: "File not accessible" on Mobile

**Diagnosis Steps:**
1. Check bot logs for attachment structure:
   ```
   Attachment attributes: [...]
   Content type: <class 'dict'> or <class 'str'>
   ```

2. Look for these patterns:
   - ❌ No `downloadUrl` found
   - ❌ `contentUrl` requires different auth
   - ❌ Content is JSON string not dict

**Solutions:**

#### A. Wait Before Sending Message
Mobile uploads are slower:
```
1. Attach file
2. Wait 10-30 seconds for upload to complete
3. Send message
```

#### B. Use Paperclip Button
Don't drag-and-drop on mobile:
```
1. Tap paperclip icon
2. Select file from file picker
3. Wait for confirmation
4. Send message
```

#### C. Check Network Connection
Mobile apps need stable connection:
- ✅ Strong Wi-Fi or cellular
- ✅ Not on VPN
- ✅ No firewall blocking OneDrive

### Issue: Works on Desktop, Not on Mobile

**Likely Cause:** Timing Issue

Mobile uploads take longer. The bot receives the message before OneDrive upload completes.

**Fix:**
```python
# Bot already implements retry logic in simple_file_handler.py:
- 3 retries with exponential backoff
- Waits 2, 4, 8 seconds between attempts
- Logs detailed error messages
```

**User Instructions:**
"⚠️ I detected file attachment(s) but couldn't access them:

**How to fix this:**

1. **Wait 10-30 seconds** after uploading, then send your message
   (Files need time to upload to OneDrive)

2. **Use the paperclip/attach button** instead of drag-and-drop

3. **Use Desktop or Web Teams** (Mobile app has known limitations)

4. **Check file size** (keep under 250 MB for best results)"

## Testing

### Desktop Test
```
1. Open Teams Desktop or Web
2. Attach "employee data.pdf"
3. Type "analyze this"
4. Send
Expected: ✓ Bot processes and analyzes file
```

### Mobile Test
```
1. Open Teams iOS/Android app
2. Tap paperclip icon
3. Select "employee data.pdf"
4. Wait 15 seconds
5. Type "analyze this"
6. Send
Expected: ✓ Bot processes and analyzes file (after wait time)
```

## Current Implementation

### File Detection ([app.py](src/app.py#L653-L668))
```python
# Accept if ANY condition is true:
has_download_url = content and content.get('downloadUrl')
has_content_url = content_url and 'sharepoint.com' in content_url
is_teams_file = content_type == 'application/vnd.microsoft.teams.file.download.info'

if is_teams_file or file_name or has_download_url or has_content_url:
    actual_files.append(att)
```

### URL Extraction ([simple_file_handler.py](src/simple_file_handler.py#L140-L187))
```python
# 1. Check content dict
download_url = attachment.content.get("downloadUrl")
content_url = attachment.content.get("contentUrl")

# 2. Try JSON parsing (mobile format)
if isinstance(attachment.content, str):
    content_obj = json.loads(attachment.content)
    download_url = content_obj.get("downloadUrl")

# 3. Check direct attributes
if not url_to_use:
    url_to_use = attachment.contentUrl or attachment.content_url

# 4. Use bot token for authentication
url_to_use = download_url or content_url
```

### Error Handling
- ✅ Network retry (3 attempts, exponential backoff)
- ✅ Detailed logging for debugging
- ✅ User-friendly error messages
- ✅ Graceful degradation

## Known Limitations

### Mobile App
- ⏱️ Slower upload (10-30 second delay)
- 📱 Network more variable
- 🔄 May need to retry attachment

### Both Platforms
- 📏 250 MB file size limit (practical)
- 🔒 Requires OneDrive for Business
- ⏰ Download URLs expire after ~1 hour
- 🌐 Needs internet connectivity

## Microsoft Documentation

See official docs:
https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bots-filesv4

Key sections:
- Use the Teams bot APIs
- Receive files in personal chat
- Mobile consent card UI differences

## Logging & Debugging

Enable detailed logging:
```python
# In app.py - already configured
logger.setLevel(logging.INFO)

# Watch for these log messages:
"✓ Keeping attachment: name=X, hasDownloadUrl=Y"
"Found downloadUrl in content (OneDrive URL)"
"Parsed content from JSON string (mobile app format)"
"No URL found - analyzing attachment structure..."
```

## Summary

**Desktop/Web**: Works reliably ✅  
**Mobile**: Works with 10-30s wait time ✅  
**Both**: Use paperclip button, wait for upload ✅  

The bot handles both platforms automatically with comprehensive error handling and retry logic.
