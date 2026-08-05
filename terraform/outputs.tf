output "poller_function_name" {
  description = "Name of the poller Lambda function."
  value       = aws_lambda_function.poller.function_name
}

output "poller_function_arn" {
  description = "ARN of the poller Lambda function."
  value       = aws_lambda_function.poller.arn
}

output "poll_schedule_rule" {
  description = "EventBridge rule that triggers the poller (rate 1 minute)."
  value       = aws_cloudwatch_event_rule.poll.name
}

output "blink_secret_arn" {
  description = "ARN of the Blink credentials secret (populate via bootstrap_auth.py)."
  value       = aws_secretsmanager_secret.blink_credentials.arn
}

output "telegram_secret_arn" {
  description = "ARN of the Telegram bot secret."
  value       = aws_secretsmanager_secret.telegram_bot_token.arn
}

output "last_seen_parameter_name" {
  description = "SSM parameter tracking the last delivered clip."
  value       = aws_ssm_parameter.last_seen_clip_id.name
}
