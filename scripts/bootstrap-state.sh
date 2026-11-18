#!/usr/bin/env bash
# Create the Terraform state bucket once per account. Safe to run again: each
# setting is (re)applied to the existing bucket.
#
# Usage: AWS_PROFILE=<admin profile> bash scripts/bootstrap-state.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

bucket=$(state_bucket)

if aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
  echo "State bucket exists: $bucket"
else
  echo "Creating state bucket: $bucket"
  aws s3api create-bucket --bucket "$bucket" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
fi

aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-ownership-controls --bucket "$bucket" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
# Old state versions are kept for 90 days, enough to recover from a bad apply.
aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration '{
  "Rules": [{
    "ID": "expire-old-state-versions",
    "Status": "Enabled",
    "Filter": {},
    "NoncurrentVersionExpiration": {"NoncurrentDays": 90},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
  }]
}'
aws s3api put-bucket-policy --bucket "$bucket" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"TlsOnly\",
    \"Effect\": \"Deny\",
    \"Principal\": \"*\",
    \"Action\": \"s3:*\",
    \"Resource\": [\"arn:aws:s3:::$bucket\", \"arn:aws:s3:::$bucket/*\"],
    \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
  }]
}"
aws s3api put-bucket-tagging --bucket "$bucket" --tagging \
  'TagSet=[{Key=Project,Value=aws-hub-spoke-network-lab},{Key=ManagedBy,Value=bootstrap-state.sh}]'

echo "State bucket ready: $bucket"
