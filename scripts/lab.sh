#!/usr/bin/env bash
# Deploy, extend, destroy or inspect the session-only lab.
#
#   bash scripts/lab.sh deploy [hours]   apply the lab, expiring in 1-10 hours (default 4),
#                                        then run the connectivity matrix
#   bash scripts/lab.sh extend <hours>   move the expiry to <hours> from now (tags only)
#   bash scripts/lab.sh matrix           run the connectivity matrix and save the report
#   bash scripts/lab.sh failover [runs]  cut VPN tunnel 1 and measure the BGP failover (default 3 runs)
#   bash scripts/lab.sh destroy          terraform destroy, then fail if anything
#                                        billable with the lab's tag is still there
#   bash scripts/lab.sh status           list billable lab resources and their expiry
#
# Egress defaults to the NAT gateway; TF_VAR_egress=nat-instance switches it.
# extend keeps whatever is deployed.
# The TTL reaper deletes the lab's billable resources once the expiry has
# passed, or 10 hours after they were created, whichever comes first.
# Run with an administrator profile: AWS_PROFILE=<profile> bash scripts/lab.sh ...
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

usage() {
  sed -n '4,12p' "$0" | sed 's/^# \{0,1\}//' >&2
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

# terraform output -json for the test scripts, outside the repository's tree.
OUTPUTS=infra/lab/.build/outputs.json
write_outputs() {
  mkdir -p "$(dirname "$OUTPUTS")"
  terraform -chdir=infra/lab output -json >"$OUTPUTS"
}

# From M3 the on-prem rows need both tunnels and BGP up; wait for them first.
matrix() {
  write_outputs
  run_python scripts/failover.py wait --outputs "$OUTPUTS"
  run_python scripts/matrix.py run --outputs "$OUTPUTS" --save docs/results
}

case "${1:-}" in
  deploy)
    apply "${2:-4}"
    matrix
    ;;
  extend)
    [ -n "${2:-}" ] || usage
    if [ -z "${TF_VAR_egress:-}" ]; then
      tf_init lab
      TF_VAR_egress=$(terraform -chdir=infra/lab output -raw egress 2>/dev/null || echo nat-gateway)
      export TF_VAR_egress
    fi
    apply "$2"
    ;;
  matrix)
    tf_init lab
    matrix
    ;;
  failover)
    tf_init lab
    write_outputs
    run_python scripts/failover.py run --outputs "$OUTPUTS" --runs "${2:-3}" --save docs/results
    ;;
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
