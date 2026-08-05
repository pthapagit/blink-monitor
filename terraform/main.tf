# Core infrastructure: poller Lambda, 1-minute schedule, SSM state, logs.
# No S3 archive, no warmup Lambda.

resource "aws_ssm_parameter" "last_seen_clip_id" {
  name        = "/blink-monitor/last-seen-clip-id"
  description = "ID of the last Blink clip successfully delivered to Telegram"
  type        = "String"
  value       = "NONE"
}

resource "aws_cloudwatch_log_group" "poller" {
  name              = "/aws/lambda/${var.project_name}-poller"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "poller" {
  function_name = "${var.project_name}-poller"
  description   = "Polls Blink SD card for new clips and sends to Telegram"
  role          = aws_iam_role.poller.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = var.lambda_timeout_seconds
  memory_size   = var.lambda_memory_mb

  filename         = var.poller_zip_path
  source_code_hash = filebase64sha256(var.poller_zip_path)

  # Optional — see var.reserved_concurrent_executions. Omitted on new accounts
  # where the 10-execution account limit cannot spare a reservation.
  reserved_concurrent_executions = var.reserved_concurrent_executions

  depends_on = [aws_cloudwatch_log_group.poller]
}

# EventBridge minimum schedule is 1 minute. Sub-minute polling is not supported
# without a Step Functions wait-loop (deliberately not used here).
resource "aws_cloudwatch_event_rule" "poll" {
  name                = "${var.project_name}-poll"
  description         = "Invoke blink-monitor poller every minute"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "poll" {
  rule      = aws_cloudwatch_event_rule.poll.name
  target_id = "${var.project_name}-poller"
  arn       = aws_lambda_function.poller.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.poll.arn
}
