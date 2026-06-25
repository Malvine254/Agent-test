"""Live Microsoft Graph + image-generation tools exposed to the LLM.

These tools are *always* available (unlike the code interpreter, which is
intent-gated). They act for the current user via app-only Graph using the
caller's Entra object id captured from the Teams activity. File-producing tools
(image generation) append to a shared artifacts list that the caller delivers
to the user after the turn, exactly like the code interpreter.

Email sending is guarded: ``compose_email`` creates a real Outlook *draft* and
shows it; the message is only sent when the user explicitly confirms and the
model calls ``send_email``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from agent.tools import Artifact
from generation.file_store import download_url, get_artifact_store

logger = logging.getLogger(__name__)


@dataclass
class GraphToolContext:
    """Per-turn context shared with the Graph/image tool handlers."""

    user_id: str = ""
    conversation_id: str = ""
    display_name: str = ""
    artifacts: list[Artifact] = field(default_factory=list)


# Pending email drafts awaiting user confirmation, keyed by conversation id.
# Only drafts created via compose_email in this process can be sent, so the
# model cannot send arbitrary messages without the user seeing them first.
_PENDING_SENDS: dict[str, dict] = {}


def _err(exc: Exception) -> str:
    from graph.client import GraphError

    if isinstance(exc, GraphError):
        if exc.status in (401, 403):
            return ("I don't have permission to do that in Microsoft 365 yet. The "
                    "Graph application permission for this action may not be granted/consented.")
        if exc.status == 404:
            return "I couldn't find that item in Microsoft 365 (it may have been moved or deleted)."
        return f"Microsoft 365 request failed: {exc.message}"
    logger.warning("Graph tool error: %s", exc, exc_info=True)
    return f"That action failed: {exc}"


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ===========================================================================
# Parameter schemas
# ===========================================================================
class SearchEmailParams(BaseModel):
    query: str = Field("", description="Free-text words to find in the email body/subject.")
    sender: str = Field("", description="Filter by sender name or email address.")
    recipient: str = Field("", description="Filter by recipient name or email address.")
    subject: str = Field("", description="Filter by words in the subject line.")
    top: int = Field(10, description="Max messages to return (1-25).")


class MessageIdParams(BaseModel):
    message_id: str = Field(..., description="The id of the email message (from a prior search/read).")


class FindAddressParams(BaseModel):
    name: str = Field(..., description="A person's display name or partial name to resolve to an email address.")


class ComposeEmailParams(BaseModel):
    to: str = Field(..., description="Recipient email address(es), comma-separated.")
    subject: str = Field(..., description="Email subject line.")
    body: str = Field(..., description="Email body text.")
    cc: str = Field("", description="Optional CC email address(es), comma-separated.")


class SendEmailParams(BaseModel):
    draft_id: str = Field("", description="The draft id returned by compose_email. Leave empty to send the most recent pending draft in this chat.")
    confirmed: bool = Field(False, description="Set true ONLY after the user has explicitly confirmed they want the email sent.")


class ReplyEmailParams(BaseModel):
    message_id: str = Field(..., description="The id of the message to reply to.")
    comment: str = Field(..., description="The reply text.")
    reply_all: bool = Field(False, description="Reply to everyone instead of just the sender.")


class ListCalendarParams(BaseModel):
    start: str = Field("", description="ISO start datetime (e.g. 2026-06-24T00:00:00). Defaults to now.")
    end: str = Field("", description="ISO end datetime. Defaults to 7 days after start.")
    top: int = Field(20, description="Max events to return (1-50).")


class CreateEventParams(BaseModel):
    subject: str = Field(..., description="Event/meeting title.")
    start: str = Field(..., description="ISO start datetime, e.g. 2026-06-25T14:00:00.")
    end: str = Field(..., description="ISO end datetime, e.g. 2026-06-25T15:00:00.")
    attendees: str = Field("", description="Attendee email address(es), comma-separated.")
    location: str = Field("", description="Optional location.")
    body: str = Field("", description="Optional agenda/description.")
    is_online: bool = Field(False, description="Create a Teams online meeting.")
    timezone: str = Field("UTC", description="IANA/Windows timezone for the times (default UTC).")


class UpdateEventParams(BaseModel):
    event_id: str = Field(..., description="The id of the event to update.")
    subject: str = Field("", description="New title (leave empty to keep).")
    start: str = Field("", description="New ISO start (leave empty to keep).")
    end: str = Field("", description="New ISO end (leave empty to keep).")
    location: str = Field("", description="New location (leave empty to keep).")
    timezone: str = Field("UTC", description="Timezone for any new times.")


class CancelEventParams(BaseModel):
    event_id: str = Field(..., description="The id of the event to cancel.")
    comment: str = Field("", description="Optional note to attendees.")


class FindMeetingTimesParams(BaseModel):
    attendees: str = Field(..., description="Attendee email address(es), comma-separated.")
    duration_minutes: int = Field(30, description="Meeting length in minutes.")


class PlanIdParams(BaseModel):
    plan_id: str = Field(..., description="The Planner plan id (from list_plans).")


class CreateTaskParams(BaseModel):
    plan_id: str = Field(..., description="The Planner plan id to add the task to.")
    title: str = Field(..., description="Task title.")
    bucket_id: str = Field("", description="Optional bucket id.")
    due: str = Field("", description="Optional ISO due datetime.")


class TaskIdParams(BaseModel):
    task_id: str = Field(..., description="The Planner task id.")


class SearchFilesParams(BaseModel):
    query: str = Field(..., description="Filename or words to search for in the user's OneDrive.")
    top: int = Field(10, description="Max files to return (1-25).")


class EmptyParams(BaseModel):
    pass


class GenerateImageParams(BaseModel):
    prompt: str = Field(..., description="A detailed description of the image to create.")
    size: str = Field("1024x1024", description="One of 1024x1024, 1792x1024, 1024x1792.")


# ===========================================================================
# Tool builder
# ===========================================================================
def build_graph_tools(ctx: GraphToolContext) -> list:
    """Build the always-on Graph + image Function tools bound to ``ctx``."""
    from microsoft_teams.ai.function import Function

    from graph import mail_service, calendar_service, planner_service, people_service, onedrive_service
    from generation import image_gen

    uid = ctx.user_id
    tools: list = []

    # ---- Email ----------------------------------------------------------
    async def _search_email(p: SearchEmailParams) -> str:
        try:
            msgs = await _run(mail_service.search_messages, uid, query=p.query,
                              sender=p.sender, recipient=p.recipient, subject=p.subject, top=p.top)
        except Exception as exc:
            return _err(exc)
        if not msgs:
            return "No matching emails were found."
        lines = [f"Found {len(msgs)} email(s):"]
        for m in msgs:
            flag = "" if m.get("is_read") else " (unread)"
            clip = "📎" if m.get("has_attachments") else ""
            lines.append(
                f"- [{m['id']}] {m['received'][:16]} | from {m['from']} | "
                f"{m['subject']}{flag} {clip}\n    {m['preview'][:160]}"
            )
        return "\n".join(lines)

    async def _read_email(p: MessageIdParams) -> str:
        try:
            m = await _run(mail_service.get_message, uid, p.message_id)
        except Exception as exc:
            return _err(exc)
        return (
            f"Subject: {m['subject']}\nFrom: {m['from']}\nTo: {m['to']}\n"
            f"Cc: {m.get('cc','')}\nReceived: {m['received']}\n"
            f"Has attachments: {m.get('has_attachments')}\n\n{m.get('body','')[:6000]}"
        )

    async def _list_attachments(p: MessageIdParams) -> str:
        try:
            atts = await _run(mail_service.list_attachments, uid, p.message_id)
        except Exception as exc:
            return _err(exc)
        if not atts:
            return "That email has no attachments."
        return "Attachments:\n" + "\n".join(
            f"- [{a['id']}] {a['name']} ({a['content_type']}, {a.get('size',0)} bytes)" for a in atts
        )

    async def _find_address(p: FindAddressParams) -> str:
        try:
            people = await _run(people_service.find_people, uid, p.name)
        except Exception as exc:
            return _err(exc)
        if not people:
            return f"No directory match found for '{p.name}'."
        return "Matches:\n" + "\n".join(
            f"- {pp['name']} <{pp['email']}>" + (f" — {pp['job_title']}" if pp['job_title'] else "")
            for pp in people
        )

    async def _compose_email(p: ComposeEmailParams) -> str:
        try:
            draft = await _run(mail_service.create_draft, uid, to=p.to, subject=p.subject,
                               body=p.body, cc=p.cc or None)
        except Exception as exc:
            return _err(exc)
        _PENDING_SENDS[ctx.conversation_id] = {
            "draft_id": draft["id"], "to": draft["to"], "subject": draft["subject"]
        }
        cc_line = f"\nCc: {draft['cc']}" if draft.get("cc") else ""
        return (
            "DRAFT CREATED (not sent yet). Show this to the user and ask them to confirm "
            "before sending.\n\n"
            f"To: {draft['to']}{cc_line}\nSubject: {draft['subject']}\n\n{p.body}\n\n"
            f"(draft_id: {draft['id']})\n"
            "To send it, call send_email with confirmed=true AFTER the user says yes."
        )

    async def _send_email(p: SendEmailParams) -> str:
        pending = _PENDING_SENDS.get(ctx.conversation_id)
        draft_id = p.draft_id or (pending or {}).get("draft_id")
        if not draft_id:
            return ("There is no prepared draft to send. Use compose_email first, show it to "
                    "the user, and only send after they confirm.")
        if not p.confirmed:
            return ("Not sent. Confirm with the user first, then call send_email with "
                    "confirmed=true.")
        try:
            await _run(mail_service.send_draft, uid, draft_id)
        except Exception as exc:
            return _err(exc)
        _PENDING_SENDS.pop(ctx.conversation_id, None)
        target = (pending or {}).get("to", "")
        return f"Email sent successfully{(' to ' + target) if target else ''}."

    async def _reply_email(p: ReplyEmailParams) -> str:
        try:
            await _run(mail_service.reply, uid, p.message_id, p.comment, reply_all=p.reply_all)
        except Exception as exc:
            return _err(exc)
        return "Reply sent."

    tools += [
        Function[SearchEmailParams](name="search_email", parameter_schema=SearchEmailParams,
            description="Search the user's mailbox by sender, recipient, subject, and/or free-text content. Returns message ids you can pass to read_email.",
            handler=_search_email),
        Function[MessageIdParams](name="read_email", parameter_schema=MessageIdParams,
            description="Read the full body and details of one email by its message id. Use this to summarize a message or extract action items.",
            handler=_read_email),
        Function[MessageIdParams](name="list_email_attachments", parameter_schema=MessageIdParams,
            description="List the attachments on an email message by its id.",
            handler=_list_attachments),
        Function[FindAddressParams](name="find_email_address", parameter_schema=FindAddressParams,
            description="Resolve a person's name to their email address from the directory/contacts.",
            handler=_find_address),
        Function[ComposeEmailParams](name="compose_email", parameter_schema=ComposeEmailParams,
            description="Draft an email (creates an Outlook draft and shows it). Does NOT send. Always show the draft and get the user's explicit confirmation before sending.",
            handler=_compose_email),
        Function[SendEmailParams](name="send_email", parameter_schema=SendEmailParams,
            description="Send the email draft created by compose_email. ONLY call this after the user has explicitly confirmed; pass confirmed=true.",
            handler=_send_email),
        Function[ReplyEmailParams](name="reply_email", parameter_schema=ReplyEmailParams,
            description="Reply (or reply-all) to an email by its message id. Confirm with the user before replying.",
            handler=_reply_email),
    ]

    # ---- Calendar -------------------------------------------------------
    async def _list_calendar(p: ListCalendarParams) -> str:
        start = p.start or _dt.datetime.utcnow().strftime("%Y-%m-%dT00:00:00")
        if p.end:
            end = p.end
        else:
            base = _dt.datetime.utcnow()
            end = (base + _dt.timedelta(days=7)).strftime("%Y-%m-%dT23:59:59")
        try:
            events = await _run(calendar_service.list_events, uid, start=start, end=end, top=p.top)
        except Exception as exc:
            return _err(exc)
        if not events:
            return "No calendar events in that range."
        lines = [f"{len(events)} event(s):"]
        for e in events:
            online = " [Teams]" if e.get("is_online") else ""
            loc = f" @ {e['location']}" if e.get("location") else ""
            lines.append(f"- [{e['id']}] {e['start'][:16]}–{e['end'][11:16]} | {e['subject']}{loc}{online}")
        return "\n".join(lines)

    async def _create_event(p: CreateEventParams) -> str:
        try:
            e = await _run(calendar_service.create_event, uid, subject=p.subject, start=p.start,
                           end=p.end, timezone=p.timezone, attendees=p.attendees or None,
                           body=p.body, location=p.location, is_online=p.is_online)
        except Exception as exc:
            return _err(exc)
        join = f"\nJoin: {e['join_url']}" if e.get("join_url") else ""
        return f"Event created: {e['subject']} ({e['start']} – {e['end']}).{join}"

    async def _update_event(p: UpdateEventParams) -> str:
        try:
            e = await _run(calendar_service.update_event, uid, p.event_id,
                           subject=p.subject or None, start=p.start or None, end=p.end or None,
                           location=p.location or None, timezone=p.timezone)
        except Exception as exc:
            return _err(exc)
        return f"Event updated: {e['subject']} ({e['start']} – {e['end']})."

    async def _cancel_event(p: CancelEventParams) -> str:
        try:
            await _run(calendar_service.cancel_event, uid, p.event_id, p.comment)
        except Exception as exc:
            return _err(exc)
        return "Event cancelled and attendees notified."

    async def _find_times(p: FindMeetingTimesParams) -> str:
        try:
            slots = await _run(calendar_service.find_meeting_times, uid,
                               attendees=p.attendees, duration_minutes=p.duration_minutes)
        except Exception as exc:
            return _err(exc)
        if not slots:
            return "No suitable meeting times were found."
        return "Suggested times:\n" + "\n".join(
            f"- {s['start'][:16]} – {s['end'][11:16]} (confidence {s.get('confidence')})" for s in slots
        )

    tools += [
        Function[ListCalendarParams](name="list_calendar", parameter_schema=ListCalendarParams,
            description="List the user's calendar events in a date range (defaults to the next 7 days).",
            handler=_list_calendar),
        Function[CreateEventParams](name="create_calendar_event", parameter_schema=CreateEventParams,
            description="Create a calendar event/meeting. Set is_online=true for a Teams meeting. Confirm details with the user first.",
            handler=_create_event),
        Function[UpdateEventParams](name="update_calendar_event", parameter_schema=UpdateEventParams,
            description="Update an existing calendar event by id (subject/time/location).",
            handler=_update_event),
        Function[CancelEventParams](name="cancel_calendar_event", parameter_schema=CancelEventParams,
            description="Cancel a calendar event the user organizes, notifying attendees. Confirm first.",
            handler=_cancel_event),
        Function[FindMeetingTimesParams](name="find_meeting_times", parameter_schema=FindMeetingTimesParams,
            description="Suggest meeting time slots that work for the given attendees.",
            handler=_find_times),
    ]

    # ---- Planner --------------------------------------------------------
    async def _list_plans(_p: EmptyParams) -> str:
        try:
            plans = await _run(planner_service.list_plans, uid)
        except Exception as exc:
            return _err(exc)
        if not plans:
            return "No Planner plans were found for the user's groups."
        return "Plans:\n" + "\n".join(f"- [{pl['id']}] {pl['title']} (group: {pl['group']})" for pl in plans)

    async def _list_tasks(p: PlanIdParams) -> str:
        try:
            tasks = await _run(planner_service.list_tasks, p.plan_id)
        except Exception as exc:
            return _err(exc)
        if not tasks:
            return "That plan has no tasks."
        return "Tasks:\n" + "\n".join(
            f"- [{t['id']}] {t['title']} — {t['status']}" + (f" (due {t['due'][:10]})" if t.get('due') else "")
            for t in tasks
        )

    async def _create_task(p: CreateTaskParams) -> str:
        try:
            t = await _run(planner_service.create_task, p.plan_id, p.title,
                           bucket_id=p.bucket_id or None, due=p.due or None)
        except Exception as exc:
            return _err(exc)
        return f"Task created: {t['title']} (id {t['id']})."

    async def _complete_task(p: TaskIdParams) -> str:
        try:
            await _run(planner_service.complete_task, p.task_id)
        except Exception as exc:
            return _err(exc)
        return "Task marked complete."

    tools += [
        Function[EmptyParams](name="list_plans", parameter_schema=EmptyParams,
            description="List the Planner plans available to the user (across their groups).",
            handler=_list_plans),
        Function[PlanIdParams](name="list_plan_tasks", parameter_schema=PlanIdParams,
            description="List the tasks in a Planner plan by plan id.",
            handler=_list_tasks),
        Function[CreateTaskParams](name="create_plan_task", parameter_schema=CreateTaskParams,
            description="Create a task in a Planner plan.",
            handler=_create_task),
        Function[TaskIdParams](name="complete_plan_task", parameter_schema=TaskIdParams,
            description="Mark a Planner task complete by task id.",
            handler=_complete_task),
    ]

    # ---- Files (OneDrive) ----------------------------------------------
    async def _search_files(p: SearchFilesParams) -> str:
        try:
            files = await _run(onedrive_service.search_my_files, uid, p.query, top=p.top)
        except Exception as exc:
            return _err(exc)
        if not files:
            return f"No OneDrive files matched '{p.query}'."
        return "Files:\n" + "\n".join(
            f"- {f['name']} ({f.get('size',0)} bytes) — {f['web_url']}" for f in files
        )

    async def _recent_files(_p: EmptyParams) -> str:
        try:
            files = await _run(onedrive_service.recent_files, uid)
        except Exception as exc:
            return _err(exc)
        if not files:
            return "No recent files were found."
        return "Recent files:\n" + "\n".join(f"- {f['name']} — {f['web_url']}" for f in files)

    tools += [
        Function[SearchFilesParams](name="search_my_onedrive", parameter_schema=SearchFilesParams,
            description="Search the user's own OneDrive for files by name or content keywords.",
            handler=_search_files),
        Function[EmptyParams](name="list_recent_files", parameter_schema=EmptyParams,
            description="List the user's recently accessed OneDrive/Office files.",
            handler=_recent_files),
    ]

    # ---- Image generation ----------------------------------------------
    async def _generate_image(p: GenerateImageParams) -> str:
        try:
            data, provider = await _run(image_gen.generate_image, p.prompt, size=p.size)
        except Exception as exc:
            from generation.image_gen import ImageGenError
            if isinstance(exc, ImageGenError):
                return str(exc)
            return f"Image generation failed: {exc}"
        store = get_artifact_store()
        safe = "".join(c for c in p.prompt[:40] if c.isalnum() or c in " -_").strip() or "image"
        filename = f"{safe.replace(' ', '_')}.png"
        token = store.save_bytes(data, filename)
        art = Artifact(filename=filename, token=token, size=len(data), url=download_url(token))
        ctx.artifacts.append(art)
        return (f"Image generated with {provider} and is being delivered to the user "
                "automatically. Briefly describe what you created; do NOT fabricate a link.")

    tools.append(
        Function[GenerateImageParams](name="generate_image", parameter_schema=GenerateImageParams,
            description="Generate an image from a text description (FLUX/DALL-E). The image file is delivered to the user automatically.",
            handler=_generate_image)
    )

    return tools


def graph_tools_instructions(now_iso: str) -> str:
    """Instruction block appended when Graph/image tools are active."""
    return (
        "\n\n## MICROSOFT 365 & IMAGE TOOLS\n"
        f"The current date/time is {now_iso} (UTC). You can act in the user's Microsoft 365 "
        "on their behalf using these tools:\n"
        "- Email: search_email, read_email, list_email_attachments, find_email_address, "
        "compose_email, send_email, reply_email.\n"
        "- Calendar: list_calendar, create_calendar_event, update_calendar_event, "
        "cancel_calendar_event, find_meeting_times.\n"
        "- Tasks: list_plans, list_plan_tasks, create_plan_task, complete_plan_task.\n"
        "- Files: search_my_onedrive, list_recent_files.\n"
        "- Images: generate_image.\n"
        "Rules:\n"
        "1. SENDING/CHANGING things (send_email, reply_email, create/cancel events, "
        "creating/completing tasks) affects real data. For sending or replying to email, "
        "ALWAYS draft with compose_email first, show it, and only call send_email with "
        "confirmed=true AFTER the user explicitly says to send.\n"
        "2. To extract action items, fetch the relevant emails/messages with search_email + "
        "read_email and list concrete, owner-attributed action items with due dates when present.\n"
        "3. Use find_email_address to resolve names to addresses before composing.\n"
        "4. Use ISO 8601 datetimes for calendar tools; resolve relative dates (e.g. 'tomorrow') "
        "against the current date above.\n"
        "5. Only state an action succeeded if the tool reported success."
    )
