# AWS CloudTrail → brain-invalidations hook

This module ships the **event source** for teammate's v0.9 event-driven
invalidation layer. It catches CloudTrail mutations (VPC, IAM, RDS,
security groups, …) and commits a structured `InvalidationEvent` JSON
file to your team's `brain-invalidations` GitHub repo.

The flow is:

```
AWS API call
  └─► CloudTrail event
        └─► EventBridge rule (filters event names you care about)
              └─► Lambda (this module's `lambda/handler.py`)
                    ├─► reads GitHub PAT from SSM Parameter Store
                    ├─► commits invalidations/YYYY/MM/DD/<slug>.json to repo
                    └─► (optional) posts a Slack notice
```

Engineers running `teammate ask` then see a banner whenever their query
retrieves a runbook that references a recently-mutated resource.

## What this ships

```
examples/infra/aws-cloudtrail-hook/
├── README.md                    ← you are here
├── terraform/
│   ├── main.tf                  ← Lambda + EventBridge + IAM + (optional) S3
│   ├── variables.tf             ← all inputs (event names, severity map, ...)
│   └── outputs.tf               ← Lambda ARN, EventBridge rule ARN
└── lambda/
    ├── handler.py               ← runtime — pure stdlib + boto3
    ├── requirements.txt         ← (empty — handler uses urllib only)
    └── tests/test_handler.py    ← 14 unit tests with mocked GitHub + EventBridge
```

## Prerequisites

1. **Brain-invalidations repo** — a private GitHub repo, e.g.
   `your-org/brain-invalidations`, with `main` branch initialised.
   Repo can be empty; the Lambda creates `invalidations/YYYY/MM/DD/`
   on its first commit.
2. **GitHub PAT** — fine-grained personal access token, scope:
   _Contents: read & write_ on the brain-invalidations repo only.
3. **SSM Parameter** — store the PAT as a `SecureString`:

   ```bash
   aws ssm put-parameter \
       --name /teammate/github_pat \
       --type SecureString \
       --value "$YOUR_PAT" \
       --region us-east-1
   ```

4. **AWS region with CloudTrail enabled** — multi-region trail recommended.
   The Lambda only listens to EventBridge; the trail itself is yours to
   provision (`aws_cloudtrail` resource, not in this module — most teams
   already have one).
5. **Terraform** ≥ 1.6, **AWS provider** ≥ 5.0.

## Deployment

```hcl
# terraform/main.tf in your platform repo:
module "teammate_cloudtrail_hook" {
  source = "github.com/your-org/teammate//examples/infra/aws-cloudtrail-hook/terraform?ref=v0.9.0"

  aws_account_id            = "000000000000"
  github_invalidations_repo = "your-org/brain-invalidations"

  # Optional overrides:
  github_pat_ssm_parameter_name = "/teammate/github_pat"
  github_branch                 = "main"
  default_severity              = "medium"

  # Severity for the events you care about most. The defaults are
  # sensible for most teams — override only if your incident playbook
  # disagrees.
  severity_map = {
    DetachVpcCidrBlock = "high"
    DeleteRole         = "critical"
    ModifyDBInstance   = "medium"
  }

  enable_event_archive = false  # set true to keep a raw S3 audit copy

  tags = {
    Owner   = "platform-team"
    Service = "teammate-invalidations"
  }
}
```

```bash
terraform init
terraform plan
terraform apply
```

Teardown is a single `terraform destroy`. The S3 archive bucket is only
removed when `enable_event_archive = false` was the last applied value
**and** the bucket is empty — drain it first if you've been archiving.

## Validation

After apply, fire a test event end-to-end:

```bash
# 1. Detach a CIDR block from a throwaway VPC in a sandbox account.
aws ec2 disassociate-vpc-cidr-block \
    --association-id vpc-cidr-assoc-EXAMPLE \
    --region us-east-1

# 2. Within ~30s, the Lambda should commit a file. Check:
gh api -X GET /repos/your-org/brain-invalidations/contents/invalidations \
    | jq '.[].name'

# 3. From an engineer laptop:
teammate impact list --since 1h
```

If you don't see the commit, `aws logs tail /aws/lambda/teammate-cloudtrail-hook
--follow` is the first place to look.

## Operational notes

- **Idempotency** — the GitHub Contents API returns 422 if a path
  already exists. The handler uses a unique-per-second slug (resource +
  action + unix timestamp), so the only way to collide is two events for
  the same resource+action in the same second. The handler logs a
  warning and returns; the duplicate event is dropped. If you need
  exactly-once, route through SQS with deduplication.
- **PAT rotation** — rotate the PAT in SSM directly. Lambda reads on
  every invocation (no in-process cache), so the next event uses the new
  value.
- **EventBridge cost** — single-rule, single-target; ~$1/M events. For
  most teams the volume is in the thousands per month.
- **Lambda concurrency** — set `reserved_concurrent_executions` to
  guard against runaway costs during a CloudTrail backfill replay.
- **Cross-account fan-out** — fan-out at the EventBridge bus level. Add
  a per-spoke-account rule that forwards matching events to a central
  bus, then deploy this module once in the central account.
- **Severity tuning** — start with the defaults. After a week of real
  events, look at `teammate impact list --since 7d` and re-tune the
  `severity_map` so HIGH only fires on changes you'd want a banner for.

## Why this lives in `examples/`

The OSS repo ships the schema, the handler, and the Terraform module —
but every team's CloudTrail trail, account topology, IAM constraints,
and severity preferences differ. We don't want to be in the business of
maintaining a one-size-fits-all `terraform-aws-teammate` module. Fork
this directory into your platform repo, tune to taste, version-pin via
git ref. You will probably want to:

- Add custom event names for in-house services that emit CloudTrail
  events (e.g. `aws_glue_job` mutations).
- Wire the Lambda to your existing EventBridge bus instead of the
  default account bus.
- Replace the GitHub Contents API commit with `git-remote-codecommit`
  if your invalidations repo lives in CodeCommit.

## Security

- The Lambda's IAM role grants `ssm:GetParameter` only on the single
  PAT parameter and `s3:PutObject` only on the optional archive bucket.
  No `*` resource ARNs.
- The PAT is read with `WithDecryption=true`; the SSM `KMS` key
  permissions on the engineer who provisioned the PAT are the trust
  root.
- The handler logs the mapped event payload at `INFO`. If your team's
  tagging includes anything sensitive (PII tags, cost-centre codes
  with employee ids), redact before passing to CloudWatch — easiest by
  setting log retention low and stripping the `metadata` map in
  `handler.py`.
- Slack webhook URLs are passed via env var. They are not technically
  secrets but treat them like one — leaking the URL lets anyone post.
