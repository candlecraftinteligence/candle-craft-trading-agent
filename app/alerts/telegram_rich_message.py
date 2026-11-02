"""Versioned, atomic rich-message bodies for the existing durable text outbox.

The prefix and HTML (including buttons) are persisted and hashed together. A
restart therefore sends the reserved presentation, not a freshly rendered plan.
No credentials, destination, retry policy or delivery authority live here.
"""

RICH_MESSAGE_PREFIX = "CCI_RICH_MESSAGE_V1\n"
MAX_RICH_PAYLOAD_LENGTH = 4096


def is_rich_message(message: str) -> bool:
    return message.startswith(RICH_MESSAGE_PREFIX)


def rich_message_html(message: str) -> str:
    if not is_rich_message(message):
        raise ValueError("Not a versioned CCI rich message")
    html = message[len(RICH_MESSAGE_PREFIX):]
    if not html.strip() or len(message) > MAX_RICH_PAYLOAD_LENGTH:
        raise ValueError("Rich message must be nonempty and fit one outbox part")
    return html


def pack_rich_message(html: str) -> str:
    message = RICH_MESSAGE_PREFIX + html
    rich_message_html(message)
    return message
