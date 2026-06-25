from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import Config
from search.ai_search_ingestion import index_sharepoint_delta
from search.ai_search_index import ensure_sharepoint_index

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process pool — runs the indexer in a *separate process* so that pypdf's
# CPU-bound, GIL-holding text extraction cannot starve the asyncio event loop.
# We keep max_workers=1 so only one indexing run can happen at a time.
# ---------------------------------------------------------------------------
_index_executor: concurrent.futures.ProcessPoolExecutor | None = None


def _get_index_executor() -> concurrent.futures.ProcessPoolExecutor:
    global _index_executor
    if _index_executor is None:
        _index_executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
    return _index_executor


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


def _run_indexing_subprocess() -> dict[str, Any]:
    """Subprocess entry point — runs indexing without touching the parent STATE.

    This function is called in a separate process by ProcessPoolExecutor.  It
    re-initialises everything it needs from os.environ (inherited on spawn) and
    returns a summary dict that the parent uses to update STATE.
    """
    started = time.monotonic()
    ensure_sharepoint_index()
    summary = index_sharepoint_delta()
    summary["duration_seconds"] = round(time.monotonic() - started, 2)
    summary["index_name"] = Config.AZURE_SEARCH_INDEX_NAME
    return summary


def run_indexing_once() -> dict[str, Any]:
    """Synchronous wrapper used only for non-subprocess callers (tests, admin endpoints)."""
    started = time.monotonic()
    STATE.running = True
    STATE.last_started_at = datetime.now(timezone.utc).isoformat()
    STATE.last_error = ""
    try:
        summary = _run_indexing_subprocess()
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
    # Track the running executor future so we never queue up a second run while
    # the previous subprocess is still in-flight (prevents unbounded backlog).
    _active_future: asyncio.Future | None = None

    while True:
        if first_run and not Config.SHAREPOINT_INDEX_RUN_ON_STARTUP:
            first_run = False
        else:
            # Skip this cycle if the previous subprocess is still running.
            if _active_future is not None and not _active_future.done():
                logger.warning(
                    "INDEXING WORKER | previous run still in progress — skipping this cycle"
                )
            else:
                STATE.running = True
                STATE.last_started_at = datetime.now(timezone.utc).isoformat()
                STATE.last_error = ""
                try:
                    loop = asyncio.get_running_loop()
                    executor = _get_index_executor()
                    # _run_indexing_subprocess runs in a separate OS process, so
                    # pypdf's GIL-heavy PDF extraction cannot starve the event loop.
                    # Hard timeout: 10 minutes per run.
                    _active_future = loop.run_in_executor(executor, _run_indexing_subprocess)
                    summary = await asyncio.wait_for(
                        asyncio.shield(_active_future),
                        timeout=600.0,
                    )
                    STATE.last_summary = summary
                    STATE.run_count += 1
                    logger.info("INDEXING WORKER | run completed: %s", summary)
                    _active_future = None
                except asyncio.TimeoutError:
                    STATE.last_error = "timed out after 600s"
                    logger.warning(
                        "INDEXING WORKER | run timed out after 600s — subprocess continues "
                        "in background, will not start new run until it finishes"
                    )
                except Exception as exc:
                    STATE.last_error = str(exc)
                    _active_future = None
                    logger.error("INDEXING WORKER | run failed: %s", exc, exc_info=True)
                finally:
                    if _active_future is None or _active_future.done():
                        STATE.running = False
                        STATE.last_finished_at = datetime.now(timezone.utc).isoformat()
            first_run = False
        await asyncio.sleep(poll_seconds)

