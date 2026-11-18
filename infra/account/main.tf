# Account-level resources: always on, applied by an administrator with
# scripts/account.sh, never from CI. They cost about $0: IAM is free, and the
# reaper, its schedule, the alert topic and the budget stay inside free tiers.
#
# - a permissions boundary that caps every IAM role in this project
# - guard rails and a read-only plan role for pull requests
# - the TTL reaper that deletes a forgotten lab (see docs/adr/0002)
# - an alert topic and a tag-filtered budget
#
# The GitHub OIDC provider already exists in this account (created by
# aws-ha-web-platform). An account can hold only one provider per URL, so it is
# read here, not created.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = var.region
  ec2_arn    = "arn:${local.partition}:ec2:${local.region}:${local.account_id}"

  state_bucket_arn = "arn:${local.partition}:s3:::${var.state_bucket}"

  # Roles in this project are named <prefix>-<name> and must carry the
  # boundary. The plan role is the exception: it is a GitHub role, read only.
  alert_topic_name = "${var.prefix}-alerts"
  alert_topic_arn  = "arn:${local.partition}:sns:${local.region}:${local.account_id}:${local.alert_topic_name}"
}

# -----------------------------------------------------------------------------
# Permissions boundary: the most any role this project creates can ever do.
# Statements are added in the milestone that first needs them.
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "workload_boundary" {
  statement {
    sid = "WriteOwnLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*${var.prefix}-*"]
  }

  statement {
    sid = "FindLabResources"
    # Describe calls do not support resource-level permissions.
    actions = [
      "ec2:DescribeVpnConnections",
      "ec2:DescribeVpcEndpoints",
      "ec2:DescribeNatGateways",
      "ec2:DescribeTransitGatewayAttachments",
      "ec2:DescribeFlowLogs",
      "ec2:DescribeInstances",
      "ec2:DescribeAddresses",
      "ec2:DescribeVolumes",
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeTags",
    ]
    resources = ["*"]
  }

  statement {
    sid = "DeleteOnlyLabResources"
    actions = [
      "ec2:DeleteVpnConnection",
      "ec2:DeleteVpcEndpoints",
      "ec2:DeleteNatGateway",
      "ec2:DeleteTransitGatewayVpcAttachment",
      "ec2:DeleteFlowLogs",
      "ec2:TerminateInstances",
      "ec2:ReleaseAddress",
      "ec2:DeleteVolume",
      "elasticloadbalancing:DeleteLoadBalancer",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_tag]
    }
  }

  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [local.alert_topic_arn]
  }

  statement {
    sid       = "InvokeOwnFunctions"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${var.prefix}-*"]
  }
}

resource "aws_iam_policy" "workload_boundary" {
  name        = "${var.prefix}-workload-boundary"
  description = "Ceiling for every IAM role the aws-hub-spoke-network-lab project creates"
  policy      = data.aws_iam_policy_document.workload_boundary.json
}

# -----------------------------------------------------------------------------
# Guard rails for the GitHub plan role. ReadOnlyAccess lets Terraform refresh
# any resource, but it also reads data; these denies take the data reads back
# out, so a plan job sees configuration, never content.
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "github_guardrails" {
  statement {
    sid    = "NoDataReads"
    effect = "Deny"
    actions = [
      "secretsmanager:GetSecretValue",
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:GetRecords",
      "sqs:ReceiveMessage",
      "kinesis:GetRecords",
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
      "logs:StartQuery",
      "logs:GetQueryResults",
      "athena:GetQueryResults",
      "ssm:GetCommandInvocation",
      "ec2:GetConsoleOutput",
      "ec2:GetConsoleScreenshot",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "NoParameterReadsButPublicOnes"
    effect = "Deny"
    # The lab reads public AMI IDs from /aws/service/; every parameter in the
    # account stays unreadable.
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    not_resources = ["arn:${local.partition}:ssm:${local.region}::parameter/aws/service/*"]
  }

  statement {
    sid           = "NoObjectReadsOutsideState"
    effect        = "Deny"
    actions       = ["s3:GetObject", "s3:GetObjectVersion"]
    not_resources = ["${local.state_bucket_arn}/*"]
  }

  statement {
    sid     = "NotTheAuditTrail"
    effect  = "Deny"
    actions = ["s3:*", "cloudtrail:*"]
    resources = [
      "arn:${local.partition}:s3:::security-*",
      "arn:${local.partition}:s3:::security-*/*",
      "arn:${local.partition}:cloudtrail:*:${local.account_id}:trail/*",
    ]
  }

  statement {
    sid    = "OnlyThisRegion"
    effect = "Deny"
    # Global services, and AWS Budgets, whose API lives in us-east-1.
    not_actions = ["iam:*", "sts:*", "organizations:Describe*", "support:*", "budgets:*"]
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }
}

resource "aws_iam_policy" "github_guardrails" {
  name        = "${var.prefix}-github-guardrails"
  description = "Denies data reads, audit-trail access and other Regions to the aws-hub-spoke-network-lab GitHub role"
  policy      = data.aws_iam_policy_document.github_guardrails.json
}

# -----------------------------------------------------------------------------
# Plan role: pull requests from this repository only. Reads AWS and state.
# There is no deploy role: the lab bills by the hour, so it is only ever
# deployed deliberately, from a laptop (docs/adr/0001).
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_subject_prefix}:pull_request"]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  name                 = "${var.prefix}-github-plan"
  description          = "GitHub Actions runs terraform plan on pull requests with this role (read only)"
  assume_role_policy   = data.aws_iam_policy_document.plan_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "plan_state" {
  statement {
    sid       = "ListState"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]
  }
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${local.state_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "plan_state" {
  name   = "terraform-state-read"
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.plan_state.json
}

resource "aws_iam_role_policy_attachment" "plan_read_only" {
  role       = aws_iam_role.github_plan.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy_attachment" "plan_guardrails" {
  role       = aws_iam_role.github_plan.name
  policy_arn = aws_iam_policy.github_guardrails.arn
}

# -----------------------------------------------------------------------------
# Alerts: reaper reports and reaper failures. Email only; the address is a
# deploy-time variable and never committed.
# -----------------------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name              = local.alert_topic_name
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# -----------------------------------------------------------------------------
# Budget: the last resort. Billing data arrives 8-24 hours late, so this
# catches what the reaper missed, not a lab left up for an afternoon. Credits
# are excluded, or the Free plan's credits would hide every charge. Budgets
# without actions are free. The Project tag must be an active cost allocation
# tag (see README).
# -----------------------------------------------------------------------------
resource "aws_budgets_budget" "lab" {
  name         = "${var.prefix}-lab-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = [format("user:Project$%s", var.project_tag)]
  }

  cost_types {
    include_credit = false
    include_refund = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "FORECASTED"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }
}
