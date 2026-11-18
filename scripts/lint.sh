#!/usr/bin/env bash
# Format-check, validate, lint and security-scan the Terraform, then scan the
# GitHub Actions workflows and lint the Python and shell code.
set -euo pipefail
cd "$(dirname "$0")/.."

# Run a linter if it is installed. Locally a missing tool is skipped with a
# warning; in CI it is an error, so nothing is skipped silently.
need() {
  if command -v "$1" >/dev/null 2>&1; then
    return 0
  elif [ "${CI:-}" = true ]; then
    echo "$1 not found" >&2
    exit 1
  else
    echo "$1 not found: skipping it" >&2
    return 1
  fi
}

if need terraform; then
  # A separate data directory, so linting never reuses (or needs credentials
  # for) a real backend set up by the deploy scripts.
  export TF_DATA_DIR=.terraform-lint
  terraform fmt -check -recursive -diff infra
  # Every directory with a versions.tf is a root module.
  for root in infra/*/; do
    [ -f "$root/versions.tf" ] || continue
    terraform -chdir="$root" init -backend=false -input=false >/dev/null
    terraform -chdir="$root" validate
  done
fi

if need tflint; then
  tflint --init
  tflint --recursive --chdir=infra
fi

if need checkov; then
  checkov --quiet --framework terraform -d infra
  checkov --quiet --framework github_actions -d .github/workflows
fi

if need ruff; then
  ruff check .
  ruff format --check .
fi

if need shellcheck; then
  shellcheck -x scripts/*.sh
fi
