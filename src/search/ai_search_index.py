from __future__ import annotations

import logging

from config import Config

logger = logging.getLogger(__name__)


def get_index_client():
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents.indexes import SearchIndexClient

    if not Config.AZURE_SEARCH_ENDPOINT or not Config.AZURE_SEARCH_ADMIN_KEY:
        raise RuntimeError("Azure AI Search endpoint/admin key is missing")
    return SearchIndexClient(Config.AZURE_SEARCH_ENDPOINT, AzureKeyCredential(Config.AZURE_SEARCH_ADMIN_KEY))


def index_exists(index_name: str) -> bool:
    client = get_index_client()
    try:
        client.get_index(index_name)
        return True
    except Exception:
        return False


def build_sharepoint_index():
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="document_id", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SearchableField(name="file_name", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="file_type", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="source_url", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="site_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="drive_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="item_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="folder_path", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="last_modified", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SimpleField(name="indexed_at", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=Config.EMBEDDING_DIMENSIONS,
            vector_search_profile_name="default-vector-profile",
        ),
        SearchableField(name="summary", type=SearchFieldDataType.String),
        SimpleField(name="source_type", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="checksum", type=SearchFieldDataType.String, filterable=True),
        SearchField(name="acl_users", type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
        SearchField(name="acl_groups", type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
        SimpleField(name="acl_everyone", type=SearchFieldDataType.Boolean, filterable=True),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="default-hnsw")],
        profiles=[VectorSearchProfile(name="default-vector-profile", algorithm_configuration_name="default-hnsw")],
    )
    semantic = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name="default-semantic-config",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="content"), SemanticField(field_name="summary")],
                    keywords_fields=[SemanticField(field_name="file_name"), SemanticField(field_name="folder_path")],
                ),
            )
        ]
    )
    return SearchIndex(name=Config.AZURE_SEARCH_INDEX_NAME, fields=fields, vector_search=vector_search, semantic_search=semantic)


def ensure_sharepoint_index(recreate: bool = False) -> None:
    client = get_index_client()
    name = Config.AZURE_SEARCH_INDEX_NAME
    exists = index_exists(name)
    if exists and not (recreate or Config.RECREATE_SEARCH_INDEX):
        logger.info("Azure AI Search index already exists: %s", name)
        return
    if exists:
        if not (recreate or Config.RECREATE_SEARCH_INDEX):
            return
        client.delete_index(name)
        logger.warning("Deleted Azure AI Search index for recreation: %s", name)
    client.create_index(build_sharepoint_index())
    logger.info("Created Azure AI Search index: %s", name)
