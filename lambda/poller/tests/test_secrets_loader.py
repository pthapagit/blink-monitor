"""Tests for secrets_loader.py: caching, required-field validation (token-based,
no password), and the persist-only-on-change token write-back."""

from unittest.mock import MagicMock, patch

import pytest

import secrets_loader


VALID = {
    "blink": {"token": "t", "refresh_token": "r", "hardware_id": "h"},
    "telegram": {"token": "123:ABC", "allowed_chat_id": 987},
}


def setup_function():
    secrets_loader.invalidate_cache()


def test_validate_accepts_token_based_blink_secret():
    # Must NOT require a password — Lambda is headless and token-driven.
    secrets_loader._validate_secrets(VALID)


def test_validate_rejects_missing_refresh_token():
    bad = {"blink": {"token": "t", "hardware_id": "h"}, "telegram": VALID["telegram"]}
    with pytest.raises(RuntimeError):
        secrets_loader._validate_secrets(bad)


def test_validate_rejects_missing_telegram_chat_id():
    bad = {"blink": VALID["blink"], "telegram": {"token": "x"}}
    with pytest.raises(RuntimeError):
        secrets_loader._validate_secrets(bad)


async def test_load_secrets_caches():
    fake_client = MagicMock()
    fake_client.get_secret_value.side_effect = [
        {"SecretString": '{"token":"t","refresh_token":"r","hardware_id":"h"}'},
        {"SecretString": '{"token":"123:ABC","allowed_chat_id":987}'},
    ]
    with patch.object(secrets_loader.boto3, "client", return_value=fake_client):
        first = await secrets_loader.load_secrets()
        second = await secrets_loader.load_secrets()  # served from cache
    assert first is second
    assert fake_client.get_secret_value.call_count == 2  # not called again


def test_persist_blink_login_writes_when_token_present():
    fake_client = MagicMock()
    with patch.object(secrets_loader.boto3, "client", return_value=fake_client):
        secrets_loader.persist_blink_login({"token": "new", "refresh_token": "r2"})
    fake_client.put_secret_value.assert_called_once()


def test_persist_blink_login_strips_password():
    """Password must never be written to Secrets Manager on token refresh."""
    fake_client = MagicMock()
    blob = {
        "token": "new",
        "refresh_token": "r2",
        "hardware_id": "hw",
        "password": "SHOULD-NOT-BE-STORED",
        "username": "you@email.com",
    }
    with patch.object(secrets_loader.boto3, "client", return_value=fake_client):
        secrets_loader.persist_blink_login(blob)

    fake_client.put_secret_value.assert_called_once()
    written = fake_client.put_secret_value.call_args.kwargs["SecretString"]
    import json

    parsed = json.loads(written)
    assert "password" not in parsed
    assert parsed["token"] == "new"
    assert parsed["refresh_token"] == "r2"
    assert parsed["hardware_id"] == "hw"


def test_sanitize_blink_blob_removes_password_null():
    sanitized = secrets_loader._sanitize_blink_blob(
        {"token": "t", "password": None, "refresh_token": "r"}
    )
    assert "password" not in sanitized
    assert sanitized["token"] == "t"


def test_persist_blink_login_skips_when_no_token():
    fake_client = MagicMock()
    with patch.object(secrets_loader.boto3, "client", return_value=fake_client):
        secrets_loader.persist_blink_login({"refresh_token": "r2"})
    fake_client.put_secret_value.assert_not_called()
