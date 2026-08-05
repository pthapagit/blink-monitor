# lambda/poller/secrets_loader.py
# Single point of truth for loading secrets from AWS Secrets Manager.
# ALL secret access in this project flows through this module only.
#
# Renamed from secrets.py: the old name SHADOWED Python's stdlib `secrets`
# module (crypto-safe randomness). Any future `import secrets` would have
# silently imported THIS file instead — a real footgun. Do not rename back.
#
# Read SECURITY.md before modifying.

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Module-level cache — reused across warm Lambda invocations.
# Secrets are loaded once per execution-environment lifetime.
_secrets_cache: dict[str, Any] = {}

AWS_REGION = "us-east-1"

SECRET_NAMES = {
    "blink": "/blink-monitor/blink-credentials",
    "telegram": "/blink-monitor/telegram-bot-token",
}


async def load_secrets() -> dict[str, Any]:
    """Load all required secrets from AWS Secrets Manager.

    Uses a module-level cache to avoid redundant API calls on warm
    invocations. Safe because secrets don't change mid-invocation and Lambda
    execution environments are short-lived.

    Returns:
        Dict with keys 'blink' and 'telegram', each holding their secret dict.
        The 'blink' value is the full blinkpy login blob (username/password
        plus token/refresh_token/hardware_id once bootstrapped).

    Raises:
        RuntimeError: If any required secret cannot be loaded or is invalid.
    """
    global _secrets_cache

    if _secrets_cache:
        logger.info("Using cached secrets (warm Lambda)")
        return _secrets_cache

    logger.info("Loading secrets from Secrets Manager (cold start or first load)")
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    loaded: dict[str, Any] = {}

    for key, secret_name in SECRET_NAMES.items():
        loaded[key] = _fetch_secret(client, secret_name)

    _validate_secrets(loaded)
    _secrets_cache = loaded
    return loaded


def _fetch_secret(client: Any, secret_name: str) -> dict[str, Any]:
    """Fetch and parse a single secret from Secrets Manager.

    Args:
        client: Boto3 Secrets Manager client.
        secret_name: Full secret name (e.g. /blink-monitor/blink-credentials).

    Returns:
        Parsed secret dict.

    Raises:
        RuntimeError: If the secret cannot be fetched or parsed.
    """
    try:
        response = client.get_secret_value(SecretId=secret_name)
        # NEVER log the response — it contains credentials.
        return json.loads(response["SecretString"])
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        # Log the error code only — never the full exception (it can echo
        # partial credential context in some edge cases).
        logger.error(
            "Failed to load secret %s — error code: %s", secret_name, error_code
        )
        raise RuntimeError(f"Cannot load secret: {secret_name}") from None
    except json.JSONDecodeError:
        logger.error("Secret %s is not valid JSON", secret_name)
        raise RuntimeError(f"Invalid secret format: {secret_name}") from None


def _validate_secrets(secrets: dict[str, Any]) -> None:
    """Validate that all required fields are present in loaded secrets.

    Args:
        secrets: Dict of loaded secret dicts.

    Raises:
        RuntimeError: If any required field is missing or empty.
    """
    # The Lambda runs HEADLESS and authenticates via OAuth token refresh, which
    # needs only refresh_token + hardware_id (never the password). The password
    # is used solely during the one-time local bootstrap and is intentionally
    # NOT stored in Secrets Manager, so a leaked secret exposes only revocable
    # tokens. If these fields are missing, the bootstrap script hasn't been run.
    required_fields = {
        "blink": ["token", "refresh_token", "hardware_id"],
        "telegram": ["token", "allowed_chat_id"],
    }

    for secret_key, fields in required_fields.items():
        secret = secrets.get(secret_key, {})
        for field in fields:
            if not secret.get(field):
                logger.error(
                    "Required field '%s' missing or empty in secret '%s'",
                    field,
                    SECRET_NAMES[secret_key],
                )
                raise RuntimeError(
                    f"Missing required field '{field}' in {SECRET_NAMES[secret_key]}"
                )

    logger.info("All secrets validated successfully")


def invalidate_cache() -> None:
    """Clear the secrets cache — forces reload on the next load_secrets() call."""
    global _secrets_cache
    _secrets_cache = {}
    logger.info("Secrets cache invalidated")


def persist_blink_login(login_attributes: dict[str, Any]) -> None:
    """Persist the full blinkpy login blob back to Secrets Manager.

    blinkpy refreshes its OAuth tokens transparently; this saves the updated
    blob so the new refresh token survives Lambda cold starts (otherwise every
    cold start would re-run the 2FA flow, which can't happen headless).

    IMPORTANT: Call this ONLY when the token actually changed — not on every
    poll. Writing on every cycle creates a new Secrets Manager version each
    minute (cost, throttling, version churn). The caller (BlinkClient) compares
    the pre/post token and only calls this on a real refresh.

    Args:
        login_attributes: blinkpy's auth.login_attributes dict (username,
            password, token, refresh_token, hardware_id, region_id, ...).
    """
    if not login_attributes or not login_attributes.get("token"):
        logger.error("Refusing to persist Blink login — no token present")
        return

    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    secret_name = SECRET_NAMES["blink"]

    try:
        client.put_secret_value(
            SecretId=secret_name,
            SecretString=json.dumps(login_attributes),
        )
        invalidate_cache()
        logger.info("Blink login blob refreshed in Secrets Manager")
    except ClientError as e:
        # Non-fatal: blinkpy will refresh again next cold start.
        logger.error(
            "Failed to persist Blink login: %s", e.response["Error"]["Code"]
        )
