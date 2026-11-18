# 2. Session-only lab with a layered safety net

- Status: accepted (layers 2 to 4 are built in M1)
- Date: 2026-11-18

## Context

The lab's hourly charges come from Transit Gateway attachments ($0.05 each),
the VPN connection ($0.05), the NAT Gateway ($0.048), the NLB ($0.0252), the
interface endpoints ($0.011 per AZ), public IPv4 addresses ($0.005 each) and
t4g.micro instances ($0.0092). These are eu-west-1 on-demand prices from the
AWS Price List, checked on 18 November 2026. Together that is about $0.42 an
hour. Left running by mistake, the lab would use up a month of credits in days.
AWS Budgets cannot catch this in time, because billing data arrives 8 to 24
hours late.

## Decision

Four layers, each one independent of the layers before it:

1. **Scripted destroy.** `lab.sh destroy` runs `terraform destroy`, then lists
   anything billable still tagged with the project and fails if it finds any.
2. **Expiry tag.** `lab.sh deploy` sets `ExpiresAt` to the deploy time plus the
   session length (4 hours by default) on every resource. `lab.sh extend`
   moves it later. A hard cap, 10 hours after creation, applies whatever the
   tag says.
3. **TTL reaper.** A Lambda function in `infra/account`, run by EventBridge
   Scheduler every 15 minutes, deletes expired lab resources in dependency
   order, the most expensive first: VPN connection, TGW attachments, NAT
   Gateway, NLB, endpoints, instances, Elastic IPs, then the transit gateway
   and the VPCs. IAM conditions on `aws:ResourceTag/Project` limit its delete
   permissions to lab resources. It emails a list of what it deleted. It
   does not need Terraform state; the next `lab.sh destroy` reconciles state
   with what is left.
4. **Budget alarm.** The last resort, for anything the other layers miss.

The reaper never trusts a list alone: during a probe on 18 November 2026, the
Resource Groups Tagging API still returned five lab resources minutes after EC2
reported them deleted. The reaper confirms each resource's state with the
owning service before acting on it or reporting it.

## Consequences

- The safety net is tested, not assumed: in M5 a lab is deployed with a
  20-minute expiry and no destroy, and the time from expiry to zero billable
  resources is measured and published.
- The reaper and scheduler run within their free tiers, so the always-on part
  costs about $0.
- A session longer than 10 hours is not possible without changing the cap.
