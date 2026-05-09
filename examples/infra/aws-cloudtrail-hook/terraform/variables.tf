variable "aws_account_id" {
  description = "AWS account that owns the EventBridge rule and Lambda."
  type        = string
}

variable "lambda_function_name" {
  description = "Name for the Lambda function."
  type        = string
  default     = "teammate-cloudtrail-hook"
}

variable "github_invalidations_repo" {
  description = "owner/repo for the brain-invalidations git repo (e.g. your-org/brain-invalidations)."
  type        = string
}

variable "github_branch" {
  description = "Branch to commit invalidation events to."
  type        = string
  default     = "main"
}

variable "github_pat_ssm_parameter_name" {
  description = "SSM Parameter Store name holding the GitHub PAT (SecureString)."
  type        = string
  default     = "/teammate/github_pat"
}

variable "slack_webhook_url" {
  description = "Optional Slack webhook URL. Empty disables Slack notifications."
  type        = string
  default     = ""
  sensitive   = true
}

# Note (advisor flag E): the spec called this `event_filter_arn`, but the
# values are CloudTrail event NAMES (DetachVpcCidrBlock etc.), not ARNs.
# Renamed for clarity.
variable "event_name_patterns" {
  description = "List of CloudTrail event names that fire the EventBridge rule."
  type        = list(string)
  default = [
    "DetachVpcCidrBlock",
    "AssociateVpcCidrBlock",
    "DeleteVpc",
    "ModifyVpcAttribute",
    "DeleteRole",
    "DetachRolePolicy",
    "PutRolePolicy",
    "DeleteSecurityGroup",
    "AuthorizeSecurityGroupIngress",
    "RevokeSecurityGroupIngress",
    "ModifyDBInstance",
    "DeleteDBInstance",
    "CreateDBClusterSnapshot",
    "DeleteDBClusterSnapshot",
  ]
}

variable "severity_map" {
  description = "Map of CloudTrail event name to severity (low/medium/high/critical)."
  type        = map(string)
  default = {
    DetachVpcCidrBlock            = "high"
    AssociateVpcCidrBlock         = "medium"
    DeleteVpc                     = "critical"
    ModifyVpcAttribute            = "medium"
    DeleteRole                    = "critical"
    DetachRolePolicy              = "high"
    PutRolePolicy                 = "high"
    DeleteSecurityGroup           = "high"
    AuthorizeSecurityGroupIngress = "medium"
    RevokeSecurityGroupIngress    = "medium"
    ModifyDBInstance              = "medium"
    DeleteDBInstance              = "critical"
    CreateDBClusterSnapshot       = "low"
    DeleteDBClusterSnapshot       = "high"
  }
}

variable "default_severity" {
  description = "Severity assigned when an event name is not in severity_map."
  type        = string
  default     = "medium"
}

variable "enable_event_archive" {
  description = "Provision an S3 bucket for the optional event archive."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Common tags applied to every resource."
  type        = map(string)
  default     = {}
}
