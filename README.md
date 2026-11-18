# aws-hub-spoke-network-lab

A hub-and-spoke AWS networking lab in Terraform: Transit Gateway segmentation,
centralised egress, site-to-site VPN with BGP, PrivateLink across overlapping
CIDRs, and an automated connectivity test. Each session deploys it, measures
it and destroys it.

> **Status:** work in progress (M0, repository bootstrap). Nothing is deployed
> yet, so this README has no results. Every number added later will be measured,
> with how and when.

## What it will show

- **Segmentation with Transit Gateway route tables.** Two spokes reach shared
  services in the hub but not each other. The simulated on-premises network
  reaches the hub and one spoke only.
- **Centralised egress.** Spokes reach the internet through the hub. IPv4 goes
  through NAT; IPv6 goes through an egress-only internet gateway. A NAT Gateway
  and a NAT instance are compared on cost.
- **Site-to-site VPN with BGP.** Both tunnels terminate on strongSwan and FRR,
  running on a t4g.micro in a VPC that plays the on-premises network. One tunnel
  is cut and the BGP failover time is measured.
- **PrivateLink where routing cannot work.** A consumer VPC whose CIDR overlaps
  a spoke reaches that spoke's service through an interface endpoint and an NLB.
- **Evidence.** VPC Flow Logs in S3 queried with Athena, Reachability Analyzer
  for selected paths, and the real cost per session from the AWS bill.

## Planned architecture

```
                         Internet
                IGW (IPv4)  |  egress-only IGW (IPv6)
              +-------------+--------------+
              | Hub 10.0.0.0/16 + IPv6     |  NAT Gateway, NAT instance,
              | shared services            |  flow logs to S3
              +-------------+--------------+
                            |
                    +-------+-------+   route tables:
                    |Transit Gateway|     spokes: hub and on-prem, never each other
                    +--+----+----+--+     hub:    everything
                       |    |    |        vpn:    hub and spoke A only
             Spoke A --+    |    +-- Spoke B
         10.1.0.0/16        |        10.2.0.0/16
         NLB + service      |
              ^        VPN, 2 tunnels, BGP
              |             |
              |        On-prem 172.16.0.0/16 (strongSwan + FRR)
              |
         PrivateLink endpoint in a consumer VPC, also 10.1.0.0/16
```

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
| M0 | Repository bootstrap: layout, connectivity matrix format, lint and tests in CI, ADRs | 0 h | $0 | in progress |
| M1 | Account root: state, GitHub plan role, boundary, TTL reaper, budget | ~0.5 h | ~$0.01 | planned |
| M2 | VPCs, Transit Gateway segmentation, centralised egress, matrix runner | ~2.5 h | ~$0.65 | planned |
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

## Cost control

The lab costs about $0.42 an hour while deployed, so it never stays up. Every
resource carries an `ExpiresAt` tag, and Terraform refuses to deploy without
one. From M1, a reaper function deletes anything past its expiry every 15
minutes, and a budget alarm backs it up. See
[ADR 2](docs/adr/0002-session-only-lab-and-safety-net.md).

## Repository layout

```
infra/lab/        Terraform root for the session-only lab
infra/account/    always-on account resources (from M1)
tests/matrix.yaml connectivity matrix, the test contract
tests/unit/       unit tests for the scripts
scripts/          lint, matrix tool and (from M1) deploy and destroy
docs/adr/         architecture decision records
```

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

## Licence

[MIT](LICENSE)
