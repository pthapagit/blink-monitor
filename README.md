# Blink Monitor

Get Blink motion clips on your phone **without a Blink subscription**.

This project runs a small AWS Lambda that polls your **Sync Module XR** SD card
for new motion clips and sends them to a **Telegram bot** with push notifications.
Clips stay on the SD card — nothing is archived to S3.

Typical delivery time after motion: **~35–55 seconds**.

---

## What you need

| Item | Notes |
|------|--------|
| **Blink Sync Module XR** | MicroSD card inserted, local storage enabled |
| **Blink cameras** | Must be configured to save clips to the Sync Module SD |
| **AWS account** | `us-east-1` recommended (same region as Blink's API) |
| **Telegram** | Bot from [@BotFather](https://t.me/BotFather) + your chat ID |
| **Local machine** | Python 3.12, AWS CLI, Terraform (optional) |

**Monthly AWS cost:** ~$1.65 (mostly Secrets Manager). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#aws-cost-estimate-personal-use).

---

## How it works

```
Camera motion → SD card → Blink cloud manifest
                              ↓
         EventBridge (every 1 min) → Lambda poller
                              ↓
              Download clip → Send to Telegram → iPhone push
```

- Polls every **1 minute** (AWS EventBridge minimum; sub-minute schedules are not supported).
- Sends up to **5 new clips per cycle**, oldest first.
- Tracks the last delivered clip in **SSM Parameter Store** so nothing is duplicated.
- Uses **blinkpy** (unofficial Blink API) with OAuth tokens stored in **Secrets Manager**.
- Your Blink **password is never stored** — only tokens, seeded via a one-time local script.

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Blink app setup (before AWS)

1. Insert a microSD card into the Sync Module XR.
2. In the Blink app: **Sync Module → Local Storage** — confirm storage is active.
3. For each camera: ensure clips are saved to **local storage** (not cloud-only).
4. Set clip length to the minimum if Lambda times out on large files.

Only clips on the **first Sync Module** in your account are polled. Cameras that
do not write to that SD card will not appear in Telegram.

---

## Quick start

Full walkthrough: **[docs/SETUP.md](docs/SETUP.md)** (~1–2 hours first time).

### 1. Clone and create a Python environment

```bash
git clone <your-repo-url> blink-monitor
cd blink-monitor

python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r lambda/poller/requirements.txt -r lambda/poller/requirements-dev.txt
```

### 2. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → save the token.
2. Get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```
   Use the numeric `"id"` inside `"chat"`.

### 3. Configure AWS

```bash
aws configure
# Region: us-east-1
```

### 4. Build the Lambda package

```bash
cd lambda/poller
pip install --require-hashes -r requirements.lock -t ./package/
cp *.py ./package/
cd package && zip -r ../poller.zip . && cd ../..
```

> **Common mistake:** `pip install --require-hashes` alone fails. You must pass
> `-r requirements.lock -t ./package/`.

Regenerate the lockfile after dependency changes:

```bash
bash scripts/lock_requirements.sh
```

### 5. Deploy infrastructure (Terraform)

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

Creates: Lambda, EventBridge rule (`rate(1 minute)`), SSM state parameter,
Secrets Manager shells, least-privilege IAM.

### 6. Store secrets

**Telegram:**

```bash
aws secretsmanager put-secret-value \
  --region us-east-1 \
  --secret-id /blink-monitor/telegram-bot-token \
  --secret-string '{"token":"YOUR_BOT_TOKEN","allowed_chat_id":YOUR_CHAT_ID}'
```

**Blink (interactive 2FA — run on your laptop, not in Lambda):**

```bash
cd /path/to/blink-monitor   # project root
python scripts/bootstrap_auth.py
```

Enter your Blink email and password when prompted. If Blink sends a verification
code, enter it. On success you will see:

```
Bootstrap complete. The Lambda can now refresh tokens headlessly.
```

### 7. Test

```bash
aws lambda invoke \
  --region us-east-1 \
  --function-name blink-monitor-poller \
  --payload '{}' \
  /tmp/response.json && cat /tmp/response.json

aws logs tail /aws/lambda/blink-monitor-poller --follow --region us-east-1
```

Trigger motion in front of a camera. You should receive a Telegram message like:

```
🚨 Motion detected
📷 Front Door
🕐 Jun 06 2026, 02:32 PM EDT
```

(with the video attached)

---

## Day-to-day usage

Once set up, you do not need to run anything manually.

| What happens | When |
|--------------|------|
| Lambda polls Blink | Every 1 minute (EventBridge) |
| New clips arrive in Telegram | Within ~35–55 s of motion (typical) |
| OAuth token refresh | Automatic inside Lambda |
| Clips on SD card | Preserved (non-deleting download) |

### Pause polling

```bash
aws events disable-rule --region us-east-1 --name blink-monitor-poll
```

### Resume polling

```bash
aws events enable-rule --region us-east-1 --name blink-monitor-poll
```

### Check status

```bash
# Recent logs
aws logs tail /aws/lambda/blink-monitor-poller --since 1h --region us-east-1

# Last clip ID delivered
aws ssm get-parameter \
  --region us-east-1 \
  --name /blink-monitor/last-seen-clip-id \
  --query Parameter.Value --output text

# Schedule state
aws events describe-rule \
  --region us-east-1 \
  --name blink-monitor-poll \
  --query '{State:State,Schedule:ScheduleExpression}'
```

More ops commands: [docs/RUNBOOK.md](docs/RUNBOOK.md)

---

## Important: the "iPhone 15 Pro" login device

When you run `bootstrap_auth.py` or when Lambda refreshes tokens, Blink may show
a login from **iPhone 15 Pro**. That is **your automation**, not a stranger.

The [blinkpy](https://github.com/fronzbot/blinkpy) library impersonates the official
Blink iOS app (`device_model: iPhone16,1` = iPhone 15 Pro) so OAuth works.

**Do not remove this device** in the Blink app (Account → logged-in devices).
Removing it revokes the token, causes auth failures, and repeated login attempts
can **lock your Blink account**.

If you are locked out:

1. Stop polling: `aws events disable-rule --region us-east-1 --name blink-monitor-poll`
2. Wait 30–60 minutes (sometimes up to 24h) — do not retry bootstrap while locked
3. Confirm the Blink app works on your real phone
4. Run `python scripts/bootstrap_auth.py` **once**
5. Re-enable the schedule

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `BlinkAuthError` / 2FA required | `python scripts/bootstrap_auth.py` (after account unlock) |
| Account locked / multiple attempts | Disable EventBridge rule, wait, bootstrap once — see above |
| No clips from a camera | Check Sync Module → Local Storage includes that camera |
| Duplicate Telegram messages | Set Lambda reserved concurrency to 1 (see RUNBOOK) |
| `pip install --require-hashes` error | Use `-r requirements.lock -t ./package/` |
| Clip stuck / retried forever | Check logs; advance SSM state manually (RUNBOOK) |
| Lambda timeout | Shorten clip length in Blink app settings |

Full runbook: [docs/RUNBOOK.md](docs/RUNBOOK.md)

---

## Redeploy after code changes

```bash
cd lambda/poller
rm -rf package poller.zip
pip install --require-hashes -r requirements.lock -t ./package/
cp *.py ./package/
cd package && zip -r ../poller.zip . && cd ..

aws lambda update-function-code \
  --region us-east-1 \
  --function-name blink-monitor-poller \
  --zip-file fileb://poller.zip
```

---

## Development

### Run tests

```bash
source .venv/bin/activate
cd lambda/poller
pytest tests/ -v
```

### Project layout

```
blink-monitor/
├── README.md                  ← you are here
├── SECURITY.md                ← security requirements (read before coding)
├── docs/
│   ├── ARCHITECTURE.md        ← system design
│   ├── SETUP.md               ← detailed setup guide
│   └── RUNBOOK.md             ← ops & debugging
├── lambda/poller/
│   ├── handler.py             ← Lambda entrypoint
│   ├── blink_client.py        ← blinkpy wrapper (SD card, non-deleting)
│   ├── telegram_client.py     ← Telegram delivery
│   ├── state_store.py         ← SSM last-seen clip ID
│   ├── secrets_loader.py      ← Secrets Manager loader
│   └── tests/
├── scripts/
│   ├── bootstrap_auth.py      ← one-time 2FA → Secrets Manager
│   └── lock_requirements.sh   ← hash-lock deps + pip-audit
└── terraform/                 ← AWS infrastructure
```

### Credential rotation

| Secret | Action |
|--------|--------|
| Blink password changed | `python scripts/bootstrap_auth.py` |
| Telegram token compromised | Revoke via @BotFather, update Secrets Manager |

---

## Security

- Secrets live in **AWS Secrets Manager only** — never in env vars, `.env`, or git.
- Blink **password is never stored** in AWS.
- IAM is least-privilege: no S3, no wildcard actions.
- Dependencies are **hash-locked** (`requirements.lock`) for supply-chain safety.
- Telegram bot is locked to a single `allowed_chat_id`.

### Do not commit

| Keep local only | Why |
|-----------------|-----|
| `.env`, credential JSON, blinkpy save files | Real tokens / passwords |
| `terraform/*.tfstate*` | AWS account IDs, ARNs, resource details |
| `terraform.tfvars` | Often holds account-specific values |
| `lambda/poller/package/`, `*.zip` | Build artifacts |
| AWS access keys, Telegram bot tokens | Rotate immediately if ever exposed |

This repo’s `.gitignore` blocks those patterns. Never force-add them.

Read [SECURITY.md](SECURITY.md) before changing anything that touches credentials.

---

## Disclaimer

This project uses **blinkpy**, an unofficial client for Blink's API. Amazon may
change the API at any time. Use at your own risk. This is not affiliated with
Ring, Amazon, or Blink.

---

## Documentation index

| Doc | Purpose |
|-----|---------|
| [docs/SETUP.md](docs/SETUP.md) | Step-by-step first-time setup |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design, data flow, cost, decisions |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Daily ops, debugging, disaster recovery |
| [SECURITY.md](SECURITY.md) | Threat model and security checklist |
| [AGENTS.md](AGENTS.md) | Contributor / agent entry point |
