# Armely / Mela — Teams SharePoint AI Assistant

A Microsoft Teams bot that answers questions about your organization's SharePoint
documents using Azure OpenAI, backed by an Azure AI Search index that a background
worker keeps in sync with your SharePoint libraries. Supports per-user security
trimming and durable conversation memory.

## Secret management

### Local development

Copy `.env.example` to `.env` and fill in your values.
Do **not** set `AZURE_KEY_VAULT_URL` locally — secrets are read from `.env` directly.

```bash
cp .env.example .env   # then edit .env with your values
```

### Deployed environments (Azure App Service / Container)

Set `AZURE_KEY_VAULT_URL` in App Service → Configuration → Application Settings.
Do **not** set any other secrets in App Settings — they are loaded from Key Vault at
startup. Assign the App Service's managed identity the **Key Vault Secrets User** role
on the vault.

Required Key Vault secrets (names must match exactly):

| Key Vault secret name        | Maps to env var                   |
|------------------------------|-----------------------------------|
| `graph-client-secret`        | `GRAPH_CLIENT_SECRET`             |
| `azure-openai-api-key`       | `AZURE_OPENAI_API_KEY`            |
| `azure-search-admin-key`     | `AZURE_SEARCH_ADMIN_KEY`          |
| `azure-search-query-key`     | `AZURE_SEARCH_QUERY_KEY`          |
| `azure-storage-conn-string`  | `AZURE_STORAGE_CONNECTION_STRING` |
| `bot-app-password`           | `SECRET_BOT_PASSWORD`             |

The startup log prints `Key Vault: loaded N secrets (...)` so you can confirm in any
deployment's logs that every secret was retrieved.

### Search keys

The retriever (reads) uses `AZURE_SEARCH_QUERY_KEY` (create a query-only key in the
Azure Portal); the indexer (writes / index management) uses `AZURE_SEARCH_ADMIN_KEY`.
Never reuse the admin key as the query key in production.

### Indexing scope

`SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN` must be high enough to cover the entire corpus
(default `2000`). If it is too low, later documents are never indexed — and with
security trimming enabled, those unindexed chunks are hidden from everyone.
