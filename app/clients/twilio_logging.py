"""Shared, visible diagnostics for Twilio without credentials or call audio."""

import logging
import re

logger = logging.getLogger("app.twilio")


def configure_logging():
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False


def safe_detail(value, secrets=()):
    """Bound provider error text and remove secrets, URLs and phone numbers."""
    text = str(value)
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?:https?|wss?)://[^\s<>\"']+", "[url redacted]", text)
    text = re.sub(
        r"(?i)(token|api_key|api_token|authorization)([\s\"':=]+)[^\s,}\"']+",
        r"\1\2[redacted]",
        text,
    )
    text = re.sub(r"\+?\d[\d ()-]{8,}\d", "[phone redacted]", text)
    return text.replace("\n", " ").replace("\r", " ")[:2000]
