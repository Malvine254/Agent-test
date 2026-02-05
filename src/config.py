import os

from dotenv import load_dotenv

# Load environment variables from project root:
# 1) .env (base)
# 2) .env.local (overrides)
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
base_env = os.path.join(ROOT_DIR, ".env")
local_env = os.path.join(ROOT_DIR, ".env.local")

# Load base first
load_dotenv(dotenv_path=base_env, override=False)
# Then override with local values if present
load_dotenv(dotenv_path=local_env, override=True)

# Also load environment files under ./env used by TeamsFx tasks/tunnels
ENV_DIR = os.path.join(ROOT_DIR, "env")
env_base2 = os.path.join(ENV_DIR, ".env")
env_local2 = os.path.join(ENV_DIR, ".env.local")
try:
    load_dotenv(dotenv_path=env_base2, override=False)
    load_dotenv(dotenv_path=env_local2, override=True)
    # Also load environment-specific files used by TeamsFx: .env.dev, .env.playground, .env.sandbox
    env_dev2 = os.path.join(ENV_DIR, ".env.dev")
    env_playground2 = os.path.join(ENV_DIR, ".env.playground")
    env_sandbox2 = os.path.join(ENV_DIR, ".env.sandbox")
    if os.path.exists(env_dev2):
        load_dotenv(dotenv_path=env_dev2, override=True)
    if os.path.exists(env_playground2):
        load_dotenv(dotenv_path=env_playground2, override=True)
    if os.path.exists(env_sandbox2):
        load_dotenv(dotenv_path=env_sandbox2, override=True)
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
    APP_PASSWORD = os.environ.get("SECRET_BOT_PASSWORD", "") or os.environ.get("CLIENT_SECRET", "")
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

    # Optional but commonly set in App Service
    WEBSITE_SITE_NAME = os.environ.get("WEBSITE_SITE_NAME", "")
    WEBSITES_CONTAINER_START_TIME_LIMIT = os.environ.get("WEBSITES_CONTAINER_START_TIME_LIMIT", "")
    
    # SharePoint Sites Configuration
    # Comma-separated list of SharePoint site URLs to search
    SHAREPOINT_SITES = os.environ.get("SHAREPOINT_SITES", "")

    # External web sources (public URLs) to fetch and use as context
    EXTERNAL_WEB_SOURCES = os.environ.get("EXTERNAL_WEB_SOURCES", "")

    # Web crawling limits for external sources
    WEB_CRAWL_MAX_PAGES = int(os.environ.get("WEB_CRAWL_MAX_PAGES", "200"))
    WEB_CRAWL_MAX_DEPTH = int(os.environ.get("WEB_CRAWL_MAX_DEPTH", "5"))

    # Resilience and performance tuning
    HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))  # seconds
    GRAPH_TIMEOUT = int(os.environ.get("GRAPH_TIMEOUT", "90"))  # seconds (increased from 60 for slow networks)
    RETRY_MAX_RETRIES = int(os.environ.get("RETRY_MAX_RETRIES", "3"))
    RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "0.8"))  # seconds
    RETRY_MAX_DELAY = float(os.environ.get("RETRY_MAX_DELAY", "8"))  # seconds
    RETRY_STATUS_FORCELIST = os.environ.get("RETRY_STATUS_FORCELIST", "429,502,503,504")
    # Minimum acceptable cache score (0-100). If top cached result is below this,
    # trigger AI Search as a fallback. Higher means more aggressive AI Search.
    MIN_CACHED_SCORE_BEFORE_AI = int(os.environ.get("MIN_CACHED_SCORE_BEFORE_AI", "55"))
    # Minimum acceptable cache score (0-100). If top cached result meets/exceeds this,
    # return cache immediately and skip live Graph search.
    MIN_CACHED_SCORE_BEFORE_GRAPH = int(os.environ.get("MIN_CACHED_SCORE_BEFORE_GRAPH", "30"))
    
    # Bot Framework API rate limit protection
    # Minimum interval (seconds) between streaming chunk emissions to prevent Teams API 429 errors
    STREAM_CHUNK_INTERVAL = float(os.environ.get("STREAM_CHUNK_INTERVAL", "0.3"))  # 300ms default
    
    # Global LLM concurrency limiter (serialize OpenAI calls to reduce 429s)
    LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "1"))

    # Context size limits to control prompt tokens
    # Maximum characters from any single document included in LLM context
    # Set high to include full documents - LLM will handle token limits
    MAX_DOC_CONTEXT_CHARS = int(os.environ.get("MAX_DOC_CONTEXT_CHARS", "50000"))
    # Soft maximum total characters contributed by document contents
    MAX_TOTAL_CONTEXT_CHARS = int(os.environ.get("MAX_TOTAL_CONTEXT_CHARS", "150000"))

    # Approximate token budgets (roughly ~4 chars/token)
    # GPT-4.1 supports 128K tokens (~500KB text) - set high for full document extraction
    MAX_PROMPT_TOKENS_APPROX = int(os.environ.get("MAX_PROMPT_TOKENS_APPROX", "120000"))
    MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "480000"))
    MAX_COMPLETION_TOKENS = int(os.environ.get("MAX_COMPLETION_TOKENS", "8192"))
    
    # Limits for LLM input sections - maximize for large file support
    MAX_DOCS = int(os.environ.get("MAX_DOCS", "20"))
    MAX_SNIPPET_CHARS = int(os.environ.get("MAX_SNIPPET_CHARS", "100000"))
    MAX_ATTACH_CHARS = int(os.environ.get("MAX_ATTACH_CHARS", "450000"))
    MAX_WEB_CHARS = int(os.environ.get("MAX_WEB_CHARS", "0"))  # 0 = no limit on web content
    MAX_MEMORY_TURNS = int(os.environ.get("MAX_MEMORY_TURNS", "20"))

    # Optional: Always call AI Search regardless of cache heuristics
    ALWAYS_CALL_AI_SEARCH = os.environ.get("ALWAYS_CALL_AI_SEARCH", "false").strip().lower() in ("1", "true", "yes")
    # Safety: don't infer user identity from document cache unless explicitly enabled
    ALLOW_CACHE_USER_INFERENCE = os.environ.get("ALLOW_CACHE_USER_INFERENCE", "false").strip().lower() in ("1", "true", "yes")
    # Disable external APIs (AI Search & Graph) when attachments are present
    DISABLE_APIS_ON_ATTACHMENTS = os.environ.get("DISABLE_APIS_ON_ATTACHMENTS", "false").strip().lower() in ("1", "true", "yes")
    
    # PERFORMANCE: Skip Graph/AI Search for follow-up questions when cached attachments exist
    # Reduces latency from ~8-10s to <2s for questions about recently uploaded files
    # Set to "false" if you want to always search external sources
    SKIP_SEARCH_FOR_CACHED_FOLLOWUPS = os.environ.get("SKIP_SEARCH_FOR_CACHED_FOLLOWUPS", "true").strip().lower() in ("1", "true", "yes")
    
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
    def get_external_web_sources(cls) -> list[str]:
        """Parse external web sources (URLs) into a list."""
        if not cls.EXTERNAL_WEB_SOURCES:
            return []
        urls = [u.strip() for u in cls.EXTERNAL_WEB_SOURCES.split(",")]
        return [u for u in urls if u]

    @classmethod
    def get_web_crawl_limits(cls) -> tuple[int, int]:
        """Return (max_pages, max_depth) for crawling external web sources."""
        return cls.WEB_CRAWL_MAX_PAGES, cls.WEB_CRAWL_MAX_DEPTH

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
