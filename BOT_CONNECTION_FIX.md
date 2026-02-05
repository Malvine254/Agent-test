# Bot Not Sending/Receiving Messages - Fix

## Problem Diagnosed

The bot was not sending or receiving any messages due to:

1. **Missing Python Dependencies** - The required packages from `requirements.txt` were not installed
2. **Missing Bot Password** - The `SECRET_BOT_PASSWORD` environment variable was not properly configured
3. **Bot Process Issues** - Multiple Python processes running but dependencies not available

## Root Causes

###1. HTTP 500 Error
When testing `http://localhost:3978/api/messages`, the bot returned a 500 Internal Server Error, indicating the bot was running but crashing on requests.

### 2. Missing Dependencies
```
ModuleNotFoundError: No module named 'requests'
```

The Python environment was missing essential packages like `requests`, `microsoft-teams-ai`, etc.

### 3. Bot Authentication Issue
The `SECRET_BOT_PASSWORD` in `env/.env.local.user` was encrypted (`crypto_` prefix) and not being decrypted properly by the app.

## Solution Applied

### Step 1: Stop Running Bot Processes
```powershell
Stop-Process -Id <PID> -Force
```

### Step 2: Install Python Dependencies
```powershell
cd src
python -m pip install -r requirements.txt
```

Required packages:
- `microsoft-teams-ai>=2.0.0a8`
- `microsoft-teams-apps>=2.0.0a8`
- `requests>=2.31.0`
- `python-dotenv>=1.0.0`
- `aiohttp>=3.9.0`
- `beautifulsoup4>=4.12.0`
- Document processing: `pypdf`, `python-docx`, `openpyxl`, `Pillow`, etc.

### Step 3: Verify Bot Configuration

Check that these environment variables are set in `env/.env.local` or `env/.env.local.user`:

```env
BOT_ID=cebba5c4-756b-49c3-be62-4ccc340a269e
SECRET_BOT_PASSWORD=<actual_password>
BOT_DOMAIN=gk0l63t8-3978.uks1.devtunnels.ms
BOT_ENDPOINT=https://gk0l63t8-3978.uks1.devtunnels.ms

# Azure OpenAI
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_MODEL_DEPLOYMENT_NAME=gpt-4.1
AZURE_OPENAI_ENDPOINT=https://ai-foundry-main-001.cognitiveservices.azure.com/

# Graph API
GRAPH_CLIENT_ID=c944b55d-4632-42f1-b27e-4cd9745218de
GRAPH_CLIENT_SECRET=<secret>
GRAPH_TENANT_ID=588cadf4-9902-4465-86c0-8bcf04f4f102
```

### Step 4: Start Bot Properly

Use Teams Toolkit tasks to start the bot:

1. **Via Tasks**: Run "Start Agent Locally" task in VS Code
   - This runs: Validate prerequisites → Start local tunnel → Provision → Deploy

2. **Manual Start** (if needed):
   ```powershell
   cd src
   python app.py
   ```

The bot will start on port 3978 and connect to the dev tunnel.

### Step 5: Verify Bot is Running

```powershell
# Check process
Get-Process python

# Check port
netstat -ano | Select-String ":3978"

# Test endpoint (should return bot response, not 500 error)
Invoke-WebRequest -Uri "http://localhost:3978/api/messages" -Method GET
```

## Verification Checklist

- [ ] Python dependencies installed (`pip list | Select-String "microsoft-teams"`)
- [ ] Bot ID and password configured in environment
- [ ] Local tunnel running and connected
- [ ] Bot process running on port 3978
- [ ] Bot endpoint responding without 500 errors
- [ ] Teams/Microsoft 365 app sideloaded with correct bot ID
- [ ] Bot receives and responds to messages in Teams

## Common Issues

### Issue: Bot returns 500 error
**Cause**: Missing dependencies or configuration
**Fix**: Install dependencies and verify environment variables

### Issue: Bot doesn't respond to messages
**Cause**: Bot authentication failure
**Fix**: Verify `SECRET_BOT_PASSWORD` is set correctly (not encrypted `crypto_` value)

### Issue: "ModuleNotFoundError"
**Cause**: Dependencies not installed in the correct Python environment
**Fix**: Run `pip install -r requirements.txt` with the correct Python executable

### Issue: Multiple Python processes but bot not working
**Cause**: Old/stale processes without proper environment
**Fix**: Kill all Python processes and restart via Teams Toolkit tasks

## How to Properly Restart the Bot

1. **Stop all running processes**:
   ```powershell
   Get-Process python | Stop-Process -Force
   ```

2. **Verify environment is configured**:
   ```powershell
   Get-Content env\.env.local | Select-String "BOT_ID|SECRET_BOT_PASSWORD"
   ```

3. **Start via VS Code Task**: 
   - Press `Ctrl+Shift+P`
   - Select "Tasks: Run Task"
   - Choose "Start Agent Locally"

4. **Or start manually**:
   ```powershell
   cd src
   python app.py
   ```

## Next Steps

Once the bot is running and responding:

1. Test basic message exchange in Teams
2. Test file uploads
3. Test Microsoft Graph integration
4. Monitor logs for any errors

## Log Monitoring

Watch bot logs for:
- `MESSAGE RECEIVED` - Confirms messages are coming in
- `Starting Teams AI app...` - Confirms successful startup
- Error tracebacks - Indicates configuration or code issues

The bot logs to console with timestamps. Look for:
```
13:45:22 | Starting Teams AI app...
13:45:23 | ===========================================================
13:45:24 | MESSAGE RECEIVED | Text: Hello
```
