#!/usr/bin/env bash
# Plan or apply the account root module: boundary, plan role, TTL reaper,
# alerts and budget. Run by an administrator, never from CI.
#
# Usage: AWS_PROFILE=<admin profile> ALERT_EMAIL=<address> bash scripts/account.sh plan|apply
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

action=${1:-plan}
case "$action" in
  plan | apply) ;;
  *) echo "Usage: $0 plan|apply" >&2; exit 2 ;;
esac
: "${ALERT_EMAIL:?Set ALERT_EMAIL to the address for reaper reports and budget alerts}"
# A value read with the Windows AWS CLI ends in a carriage return, which would
# replace the subscription and need a new confirmation.
ALERT_EMAIL=$(printf %s "$ALERT_EMAIL" | tr -d '\r')

tf_init account
# apply shows the plan and waits for "yes"; plan never prompts.
input=false
if [ "$action" = apply ]; then input=true; fi
terraform -chdir=infra/account "$action" -input="$input" \
  -var="state_bucket=$(state_bucket)" -var="alert_email=$ALERT_EMAIL"
