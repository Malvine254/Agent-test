"""eval.py — Live evaluation harness for the Mela AI Teams Bot.

Architecture
------------
1. Starts a tiny HTTP callback server (port 54321) that captures bot responses.
2. Sends test activities to http://localhost:3978/api/messages — the same endpoint
   Teams uses, but JWT auth is disabled because we're running run_local_playground.py.
3. The bot POSTs its replies to ``serviceUrl/v3/conversations/{id}/activities``.
4. Scores each reply and prints a detailed report.

Usage (from src/ with venv active)
------------------------------------
  python eval.py
  python eval.py --bot-url http://localhost:3978
  python eval.py --tests rag,email,calendar,onedrive,planner,image,upload,compare
  python eval.py --log-file ../app_debug.log   # tail the bot log in real time
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests

BOT_URL = "http://localhost:3978"
CALLBACK_PORT = 54321
BOT_ID = "0d238f90-3533-47dc-a244-47fe4dbf28dc"

# ── Callback server ────────────────────────────────────────────────────────────

_conv_responses: dict[str, list[dict]] = {}
_conv_events: dict[str, threading.Event] = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence access log
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            activity = json.loads(body)
        except Exception:
            activity = {}

        # Extract conversation id from path  /v3/conversations/{convId}/activities
        parts = self.path.strip("/").split("/")
        # parts: ['v3','conversations','{convId}','activities']
        conv_id = parts[2] if len(parts) >= 3 else "unknown"

        if conv_id not in _conv_responses:
            _conv_responses[conv_id] = []
        _conv_responses[conv_id].append(activity)

        if conv_id in _conv_events:
            _conv_events[conv_id].set()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"ok"}')

    def do_GET(self):
        # /health
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def _start_callback_server():
    srv = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ── Activity builder ───────────────────────────────────────────────────────────

def _make_activity(
    text: str,
    conv_id: str,
    aad_oid: str,
    user_name: str = "Eval User",
    attachments: list[dict] | None = None,
) -> dict:
    return {
        "type": "message",
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "channelId": "msteams",
        "serviceUrl": f"http://localhost:{CALLBACK_PORT}",
        "from": {
            "id": aad_oid,
            "name": user_name,
            "aadObjectId": aad_oid,
        },
        "conversation": {
            "id": conv_id,
            "isGroup": False,
            "conversationType": "personal",
        },
        "recipient": {
            "id": BOT_ID,
            "name": "Mela AI",
        },
        "text": text,
        "attachments": attachments or [],
        "entities": [{"type": "clientInfo", "locale": "en-US", "timezone": "UTC"}],
    }


# ── Bot interaction ────────────────────────────────────────────────────────────

def send_and_wait(
    text: str,
    conv_id: str,
    aad_oid: str,
    timeout: int = 45,
    attachments: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Send a message to the bot, wait for the response, return (full_text, activities)."""
    _conv_responses.pop(conv_id, None)
    _conv_events[conv_id] = threading.Event()

    activity = _make_activity(text, conv_id, aad_oid, attachments=attachments)
    try:
        r = requests.post(
            f"{BOT_URL}/api/messages",
            json=activity,
            # Bot processes LLM calls synchronously before returning 200;
            # GPT calls can take 30-90s for complex queries; multi-file compare
            # with background indexing can exceed 120s — use 180s to be safe.
            timeout=180,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code not in (200, 201, 202):
            return f"[HTTP {r.status_code}] {r.text[:200]}", []
    except requests.RequestException as e:
        return f"[CONNECTION ERROR] {e}", []

    # Wait for the bot to post its reply to our callback server
    deadline = time.time() + timeout
    first_content_time: float | None = None
    QUIET_AFTER_FIRST = 4.0  # seconds to keep collecting after first content arrives

    while time.time() < deadline:
        _conv_events[conv_id].wait(timeout=1)
        _conv_events[conv_id].clear()
        acts = _conv_responses.get(conv_id, [])
        # Collect all text-type activities
        texts = [
            a.get("text") or a.get("speak") or ""
            for a in acts
            if (a.get("type") or "").lower() == "message"
        ]
        combined = "\n".join(t for t in texts if t).strip()
        if combined:
            if first_content_time is None:
                first_content_time = time.time()
            # Keep collecting for QUIET_AFTER_FIRST seconds after first content
            # so that progress messages + final LLM response are all captured.
            if (time.time() - first_content_time) >= QUIET_AFTER_FIRST:
                return combined, acts
        # Also check for attachments / cards (Adaptive Card answers)
        for act in acts:
            for att in act.get("attachments") or []:
                if att.get("contentType") == "application/vnd.microsoft.card.adaptive":
                    body_items = (att.get("content") or {}).get("body") or []
                    card_text = " ".join(
                        b.get("text") or "" for b in body_items if isinstance(b, dict)
                    ).strip()
                    if card_text:
                        return card_text, acts

    return "[TIMEOUT — no response within {}s]".format(timeout), []


# ── Scoring helpers ────────────────────────────────────────────────────────────

def _contains_any(text: str, *patterns: str) -> bool:
    tl = text.lower()
    return any(p.lower() in tl for p in patterns)


def _has_citation(text: str) -> bool:
    return bool(re.search(r"\[\[?\d+\]?\]", text)) or bool(re.search(r"\*\*Source", text, re.I))


def _score(response: str, checks: list[tuple[str, bool]], name: str) -> dict:
    passed = [(label, ok) for label, ok in checks if ok]
    failed = [(label, ok) for label, ok in checks if not ok]
    pct = int(100 * len(passed) / max(len(checks), 1))
    grade = "PASS" if len(failed) == 0 else ("PARTIAL" if pct >= 50 else "FAIL")
    snippet = response[:250].replace("\n", " ")
    # Strip non-ASCII characters to avoid CP1252 encoding errors on Windows
    snippet = snippet.encode("ascii", errors="replace").decode("ascii")
    return {
        "name": name,
        "grade": grade,
        "pct": pct,
        "passed": passed,
        "failed": failed,
        "response_snippet": snippet,
    }


# ── Test cases ────────────────────────────────────────────────────────────────

def run_tests(aad_oid: str, selected: set[str] | None = None) -> list[dict]:
    results = []

    def run(tag: str, fn):
        if selected and tag not in selected:
            return
        print(f"\n  [{tag.upper():12}] ", end="", flush=True)
        t0 = time.time()
        result = fn()
        elapsed = time.time() - t0
        result["elapsed"] = round(elapsed, 1)
        results.append(result)
        icon = {"PASS": "PASS", "PARTIAL": "PART", "FAIL": "FAIL"}.get(result["grade"], "????")
        print(f"  [{icon}] {result['grade']:7} ({elapsed:.1f}s)  {result['response_snippet'][:80]}")
        if result["failed"]:
            for label, _ in result["failed"]:
                print(f"              FAIL: {label}")

    # ── 1. Direct answer (no retrieval needed) ────────────────────────────────
    def t_direct():
        cid = f"eval-direct-{uuid.uuid4().hex[:6]}"
        resp, _ = send_and_wait("What is 15 percent of 240?", cid, aad_oid, timeout=30)
        return _score(resp, [
            ("Contains '36'", _contains_any(resp, "36")),
            ("No error message", not _contains_any(resp, "error", "sorry, i couldn")),
        ], "direct_math")

    run("direct", t_direct)

    # ── 2. AI Search RAG ─────────────────────────────────────────────────────
    def t_rag():
        cid = f"eval-rag-{uuid.uuid4().hex[:6]}"
        resp, _ = send_and_wait(
            "Tell me about the Microsoft Copilot Studio licensing guide", cid, aad_oid, timeout=40
        )
        return _score(resp, [
            ("Response not empty/timeout", not resp.startswith("[TIMEOUT")),
            ("Mentions Copilot or licensing", _contains_any(resp, "copilot", "licens", "microsoft")),
            ("Has citations or sources", _has_citation(resp) or _contains_any(resp, "source", "document")),
        ], "rag_ai_search")

    run("rag", t_rag)

    # ── 3. AI Search RAG — company policies ──────────────────────────────────
    def t_policies():
        cid = f"eval-policy-{uuid.uuid4().hex[:6]}"
        resp, _ = send_and_wait("What are the company policies?", cid, aad_oid, timeout=40)
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Relevant content returned", _contains_any(resp, "policy", "polic", "procedures", "rules", "guide")),
            ("Has sources/citations", _has_citation(resp) or _contains_any(resp, "source", "according to", "document")),
        ], "rag_policies")

    run("policies", t_policies)

    # ── 4. Email (Graph tool) ─────────────────────────────────────────────────
    def t_email():
        cid = f"eval-email-{uuid.uuid4().hex[:6]}"
        resp, _ = send_and_wait("Show me my most recent emails", cid, aad_oid, timeout=45)
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Contains email data or subjects", _contains_any(
                resp, "email", "mail", "subject", "from:", "inbox", "message", "received"
            )),
            ("No permission error", not _contains_any(resp, "don't have permission", "access denied", "401", "403")),
        ], "email_graph")

    run("email", t_email)

    # ── 5. Calendar (Graph tool) ──────────────────────────────────────────────
    def t_calendar():
        cid = f"eval-cal-{uuid.uuid4().hex[:6]}"
        resp, _ = send_and_wait("What's on my calendar this week?", cid, aad_oid, timeout=45)
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Contains calendar data", _contains_any(
                resp, "event", "meeting", "calendar", "schedule", "appointment",
                "no events", "nothing scheduled"
            )),
            ("No permission error", not _contains_any(resp, "don't have permission", "401", "403")),
        ], "calendar_graph")

    run("calendar", t_calendar)

    # ── 6. Planner (Graph tool) ───────────────────────────────────────────────
    def t_planner():
        cid = f"eval-planner-{uuid.uuid4().hex[:6]}"
        resp, _ = send_and_wait("Show me my plans and tasks in Planner", cid, aad_oid, timeout=45)
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Contains planner data", _contains_any(
                resp, "plan", "task", "bucket", "planner", "no plans", "no tasks", "assigned"
            )),
            ("No permission error", not _contains_any(resp, "don't have permission", "401", "403")),
        ], "planner_graph")

    run("planner", t_planner)

    # ── 7. OneDrive (Graph tool) ──────────────────────────────────────────────
    def t_onedrive():
        cid = f"eval-od-{uuid.uuid4().hex[:6]}"
        resp, _ = send_and_wait(
            "List my 5 most recent files from my OneDrive — use the list_recent_files tool",
            cid, aad_oid, timeout=45
        )
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Contains file data", _contains_any(
                resp, "file", "document", "onedrive", ".docx", ".xlsx", ".pdf",
                "no files", "recent", "modified", "recent files"
            )),
            ("No permission error", not _contains_any(resp, "don't have permission", "401", "403")),
        ], "onedrive_graph")

    run("onedrive", t_onedrive)

    # ── 8. Image generation ───────────────────────────────────────────────────
    def t_image():
        cid = f"eval-img-{uuid.uuid4().hex[:6]}"
        resp, acts = send_and_wait("Generate a simple image of a blue mountain", cid, aad_oid, timeout=60)
        has_image_url = bool(re.search(r"https?://\S+\.(png|jpg|jpeg|webp)", resp, re.I))
        has_image_md = "![" in resp
        has_image_ref = _contains_any(resp, "image", "generated", "created", "here", "picture", "illustration")
        has_attach = any(
            (a.get("contentType") or "").startswith("image/")
            for act in acts for a in (act.get("attachments") or [])
        )
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Image delivered (url/markdown/attachment)",
             has_image_url or has_image_md or has_attach),
            ("Acknowledgement present", has_image_ref),
        ], "image_generation")

    run("image", t_image)

    # ── 9. File upload & extraction ───────────────────────────────────────────
    def t_upload():
        cid = f"eval-upload-{uuid.uuid4().hex[:6]}"
        # Attach a simple text file inline as a content attachment
        txt_content = "Employee Handbook\n\nVacation Policy: All employees receive 20 days of paid vacation per year.\nRemote Work Policy: Employees may work remotely up to 3 days per week."
        import base64
        encoded = base64.b64encode(txt_content.encode()).decode()
        att = {
            "contentType": "text/plain",
            "name": "employee_handbook.txt",
            "content": encoded,
            "contentUrl": f"data:text/plain;base64,{encoded}",
        }
        resp, _ = send_and_wait(
            "Please summarize this uploaded document",
            cid, aad_oid, timeout=45, attachments=[att],
        )
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Content referenced (vacation/remote/policy)",
             _contains_any(resp, "vacation", "remote", "handbook", "20 days", "3 days", "policy")),
            ("Summary provided (not empty/error)",
             len(resp) > 50 and not _contains_any(resp, "could not", "unable to", "error extracting")),
        ], "file_upload_extract")

    run("upload", t_upload)

    # ── 10. Multi-file comparison ─────────────────────────────────────────────
    def t_compare():
        cid = f"eval-cmp-{uuid.uuid4().hex[:6]}"
        import base64
        doc1 = "Q1 Sales Report\nRegion: North\nTotal Sales: $150,000\nUnits Sold: 300"
        doc2 = "Q1 Sales Report\nRegion: South\nTotal Sales: $210,000\nUnits Sold: 450"
        atts = [
            {
                "contentType": "text/plain",
                "name": f"q1_sales_region_{r}.txt",
                "contentUrl": f"data:text/plain;base64,{base64.b64encode(d.encode()).decode()}",
            }
            for r, d in [("north", doc1), ("south", doc2)]
        ]
        resp, _ = send_and_wait(
            "Compare these two sales reports", cid, aad_oid, timeout=90, attachments=atts
        )
        return _score(resp, [
            ("Not timeout", not resp.startswith("[TIMEOUT")),
            ("Both regions referenced", _contains_any(resp, "north") and _contains_any(resp, "south")),
            # Accept exact numbers OR written forms like "150,000" / "$150K" / "hundred fifty"
            ("Numbers or sales figures referenced",
             _contains_any(resp, "150", "210", "300", "450", "150k", "210k", "hundred", "thousand")),
            ("Comparison framing used",
             _contains_any(resp, "compar", "higher", "lower", "vs", "versus", "differ", "than",
                           "more", "less", "greater", "region")),
        ], "multi_file_compare")

    run("compare", t_compare)

    return results


# ── Report ─────────────────────────────────────────────────────────────────────

def print_report(results: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("EVALUATION REPORT")
    print("=" * 70)
    grades = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
    for r in results:
        g = r["grade"]
        grades[g] = grades.get(g, 0) + 1
        icon = {"PASS": "[PASS]", "PARTIAL": "[PART]", "FAIL": "[FAIL]"}.get(g, "[????]")
        print(f"{icon} {r['name']:25} {r['pct']:3}%  ({r['elapsed']}s)")
        for label, _ in r.get("failed", []):
            print(f"      FAIL: {label}")
    print("-" * 70)
    total = len(results)
    print(
        f"Total: {total}  PASS={grades['PASS']}  PARTIAL={grades['PARTIAL']}  FAIL={grades['FAIL']}"
        f"  Score={int(100*(grades['PASS'] + 0.5*grades['PARTIAL'])/max(total,1))}%"
    )
    print("=" * 70)


# ── Discover real user for Graph tests ─────────────────────────────────────────

def _discover_user() -> tuple[str, str]:
    """Return (aad_object_id, display_name) of a real tenant user.

    Tries in order:
    1. SENDER_UPN env var (set to leonard.mwangi@armely.com)
    2. First enabled member in the tenant directory
    """
    try:
        from dotenv import load_dotenv
        import os
        load_dotenv("../env/.env.local")
        from sharepoint.graph_client import get_graph_access_token
        import requests as _req
        tok = get_graph_access_token()
        headers = {"Authorization": f"Bearer {tok}"}

        # 1. Try SENDER_UPN directly — fastest and most reliable
        upn = os.getenv("SENDER_UPN", "").strip()
        if upn and "@" in upn:
            r = _req.get(
                f"https://graph.microsoft.com/v1.0/users/{upn}",
                headers=headers,
                params={"$select": "id,displayName"},
                timeout=15,
            )
            if r.status_code == 200:
                u = r.json()
                print(f"  [info] Resolved user from SENDER_UPN: {upn}")
                return u["id"], u.get("displayName") or upn
            print(f"  [warn] SENDER_UPN lookup failed ({r.status_code}), falling back to directory")

        # 2. Fall back to first enabled member
        r = _req.get(
            "https://graph.microsoft.com/v1.0/users",
            headers=headers,
            params={
                "$select": "id,displayName,userPrincipalName,mail",
                "$top": "10",
            },
            timeout=15,
        )
        users = (r.json() or {}).get("value") or []
        for u in users:
            uid = u.get("id", "")
            name = u.get("displayName") or u.get("userPrincipalName") or uid
            if uid:
                return uid, name
    except Exception as e:
        print(f"  [warn] Could not auto-discover user: {e}")
    return "test-user-eval-001", "Eval User"


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global BOT_URL, CALLBACK_PORT
    parser = argparse.ArgumentParser(description="Bot evaluation harness")
    parser.add_argument("--bot-url", default=BOT_URL)
    parser.add_argument("--callback-port", type=int, default=CALLBACK_PORT)
    parser.add_argument(
        "--tests",
        default="direct,rag,policies,email,calendar,planner,onedrive,image,upload,compare",
        help="Comma-separated list of tests to run",
    )
    parser.add_argument("--timeout", type=int, default=45, help="Per-test timeout (seconds)")
    args = parser.parse_args()

    BOT_URL = args.bot_url
    CALLBACK_PORT = args.callback_port
    selected = {t.strip().lower() for t in args.tests.split(",")}

    print(f"Bot evaluation — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Bot:      {BOT_URL}/api/messages")
    print(f"Callback: http://localhost:{CALLBACK_PORT}")

    # Verify bot is reachable
    try:
        requests.get(f"{BOT_URL}/api/health", timeout=3)
    except requests.ConnectionError:
        try:
            requests.post(f"{BOT_URL}/api/messages", json={}, timeout=3)
        except requests.ConnectionError:
            print(f"\nERROR: Cannot reach bot at {BOT_URL}")
            print("Make sure run_local_playground.py is running (python run_local_playground.py)")
            sys.exit(1)
        except Exception:
            pass  # Any response means bot is up

    print("\nDiscovering real tenant user for Graph tool tests...")
    aad_oid, display_name = _discover_user()
    print(f"Using: {display_name} ({aad_oid[:8]}...)")

    print("\nStarting callback server...")
    _start_callback_server()
    print(f"Callback server listening on port {CALLBACK_PORT}")

    print(f"\nRunning {len(selected)} test(s):\n")

    results = run_tests(aad_oid, selected)
    print_report(results)


if __name__ == "__main__":
    main()
