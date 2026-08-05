"""Tests for log_redact.py: secret redaction and logger hardening."""

import logging

from log_redact import RedactingFilter, _redact, configure_secure_logging


def test_redact_password_and_tokens():
    assert "[REDACTED]" in _redact("password=hunter2")
    assert "hunter2" not in _redact("password=hunter2")
    assert "[REDACTED]" in _redact('{"token":"abc123","refresh_token":"xyz"}')
    assert "abc123" not in _redact('{"token":"abc123"}')


def test_redact_signed_url_and_telegram_bot_path():
    url = "https://rest-prod.immedia-semi.com/clip?Signature=SECRET&Expires=1"
    out = _redact(url)
    assert "Signature=SECRET" not in out
    assert "?[REDACTED]" in out

    bot = "https://api.telegram.org/bot123456:AASecretToken/sendMessage"
    out = _redact(bot)
    assert "AASecretToken" not in out
    assert "/bot[REDACTED]/" in out


def test_redacting_filter_on_log_record():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="bearer eyJhbGciOiJIUzI1NiJ9.payload",
        args=(),
        exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    assert "eyJhbGciOiJIUzI1NiJ9" not in record.msg
    assert "[REDACTED]" in record.msg


def test_configure_secure_logging_sets_blinkpy_info():
    configure_secure_logging()
    assert logging.getLogger("blinkpy").level == logging.INFO
    assert logging.getLogger("blinkpy.auth").level == logging.INFO
