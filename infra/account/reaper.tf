# -----------------------------------------------------------------------------
# TTL reaper (docs/adr/0002): every 15 minutes, delete the lab's billable
# resources once their ExpiresAt tag has passed, or once they are older than
# the hard cap. Its delete permissions only reach resources tagged with the
# project, both in its own policy and in the boundary.
# -----------------------------------------------------------------------------

data "archive_file" "reaper" {
  type        = "zip"
  source_file = "${path.module}/../../src/reaper/handler.py"
  output_path = "${path.module}/.build/reaper.zip"
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "reaper" {
  name                 = "${var.prefix}-reaper"
  description          = "TTL reaper: deletes expired billable resources tagged with the lab's Project tag"
  assume_role_policy   = data.aws_iam_policy_document.lambda_trust.json
  permissions_boundary = aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "reaper" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.reaper.arn}:*"]
  }

  statement {
    sid = "FindLabResources"
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
    ]
    resources = [
      "${local.ec2_arn}:vpn-connection/*",
      "${local.ec2_arn}:vpc-endpoint/*",
      "${local.ec2_arn}:natgateway/*",
      "${local.ec2_arn}:transit-gateway-attachment/*",
      "${local.ec2_arn}:vpc-flow-log/*",
      "${local.ec2_arn}:instance/*",
      "${local.ec2_arn}:elastic-ip/*",
      "${local.ec2_arn}:volume/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_tag]
    }
  }

  statement {
    sid       = "DeleteOnlyLabLoadBalancers"
    actions   = ["elasticloadbalancing:DeleteLoadBalancer"]
    resources = ["arn:${local.partition}:elasticloadbalancing:${local.region}:${local.account_id}:loadbalancer/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_tag]
    }
  }

  statement {
    sid       = "Report"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "reaper" {
  name   = "reaper"
  role   = aws_iam_role.reaper.id
  policy = data.aws_iam_policy_document.reaper.json
}

resource "aws_cloudwatch_log_group" "reaper" {
  #checkov:skip=CKV_AWS_158:CloudWatch's default encryption is used; a KMS key costs $1/month and these logs list resource IDs only.
  #checkov:skip=CKV_AWS_338:30 days is enough to look back over a few lab sessions.
  name              = "/aws/lambda/${var.prefix}-reaper"
  retention_in_days = 30
}

resource "aws_lambda_function" "reaper" {
  #checkov:skip=CKV_AWS_116:No dead-letter queue: the next scheduled run retries, and failures go to the alert topic through an on-failure destination.
  #checkov:skip=CKV_AWS_117:The reaper calls only public AWS APIs, so it runs outside a VPC.
  #checkov:skip=CKV_AWS_115:No reserved concurrency: the account limit is 10, too low to reserve from. The 10-minute timeout is shorter than the 15-minute schedule, so runs do not overlap.
  #checkov:skip=CKV_AWS_173:The environment holds no secrets (project tag, topic ARN, cap), so the default AWS-managed key is enough.
  #checkov:skip=CKV_AWS_272:Code signing is out of scope for a single-file function deployed by an administrator.
  function_name    = "${var.prefix}-reaper"
  description      = "Deletes expired billable lab resources (ExpiresAt tag or ${var.max_age_hours} h cap)"
  role             = aws_iam_role.reaper.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256
  memory_size      = 128
  timeout          = 600

  environment {
    variables = {
      PROJECT       = var.project_tag
      TOPIC_ARN     = aws_sns_topic.alerts.arn
      MAX_AGE_HOURS = tostring(var.max_age_hours)
      DRY_RUN       = "false"
    }
  }

  tracing_config {
    mode = "PassThrough"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.reaper.name
  }

  depends_on = [aws_iam_role_policy.reaper]
}

# Scheduler invokes asynchronously. No retries: the next run is 15 minutes
# away. A run that crashes is reported to the alert topic.
resource "aws_lambda_function_event_invoke_config" "reaper" {
  function_name                = aws_lambda_function.reaper.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 900

  destination_config {
    on_failure {
      destination = aws_sns_topic.alerts.arn
    }
  }
}

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "reaper_scheduler" {
  name                 = "${var.prefix}-reaper-scheduler"
  description          = "EventBridge Scheduler invokes the TTL reaper with this role"
  assume_role_policy   = data.aws_iam_policy_document.scheduler_trust.json
  permissions_boundary = aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "reaper_scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.reaper.arn]
  }
}

resource "aws_iam_role_policy" "reaper_scheduler" {
  name   = "invoke-reaper"
  role   = aws_iam_role.reaper_scheduler.id
  policy = data.aws_iam_policy_document.reaper_scheduler.json
}

resource "aws_scheduler_schedule" "reaper" {
  #checkov:skip=CKV_AWS_297:The schedule holds no data beyond the target ARN; a customer managed key would cost $1/month.
  name        = "${var.prefix}-reaper"
  description = "Runs the TTL reaper every 15 minutes"
  # Always on: with no lab deployed, a run finds nothing and exits in seconds.
  schedule_expression = "rate(15 minutes)"
  state               = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.reaper.arn
    role_arn = aws_iam_role.reaper_scheduler.arn

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
