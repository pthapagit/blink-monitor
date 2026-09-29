# lambda/poller/handler.py
# Main Lambda entrypoint for the Blink motion-clip poller.
# Read SECURITY.md and CLAUDE.md before modifying this file.

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Clip timestamps from Blink are UTC; display in US Eastern (EST/EDT via DST).
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")

from blink_client import BlinkAPIError, BlinkAuthError, BlinkClient
from log_redact import configure_secure_logging
from secrets_loader import load_secrets
from state_store import DeliveryState, StateStore
from telegram_client import (
    TelegramAuthError,
    TelegramClient,
    TelegramPermanentError,
    TelegramTransientError,
)

configure_secure_logging()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Hard cap on clips sent per cycle so a motion storm can't flood Telegram or
# blow the 60s Lambda timeout. Oldest clips are sent first and state advances
# per-clip, so any overflow is simply picked up on the next poll — nothing is
# silently dropped.
MAX_CLIPS_PER_CYCLE = 5

# Blink clip IDs are random, not chronological. Replaying "every higher id"
# dumps the SD card. Only deliver footage recorded inside this window, and
# only if that exact id+timestamp has not already been handled.
MAX_CLIP_AGE = timedelta(hours=2)

# A clip that cannot be read from the card must not block newer motion forever.
MAX_DELIVERY_ATTEMPTS = 3


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint. Runs one poll cycle.

    Args:
        event: Lambda event (unused — EventBridge fires on a fixed schedule).
        context: Lambda context object (unused).

    Returns:
        Status dict describing what happened this cycle.
    """
    return asyncio.run(_run_poll_cycle())


async def _run_poll_cycle() -> dict[str, Any]:
    """Run a full poll cycle: find new clips and deliver them to Telegram.

    Returns:
        Status dict describing the cycle outcome.
    """
    logger.info("Poll cycle starting")

    secrets = await load_secrets()
    state_store = StateStore()
    blink_client = BlinkClient(secrets["blink"])
    telegram_client = TelegramClient(secrets["telegram"])

    try:
        delivery_state = await state_store.get_delivery_state()
        logger.info(
            "Delivery log: remembered=%d first_run=%s legacy=%s",
            len(delivery_state.seen),
            delivery_state.first_run,
            delivery_state.legacy_cursor is not None,
        )

        await blink_client.authenticate()

        clips = await blink_client.get_sorted_clips()  # oldest -> newest
        needs_persist = (
            delivery_state.first_run or delivery_state.legacy_cursor is not None
        )
        new_clips = select_clips_to_send(
            clips, delivery_state, datetime.now(timezone.utc)
        )
        if needs_persist:
            # Retire NONE / the old single-id cursor before sending, so a
            # crash cannot fall back into a full-card replay.
            await state_store.save_delivery_state(delivery_state)

        if not new_clips:
            logger.info("No new clips found — nothing to send")
            return {"status": "no_new_clips"}

        batch = new_clips[:MAX_CLIPS_PER_CYCLE]
        logger.info(
            "Found %d new clip(s); processing %d this cycle",
            len(new_clips),
            len(batch),
        )

        sent_count = 0
        for clip in batch:
            delivered = await _process_clip(
                clip, blink_client, telegram_client, delivery_state, state_store
            )
            if not delivered:
                # Transient failure — stop the batch and DO NOT advance past
                # this clip; it (and any after it) retries next cycle.
                logger.warning(
                    "Stopping batch early at clip_id=%s due to transient failure",
                    clip.id,
                )
                break
            sent_count += 1

        return {"status": "sent", "clips_sent": sent_count}

    except (BlinkAuthError, BlinkAPIError) as e:
        logger.error("Poll cycle failed (Blink): %s", str(e))
        raise
    except Exception as e:
        logger.error("Poll cycle failed: %s", type(e).__name__)
        raise
    finally:
        await blink_client.close()


async def _process_clip(
    clip: Any,
    blink_client: BlinkClient,
    telegram_client: TelegramClient,
    delivery_state: DeliveryState,
    state_store: StateStore,
) -> bool:
    """Download one clip and deliver it (or a text fallback) to Telegram.

    A clip is remembered on success, on a permanent Telegram rejection, or
    after repeated read failures. A revoked bot token (401) is not remembered,
    so the clip is retried once the secret is fixed. Other transient failures
    retry, then give up after ``MAX_DELIVERY_ATTEMPTS``.

    Args:
        clip: A LocalStorageMediaItem (.id, .name, .created_at).
        blink_client: Authenticated Blink client.
        telegram_client: Telegram sender.
        delivery_state: In-memory delivery log for this cycle.
        state_store: Persists the log after each handled clip.

    Returns:
        True if the clip was handled (delivered or given up with a text
        alert); False on a retryable failure that should halt the batch.
    """
    clip_key = clip_identity(clip)
    clip_id = getattr(clip, "id", None)
    caption = _build_caption(clip)
    if clip_key is None:
        logger.error("clip_id=%s has no usable timestamp — skipping", clip_id)
        return True

    try:
        video_bytes = await blink_client.download_clip(clip)
        await telegram_client.send_clip(video=video_bytes, caption=caption)
        await _remember(delivery_state, state_store, clip_key)
        logger.info("Delivered clip_id=%s", clip_id)
        return True

    except TelegramAuthError:
        # Token revoked or rotated. Cache is already cleared; do not mark the
        # clip seen and do not count this toward the give-up limit.
        logger.warning(
            "Telegram rejected the bot token for clip_id=%s — will retry", clip_id
        )
        return False

    except TelegramPermanentError:
        logger.error(
            "Permanent send failure for clip_id=%s — sending text fallback", clip_id
        )
        await _send_fallback(telegram_client, caption)
        await _remember(delivery_state, state_store, clip_key)
        return True

    except (BlinkAPIError, TelegramTransientError):
        attempts = delivery_state.note_failure(clip_key)
        logger.warning(
            "Transient failure for clip_id=%s (attempt %d/%d)",
            clip_id,
            attempts,
            MAX_DELIVERY_ATTEMPTS,
        )
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            logger.error("Giving up on clip_id=%s after repeated failures", clip_id)
            await _send_fallback(telegram_client, caption)
            await _remember(delivery_state, state_store, clip_key)
            return True
        await state_store.save_delivery_state(delivery_state)
        return False


async def _remember(
    delivery_state: DeliveryState, state_store: StateStore, clip_key: str
) -> None:
    """Mark a clip handled and persist the log."""
    delivery_state.remember(clip_key)
    await state_store.save_delivery_state(delivery_state)


async def _send_fallback(telegram_client: TelegramClient, caption: str) -> None:
    """Send a text-only alert when a clip can't be delivered as video."""
    try:
        await telegram_client.send_text(
            f"{caption}\n\u26a0\ufe0f Clip could not be sent (too large or rejected)."
        )
    except (TelegramPermanentError, TelegramTransientError):
        # If even the text alert fails we still advance — the failure is logged
        # inside the Telegram client. Blocking here would wedge the queue.
        logger.error("Text fallback also failed — advancing anyway")


def select_clips_to_send(
    clips: list[Any], state: DeliveryState, now: datetime
) -> list[Any]:
    """Choose clips that still need delivery.

    Blink assigns random clip IDs, so "new" means "this id and recording time
    have not been handled" and the recording is inside ``MAX_CLIP_AGE``.
    A legacy single-id cursor is converted in place: if that clip is still on
    the card, only later recordings are eligible; if the card no longer has
    it (format, full card), the current card is marked seen and nothing is
    replayed.

    Args:
        clips: Manifest clips. Order is not trusted; results are oldest first.
        state: Delivery log. Mutated when a legacy cursor is retired.
        now: Current UTC time, injected so tests are deterministic.

    Returns:
        Clips to send, oldest first. First run (``NONE``) returns only the
        newest clip.
    """
    usable = [clip for clip in clips if clip_identity(clip) is not None]
    usable.sort(key=lambda clip: _aware(clip.created_at))
    if not usable:
        return []

    if state.first_run:
        # Remember everything already on the card except the newest clip,
        # which is the only one we deliver. Otherwise the next poll would
        # treat the rest of the 2-hour window as unseen.
        logger.info("First run — sending most recent clip only")
        for clip in usable[:-1]:
            identity = clip_identity(clip)
            if identity is not None:
                state.remember(identity)
        state.first_run = False
        return [usable[-1]]

    if state.legacy_cursor is not None:
        seeded, pending = _migrate_legacy_cursor(usable, state.legacy_cursor, now)
        for clip_key in seeded:
            state.remember(clip_key)
        logger.info(
            "Retired legacy cursor %s — remembered %d clip(s), %d pending",
            state.legacy_cursor,
            len(seeded),
            len(pending),
        )
        state.legacy_cursor = None
        return pending

    cutoff = now - MAX_CLIP_AGE
    seen = state.seen_set()
    pending: list[Any] = []
    for clip in usable:
        created_at = _aware(clip.created_at)
        if clip_identity(clip) in seen or created_at < cutoff:
            continue
        pending.append(clip)
    return pending


def _migrate_legacy_cursor(
    clips: list[Any], cursor: str, now: datetime
) -> tuple[list[str], list[Any]]:
    """Convert a plain last-seen id into remembered keys plus clips to send.

    Args:
        clips: Usable manifest clips, any order.
        cursor: Clip id stored by an older build.
        now: Current UTC time.

    Returns:
        ``(keys_to_remember, clips_to_send)``. When the cursor clip is gone,
        every clip currently on the card is remembered and nothing is sent.
    """
    match = next((clip for clip in clips if str(clip.id) == str(cursor)), None)
    if match is None:
        logger.warning(
            "Legacy cursor %s is not on the card — marking %d clip(s) seen "
            "without resending",
            cursor,
            len(clips),
        )
        return [clip_identity(clip) for clip in clips if clip_identity(clip)], []

    cursor_time = _aware(match.created_at)
    cutoff = now - MAX_CLIP_AGE
    seeded: list[str] = []
    pending: list[Any] = []
    for clip in sorted(clips, key=lambda item: _aware(item.created_at)):
        created_at = _aware(clip.created_at)
        identity = clip_identity(clip)
        if identity is None:
            continue
        if created_at <= cursor_time:
            seeded.append(identity)
        elif created_at >= cutoff:
            pending.append(clip)
    return seeded, pending


def clip_identity(clip: Any) -> str | None:
    """Build the remembered key for a clip: ``id:unix_timestamp``.

    Args:
        clip: Object with ``.id`` and ``.created_at``.

    Returns:
        Key string, or None when the clip has no usable timestamp. The same
        id recorded again at a different time (for example after a card
        format) produces a different key.
    """
    created_at = getattr(clip, "created_at", None)
    if not isinstance(created_at, datetime):
        return None
    clip_id = getattr(clip, "id", None)
    if clip_id is None:
        return None
    return f"{clip_id}:{int(_aware(created_at).timestamp())}"


def _aware(created_at: datetime) -> datetime:
    """Treat a naive timestamp as UTC."""
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)
    return created_at


def _build_caption(clip: Any) -> str:
    """Build a human-readable Telegram caption for a clip.

    Args:
        clip: LocalStorageMediaItem with .name and .created_at (datetime).

    Returns:
        Caption string. No clip IDs or internal data — keep messages clean.
    """
    camera_name = getattr(clip, "name", "Camera") or "Camera"
    created_at = getattr(clip, "created_at", None)
    try:
        formatted_time = _format_clip_time(created_at)
    except (AttributeError, ValueError, TypeError):
        formatted_time = "Unknown time"

    return (
        f"\U0001f6a8 Motion detected\n"
        f"\U0001f4f7 {camera_name}\n"
        f"\U0001f554 {formatted_time}"
    )


def _format_clip_time(created_at: datetime) -> str:
    """Format a clip timestamp for Telegram captions in US Eastern time.

    Blink manifest times are UTC. Naive datetimes are treated as UTC before
    converting to America/New_York (shows EST or EDT automatically).

    Args:
        created_at: Clip creation time from blinkpy LocalStorageMediaItem.

    Returns:
        Human-readable string, e.g. ``Jun 06 2026, 02:32 PM EDT``.
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    eastern = created_at.astimezone(DISPLAY_TIMEZONE)
    return eastern.strftime("%b %d %Y, %I:%M %p %Z")
