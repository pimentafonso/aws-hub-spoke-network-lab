# Write-up: a hub-and-spoke network, measured

This lab builds the network a mid-sized AWS estate tends to end up with
(shared services in a hub, isolated workload spokes, a VPN to an on-premises
site, and a service published across an overlapping address range), then
tests it the way an operator would: does every connection that should work
work, does every connection that should fail fail at the right layer, how
long does a VPN failover really take, and what did it cost. It is deployed
for one session at a time and destroyed afterwards. Every number below comes
from a report in [`results/`](results/), with the time it was taken.

## What was built

| Milestone | Date | What it added |
|---|---|---|
| M1 | 18 Nov 2026 | The always-on account root: Terraform state, a plan-only GitHub role, a permissions boundary, the TTL reaper and a budget |
| M2 | 20 Nov | Four dual-stack VPCs, a Transit Gateway with a route table per attachment, centralised IPv4 egress, the connectivity matrix runner |
| M3 | 20 Nov | A two-tunnel site-to-site VPN with BGP to a simulated on-prem network (strongSwan and FRR on a t4g.micro), and a failover test |
| M4 | 23 Nov | A consumer VPC that overlaps spoke A and reaches it through PrivateLink; flow logs in S3 queried with Athena; Reachability Analyzer |
| M5 | 24 Nov | The whole lab end to end, left for the reaper to clear |

Segmentation is done with Transit Gateway route tables, not security groups
alone: each spoke's table blackholes the private ranges and propagates only
the hub's more specific route, so spoke A cannot reach spoke B even if a
security group would allow it. The VPN is active/standby, steered with BGP
attributes in both directions
([ADR 6](adr/0006-site-to-site-vpn-with-bgp.md)), so a failover is one clear
move that can be timed.

## How it is tested

[`tests/matrix.yaml`](../tests/matrix.yaml) is the contract: 19 connections,
each with the expected result and, for a deny, the layer that should stop it
([ADR 3](adr/0003-connectivity-matrix-as-test-contract.md)). A runner sends
each test host one SSM Run Command, the host tries its TCP connections, and
the report compares what happened with what was expected. A deny passes only
as a deny, but a timeout looks the same whatever caused it, so from M4 the
flow logs check the layer as well: a security group deny must leave a REJECT
at the destination, a routing deny must leave no record in the destination
VPC at all ([ADR 7](adr/0007-privatelink-and-flow-log-evidence.md)).

## What was measured

**Connectivity.** Every session's matrix passed in full once the lab was
ready: 11 of 11 in M2 with each NAT option, 16 of 16 in M3, 19 of 19 in M4
and M5. Allowed connections completed in 1 to 10 ms. In M4 and M5 the flow
logs confirmed the layer of all 15 rows that stay inside the lab, with the
evidence in Athena 6.7 and 4.5 minutes after the probes. Reachability
Analyzer agreed on three paths, with one wrinkle: it reports a route that
matches a blackhole with the same code as no route at all.

**PrivateLink.** The consumer VPC uses spoke A's range, 10.1.0.0/16. Through
its interface endpoint, an address in its own range, it reached spoke A's
web service in 2 ms; a probe straight to spoke A's host address failed at
once with "No route to host", because inside the consumer that address is
local. Routing cannot connect overlapping ranges; PrivateLink does not need
to.

**VPN failover.** Six runs over two sessions, cutting tunnel 1 by dropping
its traffic on the router so neither side says goodbye:

| Session | Outage (median, range) | BGP detection (range) | Failback |
|---|---|---|---|
| M3, 20 Nov | 41.3 s (39.8 to 43.3) | 21.5 to 26.5 s | no loss; BGP back in 1 to 2 s, once 50 s |
| M5, 24 Nov | 39.3 s (37.8 to 45.8) | 21.6 to 29.3 s | no loss; BGP back in 3 to 4 s |

Detection is AWS's 30 s BGP hold timer less the time since the last
keepalive; AWS does not offer BFD on VPN tunnels, so that is the floor. The
outage runs about 17 s past detection, which I read as the Transit Gateway's
own hold timer and route propagation for the return path. AWS's tunnel
status lagged the data path by 40 to 100 s, so it is no use for timing a
failover.

## What went wrong

Most of what this lab taught came from things that failed.

- **NAT switch, M2.** Moving a running lab from the NAT Gateway to a NAT
  instance failed once on a race between releasing the Elastic IP and
  deleting the gateway's interface. It also cut SSM, since the test hosts
  reached it through that egress: changing the egress changes the
  management plane too.
- **A second apply would have broken the router, M3 to M4.** A re-plan in M4
  showed that any further apply, such as extending a session, would have
  turned source/destination checking back on for the VPN router, which then
  stops forwarding. Fixed before it was ever hit.
- **Destroy blocked by evidence, M4.** AWS will not delete a Reachability
  Analyzer path that still has analyses. The evidence script now deletes its
  analyses when it has written the report.
- **Credentials that expired mid-apply, M5.** The scripts exported
  short-lived credentials from an `aws login` profile; they expired after
  about 15 minutes, ten minutes into the apply. Terraform left `errored.tfstate` and a lock. Recovery was a
  force-unlock, a state push, waiting for the VPN AWS had finished creating,
  and an untaint. Now nothing exports credentials: Terraform and boto3 read
  the profile and refresh it themselves.
- **BGP up, no routes, M5.** Both sessions came up while Terraform was still
  associating the VPN attachment with its route table. AWS then advertised
  nothing on either session, though it reported both tunnels UP, and the
  three VPN rows failed twice. A soft refresh did nothing; a hard reset
  brought both routes in 12 s. The readiness check now waits for routes, not
  just an Established session, and resets once after 120 s of silence.
- **The safety net missed two resources, M5.** See below.

## The safety net

A lab that costs about $0.44 an hour must not be forgotten. Four layers
guard it ([ADR 2](adr/0002-session-only-lab-and-safety-net.md)): a scripted
destroy that fails if anything billable is left, an `ExpiresAt` tag on every
resource, a reaper Lambda that deletes expired resources every 15 minutes,
and a budget.

M1 tested the reaper on a single Elastic IP: deleted 9 min 45 s after
expiry. M5 tested it on the whole lab. It deleted 27 of 29 billable
resources within 8.6 minutes of expiry, and then nothing more:

- the NLB, because AWS will not delete one that still fronts a PrivateLink
  endpoint service, and the reaper did not delete endpoint services;
- the VPN router's Elastic IP, because it sits on a network interface that
  outlives the instance, and the reaper skipped any address still
  associated.

After 45 minutes I destroyed the rest by hand, as planned. The fix deletes an
expired NLB's endpoint services first, and releases an expired address whose
interface is attached to nothing. It needed new actions in the reaper's
policy and in the permissions boundary as well, which only the plan
revealed. A targeted test recreated both cases against real AWS, and one
reaper run deleted both in 7 s. What it has not yet done is clear a full lab
on its own; that is the next session's first job.

The lesson is about tests more than reapers: the unit tests ran against
moto, which never refused to delete a load balancer. Only the real account
did.

## What it cost

| Session | Lab up | Estimate | Billed (before credits) |
|---|---|---|---|
| M1, 18 Nov | under 1 h, one Elastic IP | ~$0.01 | |
| M2 and M3, 20 Nov | 0.3 h and 0.5 h | ~$0.83 | $0.279 |
| M4, 23 Nov | 0.5 h | ~$0.50 | not in yet |
| M5, 24 Nov | 1.1 h, two resources 1.8 h | ~$0.70 | not in yet |

All of it was covered by AWS credits. M2 and M3 came in well under the
estimate, partly because two things in that day's bill do not add up yet:
one Transit Gateway attachment-hour where about six were expected, and some
public IPv4 time without the lab's tag. Both are checked again when
November's bill is final. Cost Explorer's finest free granularity is a day,
so each session runs on its own UTC day to be billed on its own.

## Notes

- The M5 session ran with the M4 stage, so its reports carry the `m4-`
  prefix. The two failed matrix runs are kept with the passing one.
- The strongSwan log, meant to explain M3's one slow failback, came back
  empty in M5: the script filtered on a prefix charon-systemd does not
  write. The filter now uses the journal field and is unconfirmed until the
  next failover run.
