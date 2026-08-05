# lambda/poller/state_store.py
# SSM Parameter Store-backed state for tracking the last sent clip.
# Prevents duplicate Telegram notifications across Lambda invocations.

import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

LAST_SEEN_PARAM = "/blink-monitor/last-seen-clip-id"
INITIAL_VALUE = "NONE"


class StateStore:
    """Persistent state store backed by SSM Parameter Store.

    Tracks the ID of the last clip successfully sent to Telegram.
    State survives Lambda restarts and cold starts.
    """

    def __init__(self) -> None:
        self._ssm = boto3.client("ssm", region_name="us-east-1")

    async def get_last_seen_clip_id(self) -> str:
        """Get the ID of the last clip sent to Telegram.

        Returns:
            Clip ID string, or "NONE" if no clips have been sent yet.
        """
        try:
            response = self._ssm.get_parameter(Name=LAST_SEEN_PARAM)
            value = response["Parameter"]["Value"]
            return value
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ParameterNotFound":
                logger.warning(
                    "SSM parameter %s not found — treating as first run",
                    LAST_SEEN_PARAM,
                )
                return INITIAL_VALUE
            logger.error(
                "Failed to read SSM parameter %s: %s", LAST_SEEN_PARAM, error_code
            )
            raise

    async def set_last_seen_clip_id(self, clip_id: str) -> None:
        """Update the last-seen clip ID after a successful Telegram send.

        Only call this AFTER the Telegram send succeeds. If called before,
        a failed send will result in the clip never being retried.

        Args:
            clip_id: The clip ID that was just successfully sent.
        """
        if not clip_id:
            logger.error("Attempted to set empty clip_id in state — skipping")
            return

        try:
            self._ssm.put_parameter(
                Name=LAST_SEEN_PARAM,
                Value=clip_id,
                Type="String",
                Overwrite=True,
            )
            logger.info("State updated: last_seen_clip_id=%s", clip_id)
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            logger.error(
                "Failed to update SSM parameter %s: %s", LAST_SEEN_PARAM, error_code
            )
            raise
