from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import Config
from search.ai_search_ingestion import index_all_sharepoint_documents
from search.ai_search_index import ensure_sharepoint_index

logger = logging.getLogger(__name__)


@dataclass
class IndexingWorkerState:
    running: bool = False
    last_started_at: str = ""
    last_finished_at: str = ""
    last_error: str = ""
    last_summary: dict[str, Any] = field(default_factory=dict)
    run_count: int = 0


STATE = IndexingWorkerState()


def get_indexing_status() -> dict[str, Any]:
    return {
        "running": STATE.running,
        "last_started_at": STATE.last_started_at,
        "last_finished_at": STATE.last_finished_at,
        "last_error": STATE.last_error,
        "last_summary": STATE.last_summary,
        "run_count": STATE.run_count,
        "poll_seconds": Config.SHAREPOINT_INDEX_POLL_SECONDS,
        "index_name": Config.AZURE_SEARCH_INDEX_NAME,
    }


def run_indexing_once() -> dict[str, Any]:
    started = time.monotonic()
    STATE.running = True
    STATE.last_started_at = datetime.now(timezone.utc).isoformat()
    STATE.last_error = ""
    try:
        ensure_sharepoint_index()
        summary = index_all_sharepoint_documents()
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
        summary["index_name"] = Config.AZURE_SEARCH_INDEX_NAME
        STATE.last_summary = summary
        STATE.run_count += 1
        logger.info("SharePoint AI Search indexing run completed: %s", summary)
        return summary
    except Exception as exc:
        STATE.last_error = str(exc)
        logger.error("SharePoint AI Search indexing run failed: %s", exc, exc_info=True)
        raise
    finally:
        STATE.running = False
        STATE.last_finished_at = datetime.now(timezone.utc).isoformat()


async def indexing_worker() -> None:
    if not Config.ENABLE_SHAREPOINT_INDEXING:
        logger.info("SharePoint AI Search indexing worker disabled by ENABLE_SHAREPOINT_INDEXING=false")
        return
    if not Config.SHAREPOINT_SITES:
        logger.warning("SharePoint AI Search indexing worker disabled because SHAREPOINT_SITES is empty")
        return

    poll_seconds = max(60, int(Config.SHAREPOINT_INDEX_POLL_SECONDS or 900))
    logger.info(
        "INDEXING WORKER STARTED | index=%s | poll_seconds=%s | run_on_startup=%s",
        Config.AZURE_SEARCH_INDEX_NAME,
        poll_seconds,
        Config.SHAREPOINT_INDEX_RUN_ON_STARTUP,
    )

    first_run = True
    while True:
        if first_run and not Config.SHAREPOINT_INDEX_RUN_ON_STARTUP:
            first_run = False
        else:
            try:
                await asyncio.to_thread(run_indexing_once)
            except Exception:
                pass
            first_run = False
        await asyncio.sleep(poll_seconds)
