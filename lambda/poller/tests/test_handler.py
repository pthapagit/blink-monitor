"""Tests for handler.py: clip selection, captioning, and delivery outcomes."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import handler
from blink_client import BlinkAPIError
from state_store import DeliveryState
from telegram_client import (
    TelegramAuthError,
    TelegramPermanentError,
    TelegramTransientError,
)


class FakeClip:
    """Stand-in for blinkpy's LocalStorageMediaItem."""

    def __init__(self, clip_id, name="Front Door", created_at=None):
        self.id = clip_id
        self.name = name
        self.created_at = created_at or datetime(2026, 6, 6, 14, 32, tzinfo=timezone.utc)


NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)


def at(minutes_ago, clip_id):
    """Clip recorded ``minutes_ago`` before NOW. IDs are intentionally random."""
    return FakeClip(clip_id, created_at=NOW - timedelta(minutes=minutes_ago))


def fresh_state(**kwargs):
    return DeliveryState(**kwargs)


def selected(clips, state):
    return handler.select_clips_to_send(clips, state, NOW)


# ---------------------------------------------------------------------------
# select_clips_to_send
# ---------------------------------------------------------------------------

def test_empty_manifest():
    assert selected([], fresh_state(first_run=True)) == []


def test_first_run_sends_only_newest_and_suppresses_the_rest():
    state = fresh_state(first_run=True)
    card = [at(30, 900), at(10, 5), at(50, 4000)]
    result = selected(card, state)
    assert [clip.id for clip in result] == [5]
    assert state.first_run is False
    assert handler.clip_identity(at(30, 900)) in state.seen_set()
    assert handler.clip_identity(at(10, 5)) not in state.seen_set()


def test_already_seen_clip_is_not_resent():
    clip = at(10, 42)
    state = fresh_state(seen=[handler.clip_identity(clip)])
    result = selected([clip, at(5, 99)], state)
    assert [item.id for item in result] == [99]


def test_random_lower_id_recorded_later_is_still_sent():
    # IDs do not increase with time. A smaller id can be the newer clip.
    older = at(30, 3_000_000_000)
    newer = at(5, 150_000)
    state = fresh_state(seen=[handler.clip_identity(older)])
    result = selected([older, newer], state)
    assert [clip.id for clip in result] == [150_000]


def test_clip_older_than_two_hours_is_not_sent():
    state = fresh_state()
    result = selected([at(180, 111), at(15, 222)], state)
    assert [clip.id for clip in result] == [222]


def test_unseen_clips_inside_window_go_oldest_first():
    result = selected([at(5, 3), at(40, 1), at(20, 2)], fresh_state())
    assert [clip.id for clip in result] == [1, 2, 3]


def test_same_id_new_timestamp_after_format_is_sent():
    # Card format reused clip id 7 at a new recording time.
    old = FakeClip(7, created_at=NOW - timedelta(days=2))
    new = at(10, 7)
    state = fresh_state(seen=[handler.clip_identity(old)])
    result = selected([new], state)
    assert [clip.id for clip in result] == [7]
    assert handler.clip_identity(old) != handler.clip_identity(new)


def test_legacy_cursor_still_on_card_sends_only_later_recordings():
    cursor_clip = at(40, 500)
    before = at(50, 9000)
    after = at(10, 12)
    state = fresh_state(legacy_cursor="500")
    result = selected([before, cursor_clip, after], state)
    assert [clip.id for clip in result] == [12]
    assert state.legacy_cursor is None
    assert handler.clip_identity(before) in state.seen_set()
    assert handler.clip_identity(cursor_clip) in state.seen_set()


def test_legacy_cursor_missing_after_format_or_full_card_sends_nothing():
    # The saved id is gone and every remaining id is "random". Do not replay.
    state = fresh_state(legacy_cursor="731159553")
    card = [at(10, 4), at(20, 9_000_000), at(30, 15)]
    assert selected(card, state) == []
    assert state.legacy_cursor is None
    assert len(state.seen) == 3


def test_clock_skew_within_window_is_not_hidden_behind_sort_order():
    # New clip stamped 30 minutes in the past, id not remembered.
    skewed = at(30, 77)
    state = fresh_state(seen=[handler.clip_identity(at(10, 88))])
    result = selected([at(10, 88), skewed], state)
    assert [clip.id for clip in result] == [77]


def test_future_timestamp_does_not_block_later_real_clips():
    future = FakeClip(21, created_at=NOW + timedelta(days=30))
    real = at(5, 22)
    state = fresh_state(seen=[handler.clip_identity(future)])
    result = selected([future, real], state)
    assert [clip.id for clip in result] == [22]


def test_two_cameras_same_second_both_send():
    moment = NOW - timedelta(minutes=5)
    front = FakeClip(30, name="Front Door", created_at=moment)
    outdoor = FakeClip(31, name="Outdoor", created_at=moment)
    result = selected([front, outdoor], fresh_state())
    assert {clip.id for clip in result} == {30, 31}


def test_clip_without_timestamp_is_ignored():
    broken = FakeClip(1)
    broken.created_at = None
    result = selected([broken, at(5, 2)], fresh_state())
    assert [item.id for item in result] == [2]


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

def _delivery():
    blink = AsyncMock()
    blink.download_clip.return_value = b"x" * 2048
    telegram = AsyncMock()
    store = AsyncMock()
    state = DeliveryState()
    return blink, telegram, store, state


async def test_process_clip_success_remembers_id_and_time():
    blink, telegram, store, state = _delivery()
    clip = at(5, 7)
    ok = await handler._process_clip(clip, blink, telegram, state, store)
    assert ok is True
    telegram.send_clip.assert_awaited_once()
    assert handler.clip_identity(clip) in state.seen_set()
    store.save_delivery_state.assert_awaited()


async def test_process_clip_permanent_failure_sends_text_and_remembers():
    blink, telegram, store, state = _delivery()
    telegram.send_clip.side_effect = TelegramPermanentError("too big")
    clip = at(5, 8)
    ok = await handler._process_clip(clip, blink, telegram, state, store)
    assert ok is True
    telegram.send_text.assert_awaited_once()
    assert handler.clip_identity(clip) in state.seen_set()


async def test_process_clip_transient_failure_retries_without_remembering():
    blink, telegram, store, state = _delivery()
    telegram.send_clip.side_effect = TelegramTransientError("network")
    clip = at(5, 9)
    ok = await handler._process_clip(clip, blink, telegram, state, store)
    assert ok is False
    assert handler.clip_identity(clip) not in state.seen_set()
    assert state.failures[handler.clip_identity(clip)] == 1


async def test_process_clip_gives_up_after_repeated_download_failures():
    blink, telegram, store, state = _delivery()
    blink.download_clip.side_effect = BlinkAPIError("upload not ready")
    clip = at(5, 10)
    identity = handler.clip_identity(clip)
    state.failures[identity] = handler.MAX_DELIVERY_ATTEMPTS - 1
    ok = await handler._process_clip(clip, blink, telegram, state, store)
    assert ok is True
    telegram.send_clip.assert_not_awaited()
    telegram.send_text.assert_awaited_once()
    assert identity in state.seen_set()
    assert identity not in state.failures


async def test_process_clip_auth_failure_does_not_remember_or_count():
    blink, telegram, store, state = _delivery()
    telegram.send_clip.side_effect = TelegramAuthError("sendVideo rejected with 401")
    clip = at(5, 11)
    ok = await handler._process_clip(clip, blink, telegram, state, store)
    assert ok is False
    assert handler.clip_identity(clip) not in state.seen_set()
    assert state.failures == {}
    store.save_delivery_state.assert_not_awaited()
