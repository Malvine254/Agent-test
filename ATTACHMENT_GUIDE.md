# 📎 Teams Bot File Attachment - Quick Start Guide

## ✅ What's Fixed

Your Teams bot can now properly receive and process file attachments! The implementation follows Microsoft's official documentation and best practices.

## 🚀 How to Use

### For Users (Uploading Files to Bot)

1. **Upload your file**
   - Click the **paperclip** icon (📎) in Teams chat
   - Select your file (PDF, Word, Excel, images, text files)
   - OR drag & drop onto the chat

2. **⏳ Wait 10-30 seconds**
   - Files need time to upload to OneDrive
   - Larger files take longer

3. **Send your message**
   - Type your question or request
   - The bot will automatically process the file

### Supported File Types

- **Documents:** PDF, Word (.docx), Excel (.xlsx), Text (.txt, .md, .json, .xml)
- **Images:** PNG, JPG, JPEG, GIF, BMP (with AI vision analysis)
- **Max size:** 250 MB recommended

### Best Platforms

✅ **Recommended:**
- Desktop Teams app
- Web Teams (teams.microsoft.com)

⚠️ **Limited Support:**
- Mobile Teams apps (iOS/Android) - known attachment handling issues

## 🔧 For Developers

### How It Works

```
User uploads file → OneDrive → Bot receives attachment → Bot downloads with token → Processes content
```

1. **File uploaded to OneDrive for Business**
   - Automatic when user attaches file in Teams
   
2. **Bot receives attachment notification**
   - Contains downloadUrl (short-lived) and contentUrl (permanent)
   
3. **Bot authenticates with Microsoft**
   - Uses bot's App ID and Secret
   - Obtains Bearer token
   
4. **Bot downloads file content**
   - Uses token to access OneDrive URL
   - Extracts text/data from file
   
5. **Bot processes and responds**
   - Includes file content in AI analysis

### Key Code Components

**Bot Token Acquisition** (`simple_file_handler.py`):
```python
def get_bot_access_token() -> Optional[str]:
    token_url = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": app_id,
        "client_secret": app_password,
        "scope": "https://api.botframework.com/.default"
    }
    response = requests.post(token_url, data=data)
    return response.json()["access_token"]
```

**File Download** (`simple_file_handler.py`):
```python
bot_token = get_bot_access_token()
headers = {"Authorization": f"Bearer {bot_token}"}
response = requests.get(downloadUrl, headers=headers)
content = response.content
```

### Configuration Required

**1. Environment Variables (.env)**
```env
BOT_ID=your-bot-app-id
SECRET_BOT_PASSWORD=your-bot-secret
AZURE_OPENAI_API_KEY=your-openai-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_MODEL_DEPLOYMENT_NAME=gpt-4o
```

**2. Manifest (appPackage/manifest.json)**
```json
{
  "bots": [{
    "botId": "${{BOT_ID}}",
    "supportsFiles": true,  // ⚠️ REQUIRED
    "scopes": ["personal", "groupChat", "team"]
  }]
}
```

### Testing

Run the diagnostic tool:
```bash
python test_attachment_config.py
```

This checks:
- ✓ Environment variables configured
- ✓ Bot can obtain access token  
- ✓ Manifest has supportsFiles enabled

### Monitoring & Debugging

**Successful Upload Logs:**
```
✓ Successfully obtained bot access token
✓ Downloaded 1234567 bytes from OneDrive
✓ Successfully processed attachment: document.pdf
📄 **PDF Document**: document.pdf
[extracted content...]
```

**Common Error Patterns:**

| Error | Cause | Solution |
|-------|-------|----------|
| `HTTP 403` | File not ready | Wait 10-30s after upload |
| `HTTP 401` | Bad credentials | Check BOT_ID and SECRET_BOT_PASSWORD |
| `HTTP 404` | URL expired | User should re-upload file |
| `No download URL found` | Attachment structure issue | Use desktop/web Teams |

## 📚 References

- [Official Teams Docs: Send/Receive Files](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bots-filesv4)
- [Bot Framework Attachments](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-add-media-attachments)
- [Sample Code: File Upload Bot](https://github.com/OfficeDev/Microsoft-Teams-Samples/tree/main/samples/bot-file-upload)

## 🐛 Troubleshooting

### "Bot authentication failed"
- Verify BOT_ID and SECRET_BOT_PASSWORD in .env
- Check bot is registered in Azure Portal
- Ensure credentials haven't expired

### "Access denied (HTTP 403)"
- Wait longer after upload (try 30-60 seconds)
- Try uploading from desktop Teams instead of mobile
- Verify bot has proper permissions in Azure

### "File not found (HTTP 404)"
- URL expired - user must re-upload
- File may have been deleted from OneDrive
- Try fresh upload with paperclip button

### Mobile app issues
Known limitation: Mobile Teams apps sometimes don't provide proper downloadUrl.
**Solution:** Direct users to desktop or web Teams for file uploads.

## ✨ Features

- **Automatic text extraction** from PDFs, Word, Excel
- **AI vision analysis** for images (extracts text, describes content)
- **Smart caching** of access tokens (1 hour validity)
- **Helpful error messages** guide users to fix issues
- **Robust fallback** between downloadUrl and contentUrl

## 🎯 Next Steps

1. ✅ Configuration verified with `test_attachment_config.py`
2. ✅ Deploy bot to Azure or run locally
3. ✅ Test with actual file uploads in Teams
4. ✅ Monitor logs for successful processing
5. ✅ Share with users!

---

**Need help?** Check logs for detailed error messages or consult the official documentation linked above.
