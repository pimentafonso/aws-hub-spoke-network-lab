# 2. Session-only lab with a layered safety net

- Status: accepted (built in M1; the end-to-end test of layer 3 runs in M5)
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

1. **Scripted destroy.** `scripts/lab.sh destroy` runs `terraform destroy`,
   then lists every billable resource still tagged with the project and fails
   if it finds any.
2. **Expiry tag.** `scripts/lab.sh deploy [hours]` sets `ExpiresAt` to the
   deploy time plus the session length (4 hours by default, 10 at most) on
   every resource through the provider's default tags. `scripts/lab.sh extend`
   moves it. Terraform rejects an expiry that is missing, in the past or more
   than 10 hours away.
3. **TTL reaper.** A Lambda function in `infra/account`, run by EventBridge
   Scheduler every 15 minutes. A resource carrying the project tag has expired
   when its `ExpiresAt` has passed, when the tag is missing or invalid, or when
   it was created more than 10 hours ago, whatever the tag says.
4. **Budget alarm.** A monthly budget on the project tag, before credits
   (otherwise the Free plan's credits hide every charge), emails at 50% and
   100% of $10 actual and at 100% forecast. It is the last resort.

**What the reaper deletes.** Only resources that bill by the hour or by the GB,
most expensive first and dependencies first: VPN connections, interface
endpoints, load balancers, NAT gateways, TGW VPC attachments, flow logs,
instances, then Elastic IPs and detached volumes once nothing holds them. Free
resources (VPCs, subnets, security groups, route tables, the transit gateway
itself, customer gateways, S3 buckets) are left for `lab.sh destroy`. The
reaper's job is to stop the bill. Keeping it to that list keeps its
permissions small, and Terraform state stays easy to reconcile.

**How it works.** Each pass reads every billable kind from its own service,
filtered on the project tag and re-checked in code, and deletes whatever is
expired and in a state that allows it. Resources on their way out, or blocked
(an Elastic IP still held by a NAT gateway being deleted), are picked up by
the next pass, 20 seconds later, until nothing is left or the 10-minute
timeout is close. It emails a summary whenever it deleted something, found
something it could not delete, or hit an error. A run that crashes is reported
by Lambda's on-failure destination to the same topic.

**Least privilege.** The reaper may describe the kinds above and delete them
only when `aws:ResourceTag/Project` is the lab's tag. The same limit is in the
project's permissions boundary, so widening the role's own policy alone cannot
widen what it can delete.

**Why not trust a list.** During a probe on 18 November 2026, the Resource
Groups Tagging API still returned five lab resources minutes after EC2 reported
them deleted. The reaper therefore reads each service directly and acts on the
state it reports.

## Consequences

- The safety net is tested, not assumed: in M5 a lab is deployed with a short
  expiry and no destroy, and the time from expiry to zero billable resources
  is measured and published.
- The always-on part costs about $0: the reaper, scheduler, alert topic and a
  budget without actions are within free tiers or free.
- Sessions are 10 hours at most. VPN connections report no creation time, so
  for them the cap rests on the `ExpiresAt` tag; the TGW attachments created
  with them do carry one.
- Free resources the reaper leaves behind cost nothing, but they block the
  next deploy until `lab.sh destroy` reconciles state.
