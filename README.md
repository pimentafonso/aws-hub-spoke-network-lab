# aws-hub-spoke-network-lab

A hub-and-spoke AWS networking lab in Terraform: Transit Gateway segmentation,
centralised egress, site-to-site VPN with BGP, PrivateLink across overlapping
CIDRs, and an automated connectivity test. Each session deploys it, measures
it and destroys it.

> **Status:** work in progress. M4 (PrivateLink across an overlapping range,
> flow logs with Athena, Reachability Analyzer) has been deployed, tested and
> destroyed; M5, a full session with the bill and the write-up, is next. The
> safety net is live. Every number here is measured, with how and when.

## What it will show

- **Segmentation with Transit Gateway route tables.** Two spokes reach shared
  services in the hub but not each other. The simulated on-premises network
  reaches the hub and one spoke only.
- **Centralised egress.** Spokes reach the IPv4 internet through one NAT in
  the hub, either a NAT Gateway or a NAT instance, compared on cost. IPv6
  leaves each VPC through its own egress-only internet gateway, because IPv6
  egress cannot be centralised without self-managed NAT66
  ([ADR 5](docs/adr/0005-centralised-ipv4-egress-per-vpc-ipv6-egress.md)).
- **Site-to-site VPN with BGP.** Both tunnels terminate on strongSwan and FRR,
  running on a t4g.micro in a VPC that plays the on-premises network. One tunnel
  is cut and the BGP failover time is measured.
- **PrivateLink where routing cannot work.** A consumer VPC whose CIDR overlaps
  a spoke reaches that spoke's service through an interface endpoint and an NLB.
- **Evidence.** VPC Flow Logs in S3 queried with Athena, Reachability Analyzer
  for selected paths, and the real cost per session from the AWS bill.

## Architecture

![Target architecture: a hub VPC with NAT and egress-only internet gateway attached to a Transit Gateway with four route tables; spokes A and B reach the hub but not each other; a simulated on-premises VPC connects over a two-tunnel VPN with BGP and reaches spoke A only; a consumer VPC with spoke A's CIDR reaches spoke A's NLB through PrivateLink; the always-on account root holds the plan role, state, TTL reaper and budget](docs/architecture.svg)

Every box is built and has been tested in a real session. Red dashed lines
are connections the matrix expects to fail. Between sessions only the
always-on account root exists; the lab is deployed for one session at a time.

| Area | Services |
|---|---|
| Network | VPC (dual-stack), Transit Gateway, NAT Gateway, NAT instance, egress-only internet gateway |
| Hybrid | AWS Site-to-Site VPN, BGP, strongSwan and FRR on EC2 |
| Private access | PrivateLink endpoint service, Network Load Balancer |
| Evidence | VPC Flow Logs, S3, Athena, Reachability Analyzer, SSM Run Command |
| Safety | TTL reaper (Lambda, EventBridge Scheduler), AWS Budgets |
| Delivery | Terraform, GitHub Actions with OIDC (plan only) |

## Milestones

Each milestone is one session. Costs are estimates from the AWS Price List
(eu-west-1, November 2026) and will be replaced by billed figures.

| | Milestone | Deployed | Estimate | Status |
|---|---|---|---|---|
| M0 | Repository bootstrap: layout, connectivity matrix format, lint and tests in CI, ADRs | 0 h | $0 | done |
| M1 | Account root: state, GitHub plan role, boundary, TTL reaper, budget | ~0.5 h | ~$0.01 | done |
| M2 | VPCs, Transit Gateway segmentation, centralised egress, matrix runner | 0.3 h | ~$0.65 | done; billed with M3, see below |
| M3 | Site-to-site VPN with BGP, tunnel failover measurement | 0.5 h | ~$0.18 | done; $0.28 billed for M2 and M3 together |
| M4 | PrivateLink across overlapping CIDRs, flow logs with Athena, Reachability Analyzer | 0.5 h | ~$0.50 | done; bill not in yet |
| M5 | End-to-end session, safety net test, cost from the bill, write-up | ~2.5 h | ~$1.65 | planned |
| M6 | Optional: hybrid DNS with Route 53 Resolver endpoints | ~1.5 h | ~$1.30 | planned |

## The connectivity matrix

[`tests/matrix.yaml`](tests/matrix.yaml) lists every connection the design
cares about, with the expected result: 19 rows, 11 allows and 8 denies. Each
deny names the layer that should stop it, a security group or routing (see
[ADR 3](docs/adr/0003-connectivity-matrix-as-test-contract.md)). For example:

```yaml
- id: spoke-a-to-spoke-b
  from: spoke-a
  to: spoke-b
  port: 80
  family: ipv4
  expect: deny
  layer: routing
  stage: m2
  reason: rt-spoke-a blackholes 10.0.0.0/8; only the hub's more specific /16 is propagated.
```

CI validates the file. From M2, every deploy runs it against the live lab and
fails on any difference.

**Measured, 20 November 2026 (M2).** The 11 M2 rows passed on the first run
with each NAT option, from the reports in [`docs/results/`](docs/results/):

| Egress | Report | Result |
|---|---|---|
| NAT Gateway | [m2-nat-gateway-2026-11-20T0928Z](docs/results/m2-nat-gateway-2026-11-20T0928Z.md) | 11 of 11 |
| NAT instance (t4g.micro) | [m2-nat-instance-2026-11-20T0938Z](docs/results/m2-nat-instance-2026-11-20T0938Z.md) | 11 of 11 |

Spoke to spoke timed out on IPv4 and IPv6, the hub's admin port timed out from
a spoke, and every allowed connection completed in 1 to 2 ms. From the same
session:

- `terraform apply` of the 153 resources took 3 min 36 s, and `destroy` 4 min
  28 s, after which nothing billable was left: no VPC, Transit Gateway,
  attachment, NAT, Elastic IP, instance, volume or network interface.
- Switching a running lab from the NAT Gateway to the NAT instance failed once:
  releasing the Elastic IP raced the deletion of the NAT Gateway's network
  interface (`InvalidNetworkInterfaceID.NotFound`). A second apply finished it.
- The test hosts reach SSM through the centralised egress, as there are no VPC
  endpoints. Deleting the NAT Gateway broke their open SSM connections, and the
  first matrix run after the switch timed out after 5 minutes waiting for the
  hub host, which still showed as online. Its result arrived later, and a run
  a few minutes after that passed. Changing the egress cuts the lab's
  management plane too.
- Billed cost: see below. M2 and M3 ran on the same day, and Cost Explorer's
  finest free granularity is a day, so they are billed together.

**Measured, 20 November 2026 (M3).** With the VPN up, the 16 M3 rows passed
on the first run:
[m3-nat-gateway-2026-11-20T1452Z](docs/results/m3-nat-gateway-2026-11-20T1452Z.md).
On-prem reached the hub in 6 ms and spoke A in 3 ms, and spoke A reached
on-prem in 10 ms, all through the tunnel. Spoke B to on-prem and on-prem to
spoke B both timed out: spoke B's route table blackholes 172.16.0.0/12 and
`rt-vpn` has no route to spoke B
([ADR 6](docs/adr/0006-site-to-site-vpn-with-bgp.md)). The 11 M2 rows still
passed. Destroying the 261 resources and checking for leftovers took 5 min
30 s, and a direct check found no VPC, Transit Gateway, attachment, VPN
connection, customer gateway, NAT, Elastic IP, instance, volume, network
interface or VPN key parameter left.

**Billed, 20 November 2026 (M2 and M3).** Cost Explorer, queried on 23
November for the `Project` tag, shows **$0.279** for the day before credits
and $0.00 after them; AWS still marks it estimated.

| Usage | Quantity | Cost |
|---|---|---|
| NAT Gateway hours | 3 h | $0.144 |
| Transit Gateway attachment hours | 1 h | $0.050 |
| VPN connection hours (M3 only) | 1 h | $0.050 |
| t4g.micro instance hours | 2.76 h | $0.025 |
| Public IPv4 addresses | 1.23 h | $0.006 |
| EBS, S3 requests and the rest | | $0.003 |

Two things do not add up yet. Three Transit Gateway attachments ran in each
session, so I expected about 6 attachment-hours, not 1; the untagged view
shows the same 1 hour, so it is not a tagging gap. And about 0.6 h of public
IPv4 address time was billed without the lab's tag (about $0.003). Both are
checked again when the month's bill is final.

**Measured, 23 November 2026 (M4).** All 19 rows passed on the first run:
[m4-nat-gateway-2026-11-23T2134Z](docs/results/m4-nat-gateway-2026-11-23T2134Z.md).
The three new rows are the consumer VPC, which uses spoke A's range:

- Through the PrivateLink endpoint (10.1.1.132, an address in the consumer's
  own range) it reached spoke A's web port in 2 ms.
- Its probe straight to spoke A's host, 10.1.0.10, failed at once with "No
  route to host": inside the consumer that address is local, and nothing in
  the consumer VPC has it.
- Its probe to the internet timed out; it has no route out.

## Evidence: flow logs and Reachability Analyzer

A timeout looks the same whatever stopped it, so M4 checks that the layer each
deny names is the one that acted
([ADR 7](docs/adr/0007-privatelink-and-flow-log-evidence.md)).

**Flow logs, 23 November 2026.** `bash scripts/lab.sh evidence` ran the matrix,
then polled Athena every 60 s until every row had its evidence:
[m4-flowlogs-2026-11-23T2141Z](docs/results/m4-flowlogs-2026-11-23T2141Z.md).

| Row | Rows | Flow logs showed |
|---|---|---|
| allow | 8 | 1 ACCEPT at the destination's interface each, and no REJECT |
| deny, security group | 1 | spoke A to the hub's admin port: 1 REJECT at the hub host, no ACCEPT |
| deny, routing | 6 | no record in the destination VPC, whose logs had arrived past the window |
| to the internet | 4 | not checked: the destination is outside the lab |

15 of 15 rows were confirmed. The PrivateLink row was checked at the
endpoint's interface in the consumer VPC. The last row's evidence was in
Athena 6.7 minutes after the probes ended (to within the 60 s poll), so
delivery, not the query, sets the pace.

**Reachability Analyzer, 23 November 2026.** Three analyses, $0.10 each:
[m4-reachability-2026-11-23T2142Z](docs/results/m4-reachability-2026-11-23T2142Z.md).

| Path | Matrix says | Analyzer said |
|---|---|---|
| hub to spoke A's admin port | allow | path found |
| spoke A to the hub's admin port | deny, security group | `ENI_SG_RULES_MISMATCH`, one security group |
| spoke A to spoke B | deny, routing | `TGW_RTB_NO_ROUTE_TO_TGW_ATTACHMENT`, `rt-spoke-a` |

For spoke A to spoke B the analyzer reports no route to an attachment,
although `rt-spoke-a` does match 10.2.0.0/16: its 10.0.0.0/8 route is a
blackhole. I listed the table's routes during the session to confirm it; the
analyzer reports a blackhole match with the same code as a missing route.

**Session, 23 November 2026.**

- `terraform apply` created 305 of the 306 resources in 6 min 26 s and failed
  on the last: EC2 rejects an apostrophe in a security group rule's
  description. The re-plan also showed that any second apply, such as
  `lab.sh extend`, would have turned source/destination checking back on for
  the on-prem router, stopping it forwarding, and removed its IPv6 address,
  which its subnet assigns anyway. That bug dates from M3. Both are fixed; the
  re-plan then added only the missing rule.
- The first `destroy` stopped after 6 minutes on the three Reachability
  Analyzer paths: AWS will not delete a path that still has analyses. The
  NAT, VPN, Transit Gateway, NLB, endpoints and flow log bucket were already
  gone; the five test hosts were left. I deleted the analyses and a second
  destroy removed the last 99 resources in 1 min 5 s. `evidence.py` now
  deletes its analyses once the report is written.
- A direct check then found no VPC, Transit Gateway, attachment, VPN
  connection, customer gateway, NAT, Elastic IP, instance, volume, network
  interface, VPC endpoint, endpoint service, load balancer, flow log,
  Reachability Analyzer path or analysis, flow log bucket, Glue database,
  Athena workgroup or VPN key parameter.
- The lab was up from 21:22 to 21:50 UTC. The estimate is about $0.50,
  $0.30 of it the analyses; the billed figure follows when Cost Explorer has
  it.

## VPN failover

`bash scripts/lab.sh failover` cuts tunnel 1 on the on-prem router by dropping
all traffic to and from its AWS endpoint, so neither side says goodbye, as with
a failed link. Meanwhile the on-prem host opens a TCP connection to spoke A
every 0.2 s with a 0.5 s timeout. After 90 s the cut is lifted and strongSwan
re-initiates the tunnel. BGP uses AWS's timers: keepalive 10 s, hold 30 s.

**Measured, 20 November 2026,** three runs
([m3-failover-2026-11-20T1504Z](docs/results/m3-failover-2026-11-20T1504Z.md)):

| Run | BGP down after | Outage | Probes lost | Path while cut | BGP up after restore |
|---|---|---|---|---|---|
| 1 | 21.5 s | 39.8 s | 79 | tunnel 2 | 2.2 s |
| 2 | 23.5 s | 41.3 s | 82 | tunnel 2 | 50.0 s |
| 3 | 26.5 s | 43.3 s | 86 | tunnel 2 | 1.1 s |

- **Median outage 41.3 s** (39.8 to 43.3 s). The on-prem router dropped the
  tunnel 1 session 21.5 to 26.5 s after the cut, which is the 30 s hold timer
  less the time since the last keepalive. AWS does not support BFD on VPN
  tunnels, so the hold timer is the floor.
- The outage lasted 17 to 18 s longer than that. A TCP handshake needs both
  directions, and the return path only moves when the Transit Gateway drops
  tunnel 1's routes. My reading is that this is AWS's own hold timer plus
  route propagation; the test does not time the AWS side directly.
- While cut, tunnel 1's interface received nothing and tunnel 2's about 800
  packets in each run, so every flow moved, as active/standby intends.
- Failback lost no probes: tunnel 2 kept working until tunnel 1 took over.
  BGP came back 1.1 and 2.2 s after the restore in runs 1 and 3. In run 2 it
  took 50 s, and tunnel 1 then received only 11 packets in the remaining
  40 s, so AWS kept return traffic on tunnel 2. The test records only BGP
  neighbour changes, so the cause is not known; M5 repeats the test with the
  strongSwan log captured.
- AWS's tunnel status (`VgwTelemetry`, polled every 5 s) showed tunnel 1 down
  63 to 89 s after the cut and, in runs 1 and 3, up again about 74 and 83 s
  after the restore. It lags the data path by a minute or more, so it is no
  use for timing a failover.

## Cost control

The full lab costs about $0.44 an hour while deployed (estimate, since M4), so it never stays up. Four
independent layers make sure of it
([ADR 2](docs/adr/0002-session-only-lab-and-safety-net.md)):

1. **Scripted destroy.** `scripts/lab.sh destroy` runs `terraform destroy`,
   then fails if any billable resource with the lab's tag is left.
2. **Expiry tag.** Every resource carries `ExpiresAt`, 4 hours after deploy by
   default. Terraform rejects an expiry that is missing, past, or more than 10
   hours away.
3. **TTL reaper.** A Lambda function runs every 15 minutes and deletes the
   lab's billable resources once `ExpiresAt` has passed, or 10 hours after
   they were created, whatever the tag says. It can only delete resources
   carrying the lab's tag, and it emails what it did.
4. **Budget.** A $10 monthly budget on the lab's tag, before credits, emails at
   50%, 100% and 100% forecast. Billing data is 8 to 24 hours late, so this is
   the last resort.

**Measured, 18 November 2026.** I allocated one Elastic IP tagged with the
lab's project and an `ExpiresAt` 5 minutes ahead (18:50:52 UTC), then left it.
The next scheduled reaper run started at 19:00:33 and released it at
19:00:37.9, from the reaper's CloudWatch log: 9 min 45 s from expiry to
deletion, within the 15-minute schedule. A run with nothing to delete takes
4 to 5.5 s. The worst case for a lab is the time to the next run plus the
deletion passes; M5 measures that on a full lab.

```
$ bash scripts/lab.sh status
0 billable lab resource(s) found.
```

## Repository layout

```
infra/lab/        Terraform root for the session-only lab (modules/vpc: one dual-stack VPC)
infra/account/    always-on account resources: plan role, boundary, reaper, budget
src/reaper/       the TTL reaper Lambda function
tests/matrix.yaml connectivity matrix, the test contract
tests/unit/       unit tests (the reaper against moto, the matrix runner against a fake SSM)
scripts/          lab.sh, account and state setup, matrix, failover and evidence tools, probes, lint
docs/results/     matrix, failover, flow log and Reachability Analyzer reports from real sessions
docs/adr/         architecture decision records
docs/architecture.svg  the diagram above, updated after every milestone
```

## Setting up an account

Once per account, with an administrator profile. The account must already have
the GitHub OIDC provider (`token.actions.githubusercontent.com`); this project
reads it and never creates it.

```bash
export AWS_PROFILE=<admin profile>
bash scripts/bootstrap-state.sh                         # state bucket, about $0.01 a month
ALERT_EMAIL=<address> bash scripts/account.sh apply     # boundary, plan role, reaper, budget
```

Then confirm the SNS subscription email, and activate `Project` as a cost
allocation tag in Billing (the budget filters on it; it takes up to 24 hours
to apply). For plan on pull requests, add the repository secrets
`AWS_ACCOUNT_ID` and `ALERT_EMAIL`.

## Running a lab session

```bash
bash scripts/lab.sh deploy 4     # expires in 4 hours (1-10), then runs the matrix
bash scripts/lab.sh matrix       # run the matrix again; the report goes to docs/results/
bash scripts/lab.sh failover     # cut VPN tunnel 1 three times and measure the BGP failover
bash scripts/lab.sh evidence     # run the matrix, then check every row against the flow logs
bash scripts/lab.sh reachability # Reachability Analyzer on three paths, $0.10 each
bash scripts/lab.sh status       # billable resources and their expiry
bash scripts/lab.sh extend 2     # expiry moves to 2 hours from now
bash scripts/lab.sh destroy      # destroy, then check nothing billable is left
```

Egress uses the NAT Gateway by default. `TF_VAR_egress=nat-instance bash
scripts/lab.sh deploy` switches the lab to a NAT instance; `extend` keeps
whichever is deployed.

The matrix runner sends each test host one SSM Run Command invocation. The
host runs [`scripts/matrix_probe.py`](scripts/matrix_probe.py), which tries
every TCP connection in its rows with a 5-second timeout and prints the
results as JSON. A completed handshake counts as allow; a timeout, refusal or
unreachable network counts as deny, and the reason is kept in the report.

## Running the checks locally

Needs Terraform 1.10+, Python 3.13, and optionally TFLint and ShellCheck.

```bash
pip install -r requirements-dev.txt
python -m pytest
python scripts/matrix.py validate
pip install --user -r scripts/requirements-lint.txt
bash scripts/lint.sh
```

Locally, a missing linter is skipped with a warning. In CI every linter is
required. The test and lint requirements are installed separately, because
Checkov and pytest pin conflicting versions of `packaging`.

## Decisions

- [ADR 1: Terraform, with a session-only lab root and an always-on account root](docs/adr/0001-terraform-and-module-layout.md)
- [ADR 2: Session-only lab with a layered safety net](docs/adr/0002-session-only-lab-and-safety-net.md)
- [ADR 3: A connectivity matrix as the test contract](docs/adr/0003-connectivity-matrix-as-test-contract.md)
- [ADR 4: Terraform state and GitHub access to AWS](docs/adr/0004-state-and-github-access.md)
- [ADR 5: Centralised IPv4 egress, per-VPC IPv6 egress](docs/adr/0005-centralised-ipv4-egress-per-vpc-ipv6-egress.md)
- [ADR 6: Site-to-site VPN with BGP, active/standby, and a route table per spoke](docs/adr/0006-site-to-site-vpn-with-bgp.md)
- [ADR 7: PrivateLink across an overlapping range, and flow logs as evidence](docs/adr/0007-privatelink-and-flow-log-evidence.md)

## Licence

[MIT](LICENSE)
