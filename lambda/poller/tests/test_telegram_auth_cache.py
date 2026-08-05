"""Tests for Telegram 401 → secrets cache invalidation."""

from unittest.mock import patch

import pytest

from telegram_client import TelegramClient, TelegramPermanentError


def test_raise_for_status_401_invalidates_secrets_cache():
    with patch("secrets_loader.invalidate_cache") as invalidate:
        with pytest.raises(TelegramPermanentError):
            TelegramClient._raise_for_status(401, "sendVideo")
    invalidate.assert_called_once()


def test_raise_for_status_400_does_not_invalidate():
    with patch("secrets_loader.invalidate_cache") as invalidate:
        with pytest.raises(TelegramPermanentError):
            TelegramClient._raise_for_status(400, "sendVideo")
    invalidate.assert_not_called()
