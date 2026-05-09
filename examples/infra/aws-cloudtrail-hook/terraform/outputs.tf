output "lambda_function_arn" {
  description = "ARN of the deployed Lambda function."
  value       = aws_lambda_function.this.arn
}

output "lambda_function_name" {
  description = "Name of the deployed Lambda function."
  value       = aws_lambda_function.this.function_name
}

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge rule that fires the Lambda."
  value       = aws_cloudwatch_event_rule.this.arn
}

output "iam_role_arn" {
  description = "ARN of the Lambda IAM role."
  value       = aws_iam_role.lambda.arn
}

output "event_archive_bucket" {
  description = "Name of the optional S3 archive bucket. Empty when disabled."
  value       = var.enable_event_archive ? aws_s3_bucket.event_archive[0].bucket : ""
}
