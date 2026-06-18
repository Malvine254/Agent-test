"""Azure AI Search integration for indexed SharePoint documents."""

from .ai_search_retriever import search_ai_index, search_sharepoint_chunks

__all__ = ["search_ai_index", "search_sharepoint_chunks"]
