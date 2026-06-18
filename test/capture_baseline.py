"""One-off: capture pre-refactor response baseline for Phase 5 regression checks.
Sends baseline queries through the running bot (port 3978), reads the bot's final
reply from the capture server log, and writes test/baseline_responses.txt.
Not part of the app — a test harness helper.
"""
import json
import os
import time
import urllib.request

REPLIES = r"C:\tmp\replies.jsonl"
BOT = "http://localhost:3978/api/messages"
AAD = "926b0fe1-cf51-40ad-bfb3-f941e758e4f0"


def _send(conv_id, mid, text):
    act = {
        "type": "message", "id": mid, "timestamp": "2026-06-18T17:00:00Z",
        "serviceUrl": "http://localhost:3979", "channelId": "msteams",
        "from": {"id": "u", "name": "Edgar", "aadObjectId": AAD},
        "conversation": {"conversationType": "personal", "id": conv_id, "tenantId": "588cadf4-9902-4465-86c0-8bcf04f4f102"},
        "recipient": {"id": "8077e820-3063-4981-9fb6-b281b28c854b", "name": "Armely AI"},
        "locale": "en-US", "text": text,
        "channelData": {"tenant": {"id": "588cadf4-9902-4465-86c0-8bcf04f4f102"}},
    }
    req = urllib.request.Request(BOT, data=json.dumps(act).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=90)
    except Exception:
        pass  # reply is captured async via the capture server regardless


def _final_reply(conv_id, since_line):
    """Return the last message text for conv_id appearing after byte/line offset."""
    last = ""
    try:
        with open(REPLIES, encoding="utf-8") as fh:
            lines = fh.readlines()[since_line:]
        for ln in lines:
            try:
                a = json.loads(ln).get("activity", {})
            except Exception:
                continue
            if a.get("conversation", {}).get("id") == conv_id and a.get("type") == "message" and a.get("text"):
                last = a["text"]
    except FileNotFoundError:
        pass
    return last


def _line_count():
    try:
        with open(REPLIES, encoding="utf-8") as fh:
            return len(fh.readlines())
    except FileNotFoundError:
        return 0


def run(conv_id, turns):
    """turns: list of text; returns the final reply after the last turn."""
    start = _line_count()
    for i, t in enumerate(turns):
        _send(conv_id, f"{conv_id}-{i}", t)
        time.sleep(2)
    # poll up to 90s for the final reply
    deadline = time.time() + 90
    reply = ""
    while time.time() < deadline:
        reply = _final_reply(conv_id, start)
        if reply:
            time.sleep(3)  # let streaming settle to the final chunk
            reply = _final_reply(conv_id, start)
            break
        time.sleep(3)
    return reply


CASES = [
    ("q1-idsr", ["Summarize the IDSR guideline"]),
    ("q2-daily", ["What is the Daily Brief AI spec?"]),
    ("q3-list", ["What documents do you have?"]),
    ("q4-lease", ["Tell me about the lease agreement sample"]),
    ("q5-hi", ["Hi"]),
    ("q6-thanks", ["Thanks"]),
    ("q7-sop", ["What's our outbreak investigation SOP?"]),
    ("q8-followup", ["Summarize the IDSR guideline", "Can you summarize the last thing you told me?"]),
    ("q10-twoturn", ["What is the Daily Brief AI spec?", "Can you give me more detail on the first point?"]),
]

out = []
for conv_id, turns in CASES:
    reply = run(conv_id, turns)
    out.append(f"### {conv_id}\nQUERY: {turns[-1]}\nRESPONSE:\n{reply.strip()}\n")
    print(f"captured {conv_id}: {len(reply)} chars")

pass
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), (__import__("sys").argv[1] if len(__import__("sys").argv)>1 else "baseline_responses.txt")), "w", encoding="utf-8") as fh:
    fh.write("# Phase 5 pre-refactor response baseline\n")
    fh.write("# Query 9 (PDF upload) requires manual testing — Teams attachment download not simulable here.\n\n")
    fh.write("\n".join(out))
print("BASELINE_DONE")
