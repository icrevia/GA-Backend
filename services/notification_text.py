"""Shared formatting helpers for user-facing notification text."""

FIREBASE_SUFFIX = " -- Firebase"


def append_firebase_suffix(text: str, *, max_length: int | None = None) -> str:
    clean_text = (text or "").strip()
    if not clean_text:
        return clean_text

    if max_length is not None:
        return clean_text[:max_length].rstrip()
    
    return clean_text