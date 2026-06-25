"""App-only Microsoft Graph Planner operations (acting for a specific user).

NOTE on permissions: Planner application-permission support is limited to
group-owned plans and requires Tasks.Read.All / Tasks.ReadWrite.All (admin
consented). Plans are discovered through the user's group memberships, because
``/users/{id}/planner/plans`` is delegated-only. If the app reg lacks Planner
application permission, these calls return a clear error to the model.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from typing import Optional

from graph.client import graph_get, graph_get_all, graph_post, graph_patch, user_segment

logger = logging.getLogger(__name__)


def list_user_groups(user_id: str, *, top: int = 20) -> list[dict]:
    """Return the unified groups the user belongs to (potential plan owners)."""
    seg = user_segment(user_id)
    items = graph_get_all(
        f"{seg}/memberOf?$select=id,displayName,groupTypes",
        max_items=top,
    )
    groups = []
    for g in items:
        if g.get("@odata.type", "").endswith("group") or "groupTypes" in g:
            groups.append({"id": g.get("id"), "name": g.get("displayName") or ""})
    return groups


def _fetch_group_plans(group: dict, top: int) -> list[dict]:
    """Fetch plans for a single group — safe to call from a thread."""
    gid = group.get("id")
    if not gid:
        return []
    try:
        plans = []
        for p in graph_get_all(f"/groups/{gid}/planner/plans?$select=id,title,owner", max_items=top):
            plans.append({
                "id": p.get("id"),
                "title": p.get("title") or "(untitled)",
                "group": group.get("name"),
                "group_id": gid,
            })
        return plans
    except Exception as exc:
        logger.debug("No plans for group %s: %s", gid, exc)
        return []


def list_plans(user_id: str, *, top: int = 25) -> list[dict]:
    """List Planner plans across the user's groups — fetched in parallel."""
    groups = list_user_groups(user_id)
    if not groups:
        return []

    plans: list[dict] = []
    # Fetch plans for all groups in parallel; cap total wall-time at 20 s.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_group_plans, g, top): g for g in groups}
        try:
            for fut in as_completed(futures, timeout=20):
                try:
                    plans.extend(fut.result())
                except Exception as exc:
                    logger.debug("Plan fetch task failed: %s", exc)
                if len(plans) >= top:
                    break
        except FuturesTimeout:
            logger.warning("list_plans: timed out after 20 s, returning partial results (%d plans)", len(plans))

    return plans[:top]


def list_buckets(plan_id: str) -> list[dict]:
    items = graph_get_all(f"/planner/plans/{plan_id}/buckets?$select=id,name", max_items=50)
    return [{"id": b.get("id"), "name": b.get("name") or ""} for b in items]


def list_tasks(plan_id: str, *, top: int = 40) -> list[dict]:
    items = graph_get_all(
        f"/planner/plans/{plan_id}/tasks"
        "?$select=id,title,percentComplete,dueDateTime,bucketId,assignments",
        max_items=top,
    )
    out = []
    for t in items:
        pct = t.get("percentComplete") or 0
        out.append({
            "id": t.get("id"),
            "title": t.get("title") or "(untitled)",
            "status": "completed" if pct >= 100 else ("in progress" if pct > 0 else "not started"),
            "percent_complete": pct,
            "due": t.get("dueDateTime") or "",
            "bucket_id": t.get("bucketId") or "",
            "assignee_count": len(t.get("assignments") or {}),
        })
    return out


def create_task(plan_id: str, title: str, *, bucket_id: Optional[str] = None,
                due: Optional[str] = None, assignee_ids: Optional[list] = None) -> dict:
    """Create a Planner task in a plan (optionally in a bucket / with a due date)."""
    payload: dict = {"planId": plan_id, "title": title or "Untitled task"}
    if bucket_id:
        payload["bucketId"] = bucket_id
    if due:
        payload["dueDateTime"] = due
    if assignee_ids:
        payload["assignments"] = {
            uid: {"@odata.type": "#microsoft.graph.plannerAssignment",
                  "orderHint": " !"}
            for uid in assignee_ids
        }
    created = graph_post("/planner/tasks", payload)
    return {"id": created.get("id"), "title": created.get("title") or title}


def _task_etag(task_id: str) -> str:
    task = graph_get(f"/planner/tasks/{task_id}")
    return task.get("@odata.etag") or ""


def update_task(task_id: str, *, title: Optional[str] = None,
                percent_complete: Optional[int] = None,
                bucket_id: Optional[str] = None, due: Optional[str] = None) -> None:
    """Patch a Planner task. Fetches the required ETag automatically."""
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if percent_complete is not None:
        payload["percentComplete"] = int(percent_complete)
    if bucket_id is not None:
        payload["bucketId"] = bucket_id
    if due is not None:
        payload["dueDateTime"] = due
    if not payload:
        return
    graph_patch(f"/planner/tasks/{task_id}", payload, etag=_task_etag(task_id))


def complete_task(task_id: str) -> None:
    update_task(task_id, percent_complete=100)
