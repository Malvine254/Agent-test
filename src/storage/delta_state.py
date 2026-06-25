"""Persistent storage for Microsoft Graph drive delta tokens.

The indexer runs in a separate process (ProcessPoolExecutor), so delta tokens must
persist to disk between runs. Each SharePoint drive has its own delta link; storing it
lets subsequent indexing runs fetch only changed/deleted items instead of re-walking the
entire drive every time.
"""
from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "delta_state.json")
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("delta_state: failed to read %s (%s) — treating as empty", _STATE_PATH, exc)
        return {}


def get_delta_link(drive_id: str) -> str | None:
    if not drive_id:
        return None
    with _LOCK:
        return _load().get(drive_id) or None


def set_delta_link(drive_id: str, delta_link: str | None) -> None:
    if not drive_id:
        return
    with _LOCK:
        data = _load()
        if delta_link:
            data[drive_id] = delta_link
        else:
            data.pop(drive_id, None)
        try:
            tmp = f"{_STATE_PATH}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, _STATE_PATH)
        except Exception as exc:
            logger.warning("delta_state: failed to persist delta link for %s: %s", drive_id, exc)


def clear_all() -> None:
    """Drop all stored delta tokens (forces the next run to do a full delta walk)."""
    with _LOCK:
        try:
            os.remove(_STATE_PATH)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("delta_state: failed to clear state: %s", exc)
