# 7. PrivateLink across an overlapping range, and flow logs as evidence

- Status: accepted
- Date: 2026-11-23

## Context

M4 adds two things.

**A consumer that overlaps a spoke.** A consumer VPC uses 10.1.0.0/16, the
same range as spoke A, as happens after a merger or with a partner who chose
the same range. It needs spoke A's web service. No route can join two VPCs
with the same range: in the consumer, 10.1.0.0/16 is local and wins over
anything else.

**Evidence for the matrix.** The matrix records whether each connection
worked, and each deny row names the layer that should stop it. A timeout
looks the same whatever stopped it, so the matrix alone cannot show that the
right layer did.

## Decision

**PrivateLink, not NAT.** Spoke A publishes its web port through an internal
Network Load Balancer and a VPC endpoint service. The consumer reaches it
through an interface endpoint, an address in the consumer's own range. Spoke A
sees the connection arrive from the NLB's addresses. The consumer gets that one
service and nothing else in spoke A, with no route between them.

The alternative is NAT on both sides, translating each VPC into a range the
other does not use, over a Transit Gateway or peering. It needs a NAT gateway
or instance on each side, plus routes and translated ranges to maintain, and it
opens the whole range rather than one port. PrivateLink is managed, scoped to
one service, and works however many consumers reuse the range.

- The consumer is isolated: no Transit Gateway attachment, no internet or NAT
  gateway, and no IPv6 egress. Its test host reaches SSM through three more
  interface endpoints (`ssm`, `ssmmessages`, `ec2messages`).
- Every endpoint sits in one zone, $0.011 an hour each. The NLB spans both
  zones with cross-zone load balancing, because the endpoint's zone has no
  target.
- The endpoint service allows only this account and needs no acceptance.
- Spoke A's host (10.1.0.10), the consumer's host (10.1.0.20) and the NLB's
  nodes (10.1.0.30, 10.1.1.30) have fixed addresses. The consumer's direct
  probe goes to 10.1.0.10, which must not exist in the consumer VPC, so the
  consumer's endpoints use its second subnet. Fixed addresses also keep the
  flow logs from confusing an NLB node with the consumer.

**Flow logs to S3 in Parquet, queried with Athena,** for every VPC, all
traffic, with 1-minute aggregation and hourly Hive-style partitions. A Glue
table with partition projection needs no crawler. Flow logs delivered to
CloudWatch Logs cost about twice as much per GB as delivery to S3, and SQL
over every VPC's records in one table is easier than Logs Insights.

`scripts/evidence.py flowlogs` reruns the matrix and then checks each row:

| Row | The flow logs must show |
|---|---|
| allow | an ACCEPT at the destination's network interface |
| deny, security group | a REJECT there, and no ACCEPT |
| deny, routing | no record in the destination VPC, and a later record from that VPC, so its logs for the window have arrived |
| to the internet | nothing to check |

The PrivateLink row is checked at the endpoint's network interface.

**Reachability Analyzer** repeats three rows, at $0.10 an analysis:

- spoke A to spoke B (routing);
- spoke A to the hub's admin port (security group);
- the hub to spoke A's admin port (allow).

Each should blame the layer the matrix names. The paths are free, so they are
defined in Terraform; the analyses run only on request
(`bash scripts/lab.sh reachability`).

## Consequences

- The consumer is left out of the security-group rules built from VPC ranges:
  its range would duplicate spoke A's rules. Its host has egress rules only.
- The bucket, Glue table and Athena workgroup live and die with the session.
  The evidence survives in `docs/results`, not in S3.
- Flow log delivery takes minutes, so the evidence step adds a wait to every
  session. The report measures it.
- A routing deny is proved by absence. Absence only counts once the
  destination VPC's logs are shown to cover the window, otherwise a slow
  delivery would look like a pass.
