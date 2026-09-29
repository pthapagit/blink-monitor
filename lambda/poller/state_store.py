# lambda/poller/state_store.py
# SSM Parameter Store-backed delivery log.
# Remembers recently sent clips by id + recording time so a formatted card,
# a full card, or a random Blink clip id cannot replay or hide footage.

import json
import logging
import os
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

LAST_SEEN_PARAM = "/blink-monitor/last-seen-clip-id"
INITIAL_VALUE = "NONE"
# Standard SSM parameters are capped at 4 KB. Each entry is "id:unix_ts".
MAX_SEEN_CLIPS = 80
AWS_REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)


@dataclass
class DeliveryState:
    """Clips already handled, plus retry counts for ones that failed.

    ``first_run`` is the legacy ``NONE`` marker: send only the newest clip.
    ``legacy_cursor`` is a plain clip id from older builds. The poller converts
    it once, then stores this JSON form.
    """

    seen: list[str] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    legacy_cursor: str | None = None
    first_run: bool = False

    def seen_set(self) -> set[str]:
        """Return the remembered clip keys."""
        return set(self.seen)

    def remember(self, clip_key: str) -> None:
        """Record a clip as handled and drop the oldest entries past the cap.

        Args:
            clip_key: ``"{clip_id}:{unix_timestamp}"``.
        """
        if clip_key not in self.seen_set():
            self.seen.append(clip_key)
        self.failures.pop(clip_key, None)
        if len(self.seen) > MAX_SEEN_CLIPS:
            self.seen = _newest_keys(self.seen, MAX_SEEN_CLIPS)

    def note_failure(self, clip_key: str) -> int:
        """Increment and return the failed-delivery count for a clip.

        Args:
            clip_key: ``"{clip_id}:{unix_timestamp}"``.

        Returns:
            Attempts so far, including this one.
        """
        self.failures[clip_key] = self.failures.get(clip_key, 0) + 1
        return self.failures[clip_key]


def parse_parameter_value(value: str) -> DeliveryState:
    """Parse the SSM parameter into delivery state.

    Args:
        value: Raw parameter string. ``NONE``, a legacy clip id, or JSON.

    Returns:
        Delivery state. Unknown JSON is treated as a first run so a corrupt
        value cannot replay the whole card.
    """
    raw = (value or "").strip()
    if not raw or raw == INITIAL_VALUE:
        return DeliveryState(first_run=True)
    if not raw.startswith("{"):
        return DeliveryState(legacy_cursor=raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Delivery state is not valid JSON — treating as first run")
        return DeliveryState(first_run=True)
    seen = [item for item in payload.get("seen", []) if isinstance(item, str)]
    failures_raw = payload.get("fail", {})
    failures: dict[str, int] = {}
    if isinstance(failures_raw, dict):
        for key, count in failures_raw.items():
            if isinstance(key, str) and isinstance(count, int) and count > 0:
                failures[key] = count
    return DeliveryState(seen=_newest_keys(seen, MAX_SEEN_CLIPS), failures=failures)


def serialize_state(state: DeliveryState) -> str:
    """Encode delivery state as compact JSON for SSM.

    Args:
        state: State to persist. Legacy/first-run flags are not written;
            saving always switches the parameter to the JSON log.

    Returns:
        JSON string under the 4 KB standard-parameter limit.
    """
    seen = _newest_keys(state.seen, MAX_SEEN_CLIPS)
    payload = {"seen": seen, "fail": state.failures}
    encoded = json.dumps(payload, separators=(",", ":"))
    while len(encoded) > 3500 and seen:
        seen = seen[len(seen) // 2 :]
        payload["seen"] = seen
        encoded = json.dumps(payload, separators=(",", ":"))
    return encoded


def _newest_keys(keys: list[str], limit: int) -> list[str]:
    """Keep the ``limit`` keys with the latest unix timestamp."""
    unique = list(dict.fromkeys(keys))
    unique.sort(key=_key_timestamp)
    if len(unique) <= limit:
        return unique
    return unique[-limit:]


def _key_timestamp(clip_key: str) -> int:
    """Unix seconds from a clip key, or 0 when the key is malformed."""
    try:
        return int(clip_key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


class StateStore:
    """Persistent delivery log backed by SSM Parameter Store.

    The same parameter name is kept so existing IAM stays valid. Values written
    by this class are JSON; older plain clip ids are still readable.
    """

    def __init__(self) -> None:
        self._ssm = boto3.client("ssm", region_name=AWS_REGION)

    async def get_delivery_state(self) -> DeliveryState:
        """Load the delivery log.

        Returns:
            Parsed state. A missing parameter is a first run.

        Raises:
            ClientError: On any SSM error other than ParameterNotFound.
        """
        try:
            response = self._ssm.get_parameter(Name=LAST_SEEN_PARAM)
            return parse_parameter_value(response["Parameter"]["Value"])
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ParameterNotFound":
                logger.warning(
                    "SSM parameter %s not found — treating as first run",
                    LAST_SEEN_PARAM,
                )
                return DeliveryState(first_run=True)
            logger.error(
                "Failed to read SSM parameter %s: %s", LAST_SEEN_PARAM, error_code
            )
            raise

    async def save_delivery_state(self, state: DeliveryState) -> None:
        """Persist the delivery log.

        Args:
            state: State to write. Empty seen + no failures is still written
                so a legacy cursor is retired.

        Raises:
            ClientError: If SSM rejects the write.
        """
        encoded = serialize_state(state)
        try:
            self._ssm.put_parameter(
                Name=LAST_SEEN_PARAM,
                Value=encoded,
                Type="String",
                Overwrite=True,
            )
            logger.info("Delivery state updated (%d clip(s) remembered)", len(state.seen))
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            logger.error(
                "Failed to update SSM parameter %s: %s", LAST_SEEN_PARAM, error_code
            )
            raise
