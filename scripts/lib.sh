#!/usr/bin/env bash
# Shared settings for the scripts. Sourced, not run.

# Used by the scripts that source this file.
# shellcheck disable=SC2034
REGION=${AWS_REGION:-eu-west-1}
PROJECT_PREFIX=nwlab
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION

account_id() {
  aws sts get-caller-identity --query Account --output text
}

# The bucket name holds the account ID, so it is worked out at run time and
# never committed.
state_bucket() {
  echo "$(account_id)-${PROJECT_PREFIX}-tfstate"
}

# terraform init for a root module under infra/, against the state bucket.
tf_init() {
  local dir=$1
  terraform -chdir="infra/$dir" init -input=false -reconfigure \
    -backend-config="bucket=$(state_bucket)" >/dev/null
}

# An RFC 3339 UTC time this many hours from now (GNU date, as in Git Bash).
hours_from_now() {
  date -u -d "+$1 hours" +%Y-%m-%dT%H:%M:%SZ
}

# Run a Python script from the virtualenv if there is one. boto3 reads an
# `aws login` profile itself with botocore[crt] (requirements-dev.txt) and
# refreshes it; credentials exported from the CLI expired within 15 minutes,
# shorter than a session's evidence and failover runs.
run_python() {
  local python=python3
  if [ -x .venv/Scripts/python ]; then
    python=.venv/Scripts/python
  elif [ -x .venv/bin/python ]; then
    python=.venv/bin/python
  fi
  "$python" "$@"
}
