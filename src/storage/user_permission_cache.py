from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class CachedMemberships:
    group_ids: list[str]
    fetched_at: float = field(default_factory=time.time)


class UserPermissionCache:
    """In-process cache of AAD group memberships per user.

    TTL: 300 seconds (5 minutes). Fine for single-instance session use. Replace
    with a shared store (Redis/Table) if the bot ever runs multiple instances.
    """

    TTL = 300

    def __init__(self) -> None:
        self._store: dict[str, CachedMemberships] = {}

    def get(self, user_id: str) -> list[str] | None:
        entry = self._store.get(user_id)
        if entry and (time.time() - entry.fetched_at) < self.TTL:
            return entry.group_ids
        return None

    def set(self, user_id: str, group_ids: list[str]) -> None:
        self._store[user_id] = CachedMemberships(group_ids=group_ids)

    def invalidate(self, user_id: str) -> None:
        self._store.pop(user_id, None)


# Module-level singleton used by the retrieval layer.
user_permission_cache = UserPermissionCache()
