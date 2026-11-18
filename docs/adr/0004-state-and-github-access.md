# 4. Terraform state and GitHub access to AWS

- Status: accepted
- Date: 2026-11-18

## Context

Terraform needs remote state that CI and my laptop share. GitHub Actions needs
read access to plan pull requests, but the lab must never be applied from CI
(ADR 1). The repository is public, so anything a workflow posts is public too.

## Decision

**State.** One S3 bucket per project, `<account-id>-nwlab-tfstate`, created by
`scripts/bootstrap-state.sh`: versioned (old versions expire after 90 days),
SSE-S3 encryption, public access blocked, TLS-only bucket policy. Locking uses
S3 native lock files (`use_lockfile`). The bucket name holds the account ID, so
it is passed at `init` time and never committed. Keys: `account/` and `lab/`.

**One GitHub role, read only.** `nwlab-github-plan` trusts only pull requests
from this repository, through GitHub's immutable OIDC subject
(`repo:owner@id/repo@id:pull_request`), so a re-created repository with the
same name cannot assume it. It has `ReadOnlyAccess`, reads state, and plans
with `-lock=false`. It reads the account's existing GitHub OIDC provider; it
never creates one. There is no deploy role.

**Guard rails** deny the plan role data reads (secrets, parameters other than
public AMI IDs, objects outside the state bucket, log events, Athena results,
SSM command output, instance consoles), the audit trail, and every Region but
eu-west-1, AWS Budgets excepted, because its API is in us-east-1.

**Boundary.** Every role this project creates (the reaper, its scheduler, and
from M2 the test instances) carries `nwlab-workload-boundary`. Statements are
added in the milestone that first needs them.

**Pull request comments** list resource addresses and actions only. The full
plan stays in the job log, where GitHub masks the account ID and the alert
address (both repository secrets).

## Consequences

- An administrator applies both roots from a laptop. CI cannot change AWS at
  all, including its own permissions.
- Pull requests from forks get no OIDC token, so they run lint and tests only.
- The lab plan on a pull request uses an expiry four hours ahead, so the
  `ExpiresAt` tag always shows as a change. That is expected.
