"""Tests for telegram_client.py: chat-ID locking and permanent-vs-transient
failure classification (the behavior the handler's queue logic depends on)."""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from telegram_client import (
    MAX_VIDEO_SIZE_BYTES,
    SecurityError,
    TelegramClient,
    TelegramPermanentError,
    TelegramTransientError,
)

CREDS = {"token": "123:ABC", "allowed_chat_id": 987654321}


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeSession:
    """Minimal async context-manager stand-in for aiohttp.ClientSession."""

    def __init__(self, status=200, raise_exc=None):
        self._status = status
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *args, **kwargs):
        raise_exc = self._raise
        status = self._status

        @asynccontextmanager
        async def _cm():
            if raise_exc:
                raise raise_exc
            yield _FakeResponse(status)

        return _cm()


def _patch_session(**kwargs):
    return patch("telegram_client.aiohttp.ClientSession", return_value=_FakeSession(**kwargs))


async def test_oversize_clip_is_permanent():
    client = TelegramClient(CREDS)
    with pytest.raises(TelegramPermanentError):
        await client.send_clip(b"x" * (MAX_VIDEO_SIZE_BYTES + 1), "cap")


async def test_send_clip_success():
    client = TelegramClient(CREDS)
    with _patch_session(status=200):
        await client.send_clip(b"small", "cap")  # no raise


async def test_http_4xx_is_permanent():
    client = TelegramClient(CREDS)
    with _patch_session(status=400):
        with pytest.raises(TelegramPermanentError):
            await client.send_clip(b"small", "cap")


async def test_http_5xx_is_transient():
    client = TelegramClient(CREDS)
    with _patch_session(status=503):
        with pytest.raises(TelegramTransientError):
            await client.send_clip(b"small", "cap")


async def test_network_error_is_transient():
    import aiohttp

    client = TelegramClient(CREDS)
    with _patch_session(raise_exc=aiohttp.ClientError("boom")):
        with pytest.raises(TelegramTransientError):
            await client.send_clip(b"small", "cap")


async def test_chat_id_guard_blocks_mismatch():
    client = TelegramClient(CREDS)
    with pytest.raises(SecurityError):
        await client._send_message(111111, "hi")  # not the allowed chat id
