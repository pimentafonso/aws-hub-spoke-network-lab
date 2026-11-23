# -----------------------------------------------------------------------------
# VPC Flow Logs for every VPC, to S3 as Parquet, queried with Athena
# (docs/adr/0007). scripts/evidence.py checks each matrix result against them:
# a security-group deny must show a REJECT at the destination, a routing deny
# no record in the destination VPC, an allow an ACCEPT.
#
# The bucket, the Glue table and the Athena workgroup belong to the session and
# are destroyed with it; the reports in docs/results keep what was found.
#
#   flow logs to S3     $0.25/GB delivered (a session makes kilobytes)
#   Athena              $5/TB scanned, at least 10 MB a query
# -----------------------------------------------------------------------------

locals {
  # Parquet column names are the field names with underscores.
  flow_log_fields = [
    "version", "vpc-id", "subnet-id", "interface-id", "srcaddr", "dstaddr",
    "pkt-srcaddr", "pkt-dstaddr", "srcport", "dstport", "protocol", "packets",
    "bytes", "start", "end", "action", "log-status", "flow-direction", "tcp-flags",
  ]
  flow_log_columns = {
    version        = "bigint"
    vpc_id         = "string"
    subnet_id      = "string"
    interface_id   = "string"
    srcaddr        = "string"
    dstaddr        = "string"
    pkt_srcaddr    = "string"
    pkt_dstaddr    = "string"
    srcport        = "bigint"
    dstport        = "bigint"
    protocol       = "bigint"
    packets        = "bigint"
    bytes          = "bigint"
    start          = "bigint"
    end            = "bigint"
    action         = "string"
    log_status     = "string"
    flow_direction = "string"
    tcp_flags      = "bigint"
  }

  # Where delivery writes, with Hive-style keys and one partition an hour.
  flow_log_root = "s3://${aws_s3_bucket.flow_logs.bucket}/AWSLogs/aws-account-id=${local.account_id}/aws-service=vpcflowlogs/aws-region=${var.region}/"
}

resource "aws_s3_bucket" "flow_logs" {
  #checkov:skip=CKV_AWS_18:Access logging for a bucket that lives one session would need a second bucket.
  #checkov:skip=CKV_AWS_144:Cross-region replication is not needed for one session's logs.
  #checkov:skip=CKV_AWS_145:SSE-S3 is enough; a KMS key costs $1 a month and outlives the session.
  #checkov:skip=CKV_AWS_21:Versioning is not needed: the logs are written once and destroyed with the lab.
  #checkov:skip=CKV2_AWS_62:Nothing listens for events on this bucket.
  # A generated suffix: the name holds no account ID.
  bucket_prefix = "nwlab-flow-logs-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "flow_logs" {
  bucket                  = aws_s3_bucket.flow_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "flow_logs" {
  bucket = aws_s3_bucket.flow_logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "flow_logs" {
  bucket = aws_s3_bucket.flow_logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "flow_logs" {
  bucket = aws_s3_bucket.flow_logs.id

  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "flow_logs_bucket" {
  statement {
    sid       = "LogDeliveryWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.flow_logs.arn}/AWSLogs/*"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:*"]
    }
  }

  statement {
    sid       = "LogDeliveryAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.flow_logs.arn]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:*"]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.flow_logs.arn, "${aws_s3_bucket.flow_logs.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "flow_logs" {
  bucket = aws_s3_bucket.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.flow_logs]
}

resource "aws_flow_log" "vpc" {
  for_each = local.vpcs

  vpc_id                   = module.vpc[each.key].id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = aws_s3_bucket.flow_logs.arn
  max_aggregation_interval = 60
  log_format               = join(" ", [for f in local.flow_log_fields : "$${${f}}"])

  destination_options {
    file_format                = "parquet"
    hive_compatible_partitions = true
    per_hour_partition         = true
  }

  tags = { Name = "nwlab-${each.key}" }

  depends_on = [aws_s3_bucket_policy.flow_logs]
}

# --- Athena ----------------------------------------------------------------------

resource "aws_glue_catalog_database" "flow_logs" {
  name = "nwlab_flow_logs"
}

# Partition projection: Athena works the partitions out from the path, so no
# crawler and no MSCK REPAIR. Queries must filter on year, month, day and hour.
resource "aws_glue_catalog_table" "flow_logs" {
  name          = "vpc_flow_logs"
  database_name = aws_glue_catalog_database.flow_logs.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL                    = "TRUE"
    "parquet.compression"       = "SNAPPY"
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "2026,2036"
    "projection.month.type"     = "integer"
    "projection.month.range"    = "1,12"
    "projection.month.digits"   = "2"
    "projection.day.type"       = "integer"
    "projection.day.range"      = "1,31"
    "projection.day.digits"     = "2"
    "projection.hour.type"      = "integer"
    "projection.hour.range"     = "0,23"
    "projection.hour.digits"    = "2"
    "storage.location.template" = "${local.flow_log_root}year=$${year}/month=$${month}/day=$${day}/hour=$${hour}"
  }

  dynamic "partition_keys" {
    for_each = ["year", "month", "day", "hour"]
    content {
      name = partition_keys.value
      type = "string"
    }
  }

  storage_descriptor {
    location      = local.flow_log_root
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    dynamic "columns" {
      for_each = local.flow_log_columns
      content {
        name = columns.key
        type = columns.value
      }
    }
  }
}

resource "aws_athena_workgroup" "lab" {
  name          = "nwlab"
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = false
    # A runaway query stops at 1 GB, about half a cent.
    bytes_scanned_cutoff_per_query = 1073741824

    result_configuration {
      output_location = "s3://${aws_s3_bucket.flow_logs.bucket}/athena-results/"
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}
