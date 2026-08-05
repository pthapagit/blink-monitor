variable "aws_region" {
  description = "AWS region. us-east-1 minimises latency to Blink's API."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix for all blink-monitor resources."
  type        = string
  default     = "blink-monitor"
}

variable "poller_zip_path" {
  description = <<-EOT
    Path to the Lambda deployment zip. Build before `terraform apply`:
      cd lambda/poller
      pip install --require-hashes -r requirements.lock -t ./package/
      cp *.py ./package/
      cd package && zip -r ../poller.zip . && cd ..
  EOT
  type        = string
  default     = "../lambda/poller/poller.zip"
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MB."
  type        = number
  default     = 512
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout in seconds (max 60)."
  type        = number
  default     = 60
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the poller Lambda."
  type        = number
  default     = 30
}

variable "reserved_concurrent_executions" {
  description = <<-EOT
    Reserved concurrency for the poller Lambda. Default is 1 to prevent
    duplicate Telegram messages and SSM races (required by SECURITY.md).

    Brand-new AWS accounts often have only 10 total concurrent executions,
    and AWS requires 10 to stay unreserved — reserving 1 then fails with
    InvalidParameterValueException. On those accounts set this to null
    until you request a concurrency limit increase, then re-apply with 1.
  EOT
  type        = number
  default     = 1
  nullable    = true
}
