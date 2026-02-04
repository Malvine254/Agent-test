# User Identification and Personalization Flow

## 🎯 Overview
The bot now properly identifies users and addresses them by their first name using a consistent approach across all modules.

## 📋 Complete Flow

### 1. **User Identity Extraction** (app.py)
When a message arrives in Teams:
```python
# Extract from Teams activity
aad_id = channel_data.get("user", {}).get("aadObjectId")  # AAD Object ID (GUID)
user_email = sender.get("email") or channel_data.get("user", {}).get("userPrincipalName")
user_name = sender.get("name")  # Display name from Teams
```

### 2. **Profile Enrichment via Graph API** (app.py → knowledge_base.py)
```python
# app.py calls:
profile = get_cached_user_profile(aad_id, user_assertion=user_assertion)

# Which internally calls knowledge_base.py:
profile = get_user_profile(user_id)  # Fetches from Graph /users/{id}
```

**Graph API Fields Retrieved:**
- `id` - AAD Object ID
- `displayName` - Full name (e.g., "John Smith")
- `givenName` - First name (e.g., "John")
- `userPrincipalName` - Email/UPN (e.g., "john@company.com")
- `mail` - Email address
- `jobTitle` - Job title

### 3. **Profile Caching** (Two-Level Cache)

#### Level 1: In-Memory Cache (knowledge_base.py)
```python
_USER_PROFILE_CACHE = {}  # Global dict in knowledge_base.py
_USER_PROFILE_CACHE[user_id] = profile
```

#### Level 2: Disk Cache (app.py)
```python
# Stored in: src/user_profiles_cache.json
{
  "user_id_or_email": {
    "profile": {
      "displayName": "John Smith",
      "givenName": "John",
      "mail": "john@company.com",
      ...
    },
    "cached_at": <timestamp>
  }
}
```

### 4. **Personalization in Responses** (app.py)

The bot includes user information in the context:
```python
if user_name:
    info_parts.append(f"User's name is {user_name}")
if user_email:
    info_parts.append(f"email: {user_email}")

personalization = f"\n\n[CONTEXT: Today is {weekday_name}, {date_friendly} ({current_datetime}). {info_str}.]"
```

This context is passed to the LLM so it can:
- Address the user by name: "Hi John, here's what I found..."
- Respond to "what is my name": "Your name is John Smith"

### 5. **Document Crawl Personalization** (knowledge_base.py)

When crawling documents:
```python
# Fetch user's display name
display_name = get_user_display_name(user_id)  # Returns "John" or "John Smith"

# Log with personalized messages
logger.info(f"Starting document crawl for user: {display_name} ({user_id[:8]}...)")
logger.info(f"Crawling personal OneDrive for {display_name}...")
logger.info(f"Crawling SharePoint library 'Documents' for {display_name}")
```

## 🔐 Security: User ID as Partition Key

### Document Cache Partitioning
All documents are tagged with the requesting user's ID:
```python
cache.add_document(
    doc_id=doc_id,
    name=name,
    url=url,
    content=content,
    user_id=owner_user_id,  # ← User-specific partition
    metadata=metadata
)
```

### Cache Structure
```json
{
  "users": {
    "aad-guid-123": {
      "documents": {
        "drive1:item1": { 
          "name": "My Report.docx",
          "user_id": "aad-guid-123",
          "visibility": "user"
        }
      }
    },
    "user2@company.com": {
      "documents": {
        "drive2:item2": { 
          "name": "User2 Doc.pdf",
          "user_id": "user2@company.com",
          "visibility": "user"
        }
      }
    }
  }
}
```

### Search Isolation
When searching:
```python
def search_cache(query, user_id, ...):
    # SECURITY: Only user's own documents
    docs_map = {}
    if user_id:
        for doc_id, doc in self._get_user_cache(user_id).items():
            docs_map[doc_id] = doc
    
    # Other users' documents are NEVER included
    return search_results
```

## 🎭 User Identification Priorities

### App.py Priority Order:
1. **AAD Object ID** (aadObjectId from Teams) - Most reliable
2. **User Principal Name** (email/UPN) - Fallback
3. **Sender ID** (from.id) - If looks like GUID
4. **Conversation ID** - Last resort (anonymous users)

### Knowledge_base.py Validation:
```python
def get_user_profile(user_id):
    looks_guid = _looks_like_guid(user_id)
    looks_upn = "@" in user_id
    
    if not (looks_guid or looks_upn):
        logger.warning("user_id is neither GUID nor UPN")
        return None
```

## 📝 Enhanced Logging

### Successful Profile Fetch:
```
INFO: Graph profile: method=app-only endpoint=/users/{idOrUPN} for 12345678...
INFO: Get profile (/users/{idOrUPN}) HTTP 200
INFO: Cached profile for user: John Smith (john@company.com)
INFO: Display name resolved: 'John' for user_id=12345678...
INFO: Starting document crawl for user: John (12345678...)
```

### Profile Fetch Failures:
```
WARNING: get_user_display_name: no user_id provided
WARNING: Graph profile: user_id 'xxx' is neither GUID nor UPN/email
ERROR: Graph profile: failed to acquire app-only token
WARNING: Graph profile: HTTP 404 for user_id 12345678...
```

## ✅ What's Fixed

1. **✅ Consistent User Identification**: Same approach in app.py and knowledge_base.py
2. **✅ Enhanced Error Logging**: Detailed diagnostics when profile fetch fails
3. **✅ Two-Level Caching**: In-memory + disk for performance
4. **✅ Personalized Greetings**: Bot calls users by first name
5. **✅ Secure Document Partitioning**: User-specific cache isolation
6. **✅ Better Error Handling**: Safe string slicing, null checks
7. **✅ Comprehensive Logging**: Track every step of user identification

## 🔍 Debugging Tips

### If bot says "there" instead of user name:

1. **Check the logs** for these patterns:
   ```
   WARNING: get_user_display_name: no user_id provided
   WARNING: no profile found for user_id
   ```

2. **Verify user_id format**:
   - GUID format: `12345678-1234-1234-1234-123456789012`
   - UPN format: `user@domain.com`

3. **Check Graph permissions**:
   - `User.Read.All` (application permission)
   - Admin consent granted

4. **Verify Graph credentials**:
   ```env
   GRAPH_CLIENT_ID=<app-id>
   GRAPH_CLIENT_SECRET=<secret>
   GRAPH_TENANT_ID=<tenant-id>
   ```

5. **Test Graph API directly**:
   ```bash
   # Get token
   curl -X POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token \
     -d "client_id={id}&client_secret={secret}&grant_type=client_credentials&scope=https://graph.microsoft.com/.default"
   
   # Get user profile
   curl https://graph.microsoft.com/v1.0/users/{user-id-or-upn} \
     -H "Authorization: Bearer {token}"
   ```

## 📚 Related Files

- [`src/app.py`](src/app.py) - Main user identity extraction and caching
- [`src/knowledge_base.py`](src/knowledge_base.py) - Graph API calls and display name resolution
- [`src/document_cache.py`](src/document_cache.py) - User-specific document partitioning
- [`src/user_profiles_cache.json`](src/user_profiles_cache.json) - Persistent profile storage

---

**Result**: The bot now consistently identifies users and addresses them by their first name throughout all interactions! 🎉
