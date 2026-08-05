"""Tests for blink_client.py: headless auth, 2FA surfacing, token-refresh
persistence, SD-storage guard, and the non-deleting download path."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from blinkpy.auth import BlinkTwoFARequiredError

import blink_client
from blink_client import BlinkAPIError, BlinkAuthError, BlinkClient

LOGIN = {"token": "old", "refresh_token": "r", "hardware_id": "h"}


class FakeAuth:
    def __init__(self, token="new"):
        self.token = token
        self.refresh_token = "r2"
        self.login_attributes = {"token": token, "refresh_token": "r2", "hardware_id": "h"}


class FakeSync:
    def __init__(self, local_storage=True, manifest=None):
        self.network_id = "net-1"
        self.local_storage = local_storage
        self._local_storage = {"manifest": manifest or []}
        self.update_local_storage_manifest = AsyncMock(return_value=True)


def _make_blink(sync=None, start_result=True, start_exc=None):
    blink = MagicMock()
    blink.start = AsyncMock(return_value=start_result, side_effect=start_exc)
    blink.sync = {"sm": sync} if sync is not None else {}
    return blink


def _patches(auth, blink):
    return (
        patch.object(blink_client, "Auth", return_value=auth),
        patch.object(blink_client, "Blink", return_value=blink),
        patch.object(blink_client.aiohttp, "ClientSession", return_value=MagicMock()),
    )


async def _run_authenticate(client, auth, blink, persist):
    p_auth, p_blink, p_sess = _patches(auth, blink)
    with p_auth, p_blink, p_sess, patch.object(
        blink_client, "persist_blink_login", persist
    ):
        await client.authenticate()


async def test_authenticate_persists_on_token_change():
    client = BlinkClient(LOGIN)
    persist = MagicMock()
    await _run_authenticate(client, FakeAuth(token="new"), _make_blink(FakeSync()), persist)
    assert client.network_id == "net-1"
    persist.assert_called_once()  # token changed old -> new


async def test_authenticate_skips_persist_when_token_unchanged():
    client = BlinkClient(LOGIN)
    persist = MagicMock()
    await _run_authenticate(client, FakeAuth(token="old"), _make_blink(FakeSync()), persist)
    persist.assert_not_called()


async def test_authenticate_2fa_raises_auth_error():
    client = BlinkClient(LOGIN)
    persist = MagicMock()
    with pytest.raises(BlinkAuthError):
        await _run_authenticate(
            client, FakeAuth(), _make_blink(FakeSync(), start_exc=BlinkTwoFARequiredError()), persist
        )


async def test_authenticate_no_sync_raises_api_error():
    client = BlinkClient(LOGIN)
    persist = MagicMock()
    with pytest.raises(BlinkAPIError):
        await _run_authenticate(client, FakeAuth(), _make_blink(sync=None), persist)


async def test_authenticate_local_storage_off_raises():
    client = BlinkClient(LOGIN)
    persist = MagicMock()
    with pytest.raises(BlinkAPIError):
        await _run_authenticate(
            client, FakeAuth(), _make_blink(FakeSync(local_storage=False)), persist
        )


async def test_get_sorted_clips_returns_manifest_list():
    client = BlinkClient(LOGIN)
    sync = FakeSync(manifest=["a", "b", "c"])
    client._sync = sync
    assert await client.get_sorted_clips() == ["a", "b", "c"]


async def test_download_clip_uses_non_deleting_path_and_cleans_up():
    client = BlinkClient(LOGIN)
    client._blink = MagicMock()

    clip = AsyncMock()
    clip.id = 55
    clip.prepare_download = AsyncMock(return_value=True)

    async def _write(_blink, file_name, *a, **k):
        with open(file_name, "wb") as fh:
            fh.write(b"video-bytes")
        return True

    clip.download_video = AsyncMock(side_effect=_write)

    data = await client.download_clip(clip)

    assert data == b"video-bytes"
    clip.download_video.assert_awaited_once()
    clip.download_video_delete.assert_not_called()  # SD copy must be preserved
