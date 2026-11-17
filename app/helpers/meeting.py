import re
from typing import Optional

def sanitize_meeting_url(url: str) -> str:
    """
    Cleans up Google Meet URLs (removes trailing parameters, hashes, whitespace).
    """
    if not url:
        return ""
    clean_url = url.strip()
    return clean_url

def extract_meet_code(url: str) -> Optional[str]:
    """
    Extracts the meeting code from a Google Meet URL.
    Example: https://meet.google.com/abc-defg-hij -> abc-defg-hij
    """
    match = re.search(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", url)
    if match:
        return match.group(1)
    return None
