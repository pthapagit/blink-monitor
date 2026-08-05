# Least-privilege IAM for the poller Lambda.
# No S3, no warmup Lambda, no blanket secretsmanager:*.

resource "aws_iam_role" "poller" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "poller" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.poller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.blink_credentials.arn,
          aws_secretsmanager_secret.telegram_bot_token.arn,
        ]
      },
      {
        # Scoped write: ONLY the Blink secret, so a compromised Lambda cannot
        # overwrite the Telegram token.
        Sid      = "RefreshBlinkToken"
        Effect   = "Allow"
        Action   = ["secretsmanager:PutSecretValue"]
        Resource = aws_secretsmanager_secret.blink_credentials.arn
      },
      {
        Sid    = "ReadWriteState"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter",
        ]
        Resource = aws_ssm_parameter.last_seen_clip_id.arn
      },
      {
        Sid    = "Logging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_name}-poller:*"
      },
    ]
  })
}
