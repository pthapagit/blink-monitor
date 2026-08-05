# Secret *shells* only — values are populated out-of-band:
#   - Blink:  python scripts/bootstrap_auth.py  (stores OAuth tokens, NO password)
#   - Telegram: aws secretsmanager put-secret-value (see docs/SETUP.md)

resource "aws_secretsmanager_secret" "blink_credentials" {
  name        = "/blink-monitor/blink-credentials"
  description = "Blink OAuth token blob for headless Lambda refresh (no password stored)"
}

resource "aws_secretsmanager_secret" "telegram_bot_token" {
  name        = "/blink-monitor/telegram-bot-token"
  description = "Telegram bot token and allowed personal chat ID"
}
