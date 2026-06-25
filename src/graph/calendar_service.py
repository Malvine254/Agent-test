"""App-only Microsoft Graph calendar operations (acting for a specific user).

Targets ``/users/{user_id}/...`` with Calendars.ReadWrite application
permission. Datetimes are ISO 8601 strings; callers should pass timezone-aware
values (UTC by default).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from graph.client import graph_get_all, graph_post, graph_patch, graph_delete, user_segment

logger = logging.getLogger(__name__)

_EVENT_SELECT = (
    "id,subject,start,end,location,organizer,attendees,isAllDay,"
    "isOnlineMeeting,onlineMeeting,webLink,bodyPreview"
)


def _attendee_list(attendees) -> list[dict]:
    if isinstance(attendees, str):
        parts = [a.strip() for a in re.split(r"[;,]", attendees) if a.strip()]
    else:
        parts = [str(a).strip() for a in (attendees or []) if str(a).strip()]
    return [{"emailAddress": {"address": a}, "type": "required"} for a in parts]


def _summarize(e: dict) -> dict:
    start = (e.get("start") or {}).get("dateTime") or ""
    end = (e.get("end") or {}).get("dateTime") or ""
    organizer = ((e.get("organizer") or {}).get("emailAddress") or {})
    attendees = [
        (a.get("emailAddress") or {}).get("address", "")
        for a in (e.get("attendees") or [])
    ]
    return {
        "id": e.get("id"),
        "subject": e.get("subject") or "(no subject)",
        "start": start,
        "end": end,
        "all_day": e.get("isAllDay"),
        "location": ((e.get("location") or {}).get("displayName") or ""),
        "organizer": organizer.get("address") or organizer.get("name") or "",
        "attendees": [a for a in attendees if a],
        "is_online": e.get("isOnlineMeeting"),
        "join_url": ((e.get("onlineMeeting") or {}) or {}).get("joinUrl") or "",
        "web_link": e.get("webLink") or "",
        "preview": (e.get("bodyPreview") or "").strip(),
    }


def list_events(user_id: str, *, start: str, end: str, top: int = 20) -> list[dict]:
    """List events between ISO datetimes ``start`` and ``end`` (calendarView)."""
    seg = user_segment(user_id)
    top = max(1, min(int(top or 20), 50))
    url = (
        f"{seg}/calendarView?startDateTime={start}&endDateTime={end}"
        f"&$select={_EVENT_SELECT}&$orderby=start/dateTime&$top={top}"
    )
    items = graph_get_all(url, max_items=top)
    return [_summarize(e) for e in items]


def create_event(user_id: str, *, subject: str, start: str, end: str,
                 timezone: str = "UTC", attendees=None, body: str = "",
                 location: str = "", is_online: bool = False) -> dict:
    """Create a calendar event. Returns the summarized created event."""
    seg = user_segment(user_id)
    payload: dict = {
        "subject": subject or "(no subject)",
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    if body:
        payload["body"] = {"contentType": "Text", "content": body}
    if location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = _attendee_list(attendees)
    if is_online:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"
    created = graph_post(f"{seg}/events", payload)
    return _summarize(created)


def update_event(user_id: str, event_id: str, *, subject: Optional[str] = None,
                 start: Optional[str] = None, end: Optional[str] = None,
                 timezone: str = "UTC", location: Optional[str] = None,
                 body: Optional[str] = None) -> dict:
    """Patch selected fields of an event."""
    seg = user_segment(user_id)
    payload: dict = {}
    if subject is not None:
        payload["subject"] = subject
    if start is not None:
        payload["start"] = {"dateTime": start, "timeZone": timezone}
    if end is not None:
        payload["end"] = {"dateTime": end, "timeZone": timezone}
    if location is not None:
        payload["location"] = {"displayName": location}
    if body is not None:
        payload["body"] = {"contentType": "Text", "content": body}
    updated = graph_patch(f"{seg}/events/{event_id}", payload)
    return _summarize(updated)


def cancel_event(user_id: str, event_id: str, comment: str = "") -> None:
    """Cancel an event the user organizes (notifies attendees)."""
    seg = user_segment(user_id)
    graph_post(f"{seg}/events/{event_id}/cancel", {"comment": comment or ""})


def delete_event(user_id: str, event_id: str) -> None:
    seg = user_segment(user_id)
    graph_delete(f"{seg}/events/{event_id}")


def find_meeting_times(user_id: str, *, attendees, duration_minutes: int = 30,
                       max_candidates: int = 5) -> list[dict]:
    """Suggest meeting times using the findMeetingTimes endpoint."""
    seg = user_segment(user_id)
    att = [
        {"emailAddress": a["emailAddress"], "type": "required"}
        for a in _att_objects(attendees)
    ]
    payload = {
        "attendees": att,
        "meetingDuration": f"PT{int(duration_minutes)}M",
        "maxCandidates": int(max_candidates),
        "minimumAttendeePercentage": 100,
    }
    data = graph_post(f"{seg}/findMeetingTimes", payload)
    suggestions = []
    for s in data.get("meetingTimeSuggestions", []) or []:
        slot = s.get("meetingTimeSlot") or {}
        suggestions.append({
            "start": (slot.get("start") or {}).get("dateTime", ""),
            "end": (slot.get("end") or {}).get("dateTime", ""),
            "confidence": s.get("confidence"),
        })
    return suggestions


def _att_objects(attendees) -> list[dict]:
    if isinstance(attendees, str):
        parts = [a.strip() for a in re.split(r"[;,]", attendees) if a.strip()]
    else:
        parts = [str(a).strip() for a in (attendees or []) if str(a).strip()]
    return [{"emailAddress": {"address": a}} for a in parts]


def respond_to_event(user_id: str, event_id: str, response: str, comment: str = "") -> None:
    """Accept / tentativelyAccept / decline an invite."""
    seg = user_segment(user_id)
    verb = {"accept": "accept", "tentative": "tentativelyAccept",
            "decline": "decline"}.get((response or "").lower(), "accept")
    graph_post(f"{seg}/events/{event_id}/{verb}", {"comment": comment or "", "sendResponse": True})
