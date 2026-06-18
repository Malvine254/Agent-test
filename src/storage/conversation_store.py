"""Durable conversation state (turns + summary + last_sources) in one store.

Primary backend: Azure Table Storage (set AZURE_STORAGE_CONNECTION_STRING).
Fallback backend: a local JSON file — used when no connection string is set or the
azure-data-tables SDK isn't installed. The fallback is durable across restart (so
local testing works) but NOT shared across instances; production must use Azure Table.

Sync API to match the codebase. save()/get()/delete() never raise.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TABLE_NAME = "conversations"
_MAX_TURNS_BYTES_WARN = 28_000  # Azure Table string property limit is 32KB


@dataclass
class ConversationState:
    turns: list[dict] = field(default_factory=list)        # [{role, content}, ...]
    summary: str = ""                                       # compressed older context
    last_sources: list[dict] = field(default_factory=list)  # most recent retrieval results
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _AzureTableBackend:
    def __init__(self, connection_string: str):
        from azure.data.tables import TableServiceClient

        service = TableServiceClient.from_connection_string(connection_string)
        service.create_table_if_not_exists(TABLE_NAME)
        self._table = service.get_table_client(TABLE_NAME)

    def get(self, conversation_id: str) -> ConversationState:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._table.get_entity(partition_key=conversation_id, row_key="state")
            return ConversationState(
                turns=json.loads(entity.get("turns", "[]")),
                summary=entity.get("summary", ""),
                last_sources=json.loads(entity.get("last_sources", "[]")),
                updated_at=entity.get("updated_at", ""),
            )
        except ResourceNotFoundError:
            return ConversationState()
        except Exception as exc:
            logger.warning("ConversationStore.get failed for %s: %s — empty state", conversation_id, exc)
            return ConversationState()

    def save(self, conversation_id: str, state: ConversationState) -> None:
        try:
            turns_json = json.dumps(state.turns, ensure_ascii=False)
            sources_json = json.dumps(state.last_sources, ensure_ascii=False)
            if len(turns_json) > _MAX_TURNS_BYTES_WARN:
                logger.warning(
                    "ConversationStore: turns payload for %s is %d bytes — approaching 32KB limit. "
                    "Consider reducing MAX_MEMORY_TURNS.",
                    conversation_id,
                    len(turns_json),
                )
            self._table.upsert_entity(
                {
                    "PartitionKey": conversation_id,
                    "RowKey": "state",
                    "turns": turns_json,
                    "summary": (state.summary or "")[:4000],
                    "last_sources": sources_json,
                    "updated_at": _now(),
                }
            )
        except Exception as exc:
            logger.error("ConversationStore.save failed for %s: %s", conversation_id, exc)

    def delete(self, conversation_id: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._table.delete_entity(partition_key=conversation_id, row_key="state")
        except ResourceNotFoundError:
            pass
        except Exception as exc:
            logger.warning("ConversationStore.delete failed for %s: %s", conversation_id, exc)


class _LocalFileBackend:
    """Durable-across-restart JSON file fallback. Not shared across instances."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh) or {}
        except Exception as exc:
            logger.warning("ConversationStore local file load failed (%s): %s — starting empty", path, exc)
            self._data = {}

    def get(self, conversation_id: str) -> ConversationState:
        with self._lock:
            raw = self._data.get(conversation_id)
        if not raw:
            return ConversationState()
        return ConversationState(
            turns=raw.get("turns", []),
            summary=raw.get("summary", ""),
            last_sources=raw.get("last_sources", []),
            updated_at=raw.get("updated_at", ""),
        )

    def save(self, conversation_id: str, state: ConversationState) -> None:
        with self._lock:
            self._data[conversation_id] = {
                "turns": state.turns,
                "summary": (state.summary or "")[:4000],
                "last_sources": state.last_sources,
                "updated_at": _now(),
            }
            try:
                tmp = f"{self._path}.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, ensure_ascii=False)
                os.replace(tmp, self._path)
            except Exception as exc:
                logger.error("ConversationStore local file save failed for %s: %s", conversation_id, exc)

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            if self._data.pop(conversation_id, None) is not None:
                try:
                    tmp = f"{self._path}.tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(self._data, fh, ensure_ascii=False)
                    os.replace(tmp, self._path)
                except Exception as exc:
                    logger.error("ConversationStore local file delete-persist failed for %s: %s", conversation_id, exc)


class ConversationStore:
    """Durable conversation state. Azure Table when configured, else local JSON file."""

    def __init__(self, connection_string: str | None = None, local_path: str | None = None):
        connection_string = (connection_string or "").strip()
        if connection_string:
            try:
                self._backend: object = _AzureTableBackend(connection_string)
                logger.info("ConversationStore: using Azure Table Storage backend")
                return
            except Exception as exc:
                logger.error("Azure Table backend init failed (%s) — falling back to local file", exc)
        if not local_path:
            local_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "conversation_store.json")
        self._backend = _LocalFileBackend(local_path)
        logger.warning(
            "ConversationStore: using LOCAL FILE backend at %s (durable across restart, NOT across instances). "
            "Set AZURE_STORAGE_CONNECTION_STRING for production.",
            local_path,
        )

    def get(self, conversation_id: str) -> ConversationState:
        return self._backend.get(conversation_id)

    def save(self, conversation_id: str, state: ConversationState) -> None:
        self._backend.save(conversation_id, state)

    def delete(self, conversation_id: str) -> None:
        self._backend.delete(conversation_id)
