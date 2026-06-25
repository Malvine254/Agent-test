"""Azure Key Vault secret loader.

In deployed environments set AZURE_KEY_VAULT_URL; secrets are pulled into os.environ
before config is read. Locally, leave it unset — secrets come from .env files.

NOTE: this module lives at src/keyvault.py (flat module) because `config` is a module
(config.py), not a package, so `config/keyvault.py` is not possible. It is invoked from
the top of config.py (before Config reads secrets at import time) — calling it later
from app.startup() would be too late, since Config captures env values at import.
"""
import logging
import os

logger = logging.getLogger(__name__)

# Maps Key Vault secret names (hyphens only — KV doesn't allow underscores) to the
# environment variable names the rest of the app expects. config.py derives Graph
# credentials from AZURE_CLIENT_ID/AZURE_CLIENT_SECRET first, so mapping
# 'azure-client-secret' is enough to repoint Graph auth in deployed environments.
SECRET_MAP = {
    # Identity / Graph
    "azure-client-secret": "AZURE_CLIENT_SECRET",
    "graph-client-secret": "GRAPH_CLIENT_SECRET",
    "bot-app-password": "SECRET_BOT_PASSWORD",
    # Azure OpenAI / AI Foundry
    "azure-openai-api-key": "AZURE_OPENAI_API_KEY",
    # Azure AI Search
    "azure-search-admin-key": "AZURE_SEARCH_ADMIN_KEY",
    "azure-search-query-key": "AZURE_SEARCH_QUERY_KEY",
    # Document Intelligence
    "document-intelligence-key": "AZURE_DOCUMENT_INTELLIGENCE_KEY",
    # Image generation
    "flux-api-key": "FLUX_API_KEY",
    "azure-dalle-api-key": "AZURE_DALLE_API_KEY",
    # Storage
    "azure-storage-account-key": "AZURE_STORAGE_ACCOUNT_KEY",
    "azure-storage-conn-string": "AZURE_STORAGE_CONNECTION_STRING",
    # Database
    "database-url": "DATABASE_URL",
    "azure-sql-password": "AZURE_SQL_PASSWORD",
}


def load_from_keyvault(vault_url: str) -> None:
    """Pull secrets from Azure Key Vault into os.environ.

    Uses DefaultAzureCredential (managed identity in Azure, `az login` locally). On any
    failure, logs and returns — the app falls back to whatever is already in os.environ.
    """
    if not vault_url:
        logger.debug("AZURE_KEY_VAULT_URL not set — skipping Key Vault load")
        return

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        from azure.core.exceptions import HttpResponseError, ServiceRequestError
    except ImportError:
        logger.warning(
            "azure-identity or azure-keyvault-secrets not installed — cannot load from "
            "Key Vault. Install them or set secrets via env."
        )
        return

    try:
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)

        loaded: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []

        for secret_name, env_key in SECRET_MAP.items():
            try:
                secret = client.get_secret(secret_name)
                if secret.value:
                    os.environ[env_key] = secret.value
                    loaded.append(env_key)
                else:
                    logger.warning("Key Vault: secret '%s' exists but is empty", secret_name)
                    skipped.append(secret_name)
            except HttpResponseError as exc:
                if getattr(exc, "status_code", None) == 404:
                    logger.debug("Key Vault: secret '%s' not found — skipping", secret_name)
                    skipped.append(secret_name)
                else:
                    failed.append(secret_name)
                    logger.warning("Key Vault: failed to fetch '%s': %s", secret_name, exc)

        summary = "Key Vault: loaded=%d %s | skipped=%d | failed=%s" % (
            len(loaded),
            loaded,
            len(skipped),
            failed if failed else "none",
        )
        logger.info(summary)
        # This runs at config import time — before the app configures logging — so the
        # INFO record above can be dropped. Print as well so the line is always visible
        # in startup/deployment logs (App Service captures stdout).
        if not logging.getLogger().hasHandlers():
            print(summary, flush=True)
        if failed:
            logger.error(
                "Key Vault: %d secret(s) failed to load: %s. The bot may fail on "
                "requests that need these credentials.",
                len(failed),
                failed,
            )
    except ServiceRequestError as exc:
        logger.error(
            "Key Vault: network error connecting to %s: %s — falling back to env/file secrets",
            vault_url,
            exc,
        )
    except Exception as exc:
        logger.error("Key Vault: unexpected error: %s — falling back to env/file secrets", exc)
