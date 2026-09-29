# RUNBOOK.md — Operations & Debugging

## Daily Operations

### Check if pipeline is running
```bash
aws logs tail /aws/lambda/blink-monitor-poller --since 1h --region us-east-1

aws ssm get-parameter \
  --region us-east-1 \
  --name /blink-monitor/last-seen-clip-id \
  --query Parameter.Value \
  --output text
```

### Confirm schedule is active
```bash
aws events describe-rule \
  --region us-east-1 \
  --name blink-monitor-poll \
  --query '{State:State,Schedule:ScheduleExpression}'
```

---

## Common Issues

### Issue: Not receiving Telegram notifications

**Step 1** — Check Lambda invocations
```bash
aws cloudwatch get-metric-statistics \
  --region us-east-1 \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --dimensions Name=FunctionName,Value=blink-monitor-poller \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --statistics Sum
```

**Step 2** — Check for errors
```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/lambda/blink-monitor-poller \
  --filter-pattern "ERROR"
```

**Step 3** — Check last-seen state
```bash
aws ssm get-parameter \
  --name /blink-monitor/last-seen-clip-id --region us-east-1
```
Resetting to `NONE` re-sends the most recent clip on next poll only (not the
full SD history). Use a specific recent clip ID to resume from a point in time.

---

### Issue: Old clips flooding Telegram

**Symptom:** Telegram receives a stream of old SD-card videos, oldest first,
until it catches up to the latest.

**Cause:** Blink clip IDs are random. When the saved cursor disappeared from
the card (full card, format, reboot), older builds treated other IDs as new
and replayed the card.

**Fix (immediate):**
```bash
# Stop the schedule while you reset state
aws events disable-rule --region us-east-1 --name blink-monitor-poll

# Jump to "first run" behaviour — next poll sends ONLY the newest clip
aws ssm put-parameter \
  --region us-east-1 \
  --name /blink-monitor/last-seen-clip-id \
  --value "NONE" \
  --overwrite

aws events enable-rule --region us-east-1 --name blink-monitor-poll
```

Current code remembers recently sent clips as `id:recording_time` and only
sends clips from the last 2 hours that are not in that log. A formatted card
that reuses an id is still delivered, because the recording time differs.
Setting the parameter back to `NONE` sends only the single newest clip.

---

### Issue: BlinkAuthError / 2FA required

The stored OAuth refresh token is invalid or revoked.

```bash
# Re-run local bootstrap (interactive 2FA, stores tokens only):
python scripts/bootstrap_auth.py
```

Do **not** put your Blink password in Secrets Manager.

---

### Issue: Duplicate Telegram messages

Reserved concurrency is not 1.

```bash
aws lambda get-function-concurrency \
  --function-name blink-monitor-poller --region us-east-1

aws lambda put-function-concurrency \
  --function-name blink-monitor-poller \
  --reserved-concurrent-executions 1 \
  --region us-east-1
```

---

### Issue: Clip queue wedged (same clip retried forever)

A permanently un-sendable clip (>50MB) should trigger a text fallback and
advance state. If you see repeated failures for the same `clip_id`:

```bash
# Check logs for "Permanent send failure" or "text fallback"
aws logs filter-log-events \
  --log-group-name /aws/lambda/blink-monitor-poller \
  --filter-pattern "Permanent"
```

Manual unblock — advance state past the stuck clip:
```bash
aws ssm put-parameter \
  --name /blink-monitor/last-seen-clip-id \
  --value "KNOWN_GOOD_CLIP_ID" \
  --overwrite \
  --region us-east-1
```

---

### Issue: Lambda timeout

Reduce clip length in the Blink app (Settings → Camera → Clip Length → minimum).

---

### Issue: AccessDeniedException on Secrets Manager

```bash
aws secretsmanager describe-secret \
  --secret-id /blink-monitor/blink-credentials \
  --query ARN --output text

aws iam get-role-policy \
  --role-name blink-monitor-lambda-role \
  --policy-name blink-monitor-lambda-policy
```

Confirm `PutSecretValue` is scoped to the **blink** secret ARN only.

---

## Credential Rotation

### Rotate Blink access (password changed)
1. Change password at https://blinkforhome.com
2. `python scripts/bootstrap_auth.py`

### Rotate Telegram bot token
1. @BotFather → revoke token → copy new token
2. ```bash
   aws secretsmanager put-secret-value \
     --secret-id /blink-monitor/telegram-bot-token \
     --secret-string '{"token":"NEW_TOKEN","allowed_chat_id":YOUR_ID}'
   ```

---

## Redeployment

```bash
cd lambda/poller
rm -rf package poller.zip
pip install --require-hashes -r requirements.lock -t ./package/
cp *.py ./package/
cd package && zip -r ../poller.zip . && cd ..

aws lambda update-function-code \
  --function-name blink-monitor-poller \
  --zip-file fileb://poller.zip \
  --region us-east-1
```

Before deploy: `bash scripts/lock_requirements.sh` (regenerates lock + CVE scan).

---

## Monitoring

### CloudWatch alarm for Lambda errors
```bash
aws cloudwatch put-metric-alarm \
  --alarm-name blink-monitor-errors \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --dimensions Name=FunctionName,Value=blink-monitor-poller \
  --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold 3 --comparison-operator GreaterThanOrEqualToThreshold \
  --region us-east-1
```

---

## Disaster Recovery

### Full reset
```bash
aws ssm put-parameter \
  --name /blink-monitor/last-seen-clip-id \
  --value "NONE" --overwrite --region us-east-1

# Disable schedule temporarily
aws events disable-rule --name blink-monitor-poll --region us-east-1

# Fix issue, re-enable
aws events enable-rule --name blink-monitor-poll --region us-east-1
```
