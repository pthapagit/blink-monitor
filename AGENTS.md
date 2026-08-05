# Blink Monitor — Agent Instructions

## Project Overview
A serverless AWS pipeline that polls a Blink Sync Module XR (local SD card storage,
no subscription) for new motion clips and delivers them to a Telegram bot with push
notifications. Security is the top priority throughout.

## Tech Stack
- **Runtime**: Python 3.12 (AWS Lambda)
- **Infra**: AWS (Lambda, EventBridge, SSM Parameter Store, Secrets Manager, IAM)
- **Blink integration**: blinkpy 0.25.x (unofficial Blink API, native local-storage helpers)
- **Notifications**: Telegram Bot API (raw aiohttp — no python-telegram-bot SDK)
- **IaC**: Terraform (`terraform/`)

## Project Structure
```
blink-monitor/
├── AGENTS.md                  ← you are here
├── CLAUDE.md                  ← conventions & rules for all agents
├── SECURITY.md                ← security requirements (READ BEFORE CODING)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── SETUP.md
│   └── RUNBOOK.md
├── lambda/
│   └── poller/
│       ├── handler.py
│       ├── blink_client.py
│       ├── telegram_client.py
│       ├── state_store.py
│       ├── secrets_loader.py  ← NOT secrets.py (stdlib shadowing)
│       ├── requirements.in / requirements.txt / requirements.lock
│       └── tests/
├── scripts/
│   ├── bootstrap_auth.py      ← one-time local 2FA → Secrets Manager
│   └── lock_requirements.sh   ← hash-lock + pip-audit
└── terraform/
    ├── main.tf
    ├── iam.tf
    ├── secrets.tf
    └── variables.tf
```

## Agent Task Index

| Task | File to read | File(s) to produce |
|------|-------------|-------------------|
| Understand full architecture | docs/ARCHITECTURE.md | — |
| Initial AWS + secret setup | docs/SETUP.md | — |
| Implement Lambda poller | CLAUDE.md + SECURITY.md | lambda/poller/*.py |
| Provision AWS infra | terraform/*.tf | — |
| Debug / ops | docs/RUNBOOK.md | — |

## Non-Negotiable Rules
1. Read SECURITY.md before writing any code that touches credentials or AWS.
2. Never hardcode secrets, tokens, or passwords anywhere.
3. Every AWS resource must use least-privilege IAM — no `*` actions or resources.
4. All Blink credentials stored in AWS Secrets Manager only (OAuth tokens, **no password**).
5. No S3 archive in the current design — clips stay on SD card + Telegram.
6. EventBridge schedule minimum is **1 minute** — `rate(20 seconds)` is invalid.
7. Deploy dependencies with `pip install --require-hashes -r requirements.lock`.
8. All inter-service communication over HTTPS/TLS only.
