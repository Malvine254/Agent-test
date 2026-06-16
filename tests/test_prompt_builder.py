import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from prompt_builder import build_prompt


def test_followup_prompt_uses_previous_sources():
    prompt = build_prompt(
        user_question="what should I add",
        source_snippets=[{"title": "Plan", "url": "https://example.test/plan", "snippet": "Current plan text"}],
        memory_summary="Previous answer summarized Plan.",
    )
    assert "Armely AI" in prompt
    assert "Current plan text" in prompt
    assert "Previous answer summarized Plan." in prompt


def test_uploaded_file_prompt_does_not_inject_sharepoint_when_no_sources():
    prompt = build_prompt(
        user_question="summarize this file",
        uploaded_file_snippets=[{"filename": "upload.docx", "snippet": "Uploaded content only"}],
        source_snippets=[],
    )
    assert "Uploaded content only" in prompt
    assert "Retrieved sources:" not in prompt


def test_prompt_omits_web_sources():
    prompt = build_prompt(
        user_question="what does this say",
        source_snippets=[
            {"title": "Website", "source_type": "web", "snippet": "Web content"},
            {"title": "Policy", "source_type": "sharepoint", "snippet": "SharePoint content"},
        ],
    )
    assert "SharePoint content" in prompt
    assert "Web content" not in prompt
