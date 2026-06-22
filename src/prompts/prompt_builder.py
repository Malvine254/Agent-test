"""Prompt construction — extracted verbatim from app.py (Phase 5.3).

is_document_summary_request stays in app.py (owns SUMMARY_REQUEST_PATTERNS, used elsewhere);
build_llm_input takes the precomputed is_summary_request flag to avoid a circular import.
_strip_html is imported back into app.py for its other uses.
"""
from __future__ import annotations

import html
import logging
import os
import re

from config import Config

logger = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    """Convert simple HTML to plain text by removing tags."""
    try:
        import re, html
        # Remove script/style content
        text = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        # Replace <br> and <p> with newlines
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        # Remove remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Unescape HTML entities
        text = html.unescape(text)
        # Normalize whitespace
        return " ".join(text.split())
    except Exception:
        return text


def _has_meaningful_source_content(doc: dict | None, min_chars: int = 100) -> bool:
    """Return True if a source has enough content to justify references."""
    if not doc:
        return False
    if doc.get("_access_denied"):
        return False
    content = (doc.get("content") or doc.get("snippet") or "").strip()
    return len(content) >= min_chars


def build_llm_input(
    user_text: str,
    attachment_texts: list[str],
    doc_items: list[dict],
    personalization: str,
    memory_text: str = "",
    is_summary_request: bool = False,
) -> tuple[str, dict]:
    """Construct a token-safe LLM prompt using priority-based compression.

    Priority order:
        1. User question          â€” never compressed
        2. Document snippets      â€” compressed per-doc and total (Includes Web results)
        3. Attachment snippets    â€” compressed per-file and total
        4. Memory                 â€” compressed

    doc_items: [{"title": str, "url": str, "snippet": str}]
    Returns (prompt_text, log_info)
    """
    from utils.context_budget import compress_for_llm, enforce_context_budget

    # â”€â”€ Budget constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    MAX_PROMPT_CHARS  = int(getattr(Config, "MAX_PROMPT_CHARS", 80000))
    MAX_DOCS          = int(getattr(Config, "MAX_DOCS", 8))
    MAX_DOC_PER_DOC   = int(getattr(Config, "MAX_DOC_SNIPPET_CHARS", 6000))
    MAX_DOC_TOTAL     = int(getattr(Config, "MAX_TOTAL_CONTEXT_CHARS", 24000))
    # FIX: Summary/overview requests need enough cached document content to be useful.
    # Otherwise a full handbook is reduced to a tiny excerpt and the model says
    # it cannot summarize. Keep this bounded but larger than normal Q&A snippets.
    if re.search(r"\b(summarize|summary|overview|tell me about|what is|explain)\b", (user_text or "").lower()):
        MAX_DOC_PER_DOC = max(MAX_DOC_PER_DOC, int(os.getenv("SUMMARY_DOC_SNIPPET_CHARS", "18000")))
        MAX_DOC_TOTAL = max(MAX_DOC_TOTAL, int(os.getenv("SUMMARY_TOTAL_CONTEXT_CHARS", "36000")))
    MAX_ATTACH_CHARS  = int(getattr(Config, "MAX_ATTACH_CHARS", 40000))
    MAX_LLM_ATTACH    = int(getattr(Config, "MAX_LLM_ATTACH_CHARS", 100000))
    MAX_MEMORY_TURNS  = int(getattr(Config, "MAX_MEMORY_TURNS", 1))

    # ISSUE 5 â€” Reduce doc count when PDFs present (token-dense)
    _has_pdf = any(
        (d.get("title") or d.get("name") or "").lower().endswith(".pdf")
        for d in (doc_items or [])
    )
    if _has_pdf and MAX_DOCS > 2:
        MAX_DOCS = 2
        logger.info(f"ðŸ“„ ISSUE 5: PDFs detected in doc_items â€” limiting to {MAX_DOCS} docs")

    # â”€â”€ 1. User text (priority 1 â€” never compressed) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    utext = (user_text or "").strip()
    ptext = _strip_html(personalization or "")
    utext_lower_for_intent = utext.lower()
    document_advice_intent = any(
        phrase in utext_lower_for_intent
        for phrase in (
            "what do you suggest", "what should i add", "what can i add",
            "what would you add", "suggest i add", "suggestions",
            "recommend", "recommendations", "improve it", "improve this",
            "improve the document", "what is missing", "what's missing",
            "anything missing", "gaps", "make it better", "how can i improve",
            "what else should",
        )
    )

    # â”€â”€ 2. Documents (priority 2 â€” compressed per-doc + total) â”€â”€â”€â”€â”€â”€â”€
    docs = []
    doc_used = 0
    compare_intent = False
    try:
        utext_lower = (utext or "").lower()
        compare_keywords = ("compare", "difference", "differences", "diff",
                            "similar", "similarities", "contrast", "vs", "versus")
        compare_intent = len(attachment_texts or []) > 1 and any(
            k in utext_lower for k in compare_keywords
        )
        if (len(attachment_texts or []) > 1
                and "summarize" in utext_lower
                and not compare_intent):
            utext = f"{utext}\nPlease summarize each attachment separately."
    except Exception:
        compare_intent = False

    for d in (doc_items or []):
        if len(docs) >= MAX_DOCS:
            break
        title = (d.get("title") or d.get("name") or "Untitled").strip()
        url   = (d.get("url") or "").strip()
        # ACCURACY FIX: prefer the richer of snippet/content. Search docs often carry a
        # 1000-char `snippet` alongside the full `content`; using only the snippet
        # starved the model and caused hallucination. compress_for_llm caps size below.
        _snip = d.get("snippet") or ""
        _cont = d.get("content") or ""
        raw   = _strip_html(_cont if len(_cont) > len(_snip) else _snip)
        if not raw:
            continue

        # Per-doc compression â€” NEVER append raw content
        remaining = max(0, MAX_DOC_TOTAL - doc_used)
        per_doc_cap = MAX_DOC_PER_DOC
        if d.get("primary") and is_summary_request:
            per_doc_cap = max(per_doc_cap, int(os.getenv("SUMMARY_PRIMARY_DOC_CHARS", "20000")))
        cap = min(per_doc_cap, remaining)
        if cap <= 0:
            logger.info(f"â›” Doc budget full ({doc_used:,}/{MAX_DOC_TOTAL:,}). Skipping: {title}")
            break
        snippet = compress_for_llm(raw, cap, label=f"doc:{title[:40]}")
        total_chars = int(d.get("total_chars") or len(raw))
        truncated = bool(d.get("truncated") or total_chars > len(snippet))
        if truncated:
            snippet += (
                "\n\n[TRUNCATION NOTE: Only part of this document may be included in the prompt. "
                "Do not claim the full document has no additional content unless the full document was actually included. "
                "If summarizing, say: This summary is based on the included portion of the document.]"
            )
        doc_used += len(snippet)
        docs.append({"title": title, "url": url, "snippet": snippet, "truncated": truncated, "total_chars": total_chars})

    # â”€â”€ 3. Attachments (priority 3 â€” compressed per-file + total) â”€â”€â”€â”€
    attach_segments = []
    for i, text in enumerate(attachment_texts or []):
        if not text:
            continue
        cleaned = _strip_html(text)
        compressed = compress_for_llm(cleaned, MAX_ATTACH_CHARS,
                                      label=f"attachment-{i+1}")
        attach_segments.append(compressed)

    # Enforce total attachment budget
    total_attach = sum(len(s) for s in attach_segments)
    if total_attach > MAX_LLM_ATTACH:
        logger.info(
            f"Total attachment chars ({total_attach:,}) > "
            f"MAX_LLM_ATTACH_CHARS ({MAX_LLM_ATTACH:,}). Trimming."
        )
        trimmed, running = [], 0
        for seg in attach_segments:
            if running + len(seg) > MAX_LLM_ATTACH:
                trimmed.append(seg[:MAX_LLM_ATTACH - running])
                break
            trimmed.append(seg)
            running += len(seg)
        attach_segments = trimmed
    attach_plain = "\n\n---\n\n".join(attach_segments) if attach_segments else ""

    # â”€â”€ 4. Memory (priority 4) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    mem_plain = ""
    if memory_text:
        mem_plain = compress_for_llm(_strip_html(memory_text), 4000,
                                     label="memory")

    # â”€â”€ Assemble prompt blocks with priorities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    blocks = []
    priorities = []

    blocks.append(utext);                                      priorities.append(1)
    if ptext:
        blocks.append(ptext);                                  priorities.append(1)
    if attach_plain:
        blocks.append(f"[ATTACHMENTS]\n{attach_plain}");       priorities.append(2)
    if docs:
        doc_lines = []
        for i, d in enumerate(docs, 1):
            lines = [f"[DOC {i}] {d['title']}"]
            if d.get("url"):
                lines.append(f"URL: {d['url']}")
            lines.append(d["snippet"])
            doc_lines.append("\n".join(lines))
        blocks.append("\n\n".join(doc_lines));                 priorities.append(3)
    if mem_plain:
        blocks.append(
            f"[MEMORY (last {MAX_MEMORY_TURNS})]\n{mem_plain}"
        );                                                      priorities.append(4)

    # Citation instructions
    if docs:
        if document_advice_intent:
            blocks.append(
                "[DOCUMENT REVIEW / RECOMMENDATION MODE]\n"
                "The user is asking for professional suggestions about improving the document, "
                "not asking whether the document itself contains recommendations.\n"
                "Use the included document content to identify what is already covered, then provide "
                "clearly labeled recommendations for additions, gaps, or improvements.\n"
                "Cite the document when describing what it currently includes. Do not cite a recommendation "
                "as if the document stated it. Recommendations may be uncited when clearly labeled as suggested additions.\n"
                "If the document is truncated, say the recommendations are based on the included portion."
            )
            priorities.append(1)

        cite_map_lines = []
        for i, d in enumerate(docs, 1):
            u = d.get("url") or ""
            cite_map_lines.append(
                f"  [{i}] {d['title']} -> {u}" if u else f"  [{i}] {d['title']}"
            )
        cite_map = "\n".join(cite_map_lines)
        blocks.append(
            "[CITATION REQUIREMENTS - MANDATORY]\n"
            "ðŸš¨ YOU MUST cite sources inline for EVERY organizational fact.\n"
            "Citations are MANDATORY, not optional.\n\n"
            "Format: [[number]](URL) placed right after the sentence that uses the source.\n"
            "Example: 'The CIO is John Smith [[1]](https://sharepoint.../doc.pdf).'\n\n"
            f"Available sources:\n{cite_map}\n\n"
            "CRITICAL RULES:\n"
            "âœ… Cite EVERY fact from organizational sources\n"
            "âœ… Use [[N]](URL) format immediately after each cited fact\n"
            "âœ… Number citations sequentially [1], [2], [3]...\n"
            "âŒ NEVER provide information without citations when sources are available\n"
            "âŒ NEVER use general knowledge for any topic that triggered a search\n"
            "âŒ NEVER cite documents you did not actually use\n\n"
            "Do NOT include a References section at the end â€” it will be added automatically.\n"
            "Exception: for clearly labeled document-improvement recommendations, cite only the document facts "
            "you reviewed; do not pretend the recommendation itself appears in the source.\n\n"
            "Only cite documents whose content you actually used in your answer."
        )
        priorities.append(2)
        
        # ZERO VAGUE REFERENCES - tie everything to extracted content
        blocks.append(
            "[ZERO VAGUE REFERENCES - CRITICAL]\n"
            "ðŸš¨ EVERY statement MUST be tied to SPECIFIC extracted content above.\n\n"
            "âŒ FORBIDDEN VAGUE PHRASES:\n"
            "â€¢ 'According to the documents...'\n"
            "â€¢ 'The files mention...'\n"
            "â€¢ 'Based on the information provided...'\n"
            "â€¢ 'It appears that...'\n"
            "â€¢ 'The search results indicate...'\n"
            "â€¢ 'The organization provides...'\n"
            "â€¢ Any general statement without direct reference to specific text\n\n"
            "âœ… REQUIRED PATTERN:\n"
            "Every claim â†’ Quote specific text from [DOC N] above + cite [[N]](URL)\n\n"
            "Example of WRONG:\n"
            "'Swope Health offers various healthcare services.'\n\n"
            "Example of CORRECT:\n"
            "'Swope Health offers \"primary care, dental, and behavioral health services\" [[1]](URL).'\n\n"
            "ðŸŽ¯ VERIFICATION: Before each statement, ask:\n"
            "RECOMMENDATION EXCEPTION:\n"
            "When the user asks what to add, improve, or what is missing from a document, you may provide "
            "clearly labeled recommendations derived from gaps in the included content. Cite the document "
            "for what it currently contains, and label your additions as recommendations instead of source facts.\n\n"
            "1. Can I point to the EXACT text in [DOC N] that supports this?\n"
            "2. Did I quote or closely paraphrase that specific text?\n"
            "3. Did I cite the source?\n"
            "If ANY answer is NO, rewrite or remove the statement unless it is clearly labeled as a recommendation."
        )
        priorities.append(1)
    else:
        blocks.append(
            "[NO DOCUMENT SOURCES IN THIS PROMPT]\n"
            "CRITICAL RULES:\n"
            "1. If this is casual conversation, reply naturally and briefly.\n"
            "2. If this is a general knowledge question, answer from general knowledge.\n"
            "3. If this is an organizational/document question and no source content was retrieved, say no matching source content was found after checking all available sources.\n"
            "4. Do not invent organizational facts.\n"
            "5. Do not claim you searched unless the app actually performed a search.\n"
            "6. Do NOT include a References section.\n"
        )
        priorities.append(1)
    blocks.append(
        "[CRITICAL: Source Usage Verification]\n"
        "ðŸš¨ STRICT CONTENT TRACEABILITY:\n"
        "1. ONLY reference information that appears in the [DOC N] blocks above\n"
        "2. If a fact is in your answer, it MUST be quotable from [DOC N] content\n"
        "3. Do not claim to have used web or document sources unless their actual content appears in this prompt\n"
        "4. For all search-based questions: search all available sources first, then either provide source-based answers with citations or an explicit 'not found' statement. Nothing in between.\n\n"
        "If you cannot find specific text in the [DOC N] blocks to support a factual claim, do NOT make that claim. "
        "For clearly labeled document-improvement recommendations, you may suggest additions based on gaps in the included content."
    )
    priorities.append(1)

    # Formatting rules
    blocks.append(
        "[FORMATTING RULES]\n"
        "Structure every multi-point answer with **bold section headings** on their own line, "
        "followed by bullet points (use - ) under each heading.\n"
        "Wrap key terms, names, dates, and file names in **bold**.\n"
        "Use numbered lists when order matters.\n"
        "Start summaries with a 1-2 sentence overview before the headed sections.\n"
        "Never output a flat list of dashes without section headings.\n"
        "Keep each bullet to 1-2 sentences."
    )
    priorities.append(1)

    # â”€â”€ Priority-aware assembly with budget enforcement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    prompt = enforce_context_budget(
        blocks, max_chars=MAX_PROMPT_CHARS, priorities=priorities
    )

    # Absolute hard-cap safety net
    ABSOLUTE_MAX = 120_000
    if len(prompt) > ABSOLUTE_MAX:
        logger.error(
            f"Prompt exceeded ABSOLUTE MAX ({len(prompt):,}) â†’ forcing trim"
        )
        prompt = prompt[:ABSOLUTE_MAX]

    # â”€â”€ Logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    sizes = {
        "user": len(utext),
        "attachments": len(attach_plain),
        "docs": sum(len(d["snippet"]) for d in docs),
        "memory": len(mem_plain),
    }
    est_tokens = max(1, len(prompt) // 4)
    actions = []
    if doc_used >= MAX_DOC_TOTAL:
        actions.append("doc_budget_full")
    if total_attach > MAX_LLM_ATTACH if attach_segments else False:
        actions.append("attach_trimmed")

    log_info = {
        "sizes": sizes,
        "estimated_tokens": est_tokens,
        "truncation_actions": actions,
        "doc_count": len(docs),
    }

    logger.info(
        f"build_llm_input: prompt={len(prompt):,} chars "
        f"(~{est_tokens:,} tokens) | docs={len(docs)} | "
        f"sizes={sizes} | actions={actions}"
    )

    return prompt, log_info
