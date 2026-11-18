#!/usr/bin/env bash
# Deploy, extend, destroy or inspect the session-only lab.
#
#   bash scripts/lab.sh deploy [hours]   apply the lab, expiring in 1-10 hours (default 4)
#   bash scripts/lab.sh extend <hours>   move the expiry to <hours> from now (tags only)
#   bash scripts/lab.sh destroy          terraform destroy, then fail if anything
#                                        billable with the lab's tag is still there
#   bash scripts/lab.sh status           list billable lab resources and their expiry
#
# The TTL reaper deletes the lab's billable resources once the expiry has
# passed, or 10 hours after they were created, whichever comes first.
# Run with an administrator profile: AWS_PROFILE=<profile> bash scripts/lab.sh ...
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

usage() {
  sed -n '4,8p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

check_hours() {
  case "$1" in
    [1-9] | 10) ;;
    *) echo "Hours must be a whole number from 1 to 10, got '$1'." >&2; exit 2 ;;
  esac
}

apply() {
  local hours=$1 expires
  check_hours "$hours"
  expires=$(hours_from_now "$hours")
  echo "Lab expires at $expires ($hours h from now)."
  tf_init lab
  terraform -chdir=infra/lab apply -input=true -var="expires_at=$expires"
}

case "${1:-}" in
  deploy) apply "${2:-4}" ;;
  extend) [ -n "${2:-}" ] || usage; apply "$2" ;;
  destroy)
    tf_init lab
    # destroy still evaluates the variables; any valid expiry will do.
    terraform -chdir=infra/lab destroy -input=false -auto-approve \
      -var="expires_at=$(hours_from_now 1)"
    echo "Checking for billable lab resources left behind..."
    run_python src/reaper/handler.py --check
    ;;
  status) run_python src/reaper/handler.py --list ;;
  *) usage ;;
esac
