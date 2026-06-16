import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from router import build_search_query, decide_route


def test_hi_answers_direct():
    route = decide_route("hi", {})
    assert route.action == "answer_direct"
    assert route.source_required is False


def test_general_knowledge_answers_direct():
    route = decide_route("what is machine learning", {})
    assert route.action == "answer_direct"
    assert route.source_required is False


def test_vacation_policy_searches_documents():
    route = decide_route("what is the vacation policy", {})
    assert route.action == "search_ai_index"
    assert route.source_required is True


def test_summarize_this_file_uses_attachments():
    route = decide_route("summarize this file", {"has_attachment": True})
    assert route.action == "use_uploaded_files"
    assert route.source_required is True


def test_compare_file_against_company_policy_searches_documents():
    route = decide_route("compare this file against company policy", {"has_attachment": True})
    assert route.action == "search_ai_index"
    assert route.source_required is True


def test_followup_reuses_previous_sources():
    route = decide_route("what should I add", {"has_previous_sources": True})
    assert route.action == "use_previous_context"
    assert route.source_required is True


def test_search_again_is_new_document_search():
    route = decide_route("search again for another policy", {"has_previous_sources": True})
    assert route.action == "search_ai_index"
    assert route.source_required is True


def test_website_search_is_not_routed_to_web():
    route = decide_route("search the website for this", {})
    assert route.action == "answer_direct"
    assert route.source_required is False
    assert "Azure AI Search indexed SharePoint documents and uploaded files only" in route.reason


def test_dress_code_policy_searches_sharepoint():
    route = decide_route("what is the dress code policy", {})
    assert route.action == "search_ai_index"
    assert route.source_required is True


def test_llc_entity_question_searches_sharepoint():
    route = decide_route("Tell me about armely llc", {})
    assert route.action == "search_ai_index"
    assert route.query == "armely llc"


def test_source_only_search_reuses_previous_topic():
    route = decide_route("search from sharepoint", {"last_query": "Tell me about armely llc"})
    assert route.action == "search_ai_index"
    assert route.query == "armely llc"


def test_source_only_search_reuses_recent_user_topic():
    route = decide_route(
        "search from sharepoint",
        {"recent_history": ["User: Tell me about armely llc", "Assistant: I need sources."]},
    )
    assert route.action == "search_ai_index"
    assert route.query == "armely llc"


def test_search_query_keeps_four_or_more_meaningful_terms():
    query = build_search_query("what does the client onboarding procedure say about vendor reports")
    assert query.split() == ["client", "onboarding", "procedure", "vendor", "reports"]


def test_its_contact_details_uses_previous_context():
    route = decide_route("its contact details", {"has_previous_sources": True})
    assert route.action == "use_previous_context"
    assert route.source_required is True


def test_phone_number_followup_uses_previous_context():
    route = decide_route("i mean the phone number, etc", {"has_previous_sources": True})
    assert route.action == "use_previous_context"
    assert route.source_required is True


def test_onedrive_search_is_disabled():
    route = decide_route("search OneDrive for this", {})
    assert route.action == "answer_direct"
    assert route.source_required is False
    assert "Azure AI Search indexed SharePoint documents and uploaded files only" in route.reason
