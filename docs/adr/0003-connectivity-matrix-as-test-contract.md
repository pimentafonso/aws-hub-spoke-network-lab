# 3. A connectivity matrix as the test contract

- Status: accepted (the runner that probes the instances is built in M2)
- Date: 2026-11-18

## Context

Network designs are usually shown with a diagram and a few screenshots of a
successful ping. That proves one path works, not that the segmentation holds:
a missing blackhole route or an over-broad security group is invisible until
something reaches a host it should not.

## Decision

`tests/matrix.yaml` lists every connection the design cares about, one row per
source, destination, TCP port and IP family, with the expected result and a
one-line reason. Deny rows also name the layer that should stop the traffic:

- `security-group`: the packet reaches the destination VPC and is rejected, so
  VPC Flow Logs show a `REJECT` record, which an Athena query can find (M4).
- `routing`: the packet has no route (a TGW blackhole, a route that is not
  propagated, an isolated VPC or an overlapping CIDR) and never reaches the
  destination VPC, so its flow logs have no record of it.

Rows belong to the milestone stage that deploys them (m2, m3, m4). Stages are
cumulative. `scripts/matrix.py validate` checks the file in CI: unique IDs,
known hosts, valid ports, a layer on every deny, an address for the requested IP
family, and no host used before its stage. From M2, `scripts/matrix.py run`
probes every row from the test instances through SSM Run Command after each
deploy, prints a table and exits non-zero on any difference.

## Consequences

- Allowed and denied paths are tested with equal weight; about half the rows
  are denies.
- Changing the design means changing the matrix in the same pull request, so
  the expected behaviour is reviewed with the code.
- SSM Run Command has no charge, so a matrix run costs nothing beyond the lab's
  hourly cost.
