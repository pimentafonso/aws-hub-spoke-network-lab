# 1. Terraform, with a session-only lab root and an always-on account root

- Status: accepted
- Date: 2026-11-18

## Context

This is the third project in the portfolio.
[aws-ha-web-platform](https://github.com/pimentafonso/aws-ha-web-platform) uses
CloudFormation and
[aws-event-driven-orders](https://github.com/pimentafonso/aws-event-driven-orders)
uses Terraform. Unlike those two, this lab is never left running: it costs about
$0.42 an hour while deployed (about $310 a month), so every session ends with a
destroy. A few things must outlive a session, though: the GitHub roles, the
permissions boundary and the safety net that deletes a forgotten lab.

## Decision

Use Terraform 1.10 or later with the `hashicorp/aws` provider 6.x, and reuse the
orders project's patterns: S3 state with native locking, the existing
account-wide GitHub OIDC provider read as a data source (never created again), a
permissions boundary with a guardrails policy, and TFLint, Checkov and
ShellCheck on every pull request.

Two root modules with different lifetimes:

| Root | Lifetime | Applied by | Holds |
|---|---|---|---|
| `infra/account` (M1) | always on, costs about $0 | an administrator, locally | GitHub plan role, boundary, guardrails, the TTL reaper, the budget |
| `infra/lab` | one session | the deploy script, locally | VPCs, Transit Gateway, VPN, NAT, PrivateLink, test instances, flow logs |

CI plans both on pull requests but applies neither. Merging a pull request
should never start a stack that bills by the hour.

## Consequences

- One lab, not dev and prod. The lab is the environment; its size never changes.
- Every deploy and destroy is a deliberate local command, run with a known
  session length (see ADR 2).
- Each lab resource carries `Project` and `ExpiresAt` tags from the provider's
  `default_tags`, and `expires_at` has no default, so the lab cannot be deployed
  without an expiry.
