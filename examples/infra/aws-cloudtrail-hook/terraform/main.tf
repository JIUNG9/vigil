terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# ---------- IAM role for the lambda ----------

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.lambda_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_policy" {
  statement {
    sid     = "ReadGitHubPat"
    effect  = "Allow"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:*:${var.aws_account_id}:parameter${var.github_pat_ssm_parameter_name}",
    ]
  }

  dynamic "statement" {
    for_each = var.enable_event_archive ? [1] : []

    content {
      sid    = "WriteEventArchive"
      effect = "Allow"
      actions = [
        "s3:PutObject",
      ]
      resources = [
        "${aws_s3_bucket.event_archive[0].arn}/*",
      ]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_policy.json
}

# ---------- Lambda packaging + function ----------

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/.build/lambda.zip"
  excludes    = ["tests", "tests/test_handler.py", "__pycache__"]
}

resource "aws_lambda_function" "this" {
  function_name    = var.lambda_function_name
  role             = aws_iam_role.lambda.arn
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.11"
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      GITHUB_REPO              = var.github_invalidations_repo
      GITHUB_BRANCH            = var.github_branch
      GITHUB_PAT_SSM_PARAMETER = var.github_pat_ssm_parameter_name
      SLACK_WEBHOOK_URL        = var.slack_webhook_url
      SEVERITY_MAP_JSON        = jsonencode(var.severity_map)
      DEFAULT_SEVERITY         = var.default_severity
    }
  }

  tags = var.tags
}

# ---------- EventBridge rule ----------

resource "aws_cloudwatch_event_rule" "this" {
  name        = "${var.lambda_function_name}-rule"
  description = "Catches CloudTrail mutations relevant to the team brain."

  event_pattern = jsonencode({
    source      = ["aws.ec2", "aws.iam", "aws.rds"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventName = var.event_name_patterns
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "this" {
  rule      = aws_cloudwatch_event_rule.this.name
  target_id = var.lambda_function_name
  arn       = aws_lambda_function.this.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.this.arn
}

# ---------- Optional event archive ----------

resource "aws_s3_bucket" "event_archive" {
  count  = var.enable_event_archive ? 1 : 0
  bucket = "${var.lambda_function_name}-archive-${var.aws_account_id}"
  tags   = var.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "event_archive" {
  count  = var.enable_event_archive ? 1 : 0
  bucket = aws_s3_bucket.event_archive[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "event_archive" {
  count                   = var.enable_event_archive ? 1 : 0
  bucket                  = aws_s3_bucket.event_archive[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
