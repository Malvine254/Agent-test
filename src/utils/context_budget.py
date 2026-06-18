"""Context budget enforcement and compression for LLM prompts.

Provides a single, priority-aware compression layer that ensures:
- Total prompt never exceeds MAX_PROMPT_CHARS
- Each content source gets a proportional budget
- Lower-priority blocks are dropped first when near budget
- All content is compressed, never injected raw
"""
import logging
import re

logger = logging.getLogger(__name__)

# Priority levels (1 = highest, 5 = lowest)
PRIORITY_USER = 1
PRIORITY_ATTACHMENTS = 2
PRIORITY_DOCS = 3
PRIORITY_MEMORY = 4
PRIORITY_WEB = 5


def compress_for_llm(text: str, max_chars: int, label: str = "content") -> str:
    """Compress text for LLM context.  Never return raw/full content.

    Strategy:
    1. If already within budget → return as-is.
    2. Extract leading sentences as a lightweight "summary".
    3. Hard-truncate to *max_chars* and append a notice.

    Args:
        text:      Raw input text (may be hundreds of thousands of chars).
        max_chars: Hard ceiling for the returned string.
        label:     Human-readable label for logging.

    Returns:
        Compressed string guaranteed ≤ max_chars.
    """
    if not text:
        return ""

    text = text.strip()
    if len(text) <= max_chars:
        return text

    original_len = len(text)

    # --- lightweight extractive summary ---
    # Grab complete sentences from the first portion of the text.
    # This preserves more meaning than a blind char-slice.
    summary_budget = int(max_chars * 0.90)  # leave 10 % for the truncation notice
    sentences = _extract_sentences(text, summary_budget)

    if len(sentences) >= summary_budget:
        sentences = sentences[:summary_budget]

    notice = (
        f"\n\n[...COMPRESSED from {original_len:,} to {len(sentences):,} chars. "
        f"Full content available in cache.]"
    )

    result = sentences + notice

    # Final hard-cap safety net
    if len(result) > max_chars:
        result = result[:max_chars]

    logger.info(f"🗜️  Compressed {label}: {original_len:,} → {len(result):,} chars")
    return result


def _extract_sentences(text: str, budget: int) -> str:
    """Return as many leading *complete* sentences as fit in *budget* chars."""
    # Quick split on sentence-ending punctuation followed by whitespace.
    # Falls back to first *budget* chars if no sentence boundary is found.
    parts: list[str] = re.split(r'(?<=[.!?])\s+', text)
    collected: list[str] = []
    used = 0
    for part in parts:
        if used + len(part) + 1 > budget:
            break
        collected.append(part)
        used += len(part) + 1  # +1 for the space we'll join with
    if not collected:
        # No sentence boundary within budget — just slice
        return text[:budget]
    return " ".join(collected)


def enforce_context_budget(
    context_blocks: list[str],
    max_chars: int | None = None,
    *,
    priorities: list[int] | None = None,
) -> str:
    """Combine *context_blocks* respecting a character budget.

    Blocks are included in order of *priorities* (lower number = higher
    priority).  When the budget is exhausted, remaining blocks are dropped
    and each dropped block is logged.

    Args:
        context_blocks: Text sections to combine.
        max_chars:      Hard ceiling in characters.
        priorities:     Optional parallel list of priority values
                        (see module-level constants).  When omitted every
                        block gets the same priority and ordering is
                        preserved.

    Returns:
        Combined text within budget.
    """
    if max_chars is None:
        max_chars = 24_000  # safe default

    if not context_blocks:
        return ""

    # Pair blocks with their priorities and original index
    if priorities and len(priorities) == len(context_blocks):
        indexed = sorted(
            zip(priorities, range(len(context_blocks)), context_blocks),
            key=lambda t: (t[0], t[1]),
        )
    else:
        indexed = [(0, i, b) for i, b in enumerate(context_blocks)]

    combined_parts: list[str] = []
    used = 0
    included = 0
    dropped = 0

    for priority, idx, block in indexed:
        if not block or not block.strip():
            continue

        block_len = len(block)
        if used + block_len > max_chars:
            # Attempt to fit a compressed version
            remaining = max_chars - used
            if remaining > 200:
                compressed = compress_for_llm(block, remaining, label=f"block-{idx}")
                combined_parts.append(compressed)
                used += len(compressed)
                included += 1
            else:
                dropped += 1
                logger.warning(
                    f"⛔ Context budget: dropped block {idx} "
                    f"(priority={priority}, {block_len:,} chars) — "
                    f"budget exhausted ({used:,}/{max_chars:,})"
                )
            continue

        combined_parts.append(block)
        used += block_len
        included += 1

    result = "\n\n".join(combined_parts).strip()
    logger.info(
        f"Context budget: {len(result):,} chars from {included} blocks "
        f"({dropped} dropped, limit={max_chars:,})"
    )
    return result


# ─────────────────────────────────────────────────────────────────────
# ISSUE 2 — PDF Chunking & Relevance Ranking
# ─────────────────────────────────────────────────────────────────────

def chunk_text(text: str, size: int = 3000) -> list[str]:
    """Split *text* into non-overlapping chunks of at most *size* chars.

    Tries to break on paragraph/sentence boundaries when possible so that
    individual chunks remain semantically coherent.
    """
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        if end >= len(text):
            chunks.append(text[start:])
            break
        # Try to break at a paragraph boundary first (\n\n)
        boundary = text.rfind("\n\n", start, end)
        if boundary <= start:
            # Fall back to sentence boundary
            boundary = text.rfind(". ", start, end)
            if boundary <= start:
                boundary = end  # hard break
            else:
                boundary += 2  # include the ". "
        else:
            boundary += 2  # skip past the \n\n
        chunks.append(text[start:boundary])
        start = boundary

    return chunks


def rank_chunks_by_relevance(
    chunks: list[str],
    query: str,
    *,
    top_k: int = 3,
) -> list[str]:
    """Return the *top_k* chunks most relevant to *query*.

    Uses simple token-overlap scoring (no external model required).
    Each chunk is scored by the fraction of query tokens that appear in it.
    """
    if not chunks:
        return []
    if not query or not query.strip():
        return chunks[:top_k]

    # Normalise query into keyword set
    q_tokens = set(re.sub(r"[^\w\s]", "", query.lower()).split())
    q_tokens -= {"the", "a", "an", "is", "are", "was", "were", "of", "in",
                 "to", "for", "and", "or", "on", "at", "by", "it", "be",
                 "this", "that", "with", "from", "as", "not", "but", "if"}
    if not q_tokens:
        return chunks[:top_k]

    scored: list[tuple[float, int, str]] = []
    for idx, chunk in enumerate(chunks):
        lower = chunk.lower()
        hits = sum(1 for t in q_tokens if t in lower)
        score = hits / len(q_tokens)
        scored.append((score, idx, chunk))

    # Sort by score (desc), then by original order (asc) for tie-breaking
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [chunk for _, _, chunk in scored[:top_k]]


def select_relevant_chunks(
    text: str,
    query: str,
    *,
    chunk_size: int = 3000,
    max_chunks: int = 3,
    max_chars: int = 6000,
    label: str = "doc",
) -> str:
    """Chunk → rank → compress: the full pipeline for large documents.

    1. Split *text* into chunks of *chunk_size*.
    2. Rank chunks against *query* and keep top *max_chunks*.
    3. Join selected chunks and compress to *max_chars*.

    This is the function that should be called whenever a document
    (especially a PDF) exceeds MAX_DOC_SNIPPET_CHARS.
    """
    if not text:
        return ""

    if len(text) <= max_chars:
        return text  # small enough, no chunking needed

    chunks = chunk_text(text, size=chunk_size)
    logger.info(f"📄 Chunked {label}: {len(text):,} chars → {len(chunks)} chunks of ~{chunk_size}")

    top_chunks = rank_chunks_by_relevance(chunks, query, top_k=max_chunks)
    joined = "\n\n".join(top_chunks)

    # Final compression to hard cap
    result = compress_for_llm(joined, max_chars, label=f"chunked:{label}")
    logger.info(
        f"📄 select_relevant_chunks({label}): "
        f"{len(text):,} → {len(chunks)} chunks → top {len(top_chunks)} → {len(result):,} chars"
    )
    return result


# ─────────────────────────────────────────────────────────────────────
# ISSUE 6 — Summarise Oversized Documents
# ─────────────────────────────────────────────────────────────────────

def summarize_text(text: str, max_chars: int = 6000, label: str = "doc") -> str:
    """Create a lightweight extractive summary for documents that exceed *max_chars*.

    Strategy:
    - Extract the first ~30 % of budget from the document's leading content
      (titles, headings, intro).
    - Extract key sentences from the remaining body using a simple
      keyword-density heuristic.
    - Hard-cap to *max_chars*.

    This does NOT call the LLM — it is a fast, pure-Python fallback.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    original_len = len(text)

    # ── Part A: leading context (30 % budget) ──────────────────────
    lead_budget = int(max_chars * 0.30)
    lead = _extract_sentences(text, lead_budget)

    # ── Part B: body key sentences (60 % budget) ──────────────────
    body_budget = int(max_chars * 0.60)
    body_start = len(lead) + 1
    body_text = text[body_start:]

    # Score sentences by "information density": longer sentences with
    # more capital-letter words (names, headings) rank higher.
    body_sentences = re.split(r'(?<=[.!?])\s+', body_text)
    scored_sents: list[tuple[float, int, str]] = []
    for i, sent in enumerate(body_sentences):
        words = sent.split()
        if len(words) < 4:
            continue
        caps = sum(1 for w in words if w and w[0].isupper())
        density = caps / max(len(words), 1) + len(sent) / 500
        scored_sents.append((density, i, sent))

    scored_sents.sort(key=lambda t: -t[0])
    body_parts: list[str] = []
    body_used = 0
    for _, orig_idx, sent in scored_sents:
        if body_used + len(sent) + 2 > body_budget:
            break
        body_parts.append(sent)
        body_used += len(sent) + 2

    # Restore original order for readability
    body_parts_ordered = sorted(
        body_parts, key=lambda s: body_text.find(s)
    )
    body_summary = " ".join(body_parts_ordered)

    # ── Combine ────────────────────────────────────────────────────
    notice = (
        f"\n\n[SUMMARISED from {original_len:,} chars. "
        f"Full content available in cache.]"
    )
    result = lead + "\n\n" + body_summary + notice
    if len(result) > max_chars:
        result = result[:max_chars]

    logger.info(f"📋 summarize_text({label}): {original_len:,} → {len(result):,} chars")
    return result


# ─────────────────────────────────────────────────────────────────────
# ISSUE 8 — Token-Aware Gatekeeper Loop
# ─────────────────────────────────────────────────────────────────────

# Label → priority mapping (lower number = keep longer)
_BLOCK_PRIORITY = {
    "[WEB]": 5,
    "<!-- web": 5,
    "[DOC ": 3,           # Reduced priority (drop before attachments)
    "<!-- full-document": 3,
    "[ATTACHMENTS]": 2,    # Increased priority (keep longer)
    "[MEMORY": 4,
    "[CITATION": 2,
    "[FORMATTING": 1,
    "[IMPORTANT": 1,
    "[CONTEXT": 1,
}


def _block_priority(block: str) -> int:
    """Return the drop-priority for a prompt block (5 = drop first)."""
    upper = block[:30]
    for prefix, prio in _BLOCK_PRIORITY.items():
        if prefix in upper:
            return prio
    return 1  # unknown blocks are high-priority (keep)


def token_gatekeeper(
    prompt: str,
    max_tokens: int = 40_000,
    chars_per_token: int = 4,
) -> str:
    """Iteratively remove the lowest-priority block until prompt fits.

    This is the FINAL safety net called immediately before the LLM call.
    It guarantees the prompt will not exceed *max_tokens* (estimated).

    Drop priority (lowest → highest):
        web(5) → memory(4) → attachments(3) → docs(2) → user/system(1)
    """
    max_chars = max_tokens * chars_per_token
    if len(prompt) <= max_chars:
        return prompt

    logger.warning(
        f"🚧 token_gatekeeper: {len(prompt):,} chars (~{len(prompt)//4:,} tokens) "
        f"> limit {max_tokens:,} tokens. Reducing."
    )

    blocks = prompt.split("\n\n")
    tagged: list[tuple[int, int, str]] = [
        (_block_priority(b), idx, b) for idx, b in enumerate(blocks)
    ]

    iterations = 0
    while len("\n\n".join(b for _, _, b in tagged)) > max_chars and tagged:
        # Find the lowest-priority (highest number) block
        worst_idx = max(range(len(tagged)), key=lambda i: (tagged[i][0], -tagged[i][1]))
        prio, orig_idx, dropped = tagged.pop(worst_idx)
        preview = dropped[:60].replace("\n", " ")
        logger.warning(
            f"  → Dropped block {orig_idx} (priority={prio}): '{preview}…'"
        )
        iterations += 1
        if iterations > 50:
            break  # safety valve

    result = "\n\n".join(b for _, _, b in sorted(tagged, key=lambda t: t[1]))

    # Final hard-cap
    if len(result) > max_chars:
        result = result[:max_chars]
        logger.error("  → Hard-capped after gatekeeper loop")

    logger.info(
        f"🚧 token_gatekeeper: reduced to {len(result):,} chars "
        f"(~{len(result)//4:,} tokens) in {iterations} iteration(s)"
    )
    return result
