# lambda/poller/log_redact.py
# Redacts tokens, passwords, and signed URL query strings from log records.
# Applied to the root logger so third-party libs (blinkpy) cannot leak secrets.

from __future__ import annotations

import logging
import re

_PASSWORD = re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+")
_TOKEN_KV = re.compile(
    r"(?i)\b(refresh[_-]?token|access[_-]?token|bearer)\s*[:=]?\s*\S+"
)
_JSON_TOKEN = re.compile(r'(?i)("(?:refresh_)?token"\s*:\s*")[^"]+(")')
_SIGNED_URL = re.compile(r"(https?://[^\s?]+)\?[^\s]+")
_TELEGRAM_BOT_PATH = re.compile(r"(?i)/bot[0-9]+:[A-Za-z0-9_-]+/")


def _redact(text: str) -> str:
    """Replace sensitive substrings in a log message."""
    text = _PASSWORD.sub(r"\1=[REDACTED]", text)
    text = _TOKEN_KV.sub(r"\1=[REDACTED]", text)
    text = _JSON_TOKEN.sub(r"\1[REDACTED]\2", text)
    text = _SIGNED_URL.sub(r"\1?[REDACTED]", text)
    text = _TELEGRAM_BOT_PATH.sub("/bot[REDACTED]/", text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from record messages and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: _redact(value) if isinstance(value, str) else value
                        for key, value in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        _redact(arg) if isinstance(arg, str) else arg
                        for arg in record.args
                    )
        except Exception:
            # Never break logging from the redactor itself.
            pass
        return True


def configure_secure_logging() -> None:
    """Configure root + third-party loggers for production Lambda.

    - Root/blinkpy at INFO (blocks DEBUG URL dumps from blinkpy)
    - Redacting filter on loggers so handlers never see raw secrets
    """
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    redactor = RedactingFilter()
    root.addFilter(redactor)

    for name in (
        "handler",
        "blink_client",
        "secrets_loader",
        "telegram_client",
        "state_store",
        "log_redact",
        "blinkpy",
        "blinkpy.auth",
        "blinkpy.api",
        "blinkpy.helpers",
        "blinkpy.sync_module",
        "aiohttp",
        "urllib3",
        "botocore",
        "boto3",
    ):
        named = logging.getLogger(name)
        named.setLevel(logging.INFO)
        named.addFilter(redactor)
