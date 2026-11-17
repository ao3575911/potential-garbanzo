"""Conservative text redaction. Free-form prose still needs sender review."""

import re
import unicodedata

from telegram import MessageEntity

PATTERNS = [
    r"(?i)\b(?:https?://|tg://|www\.|t\.me/|telegram\.me/)\S+",
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    r"(?<!\w)@[\w]{3,}",
    r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|au|me|xyz|co|app|info|biz)(?:/[^\s]*)?\b",
    r"(?<!\w)\+?\d[\d ().-]{6,}\d(?!\w)",
]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c in "\n\t" or (not unicodedata.category(c).startswith("C")))


def redact(text: str, sender_names: tuple[str, ...] = (), entities=()) -> str:
    # Telegram entity offsets are UTF-16 code units, not Python string positions.
    raw = text.encode("utf-16-le")
    private_types = {
        MessageEntity.MENTION, MessageEntity.TEXT_MENTION, MessageEntity.EMAIL,
        MessageEntity.PHONE_NUMBER, MessageEntity.URL, MessageEntity.TEXT_LINK,
    }
    for entity in sorted(entities or (), key=lambda e: e.offset, reverse=True):
        if entity.type in private_types:
            start, end = entity.offset * 2, (entity.offset + entity.length) * 2
            raw = raw[:start] + "[removed]".encode("utf-16-le") + raw[end:]
    result = normalize(raw.decode("utf-16-le"))
    for pattern in PATTERNS:
        result = re.sub(pattern, "[removed]", result)
    for name in sorted(set(sender_names), key=len, reverse=True):
        name = normalize(name).strip()
        if len(name) >= 3:
            result = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "[removed]", result, flags=re.I)
    return result.strip()


def has_link(text: str, entities=()) -> bool:
    return any(e.type in {MessageEntity.URL, MessageEntity.TEXT_LINK} for e in entities or ()) or any(
        re.search(pattern, normalize(text)) for pattern in (PATTERNS[0], PATTERNS[3])
    )
