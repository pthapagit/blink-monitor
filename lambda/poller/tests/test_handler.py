"""Tests for handler.py: clip deduplication, captioning, and the per-clip
delivery state machine (success / permanent-fail fallback / transient halt)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import handler
from telegram_client import TelegramPermanentError, TelegramTransientError
from blink_client import BlinkAPIError


class FakeClip:
    """Stand-in for blinkpy's LocalStorageMediaItem."""

    def __init__(self, clip_id, name="Front Door", created_at=None):
        self.id = clip_id
        self.name = name
        self.created_at = created_at or datetime(2026, 6, 6, 14, 32, tzinfo=timezone.utc)


def clips(*ids):
    return [FakeClip(i) for i in ids]


# ---------------------------------------------------------------------------
# _find_new_clips — the dedup core
# ---------------------------------------------------------------------------

def test_find_new_clips_empty_manifest():
    assert handler._find_new_clips([], "NONE") == []


def test_first_run_sends_only_most_recent():
    result = handler._find_new_clips(clips(1, 2, 3), "NONE")
    assert [c.id for c in result] == [3]


def test_returns_clips_after_last_seen():
    result = handler._find_new_clips(clips(1, 2, 3, 4), "2")
    assert [c.id for c in result] == [3, 4]


def test_last_seen_is_newest_returns_nothing():
    assert handler._find_new_clips(clips(1, 2, 3), "3") == []


def test_last_seen_missing_returns_all_manifest_clips():
    # Last-seen clip rotated off the SD card — deliver remaining clips so
    # intermediate motion events are not silently dropped.
    result = handler._find_new_clips(clips(5, 6, 7), "2")
    assert [c.id for c in result] == [5, 6, 7]


# ---------------------------------------------------------------------------
# _build_caption
# ---------------------------------------------------------------------------

def test_caption_includes_camera_and_time_no_clip_id():
    caption = handler._build_caption(FakeClip(99, name="Back Yard"))
    assert "Back Yard" in caption
    assert "2026" in caption
    assert "99" not in caption  # never leak internal IDs into messages
    assert "UTC" not in caption


def test_caption_shows_us_eastern_not_utc():
    # 2026-06-06 18:32 UTC → 02:32 PM EDT (summer)
    clip = FakeClip(
        1,
        created_at=datetime(2026, 6, 6, 18, 32, tzinfo=timezone.utc),
    )
    caption = handler._build_caption(clip)
    assert "EDT" in caption
    assert "02:32 PM" in caption


def test_caption_handles_bad_timestamp():
    clip = FakeClip(1)
    clip.created_at = "not-a-datetime"
    caption = handler._build_caption(clip)
    assert "Unknown time" in caption


# ---------------------------------------------------------------------------
# _process_clip — delivery state machine
# ---------------------------------------------------------------------------

def _mocks():
    blink = AsyncMock()
    blink.download_clip.return_value = b"x" * 2048
    telegram = AsyncMock()
    state = AsyncMock()
    return blink, telegram, state


async def test_process_clip_success_advances_state():
    blink, telegram, state = _mocks()
    ok = await handler._process_clip(FakeClip(7), blink, telegram, state)
    assert ok is True
    telegram.send_clip.assert_awaited_once()
    state.set_last_seen_clip_id.assert_awaited_once_with("7")


async def test_process_clip_permanent_failure_sends_text_and_advances():
    blink, telegram, state = _mocks()
    telegram.send_clip.side_effect = TelegramPermanentError("too big")
    ok = await handler._process_clip(FakeClip(8), blink, telegram, state)
    assert ok is True
    telegram.send_text.assert_awaited_once()  # text fallback fired
    state.set_last_seen_clip_id.assert_awaited_once_with("8")


async def test_process_clip_transient_failure_does_not_advance():
    blink, telegram, state = _mocks()
    telegram.send_clip.side_effect = TelegramTransientError("network")
    ok = await handler._process_clip(FakeClip(9), blink, telegram, state)
    assert ok is False
    state.set_last_seen_clip_id.assert_not_awaited()


async def test_process_clip_blink_download_error_is_transient():
    blink, telegram, state = _mocks()
    blink.download_clip.side_effect = BlinkAPIError("upload not ready")
    ok = await handler._process_clip(FakeClip(10), blink, telegram, state)
    assert ok is False
    telegram.send_clip.assert_not_awaited()
    state.set_last_seen_clip_id.assert_not_awaited()
