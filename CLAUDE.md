# CLAUDE.md — Conventions & Agent Rules

## Read This First
This project has strict security requirements. Before writing any code:
1. Read SECURITY.md completely
2. Read docs/ARCHITECTURE.md for context
3. Never skip validation steps listed in this file

---

## Language & Runtime
- Python 3.12 only
- Async-first: use `asyncio` and `aiohttp` throughout (blinkpy is async-native)
- Type hints required on all function signatures
- Docstrings required on all public functions and classes

## Code Style
```python
# Good — typed, documented, async
async def fetch_manifest(client: BlinkClient, network_id: str) -> dict:
    """Fetch the local storage manifest from the Sync Module.
    
    Args:
        client: Authenticated BlinkClient instance
        network_id: Blink network ID from account
        
    Returns:
        Manifest dict containing clip list
        
    Raises:
        BlinkAuthError: If token has expired
        BlinkAPIError: If manifest request fails
    """

# Bad — no types, no docs, sync
def fetch_manifest(client, network_id):
    ...
```

## Secrets — Absolute Rules
- NEVER use os.environ for secrets (env vars are visible in Lambda console logs)
- ALWAYS use secrets_loader.py (Secrets Manager loader) for any credential
- NEVER name a module `secrets.py` — it shadows Python's stdlib `secrets`
- NEVER store Blink password in Secrets Manager (bootstrap strips it; tokens only)
- NEVER log secret values, tokens, or passwords — not even partially
- NEVER commit .env files, credential JSON files, or blinkpy save files
- Secret names follow the pattern: `/blink-monitor/{secret-name}`

```python
# Good
from secrets_loader import load_secrets
secrets = await load_secrets()
blink_blob = secrets["blink"]

# Bad — never do this
TELEGRAM_TOKEN = "1234567890:ABCdef..."
blink_pass = os.environ["BLINK_PASSWORD"]
```

## Error Handling
- All blinkpy calls must be wrapped in try/except
- blinkpy handles OAuth token refresh transparently; persist to Secrets Manager
  only when the token actually changes (via `persist_blink_login`)
- On Telegram **transient** failure: do NOT update SSM — clip retries next poll
- On Telegram **permanent** failure (oversize, 4xx): text fallback + advance SSM
- Never swallow exceptions silently

```python
# Good — transient vs permanent
try:
    await telegram_client.send_clip(video=video_bytes, caption=caption)
    await state_store.set_last_seen_clip_id(clip_id)
except TelegramPermanentError:
    await telegram_client.send_text(fallback_message)
    await state_store.set_last_seen_clip_id(clip_id)
except TelegramTransientError:
    return False  # halt batch, retry next cycle
```

## Logging
- Use Python's `logging` module, not `print()`
- Log level: INFO for normal flow, ERROR for failures, DEBUG for request details
- NEVER log: passwords, tokens, full clip URLs (they contain signed params), face data
- Always log: clip IDs, timestamps, function entry/exit for key steps

```python
import logging
logger = logging.getLogger(__name__)

# Good
logger.info("New clip detected: clip_id=%s", clip_id)
logger.error("Telegram send failed for clip_id=%s: %s", clip_id, str(e))

# Bad
print(f"Token: {token}")
logger.debug("Full response: %s", response_with_signed_url)
```

## State Management
- Last-seen clip ID stored in SSM Parameter Store only
- Parameter name: `/blink-monitor/last-seen-clip-id`
- Advance state **per clip** after successful Telegram send
- Transient failure: do NOT advance (retry next poll)
- Permanent failure: text fallback, then advance (prevents queue wedge)

## Lambda Constraints
- Max memory: 512MB (clips are small, this is sufficient)
- Max timeout: 60s (the entire poll + download + send must fit)
- Always handle Lambda cold starts gracefully (no assumptions about in-memory cache)
- Keep dependencies minimal — every MB adds to cold start time

## Dependencies (requirements.in / requirements.lock rules)
- Pin direct deps in `requirements.in`; generate hash-locked `requirements.lock`
  via `bash scripts/lock_requirements.sh`
- Deploy with: `pip install --require-hashes -r requirements.lock -t ./package/`
- Reason: blinkpy tracks Blink's unofficial API — unpinned updates can break auth overnight
- Review blinkpy changelog before any version bump; run `pip-audit --strict` before deploy
- Direct runtime dependencies:
  - `blinkpy` — Blink API + native local-storage helpers
  - `aiohttp` — async HTTP (must stay on a CVE-patched pin, currently 3.14.0)
  - `boto3` — AWS SDK (pre-installed in Lambda, still pin for local dev)

## Testing
- Unit test every function in blink_client.py and state_store.py
- Mock all external calls (blinkpy, boto3, Telegram) — never hit real APIs in tests
- Test the "already seen clip" deduplication logic explicitly
- Test token refresh flow

## File Naming
- Snake case for all Python files
- No abbreviations in variable names: `clip_identifier` not `cid`
- Constants in ALL_CAPS at module level

## Git Hygiene
- .gitignore must include: `*.json` (catches blinkpy credential saves), `.env`, `__pycache__`
- Never commit Lambda zip files or build artifacts
- Commit messages: `[component] short description` e.g. `[poller] add clip deduplication`
