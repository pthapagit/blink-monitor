# ARCHITECTURE.md — System Design

## Overview

A serverless AWS pipeline that polls a Blink Sync Module XR for new motion clips
stored on a local SD card (no Blink subscription required) and delivers them to
your Telegram chat with a push notification.

Clips stay on the SD card (downloads use blinkpy's non-deleting path). There is
**no S3 archive** in this design — Telegram is the delivery channel; the SD card
is the local copy.

---

## Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  YOUR HOME                                                       │
│                                                                  │
│  Blink Camera ──motion──► Sync Module XR ──saves──► SD Card     │
│                                │                                 │
│                                │ always-on WiFi                  │
└────────────────────────────────┼────────────────────────────────┘
                                 │ HTTPS (Blink's API)
                                 ▼
                    ┌────────────────────────┐
                    │   Blink Cloud (AWS)    │
                    │   immedia-semi.com     │
                    └────────────┬───────────┘
                                 │ HTTPS
                    ┌────────────▼───────────────────────────────┐
                    │  YOUR AWS ACCOUNT (us-east-1)              │
                    │                                            │
                    │  EventBridge (every 1 minute)              │
                    │       │                                    │
                    │       ▼                                    │
                    │  Lambda: blink-monitor-poller              │
                    │    1. Load secrets from Secrets Manager    │
                    │    2. Read last-seen clip from SSM         │
                    │    3. Auth to Blink via blinkpy (OAuth)    │
                    │    4. Refresh local-storage manifest       │
                    │    5. Find new clips vs last-seen          │
                    │    6. For each new clip (cap 5/cycle):     │
                    │       a. SD→cloud upload + download        │
                    │       b. Send video to Telegram              │
                    │       c. Advance SSM per successful send   │
                    │       d. On permanent fail: text fallback   │
                    │    7. Persist refreshed OAuth token (if     │
                    │       changed) back to Secrets Manager     │
                    │       │                                    │
                    │       ├──► SSM Parameter Store             │
                    │       │    └── /blink-monitor/last-seen    │
                    │       │                                    │
                    │       └──► Secrets Manager                 │
                    │            ├── /blink-monitor/blink-creds  │
                    │            │   (token blob, NO password)   │
                    │            └── /blink-monitor/telegram     │
                    └────────────────────┬───────────────────────┘
                                         │ HTTPS (Telegram API)
                                         ▼
                              ┌──────────────────────┐
                              │   Telegram servers   │
                              └──────────┬───────────┘
                                         │ APNs push
                                         ▼
                              ┌──────────────────────┐
                              │   Your iPhone        │
                              │   Telegram app       │
                              │   🔔 notification    │
                              │   ▶️ video inline    │
                              └──────────────────────┘
```

---

## Component Details

### EventBridge (CloudWatch Events rule)
- Runs every **1 minute** (`rate(1 minute)`)
- AWS does **not** support sub-minute schedules on EventBridge; `rate(20 seconds)` is invalid
- Invokes the poller Lambda directly (no Step Functions loop)
- ~43K invocations/month

### Lambda: blink-monitor-poller
- Runtime: Python 3.12
- Memory: 512MB
- Timeout: 60s
- Region: us-east-1 (same as Blink's servers — minimises latency)
- **Reserved concurrency = 1** (prevents duplicate Telegram messages and SSM races)

**Per-cycle behaviour:**
- Sends all new clips **oldest → newest**, capped at 5 per cycle
- Advances SSM state **per clip** after successful delivery
- Permanent send failure (oversize clip, HTTP 4xx): text fallback + advance
- Transient failure (network, HTTP 5xx): stop batch, retry next cycle
- OAuth token refresh persisted to Secrets Manager **only when the token changes**

### No warmup Lambda
At a 1-minute poll interval the execution environment stays warm naturally.
A separate warmup function added IAM surface for no benefit and was removed.

### No S3 archive
Footage is not stored in AWS. Clips exist on the SD card and in your Telegram
chat. S3 may be added later if Rekognition / AI features are built.

### SSM Parameter Store
- `/blink-monitor/last-seen-clip-id` — ID of the last clip delivered
- Updated per clip after successful Telegram send (or text fallback on permanent failure)
- Not updated on transient failures → clip retries next poll

### Secrets Manager
- `/blink-monitor/blink-credentials` — OAuth token blob (`token`, `refresh_token`,
  `hardware_id`, `username`). **Password is NOT stored** — seeded via local bootstrap.
- `/blink-monitor/telegram-bot-token` — bot token + your allowed chat ID
- Lambda IAM: `GetSecretValue` on both; `PutSecretValue` on **blink secret only**

---

## Latency Budget

```
Camera detects motion + saves clip:     5–10s  (hardware, fixed)
EventBridge polling gap (avg):          0–60s  (avg 30s at 1-min interval)
Lambda cold start (occasional):         1–3s
blinkpy auth + manifest refresh:        2–4s
SD card → Blink cloud upload:           3–8s   (depends on home upload speed)
Lambda downloads clip:                  1–2s
Lambda sends to Telegram:               1–2s
Telegram → iPhone push notification:    1–3s
─────────────────────────────────────────────
Best case total:                        ~14s
Typical total:                          ~35–55s
Worst case (slow home internet):        ~60s
```

---

## AWS Cost Estimate (Personal Use)

| Service | Usage | Monthly Cost |
|---------|-------|-------------|
| Lambda (poller) | ~43K invocations/month at 1 min | $0 (free tier: 1M) |
| EventBridge | ~43K events/month | ~$0.01 |
| SSM Parameter Store | Standard tier | $0 |
| Secrets Manager | 2 secrets | ~$1.60 |
| CloudWatch Logs | ~50MB/month | ~$0.03 |
| **Total** | | **~$1.65/month** |

---

## Key Design Decisions

### Why Lambda over ECS/EKS?
- Poller runs ~5s per minute — a 24/7 container costs ~$5–10/month for no gain
- Lambda is effectively free at this scale

### Why SSM for state instead of DynamoDB?
- Single string value (last clip ID) doesn't need a database
- SSM Parameter Store is free for standard parameters

### Why no S3 archive?
- Telegram already delivers and retains clips in your chat
- SD card retains the local copy
- Storing home-surveillance footage in S3 widens the attack surface with no
  current functional benefit (Rekognition is explicitly future work)

### Why reserved concurrency = 1?
- Prevents duplicate notifications from concurrent executions
- Prevents race conditions on SSM state updates
- Blink's API has rate limits

### Why us-east-1?
- Blink's infrastructure runs on AWS us-east-1
- Same-region Lambda → lower latency to Blink API

### Why hash-locked dependencies?
- `pip install --require-hashes -r requirements.lock` refuses tampered PyPI
  packages — supply-chain hardening for a Lambda that handles camera footage

---

## Future Stages (Not in Scope Now)

```
Stage 2: Face recognition
  → Add AWS Rekognition (requires S3 archive — add bucket then)
  → Match against known faces collection
  → Include "Alex (97%)" in Telegram caption

Stage 3: Scene description
  → Extract frames with FFmpeg Lambda layer
  → Call GPT-4o Vision or Claude Vision API

Stage 4: Private face recognition
  → Replace Rekognition with InsightFace on ECS Fargate
  → Completely private — no footage leaves your AWS account
```
