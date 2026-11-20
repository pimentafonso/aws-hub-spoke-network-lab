# aws-hub-spoke-network-lab

A hub-and-spoke AWS networking lab in Terraform: Transit Gateway segmentation,
centralised egress, site-to-site VPN with BGP, PrivateLink across overlapping
CIDRs, and an automated connectivity test. Each session deploys it, measures
it and destroys it.

> **Status:** work in progress. M2 (VPCs, Transit Gateway, egress, matrix
> runner) has been deployed, tested with both NAT options and destroyed; the
> M3 VPN is next. The safety net is live. Every number here is measured, with
> how and when.

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

![Target architecture: a hub VPC with NAT and egress-only internet gateway attached to a Transit Gateway with three route tables; spokes A and B reach the hub but not each other; a simulated on-premises VPC connects over a two-tunnel VPN with BGP and reaches spoke A only; a consumer VPC with spoke A's CIDR reaches spoke A's NLB through PrivateLink; the always-on account root holds the plan role, state, TTL reaper and budget](docs/architecture.svg)

Solid boxes are built and have been tested in a real session, dashed amber
boxes come in the next milestone and dashed grey boxes later. Red dashed lines
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
| M2 | VPCs, Transit Gateway segmentation, centralised egress, matrix runner | 0.3 h | ~$0.65 | done, billed cost pending |
| M3 | Site-to-site VPN with BGP, tunnel failover measurement | ~3 h | ~$1.10 | planned |
| M4 | PrivateLink across overlapping CIDRs, flow logs with Athena, Reachability Analyzer | ~3 h | ~$2.10 | planned |
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
  reason: rt-spokes blackholes the spoke ranges, so spokes cannot reach each other.
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
- The billed cost of the session replaces the estimate once Cost Explorer has
  it.

## Cost control

The lab costs about $0.42 an hour while deployed, so it never stays up. Four
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
scripts/          lab.sh, account and state setup, matrix tool and probe, lint
docs/results/     connectivity matrix reports from real lab sessions
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

## Licence

[MIT](LICENSE)
