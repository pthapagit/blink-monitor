# SECURITY.md — Security Requirements

## ⚠️ Read Before Writing Any Code

This document defines non-negotiable security requirements.
Every agent and developer must read this before touching any code or config.

---

## Threat Model

### What we're protecting
| Asset | Risk if compromised |
|-------|-------------------|
| Blink OAuth tokens | Attacker can view/control all your cameras |
| Telegram bot token | Attacker can read all your clips and send messages as your bot |
| Home video footage (in transit) | Clips pass through Blink cloud briefly during SD→cloud upload |
| AWS IAM credentials | Full AWS account compromise |

### Threat actors
- Random internet scanners hitting exposed endpoints
- Compromised Lambda environment (supply chain attack via a dependency)
- Leaked credentials in git history or logs

### What we deliberately do NOT store in AWS
- Blink **password** (bootstrap uses it locally, strips it before Secrets Manager write)
- Video **archive** in S3 (clips stay on SD card + Telegram only)

---

## 1. Secrets Management

### Rule: All secrets in AWS Secrets Manager. No exceptions.

**Secrets to store:**
```
/blink-monitor/blink-credentials     → OAuth token blob (NO password):
  {
    "username": "you@email.com",
    "token": "...",
    "refresh_token": "...",
    "hardware_id": "...",
    "region_id": "...",
    ...
  }

/blink-monitor/telegram-bot-token    → {
  "token": "123456:ABCdef...",
  "allowed_chat_id": 987654321
}
```

**How to create:**
```bash
# Blink — run the local bootstrap (handles 2FA, stores tokens only):
python scripts/bootstrap_auth.py

# Telegram — create manually:
aws secretsmanager create-secret \
  --name /blink-monitor/telegram-bot-token \
  --description "Telegram bot token from BotFather" \
  --secret-string '{"token":"YOUR_BOT_TOKEN","allowed_chat_id":987654321}'
```

Or provision secret shells via `terraform apply` then populate values.

**Rotation:**
- Blink: change password at blinkforhome.com, then re-run `scripts/bootstrap_auth.py`
- Telegram: rotate via @BotFather if compromised, update secret immediately
- Review secrets every 90 days minimum

**Persist path:** `persist_blink_login` strips `password` (and related fields) before
every Secrets Manager write — same guarantee as bootstrap. Never put a Blink
password into the secret manually.

### What NEVER goes in:
- `.env` files
- Lambda environment variables (visible in AWS console to anyone with Lambda read access)
- `os.environ` for secrets
- Hardcoded strings in source code
- Git commits (use `git secrets` or `gitleaks` pre-commit hook)
- Blink password in Secrets Manager

---

## 2. IAM — Least Privilege

### Lambda Execution Role — Allowed Actions Only
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadSecrets",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "arn:aws:secretsmanager:REGION:ACCOUNT:secret:/blink-monitor/blink-credentials-*",
        "arn:aws:secretsmanager:REGION:ACCOUNT:secret:/blink-monitor/telegram-bot-token-*"
      ]
    },
    {
      "Sid": "RefreshBlinkToken",
      "Effect": "Allow",
      "Action": ["secretsmanager:PutSecretValue"],
      "Resource": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:/blink-monitor/blink-credentials-*"
    },
    {
      "Sid": "ReadWriteState",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:PutParameter"],
      "Resource": "arn:aws:ssm:REGION:ACCOUNT:parameter/blink-monitor/last-seen-clip-id"
    },
    {
      "Sid": "Logging",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:REGION:ACCOUNT:log-group:/aws/lambda/blink-monitor-*"
    }
  ]
}
```

### What the role must NOT have:
- Any `s3:*` (no S3 archive in current design)
- `secretsmanager:PutSecretValue` on the Telegram secret (read-only for Telegram)
- `secretsmanager:*` wildcard
- `iam:*` (never)
- `lambda:*` (never self-modify)
- Any `*` wildcard on actions

---

## 3. Network Security

### Lambda has no public endpoint
- Lambda is invoked only by EventBridge — no API Gateway, no public URL
- No VPC needed for this architecture
- All outbound calls (Blink API, Telegram API) go over HTTPS only

### Validate all HTTPS responses
```python
# Good — aiohttp validates SSL by default, never disable it
async with aiohttp.ClientSession() as session:
    async with session.get(url) as response:
        ...

# NEVER do this
async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
    ...
```

---

## 4. Telegram Bot Security

### Lock bot to your chat ID only
```python
# telegram_client.py validates allowed_chat_id on every send
if chat_id != self._allowed_chat_id:
    raise SecurityError("Unauthorized chat_id")
```

### Get your personal chat ID:
1. Message your bot once
2. Call: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Copy the `chat.id` value — store it in Secrets Manager alongside the token

---

## 5. Logging Security

### CloudWatch Log Group settings:
- Retention: 30 days
- Never log secrets, tokens, or signed URLs
- Enable log group encryption with KMS (optional but recommended)

### Sensitive data patterns to NEVER log:
- `password`
- `token` (followed by a value)
- Full clip download URLs
- Any JWT or bearer token string

---

## 6. Dependency Security

### Pin all versions — deploy from hash-locked lockfile
```
blinkpy==0.25.5
aiohttp==3.14.3
boto3==1.43.24
```

Generate and audit the lockfile:
```bash
bash scripts/lock_requirements.sh
# produces lambda/poller/requirements.lock (every package + sha256 hash)

# Deploy with hash verification:
cd lambda/poller
pip install --require-hashes -r requirements.lock -t ./package/
```

### Before any dependency update:
1. Check the changelog for breaking changes
2. Run `bash scripts/lock_requirements.sh` (includes `pip-audit --strict`)
3. Test locally before deploying
4. Never auto-update dependencies in CI without review

---

## 7. Incident Response

### If Blink OAuth tokens are compromised:
1. Change Blink password at blinkforhome.com
2. Revoke active sessions in the Blink app
3. Re-run `python scripts/bootstrap_auth.py` to seed fresh tokens
4. Review CloudWatch logs for unauthorized clip access

### If Telegram token is compromised:
1. Revoke token immediately via @BotFather → `/revoke`
2. Generate new token, update secret in Secrets Manager
3. Lambda reloads on next cold start, or immediately after a Telegram **401**
   (warm cache is invalidated on 401)

### If AWS credentials are compromised:
1. Deactivate the IAM key immediately
2. Rotate all secrets in Secrets Manager
3. Review CloudTrail for unauthorized actions

---

## Security Checklist (Before First Deploy)

- [ ] Blink secret populated via `bootstrap_auth.py` (tokens only, no password)
- [ ] Telegram secret has `token` + `allowed_chat_id`
- [ ] IAM role matches the policy above — verified in console or `terraform/iam.tf`
- [ ] `PutSecretValue` scoped to blink secret ONLY
- [ ] No S3 permissions on the Lambda role
- [ ] Telegram bot locked to your personal chat ID
- [ ] `bash scripts/lock_requirements.sh` passes (`pip-audit --strict`)
- [ ] Lambda deployed with `pip install --require-hashes -r requirements.lock`
- [ ] `.gitignore` includes `*.json`, `.env`, `credentials*`
- [ ] CloudWatch log retention set to 30 days
- [ ] Reserved concurrency = 1 on the poller Lambda (Terraform default)
- [ ] CI green: hash-locked install + `pip-audit --strict` + unit tests
- [ ] No `print()` statements that could leak secrets
- [ ] Confirm `persist_blink_login` strips password (covered by unit tests)
