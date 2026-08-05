# lambda/poller/blink_client.py
# Thin wrapper around blinkpy (0.25.x) for Sync Module SD-card clip access.
#
# We deliberately use blinkpy's NATIVE local-storage helpers instead of
# hand-rolling the manifest/upload/download HTTP calls. That code tracked an
# unofficial API and was the most fragile, hardest-to-secure part of the
# project; the maintained library now owns it.
#
# IMPORTANT: we download with LocalStorageMediaItem.download_video() — NOT
# download_video_delete() — so the clip is PRESERVED on the SD card. The user
# relies on the local copy.
#
# Read SECURITY.md before modifying — Blink credentials are sensitive.

import logging
import os
from typing import Any

import aiohttp
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink

from secrets_loader import persist_blink_login

logger = logging.getLogger(__name__)

# Where we briefly stage a downloaded clip before reading its bytes.
# Lambda /tmp is ephemeral (cleared between cold starts) and isolated per
# execution environment.
TMP_DIR = "/tmp"


class BlinkAuthError(Exception):
    """Raised when Blink authentication fails and cannot be recovered headless."""


class BlinkAPIError(Exception):
    """Raised when a Blink API call fails after the library's own retries."""


class BlinkClient:
    """Wrapper around blinkpy for SD-card clip access.

    Handles authentication (token-based, headless), local-storage manifest
    refresh, and the SD card -> Blink cloud upload -> download flow without
    deleting the local copy.
    """

    def __init__(self, login_blob: dict[str, Any]) -> None:
        """Initialise the client.

        Args:
            login_blob: The blinkpy login dict from Secrets Manager. Must
                contain username/password and, after bootstrap, token +
                refresh_token + hardware_id. Loaded via secrets_loader only.
        """
        self._login = login_blob
        self._initial_token: str | None = login_blob.get("token")
        self._blink: Blink | None = None
        self._auth: Auth | None = None
        self._sync: Any = None
        self._session: aiohttp.ClientSession | None = None
        self.network_id: str = ""

    async def authenticate(self) -> None:
        """Authenticate to Blink using the stored token, headless.

        blinkpy refreshes its OAuth token transparently using the stored
        refresh_token + hardware_id. If that fails (e.g. refresh token revoked)
        Blink demands 2FA, which cannot be satisfied inside Lambda — we surface
        that as a clear, actionable error instead of hanging.

        Persists the refreshed login blob back to Secrets Manager ONLY when the
        access token actually changed (avoids per-poll secret churn).

        Raises:
            BlinkAuthError: If authentication fails or 2FA is required.
        """
        self._session = aiohttp.ClientSession()

        # Pass a COPY so blinkpy can mutate its working dict without aliasing
        # our cached secret.
        auth = Auth(dict(self._login), no_prompt=True, session=self._session)
        self._blink = Blink(session=self._session)
        self._blink.auth = auth

        try:
            started = await self._blink.start()
        except BlinkTwoFARequiredError as e:
            logger.error(
                "Blink requires 2FA — the stored token is no longer valid. "
                "Re-run scripts/bootstrap_auth.py to seed a fresh token."
            )
            raise BlinkAuthError("Blink requires 2FA (headless re-auth impossible)") from e
        except Exception as e:
            logger.error("Blink authentication failed: %s", type(e).__name__)
            raise BlinkAuthError("Failed to authenticate with Blink") from e

        if not started:
            raise BlinkAuthError("Blink setup did not complete (start() returned False)")

        self._auth = auth
        self._select_sync_module()

        # Persist only on a real token refresh.
        if auth.token and auth.token != self._initial_token:
            logger.info("Blink access token was refreshed — persisting new blob")
            persist_blink_login(auth.login_attributes)

        logger.info("Blink authentication successful, network_id=%s", self.network_id)

    def _select_sync_module(self) -> None:
        """Pick the (single) Sync Module and verify SD storage is active.

        Raises:
            BlinkAPIError: If no sync module is found or local storage is off.
        """
        if not self._blink or not self._blink.sync:
            raise BlinkAPIError("No sync modules found after authentication")

        sync_name = next(iter(self._blink.sync))
        self._sync = self._blink.sync[sync_name]
        self.network_id = str(getattr(self._sync, "network_id", ""))

        if not self.network_id:
            raise BlinkAPIError("Could not extract network_id from sync module")

        if not self._sync.local_storage:
            raise BlinkAPIError(
                "Local storage (SD card) is not active on the Sync Module"
            )

    async def get_sorted_clips(self) -> list[Any]:
        """Refresh the local-storage manifest and return clips oldest -> newest.

        Returns:
            List of blinkpy LocalStorageMediaItem objects, each exposing
            .id (int), .name (camera), .created_at (datetime). Empty if none.

        Raises:
            BlinkAPIError: If the manifest could not be refreshed.
        """
        if not self._sync:
            raise BlinkAPIError("Not authenticated — call authenticate() first")

        result = await self._sync.update_local_storage_manifest()
        if result is None:
            raise BlinkAPIError("Failed to refresh local storage manifest")

        # The manifest is a SortedSet ordered ascending by created_at. blinkpy
        # exposes no public accessor, so we read the documented internal store.
        manifest = self._sync._local_storage.get("manifest")
        if not manifest:
            return []
        return list(manifest)

    async def download_clip(self, clip_item: Any) -> bytes:
        """Trigger SD -> cloud upload and download the clip WITHOUT deleting it.

        Args:
            clip_item: A LocalStorageMediaItem from get_sorted_clips().

        Returns:
            Raw MP4 bytes.

        Raises:
            BlinkAPIError: If the upload trigger or download fails.
        """
        if not self._blink:
            raise BlinkAPIError("Not authenticated — call authenticate() first")

        clip_id = clip_item.id
        logger.info("Requesting SD-card upload for clip_id=%s", clip_id)

        prepared = await clip_item.prepare_download(self._blink)
        if not prepared:
            raise BlinkAPIError(f"Failed to trigger upload for clip {clip_id}")

        tmp_path = os.path.join(TMP_DIR, f"blink-{clip_id}.mp4")
        try:
            ok = await clip_item.download_video(self._blink, tmp_path)
            if not ok:
                raise BlinkAPIError(f"Clip {clip_id} download failed")
            with open(tmp_path, "rb") as fh:
                data = fh.read()
            logger.info("Clip downloaded: clip_id=%s (%d bytes)", clip_id, len(data))
            return data
        finally:
            # Don't leave footage sitting in /tmp longer than necessary.
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                logger.warning("Could not remove temp file for clip_id=%s", clip_id)

    async def close(self) -> None:
        """Close the aiohttp session. Always call when done with the client."""
        if self._session and not self._session.closed:
            await self._session.close()
