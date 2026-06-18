from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from citations import cited_numbers


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    response: str = ""
    reason: str = ""


def not_found_response(topic: str) -> str:
    topic = (topic or "that topic").strip()
    return (
        "I searched the Azure AI Search index and the available fallback sources "
        f"for {topic}, but I could not find matching information."
    )


def before_llm(route: Any, retrieval_result: Any = None, attachment_context: Any = None) -> GuardResult:
    sources = getattr(retrieval_result, "sources", None) if retrieval_result is not None else None
    if sources is None and isinstance(retrieval_result, dict):
        sources = retrieval_result.get("sources")
    if sources is None and isinstance(retrieval_result, list):
        sources = retrieval_result
    source_required = bool(getattr(route, "source_required", False) if not isinstance(route, dict) else route.get("source_required"))
    query = getattr(route, "query", "") if not isinstance(route, dict) else route.get("query", "")
    if source_required and not sources and not attachment_context:
        return GuardResult(False, not_found_response(query), "source required but no source content was retrieved")
    return GuardResult(True)


def validate_answer(answer: str, sources: list[dict], source_required: bool) -> GuardResult:
    if not source_required:
        return GuardResult(True)
    if not sources:
        return GuardResult(False, not_found_response("that topic"), "no sources available")
    numbers = cited_numbers(answer)
    if not numbers:
        return GuardResult(False, "", "source-backed answer has no inline citations")
    valid = set(range(1, len(sources) + 1))
    invalid = numbers - valid
    if invalid:
        return GuardResult(False, "", f"answer cited unavailable source numbers: {sorted(invalid)}")
    org_claim_lines = [
        line for line in (answer or "").splitlines()
        if re.search(r"\b(policy|procedure|company|department|staff|employee|client|vendor|report|project)\b", line, re.I)
    ]
    uncited = [line for line in org_claim_lines if not re.search(r"\[\[\d+\]\]", line)]
    if uncited:
        return GuardResult(False, "", "organizational claims were not cited")
    return GuardResult(True)
