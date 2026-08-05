# SETUP.md — Step-by-Step Setup Guide

Complete this guide in order. Estimated time: 1–2 hours for first-time setup.

**Recommended path:** Terraform (`terraform/`) for IAM, Lambda, EventBridge, secrets shells.
Manual CLI steps are included where Terraform cannot populate secret values.

---

## Prerequisites

- AWS account with admin access (for initial setup only)
- AWS CLI configured: `aws configure`
- Python 3.12: `python3.12 --version`
- Terraform >= 1.5 (optional but recommended)
- Telegram app on your iPhone
- Blink Sync Module XR with microSD card inserted and local storage active

---

## Step 1 — Telegram Bot (5 minutes)

### 1.1 Create your bot
1. Open Telegram → search `@BotFather`
2. Send `/newbot`, follow prompts
3. Save the token — treat it like a password

### 1.2 Get your personal chat ID
```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```
Copy `"chat":{"id":XXXXXXXXX}` — this is your `allowed_chat_id`.

### 1.3 Test
```bash
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": YOUR_CHAT_ID, "text": "Blink Monitor bot is working!"}'
```

---

## Step 2 — Python environment (5 minutes)

From the project root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r lambda/poller/requirements.txt -r lambda/poller/requirements-dev.txt
```

### Common pip mistake

`pip install --require-hashes` **by itself** fails with:
```
ERROR: You must give at least one requirement to install
```

You must pass a requirements file **and** a target directory:

```bash
cd lambda/poller
pip install --require-hashes -r requirements.lock -t ./package/
```

The lockfile is generated once (or after dependency bumps) with:

```bash
# from project root, with venv active:
bash scripts/lock_requirements.sh
```

This runs `pip-compile --generate-hashes` then `pip-audit --strict`.

---

## Step 3 — Provision AWS infrastructure

### Option A — Terraform (recommended)

```bash
# 1. Build the Lambda zip first (Terraform needs the file to exist)
cd lambda/poller
pip install --require-hashes -r requirements.lock -t ./package/
cp *.py ./package/
cd package && zip -r ../poller.zip . && cd ../..

# 2. Apply infrastructure
cd ../../terraform
terraform init
terraform plan
terraform apply
```

This creates:
- Poller Lambda (reserved concurrency = 1)
- EventBridge rule `rate(1 minute)`
- SSM parameter `/blink-monitor/last-seen-clip-id` = `NONE`
- Secrets Manager secret shells (empty values)
- Least-privilege IAM (no S3, no warmup)

### Option B — Manual CLI

See `terraform/*.tf` for the exact resources and replicate via AWS Console/CLI.
Key settings: **1-minute schedule**, **concurrency = 1**, **no S3 permissions**.

---

## Step 4 — Populate secrets

Terraform creates empty secret shells. Populate them out-of-band.

### 4.1 Telegram secret
```bash
aws secretsmanager put-secret-value \
  --region us-east-1 \
  --secret-id /blink-monitor/telegram-bot-token \
  --secret-string '{
    "token": "YOUR_BOT_TOKEN",
    "allowed_chat_id": YOUR_CHAT_ID
  }'
```

### 4.2 Blink OAuth tokens (local bootstrap — handles 2FA)

**Do this on your laptop, not in Lambda.** The script prompts for your Blink
password interactively, completes 2FA, then writes **tokens only** (password
stripped) to Secrets Manager.

```bash
# from project root, venv active, AWS creds configured:
python scripts/bootstrap_auth.py
```

If the secret doesn't exist yet:
```bash
aws secretsmanager create-secret \
  --name /blink-monitor/blink-credentials \
  --description "Blink OAuth token blob (no password)"
# then re-run bootstrap_auth.py
```

### 4.3 Verify
```bash
aws secretsmanager list-secrets --region us-east-1 \
  --query "SecretList[?starts_with(Name, '/blink-monitor')].Name" \
  --output table
```

---

## Step 5 — First run & validation

### 5.1 Invoke manually
```bash
aws lambda invoke \
  --region us-east-1 \
  --function-name blink-monitor-poller \
  --payload '{}' \
  /tmp/response.json && cat /tmp/response.json
```

### 5.2 Tail logs
```bash
aws logs tail /aws/lambda/blink-monitor-poller --follow
```

### 5.3 Trigger motion
Walk in front of your camera. Within ~35–55 seconds you should receive a
Telegram message with the video clip.

### 5.4 Confirm schedule
```bash
aws events describe-rule --name blink-monitor-poll --region us-east-1
# ScheduleExpression should be: rate(1 minute)
```

---

## Step 6 — Security verification

Run every item in `SECURITY.md`'s checklist.

```bash
# Dependency CVE scan
bash scripts/lock_requirements.sh

# Reserved concurrency
aws lambda get-function-concurrency \
  --function-name blink-monitor-poller --region us-east-1

# IAM — confirm no s3:* actions
aws iam get-role-policy \
  --role-name blink-monitor-lambda-role \
  --policy-name blink-monitor-lambda-policy
```

---

## Redeployment (after code changes)

```bash
cd lambda/poller
rm -rf package poller.zip

# Hash-verified install (supply-chain safe)
pip install --require-hashes -r requirements.lock -t ./package/
cp *.py ./package/
cd package && zip -r ../poller.zip . && cd ..

aws lambda update-function-code \
  --region us-east-1 \
  --function-name blink-monitor-poller \
  --zip-file fileb://poller.zip
```

If using Terraform, rebuild `poller.zip` then `terraform apply` (picks up new hash).

---

## Troubleshooting

| Error | Likely cause | Fix |
|-------|-------------|-----|
| `You must give at least one requirement to install` | Ran `pip install --require-hashes` without `-r requirements.lock` | Use full command from Step 2 |
| `BlinkAuthError` / 2FA required | Token expired or bootstrap not run | `python scripts/bootstrap_auth.py` |
| `AccessDeniedException` on Secrets Manager | IAM ARN mismatch | Compare secret ARN to `terraform/iam.tf` |
| Duplicate Telegram messages | Concurrency not 1 | `aws lambda put-function-concurrency ... 1` |
| Clip queue wedged | Oversize clip blocking retries | Should auto text-fallback; check logs for `Permanent send failure` |
| No new clips | Already seen / SD issue | Check SSM value; reset to `NONE` only if intentional |
| `rate(20 seconds)` rejected | AWS minimum is 1 minute | Use `rate(1 minute)` |
