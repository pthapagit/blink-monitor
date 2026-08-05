# lambda/poller/handler.py
# Main Lambda entrypoint for the Blink motion-clip poller.
# Read SECURITY.md and CLAUDE.md before modifying this file.

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Clip timestamps from Blink are UTC; display in US Eastern (EST/EDT via DST).
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")

from blink_client import BlinkAPIError, BlinkAuthError, BlinkClient
from secrets_loader import load_secrets
from state_store import StateStore
from telegram_client import (
    TelegramClient,
    TelegramPermanentError,
    TelegramTransientError,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Hard cap on clips sent per cycle so a motion storm can't flood Telegram or
# blow the 60s Lambda timeout. Oldest clips are sent first and state advances
# per-clip, so any overflow is simply picked up on the next poll — nothing is
# silently dropped.
MAX_CLIPS_PER_CYCLE = 5

INITIAL_STATE = "NONE"


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
        last_seen_clip_id = await state_store.get_last_seen_clip_id()
        logger.info("Last seen clip ID: %s", last_seen_clip_id)

        await blink_client.authenticate()

        clips = await blink_client.get_sorted_clips()  # oldest -> newest
        new_clips = _find_new_clips(clips, last_seen_clip_id)

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
                clip, blink_client, telegram_client, state_store
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
    state_store: StateStore,
) -> bool:
    """Download one clip and deliver it (or a text fallback) to Telegram.

    State is advanced past the clip on success OR on a permanent failure (so a
    single un-sendable clip can't wedge the queue forever). It is NOT advanced
    on a transient failure, so the clip is retried next cycle.

    Args:
        clip: A LocalStorageMediaItem (.id, .name, .created_at).
        blink_client: Authenticated Blink client.
        telegram_client: Telegram sender.
        state_store: SSM-backed state.

    Returns:
        True if the clip was handled (delivered or permanently skipped with a
        text alert); False on a transient failure that should halt the batch.
    """
    clip_id = str(clip.id)
    caption = _build_caption(clip)

    try:
        video_bytes = await blink_client.download_clip(clip)
        await telegram_client.send_clip(video=video_bytes, caption=caption)
        await state_store.set_last_seen_clip_id(clip_id)
        logger.info("Delivered clip_id=%s", clip_id)
        return True

    except TelegramPermanentError as e:
        # Clip is fundamentally un-sendable (too large / rejected). Send a text
        # alert so the event isn't silent, then advance so newer clips flow.
        logger.error(
            "Permanent send failure for clip_id=%s (%s) — sending text fallback",
            clip_id,
            type(e).__name__,
        )
        await _send_fallback(telegram_client, caption)
        await state_store.set_last_seen_clip_id(clip_id)
        return True

    except (BlinkAPIError, TelegramTransientError) as e:
        # Transient (network/5xx/Blink upload not ready) — leave state untouched
        # so the clip retries next cycle.
        logger.warning(
            "Transient failure for clip_id=%s: %s", clip_id, type(e).__name__
        )
        return False


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


def _find_new_clips(clips: list[Any], last_seen_clip_id: str) -> list[Any]:
    """Return clips newer than last_seen, oldest first.

    Args:
        clips: LocalStorageMediaItem list, oldest -> newest.
        last_seen_clip_id: ID of the last clip successfully delivered, or
            "NONE" on first run.

    Returns:
        Sublist of clips that still need to be sent, oldest first.
    """
    if not clips:
        return []

    if last_seen_clip_id == INITIAL_STATE:
        # First run: send only the single most recent clip, never the whole
        # SD-card backlog.
        logger.info("First run — sending most recent clip only")
        return [clips[-1]]

    clip_ids = [str(clip.id) for clip in clips]
    if last_seen_clip_id not in clip_ids:
        # Last-seen clip rotated off the SD card. Safe default: most recent only.
        logger.warning(
            "last_seen_clip_id=%s not in manifest — defaulting to most recent",
            last_seen_clip_id,
        )
        return [clips[-1]]

    last_index = clip_ids.index(last_seen_clip_id)
    return clips[last_index + 1:]


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
