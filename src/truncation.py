def safe_truncate(text: str, limit: int) -> str:
    """Safely truncate text to a character limit, ending at a word boundary if possible."""
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    # Try to avoid cutting off in the middle of a word
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated + "..."


def normalize_content(text: str) -> str:
    """
    Normalize text content by removing extra whitespace and newlines.
    
    Args:
        text: Raw text content
        
    Returns:
        Normalized text with single spaces
    """
    if not text:
        return ""
    
    # Replace newlines with spaces and collapse multiple spaces
    normalized = " ".join(str(text).replace("\n", " ").split())
    return normalized
