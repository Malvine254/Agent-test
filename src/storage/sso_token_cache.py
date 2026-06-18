"""In-process per-user Teams SSO token cache (Phase 7-Pre).

The bot captures the user's SSO token on each `signin/tokenExchange` invoke and stores
it here; the OBO exchange (get_graph_token_delegated) reads it. TTL 50 min (AAD SSO
tokens expire at 60). Single-instance only — replace with Redis for multi-instance.
"""
from __future__ import annotations

import threading
import time


class SSOTokenCache:
    TTL_SECONDS = 50 * 60

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def set(self, user_id: str, token: str) -> None:
        with self._lock:
            self._store[user_id] = (token, time.time())

    def get(self, user_id: str) -> str | None:
        with self._lock:
            entry = self._store.get(user_id)
            if not entry:
                return None
            token, captured_at = entry
            if time.time() - captured_at > self.TTL_SECONDS:
                del self._store[user_id]
                return None
            return token

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._store.pop(user_id, None)
