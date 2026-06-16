import os

from dotenv import dotenv_values

# Load environment variables from project root:
# 1) .env (base)
# 2) .env.local (overrides)
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
base_env = os.path.join(ROOT_DIR, ".env")
local_env = os.path.join(ROOT_DIR, ".env.local")

def _load_env_file(path: str, *, override: bool) -> None:
    """Load dotenv values without letting blank entries erase valid settings."""
    if not os.path.exists(path):
        return

    for key, value in dotenv_values(path).items():
        if value in (None, ""):
            continue
        if override or not os.environ.get(key):
            os.environ[key] = value


# Load base first, then local overrides.
_load_env_file(base_env, override=False)
_load_env_file(local_env, override=True)

# Also load the selected TeamsFx environment under ./env. Loading every
# env/.env.* file lets dev/playground values overwrite local bot credentials.
ENV_DIR = os.path.join(ROOT_DIR, "env")
try:
    _load_env_file(os.path.join(ENV_DIR, ".env"), override=False)

    teamsfx_env = os.environ.get("TEAMSFX_ENV", "local").strip() or "local"
    for suffix in (teamsfx_env, f"{teamsfx_env}.user"):
        _load_env_file(os.path.join(ENV_DIR, f".env.{suffix}"), override=True)
except Exception:
    pass


def _require_env(key: str) -> str:
    """Fetch a required environment variable with a clearer error."""
    value = os.environ.get(key)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value

class Config:
    """Bot Configuration"""

    PORT = 3978
    APP_ID = os.environ.get("BOT_ID") or os.environ.get("CLIENT_ID", "")
    # Prefer user-secret overrides; TeamsFx may leave stale BOT_PASSWORD values in env files.
    APP_PASSWORD = os.environ.get("SECRET_BOT_PASSWORD", "") or os.environ.get("CLIENT_SECRET", "") or os.environ.get("BOT_PASSWORD", "")
    APP_TYPE = os.environ.get("BOT_TYPE", "MultiTenant")
    APP_TENANTID = os.environ.get("TEAMS_APP_TENANT_ID") or os.environ.get("TENANT_ID", "")
    GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", APP_ID)
    GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", APP_PASSWORD)
    GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID", APP_TENANTID)
    SENDER_UPN = os.environ.get("SENDER_UPN", "")
    BOT_DOMAIN = os.environ.get("BOT_DOMAIN", "")
    BOT_ENDPOINT = os.environ.get("BOT_ENDPOINT", "")

    # Timezone for date/time display (IANA tz like "UTC", "America/Chicago")
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "UTC")

    # No default sender mailbox is used anymore. Sender defaults to current user when possible.

    # Required: Azure OpenAI configuration
    AZURE_OPENAI_API_KEY = _require_env("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_MODEL_DEPLOYMENT_NAME = _require_env("AZURE_OPENAI_MODEL_DEPLOYMENT_NAME")
    AZURE_OPENAI_ENDPOINT = _require_env("AZURE_OPENAI_ENDPOINT")
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))

    # Azure AI Search is the primary retrieval layer for indexed SharePoint documents.
    AZURE_SEARCH_ENDPOINT = os.environ.get("AZURE_SEARCH_ENDPOINT", "")
    AZURE_SEARCH_ADMIN_KEY = os.environ.get("AZURE_SEARCH_ADMIN_KEY", "")
    AZURE_SEARCH_QUERY_KEY = os.environ.get("AZURE_SEARCH_QUERY_KEY", AZURE_SEARCH_ADMIN_KEY)
    AZURE_SEARCH_INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX_NAME", "sharepoint-documents")
    AZURE_SEARCH_API_VERSION = os.environ.get("AZURE_SEARCH_API_VERSION", "2025-09-01")
    AZURE_SEARCH_SEMANTIC_CONFIG = os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIG", "default-semantic-config")
    RECREATE_SEARCH_INDEX = os.environ.get("RECREATE_SEARCH_INDEX", "false").strip().lower() in ("1", "true", "yes")
    SHAREPOINT_INDEX_POLL_SECONDS = int(os.environ.get("SHAREPOINT_INDEX_POLL_SECONDS", "900"))
    SHAREPOINT_INDEX_RUN_ON_STARTUP = os.environ.get("SHAREPOINT_INDEX_RUN_ON_STARTUP", "true").strip().lower() in ("1", "true", "yes")
    SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN = int(os.environ.get("SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN", "200"))
    SHAREPOINT_INDEX_MAX_DEPTH = int(os.environ.get("SHAREPOINT_INDEX_MAX_DEPTH", "8"))

    # Optional but commonly set in App Service
    WEBSITE_SITE_NAME = os.environ.get("WEBSITE_SITE_NAME", "")
    WEBSITES_CONTAINER_START_TIME_LIMIT = os.environ.get("WEBSITES_CONTAINER_START_TIME_LIMIT", "")
    
    # SharePoint Sites Configuration
    # Comma-separated list of SharePoint site URLs to search
    SHAREPOINT_SITES = os.environ.get("SHAREPOINT_SITES", "")

    # Data-source policy. SharePoint is indexed into Azure AI Search; Teams uploads stay local.
    DATA_SOURCE_MODE = os.environ.get("DATA_SOURCE_MODE", "sharepoint_ai_search_uploads_only").strip().lower()
    ENABLE_SHAREPOINT_INDEXING = os.environ.get("ENABLE_SHAREPOINT_INDEXING", "true").strip().lower() in ("1", "true", "yes")
    ENABLE_AI_SEARCH = True
    ENABLE_UPLOADS = True
    ENABLE_WEB_SEARCH = False
    ENABLE_WEB_INDEXER = False
    ENABLE_ONEDRIVE_SEARCH = False
    ENABLE_LOCAL_JSON_SEARCH = False
    ENABLE_SHAREPOINT_SEARCH = False
    ENABLE_SHAREPOINT_GRAPH_ANSWERING = False
    ENABLE_SHAREPOINT_CACHE = False
    ENABLE_SHAREPOINT_STARTUP_CRAWL = False
    ENABLE_LIVE_GRAPH_FALLBACK = False
    SHAREPOINT_CACHE_FIRST = False
    SHAREPOINT_CACHE_MIN_SCORE = int(os.environ.get("SHAREPOINT_CACHE_MIN_SCORE", "30"))
    SHAREPOINT_CACHE_USER_ID = os.environ.get("SHAREPOINT_CACHE_USER_ID", "shared")
    SHAREPOINT_CRAWL_MAX_ITEMS_PER_DRIVE = int(os.environ.get("SHAREPOINT_CRAWL_MAX_ITEMS_PER_DRIVE", "300"))
    SHAREPOINT_CRAWL_MAX_DEPTH = int(os.environ.get("SHAREPOINT_CRAWL_MAX_DEPTH", "6"))

    # Resilience and performance tuning
    HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "8"))  # seconds
    GRAPH_TIMEOUT = int(os.environ.get("GRAPH_TIMEOUT", "8"))  # seconds - fast-fail for Graph API calls
    CACHE_LOAD_TIMEOUT = int(os.environ.get("CACHE_LOAD_TIMEOUT", "2"))  # seconds for loading cache files
    PROFILE_LOOKUP_TIMEOUT = int(os.environ.get("PROFILE_LOOKUP_TIMEOUT", "2"))  # seconds for Graph profile lookups
    USER_DETAILS_TIMEOUT = int(os.environ.get("USER_DETAILS_TIMEOUT", "1"))  # seconds for user details disk I/O
    ATTACHMENT_CHECK_TIMEOUT = int(os.environ.get("ATTACHMENT_CHECK_TIMEOUT", "1"))  # seconds for attachment checks
    CONVERSATION_HISTORY_TIMEOUT = int(os.environ.get("CONVERSATION_HISTORY_TIMEOUT", "1"))  # seconds for conversation history lookups
    RETRY_MAX_RETRIES = int(os.environ.get("RETRY_MAX_RETRIES", "1"))  # fail faster
    RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "0.5"))  # seconds
    RETRY_MAX_DELAY = float(os.environ.get("RETRY_MAX_DELAY", "4"))  # seconds
    RETRY_STATUS_FORCELIST = os.environ.get("RETRY_STATUS_FORCELIST", "429,502,503,504")
    # Minimum acceptable cache score (0-100). If top cached result is below this,
    # trigger AI Search as a fallback. Higher means more aggressive AI Search.
    MIN_CACHED_SCORE_BEFORE_AI = int(os.environ.get("MIN_CACHED_SCORE_BEFORE_AI", "55"))
    # Minimum acceptable cache score (0-100). If top cached result meets/exceeds this,
    # return cache immediately and skip live Graph search.
    MIN_CACHED_SCORE_BEFORE_GRAPH = int(os.environ.get("MIN_CACHED_SCORE_BEFORE_GRAPH", "30"))
    # Keep searching beyond cache until several distinct source documents are available.
    # This avoids a single high-scoring cached file hiding other relevant matches.
    MIN_SEARCH_RESULTS_BEFORE_GRAPH = int(os.environ.get("MIN_SEARCH_RESULTS_BEFORE_GRAPH", "3"))
    REQUIRE_MULTI_DOCUMENT_SEARCH = os.environ.get("REQUIRE_MULTI_DOCUMENT_SEARCH", "true").strip().lower() in ("1", "true", "yes")
    
    # Bot Framework API rate limit protection
    # Minimum interval (seconds) between streaming chunk emissions to prevent Teams API 429 errors
    STREAM_CHUNK_INTERVAL = float(os.environ.get("STREAM_CHUNK_INTERVAL", "0.15"))  # fast streaming, still rate-limit aware
    
    # Global LLM concurrency limiter (serialize OpenAI calls to reduce 429s)
    LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "1"))

    # Context size limits to control prompt tokens
    # Maximum characters from any single document included in LLM context
    MAX_DOC_CONTEXT_CHARS = int(os.environ.get("MAX_DOC_CONTEXT_CHARS", "12000"))  # reduced for faster LLM processing
    # Soft maximum total characters contributed by document contents
    MAX_TOTAL_CONTEXT_CHARS = int(os.environ.get("MAX_TOTAL_CONTEXT_CHARS", "35000"))  # reduced from 100000
    
    # Search result limits for OneDrive/SharePoint
    # Maximum results to return from Graph API search
    MAX_GRAPH_SEARCH_RESULTS = int(os.environ.get("MAX_GRAPH_SEARCH_RESULTS", "25"))
    # Maximum results per query in parallel search
    MAX_RESULTS_PER_QUERY = int(os.environ.get("MAX_RESULTS_PER_QUERY", "10"))  # reduced from 20

    # Approximate token budgets - GPT-5.2-chat optimized (272K token limit observed)
    # Conservative limits to prevent crashes on follow-up questions with attachments
    MAX_PROMPT_TOKENS_APPROX = int(os.environ.get("MAX_PROMPT_TOKENS_APPROX", "30000"))  # reduced
    MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "60000"))  # reduced from 120000 for faster LLM
    MAX_COMPLETION_TOKENS = int(os.environ.get("MAX_COMPLETION_TOKENS", "768"))
    
    # === LLM Context/Snippet/Attachment Limits (Token Overflow Protection) ===
    # These limits prevent token overflow by controlling content injected into LLM prompts
    MAX_LLM_CONTEXT_CHARS = int(os.environ.get("MAX_LLM_CONTEXT_CHARS", "10000"))
    MAX_DOC_SNIPPET_CHARS = int(os.environ.get("MAX_DOC_SNIPPET_CHARS", "1500"))  # per doc in LLM
    MAX_ATTACH_SNIPPET_CHARS = int(os.environ.get("MAX_ATTACH_SNIPPET_CHARS", "1000"))
    MAX_TOTAL_CONTEXT_CHARS = int(os.environ.get("MAX_TOTAL_CONTEXT_CHARS", "12000"))  # Total doc budget
    MAX_LLM_EXPOSURE_CHARS = int(os.environ.get("MAX_LLM_EXPOSURE_CHARS", "1500"))
    
    # File size limits
    MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
    MAX_CONTENT_SIZE_CHARS = int(os.environ.get("MAX_CONTENT_SIZE_CHARS", "5000000"))  # 5M chars for storage
    MAX_EXTRACTED_CHARS = int(os.environ.get("MAX_EXTRACTED_CHARS", "200000"))  # 200K chars extraction limit (was 20M)
    
    # Cache limits
    MAX_CACHE_SIZE_MB = int(os.environ.get("MAX_CACHE_SIZE_MB", "500"))
    MAX_MEMORY_CACHE_ITEMS = int(os.environ.get("MAX_MEMORY_CACHE_ITEMS", "100"))
    MAX_CONVERSATION_ATTACHMENTS = int(os.environ.get("MAX_CONVERSATION_ATTACHMENTS", "8"))
    
    # Core LLM prompt limits (safe defaults)
    MAX_DOCS = int(os.environ.get("MAX_DOCS", "3"))  # max docs to include in LLM context
    MAX_SNIPPET_CHARS = int(os.environ.get("MAX_SNIPPET_CHARS", "1500"))
    MAX_ATTACH_CHARS = int(os.environ.get("MAX_ATTACH_CHARS", "6000"))
    MAX_LLM_ATTACH_CHARS = int(os.environ.get("MAX_LLM_ATTACH_CHARS", "12000"))
    MAX_MEMORY_TURNS = int(os.environ.get("MAX_MEMORY_TURNS", "1"))  # 1 turn = 2 messages

    # Safety: don't infer user identity from document cache unless explicitly enabled
    ALLOW_CACHE_USER_INFERENCE = os.environ.get("ALLOW_CACHE_USER_INFERENCE", "false").strip().lower() in ("1", "true", "yes")
    # Disable external APIs (AI Search & Graph) when attachments are present
    DISABLE_APIS_ON_ATTACHMENTS = os.environ.get("DISABLE_APIS_ON_ATTACHMENTS", "false").strip().lower() in ("1", "true", "yes")
    
    # PERFORMANCE: Skip Graph/AI Search for follow-up questions when cached attachments exist
    # Reduces latency from ~8-10s to <2s for questions about recently uploaded files
    # Set to "false" if you want to always search external sources
    SKIP_SEARCH_FOR_CACHED_FOLLOWUPS = os.environ.get("SKIP_SEARCH_FOR_CACHED_FOLLOWUPS", "true").strip().lower() in ("1", "true", "yes")
    # Allow retrying Graph search with app-only token when delegated search returns zero results
    GRAPH_ALLOW_APP_ONLY_FALLBACK = os.environ.get("GRAPH_ALLOW_APP_ONLY_FALLBACK", "true").strip().lower() in ("1", "true", "yes")
    
    @classmethod
    def get_retry_status_forcelist(cls) -> set[int]:
        try:
            return {int(s.strip()) for s in cls.RETRY_STATUS_FORCELIST.split(',') if s.strip()}
        except Exception:
            return {429, 502, 503, 504}
    
    @classmethod
    def get_sharepoint_sites_list(cls) -> list[str]:
        """Parse SharePoint sites from config into a list."""
        if not cls.SHAREPOINT_SITES:
            return []
        # Split by comma and strip whitespace
        sites = [site.strip() for site in cls.SHAREPOINT_SITES.split(",")]
        # Filter out empty strings
        return [site for site in sites if site]

    @classmethod
    @classmethod
    def validate(cls) -> None:
        """Validate required identity settings early to surface misconfig."""
        if cls.APP_TYPE == "UserAssignedMsi":
            _require_env("CLIENT_ID")  # MSI client ID must be present
        else:
            _require_env("CLIENT_SECRET")
            _require_env("CLIENT_ID")
            _require_env("TENANT_ID")

# File Upload Restrictions
Config.MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "10"))  # 10MB default - prevents memory crashes
Config.ALLOWED_FILE_TYPES = {
    # Documents
    '.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
    # Text files
    '.txt', '.csv', '.json', '.xml', '.md',
    # Images
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff',
    # Archives (optional)
    '.zip', '.rar'
}
Config.BLOCKED_FILE_TYPES = {
    # Executable files
    '.exe', '.bat', '.cmd', '.com', '.scr', '.msi',
    # Script files
    '.ps1', '.vbs', '.js', '.jar',
    # System files
    '.dll', '.sys', '.ini'
}
