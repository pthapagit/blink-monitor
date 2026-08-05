#!/usr/bin/env python3
"""scripts/bootstrap_auth.py — one-time, LOCAL Blink auth bootstrap.

Blink requires interactive 2FA on first login, which cannot happen inside a
headless Lambda. Run this ONCE on your own machine: it logs in, prompts for the
2FA code Blink emails/texts you, and writes the resulting OAuth token blob into
AWS Secrets Manager so the Lambda can refresh headlessly from then on.

SECURITY:
  - Your Blink PASSWORD is typed at runtime and is STRIPPED before anything is
    written to Secrets Manager. Only revocable tokens are persisted.
  - Nothing is written to disk. No secrets are logged.
  - Requires AWS credentials with secretsmanager:PutSecretValue (and
    CreateSecret on first run) for /blink-monitor/blink-credentials.

Usage:
    pip install -r lambda/poller/requirements.txt
    python scripts/bootstrap_auth.py
"""

import asyncio
import getpass
import json
import logging
import sys

import boto3
from aiohttp import ClientSession
from blinkpy import api
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.helpers.pkce import generate_pkce_pair

SECRET_NAME = "/blink-monitor/blink-credentials"
AWS_REGION = "us-east-1"

# Never persist these to Secrets Manager — they are needed only for the
# interactive bootstrap, not for headless token refresh.
FIELDS_TO_STRIP = ("password",)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


async def _oauth_login_interactive(auth: Auth) -> None:
    """Run Blink OAuth login, always offering a 2FA prompt when needed.

    blinkpy only treats HTTP 412 as 2FA_REQUIRED. Blink sometimes returns a
    different status while still emailing a code — so we keep the OAuth PKCE
    state and let the user enter the code whenever the password step does not
    finish with tokens immediately.
    """
    code_verifier, code_challenge = generate_pkce_pair()

    if not await api.oauth_authorize_request(auth, auth.hardware_id, code_challenge):
        print("ERROR: OAuth authorization request failed.", file=sys.stderr)
        sys.exit(1)

    csrf_token = await api.oauth_get_signin_page(auth)
    if not csrf_token:
        print("ERROR: Failed to load Blink sign-in page (CSRF token).", file=sys.stderr)
        sys.exit(1)

    email = auth.data["username"]
    password = auth.data["password"]
    login_result = await api.oauth_signin(auth, email, password, csrf_token)

    # Stash OAuth state so complete_2fa_login can finish the flow.
    auth._oauth_csrf_token = csrf_token
    auth._oauth_code_verifier = code_verifier

    if login_result == "SUCCESS":
        code = await api.oauth_get_authorization_code(auth)
        if not code:
            print("ERROR: Failed to get authorization code.", file=sys.stderr)
            sys.exit(1)
        token_data = await api.oauth_exchange_code_for_token(
            auth, code, code_verifier, auth.hardware_id
        )
        if not token_data:
            print("ERROR: Failed to exchange authorization code for token.", file=sys.stderr)
            sys.exit(1)
        await auth._process_token_data(token_data)
        return

    if login_result == "2FA_REQUIRED":
        print("Blink requires two-factor authentication.")
    else:
        print(
            "Password sign-in did not complete automatically.\n"
            "If Blink emailed or texted you a verification code, enter it below."
        )

    twofa_code = input("Enter your Blink 2FA code: ").strip()
    if not twofa_code:
        print("ERROR: No 2FA code provided.", file=sys.stderr)
        sys.exit(1)

    if not await auth.complete_2fa_login(twofa_code):
        print("ERROR: 2FA verification failed. Check the code and try again.", file=sys.stderr)
        sys.exit(1)


async def _login() -> dict:
    """Run interactive login + 2FA and return the blinkpy login blob."""
    username = input("Blink account email: ").strip()
    password = getpass.getpass("Blink password (not stored): ")

    async with ClientSession() as session:
        auth = Auth(
            {"username": username, "password": password},
            no_prompt=True,
            session=session,
        )
        blink = Blink(session=session)
        blink.auth = auth

        try:
            await _oauth_login_interactive(auth)
        except BlinkTwoFARequiredError:
            # Legacy path — blinkpy raised before we could prompt.
            twofa_code = input("Enter your Blink 2FA code: ").strip()
            if not await auth.complete_2fa_login(twofa_code):
                print("ERROR: 2FA verification failed.", file=sys.stderr)
                sys.exit(1)

        # Confirm the session works end-to-end (sync modules, etc.).
        blink.setup_urls()
        try:
            await blink.get_homescreen()
            await blink.setup_post_verify()
        except Exception as exc:
            print(f"WARNING: Post-login setup check failed: {type(exc).__name__}")
            print("Tokens were obtained — continuing if token fields are present.")

        if not auth.token or not auth.refresh_token:
            print("ERROR: login did not yield a usable token.", file=sys.stderr)
            sys.exit(1)

        blob = dict(auth.login_attributes)
        for field in FIELDS_TO_STRIP:
            blob.pop(field, None)
        return blob


def _persist(blob: dict) -> None:
    """Write the login blob into Secrets Manager (create on first run)."""
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    payload = json.dumps(blob)
    try:
        sm.put_secret_value(SecretId=SECRET_NAME, SecretString=payload)
        print(f"Updated existing secret {SECRET_NAME}.")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name=SECRET_NAME,
            Description="Blink OAuth token blob (no password)",
            SecretString=payload,
        )
        print(f"Created secret {SECRET_NAME}.")


def main() -> None:
    blob = asyncio.run(_login())
    _persist(blob)
    print("Bootstrap complete. The Lambda can now refresh tokens headlessly.")


if __name__ == "__main__":
    main()
