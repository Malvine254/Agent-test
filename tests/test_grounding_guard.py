import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from grounding_guard import before_llm, validate_answer
from router import RouteDecision


def test_org_question_with_no_results_returns_not_found():
    route = RouteDecision("search_ai_index", True, "vacation policy", "test")
    result = before_llm(route, {"sources": []}, None)
    assert result.allowed is False
    assert "Azure AI Search" in result.response
    assert "could not find information about vacation policy" in result.response


def test_org_answer_with_results_requires_citations():
    sources = [{"title": "Vacation Policy", "url": "https://example.test/policy", "snippet": "Vacation is tracked."}]
    assert validate_answer("Vacation is tracked [[1]](https://example.test/policy).", sources, True).allowed is True
    assert validate_answer("The company vacation policy is tracked.", sources, True).allowed is False


def test_invalid_citation_number_fails():
    sources = [{"title": "Policy", "url": "https://example.test", "snippet": "Policy text"}]
    assert validate_answer("The policy says this [[2]](https://example.test).", sources, True).allowed is False
