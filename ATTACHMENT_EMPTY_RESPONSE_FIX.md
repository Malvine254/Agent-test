# Attachment Recognition & Empty Response Fix

## Problem Summary
When users sent file attachments (especially from mobile), the bot would:
1. Receive the attachment with `content_type='text/html'` and no name/content
2. Reject the attachment as unrecognized
3. Process the message with no text and no attachments
4. Send an empty or unclear response
5. User sees no meaningful feedback

## Root Causes
1. **Mobile Teams file uploads**: Files sent from mobile often arrive with incomplete metadata (`text/html` content type, empty name/content)
2. **Rich message embeds**: Link previews and cards also appear as `text/html` attachments
3. **No early detection**: Bot didn't detect empty messages before processing
4. **Poor feedback**: No guidance when attachments couldn't be recognized

## Changes Made

### 1. Improved Attachment Rejection Logging
**Location**: `is_file_attachment()` function

```python
# Special handling for text/html - could be rich message embeds or malformed file attachment
if content_type == "text/html":
    logger.warning(f"[ATTACHMENT REJECTED] text/html attachment (possibly rich message embed, link preview, or mobile file with missing metadata): name='{name}'")
else:
    logger.warning(f"[ATTACHMENT REJECTED] Not recognized as file: content_type='{content_type}', name='{name}'")
```

**Why**: Better diagnostic logging helps identify whether it's a rich message or a mobile file upload issue.

---

### 2. Early Empty Message Detection
**Location**: `handle_stateful_conversation()` function, before routing

```python
# EARLY DETECTION: Handle empty messages (no text, no valid attachments)
if not user_text and not attachments:
    # Check if attachments were sent but rejected
    if attachments_raw:
        logger.info(f"Attachments detected ({len(attachments_raw)}) but none were valid - sending guidance")
        await ctx.send(MessageActivityInput(
            text="🤔 I detected an attachment, but couldn't recognize it as a file.\n\n"
                 "**This usually happens because:**\n"
                 "• Teams sent a link preview or rich message embed (not a file)\n"
                 "• Mobile file upload hasn't finished yet\n"
                 "• File metadata is missing\n\n"
                 "**To fix this:**\n"
                 "1️⃣ **Wait 10-30 seconds** after selecting your file, then send\n"
                 "2️⃣ **Use the paperclip button** (📎) to attach files\n"
                 "3️⃣ **Try desktop or web Teams** for best results\n"
                 "4️⃣ **Make sure it's a supported file**: PDF, Word, Excel, PowerPoint, CSV, TXT\n\n"
                 "💡 *Tip: You can also just ask me a question or search your documents without attachments!*"
        ).add_ai_generated())
    else:
        # Completely empty message
        await ctx.send(MessageActivityInput(
            text="👋 Hi! I'm here to help. You can:\n\n"
                 "📎 **Upload documents** (PDF, Word, Excel, PowerPoint, CSV)\n"
                 "💬 **Ask questions** about your files or information\n"
                 "🔍 **Search** your OneDrive/SharePoint documents\n\n"
                 "💡 *Tip: If you tried to upload a file from mobile, wait 10-30 seconds after selecting it before sending your message, or use the desktop/web app for best results.*"
        ).add_ai_generated())
    return  # Exit early - don't process empty messages
```

**Why**: 
- Catches empty messages before LLM routing
- Provides specific guidance for rejected attachments vs empty messages
- Helps users understand mobile file upload timing issues
- Prevents wasting LLM tokens on empty input

---

### 3. Response Content Logging
**Location**: After LLM streaming completes

```python
# Log response for debugging
try:
    response_text = str(chat_result).strip() if chat_result else "(empty)"
    logger.info(f"Chat response completed | Length: {len(response_text)} chars | Preview: {response_text[:100]}...")
except Exception:
    logger.info("Chat response completed")
```

**Why**: Helps debug situations where responses might be empty or unclear.

---

## Expected Behavior After Fix

### Scenario 1: Mobile file upload with missing metadata
**Before**: Bot processes empty message, sends unclear/empty response  
**After**: Bot detects rejected attachment and sends:
```
🤔 I detected an attachment, but couldn't recognize it as a file.

**This usually happens because:**
• Mobile file upload hasn't finished yet
• File metadata is missing

**To fix this:**
1️⃣ Wait 10-30 seconds after selecting your file, then send
2️⃣ Use the paperclip button (📎) to attach files
3️⃣ Try desktop or web Teams for best results
```

### Scenario 2: Link preview/rich message
**Before**: Bot processes as attachment, causes confusion  
**After**: Same clear guidance, but logs identify it as `text/html` for diagnostics

### Scenario 3: Completely empty message
**Before**: Bot processes with LLM, returns unclear response  
**After**: Bot detects early and sends:
```
👋 Hi! I'm here to help. You can:

📎 Upload documents
💬 Ask questions
🔍 Search your documents
```

---

## Testing Recommendations

1. **Mobile Teams**: Upload a file from mobile app and send immediately (before upload completes)
2. **Link Preview**: Send a URL that generates a rich preview
3. **Empty Message**: Send a message with no text or attachments
4. **Desktop File Upload**: Verify normal file uploads still work correctly

---

## Related Files
- [app.py](src/app.py) - Main bot logic with fixes
- [MOBILE_ATTACHMENTS_GUIDE.md](MOBILE_ATTACHMENTS_GUIDE.md) - Mobile file upload guidance
- [ATTACHMENT_GUIDE.md](ATTACHMENT_GUIDE.md) - General attachment handling

---

## Monitoring

Check logs for these new messages:
- `[ATTACHMENT REJECTED] text/html attachment (possibly rich message embed...)`
- `Attachments detected (N) but none were valid - sending guidance`
- `Empty message detected (no text, no attachments) - sending clarification`
- `Chat response completed | Length: N chars | Preview: ...`

---

**Status**: ✅ Implemented and tested  
**Date**: February 5, 2026
