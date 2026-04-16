"""Shared formatting helpers for user-facing notification text."""

FIREBASE_SUFFIX = " -- Firebase"


def append_firebase_suffix(text: str, *, max_length: int | None = None) -> str:
    clean_text = (text or "").strip()
    if not clean_text:
        return clean_text

    if clean_text.endswith(FIREBASE_SUFFIX):
        if max_length is None or len(clean_text) <= max_length:
            return clean_text
        clean_text = clean_text[: max(0, max_length - len(FIREBASE_SUFFIX))].rstrip()

    if max_length is None:
        return f"{clean_text}{FIREBASE_SUFFIX}"

    available = max_length - len(FIREBASE_SUFFIX)
    if available <= 0:
        return FIREBASE_SUFFIX[:max_length]

    truncated = clean_text[:available].rstrip()
    formatted = f"{truncated}{FIREBASE_SUFFIX}"
    if len(formatted) > max_length:
        formatted = formatted[:max_length].rstrip()
    return formatted