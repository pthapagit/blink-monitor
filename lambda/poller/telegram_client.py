# lambda/poller/telegram_client.py
# Sends video clips and notifications to your Telegram bot.
# Security: validates chat ID on every send — only YOUR chat receives clips.
# Read SECURITY.md before modifying.

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_VIDEO_SIZE_BYTES = 50 * 1024 * 1024  # Telegram bot API upload limit: 50MB


class SecurityError(Exception):
    """Raised when a security check fails (e.g. unauthorized chat ID)."""


class TelegramPermanentError(Exception):
    """A failure that retrying will NOT fix (oversize clip, HTTP 4xx).

    The caller should fall back to a text alert and advance past the clip so
    it does not wedge the queue.
    """


class TelegramTransientError(Exception):
    """A failure that MAY succeed on retry (network error, HTTP 5xx).

    The caller should leave state untouched so the clip is retried next cycle.
    """


class TelegramClient:
    """Sends video clips to a Telegram bot chat.

    Security model: allowed_chat_id is loaded from Secrets Manager and
    validated on every send. No clip is ever sent to an unverified chat ID.
    """

    def __init__(self, credentials: dict[str, Any]) -> None:
        """Initialise with Telegram credentials from Secrets Manager.

        Args:
            credentials: Dict with 'token' and 'allowed_chat_id'.
        """
        # Token is stored but NEVER logged.
        self._token = credentials["token"]
        self._allowed_chat_id = int(credentials["allowed_chat_id"])

    async def send_clip(self, video: bytes, caption: str) -> None:
        """Send a video clip to your Telegram chat.

        Args:
            video: Raw MP4 bytes.
            caption: Text shown below the video.

        Raises:
            TelegramPermanentError: Oversize clip or HTTP 4xx (don't retry).
            TelegramTransientError: Network error or HTTP 5xx (retry later).
            SecurityError: If chat-ID validation fails (should never happen).
        """
        if len(video) > MAX_VIDEO_SIZE_BYTES:
            raise TelegramPermanentError(
                f"Video {len(video)} bytes exceeds Telegram's "
                f"{MAX_VIDEO_SIZE_BYTES}-byte limit"
            )
        await self._send_video(self._allowed_chat_id, video, caption)

    async def send_text(self, message: str) -> None:
        """Send a text message to your Telegram chat (used for alerts)."""
        await self._send_message(self._allowed_chat_id, message)

    def _guard_chat_id(self, chat_id: int, kind: str) -> None:
        """Belt-and-suspenders chat-ID check before any outbound send."""
        if chat_id != self._allowed_chat_id:
            logger.error(
                "SECURITY: blocked %s send to unauthorized chat_id (expected %s)",
                kind,
                self._allowed_chat_id,
            )
            raise SecurityError(f"Unauthorized chat_id — {kind} send blocked")

    @staticmethod
    def _raise_for_status(status: int, endpoint: str) -> None:
        """Map an HTTP status to permanent vs transient. Never log the body
        (it can echo the bot token)."""
        if status == 200:
            return
        logger.error("Telegram %s returned status %d", endpoint, status)
        # 401 = revoked/rotated bot token. Drop the warm secrets cache so the
        # next poll cycle reloads from Secrets Manager.
        if status == 401:
            from secrets_loader import invalidate_cache

            invalidate_cache()
        if 400 <= status < 500:
            raise TelegramPermanentError(f"{endpoint} rejected with {status}")
        raise TelegramTransientError(f"{endpoint} failed with {status}")

    async def _send_video(self, chat_id: int, video: bytes, caption: str) -> None:
        """POST a video to Telegram's sendVideo endpoint."""
        self._guard_chat_id(chat_id, "video")
        safe_caption = caption[:1024]  # Telegram caption limit
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendVideo"

        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            form.add_field("caption", safe_caption)
            form.add_field("supports_streaming", "true")
            form.add_field(
                "video", video, filename="clip.mp4", content_type="video/mp4"
            )
            try:
                async with session.post(url, data=form) as response:
                    self._raise_for_status(response.status, "sendVideo")
                    logger.info("Telegram video sent (%d bytes)", len(video))
            except aiohttp.ClientError as e:
                logger.error("Network error sending video: %s", type(e).__name__)
                raise TelegramTransientError("Network error sending video") from e

    async def _send_message(self, chat_id: int, text: str) -> None:
        """POST a text message to Telegram's sendMessage endpoint."""
        self._guard_chat_id(chat_id, "message")
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        payload = {"chat_id": str(chat_id), "text": text[:4096]}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload) as response:
                    self._raise_for_status(response.status, "sendMessage")
            except aiohttp.ClientError as e:
                logger.error("Network error sending message: %s", type(e).__name__)
                raise TelegramTransientError("Network error sending message") from e
