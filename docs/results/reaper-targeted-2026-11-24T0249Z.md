# Reaper: targeted test of the two M5 gaps, 24 November 2026

In the M5 session the reaper deleted 27 of 29 billable resources and left two
for the manual destroy:

- the NLB, because the PrivateLink endpoint service in front of it made every
  delete fail with `ResourceInUse`;
- the router's Elastic IP, because it sits on the router's own network
  interface, which outlives the instance, so the address stayed "associated".

The fix deletes an expired NLB's endpoint services before the NLB, and
disassociates and releases an expired address whose interface is attached to
nothing. It was deployed with `scripts/account.sh apply` (boundary, reaper
policy and code, 3 changed in place).

## Test

Created by CLI in eu-west-1, every resource tagged `Project` and an
`ExpiresAt` one hour in the past:

| Resource | Purpose |
|---|---|
| VPC 10.9.0.0/16, subnet, internet gateway | free; the gateway lets an Elastic IP be associated |
| Internal NLB `nwlab-reaper-test` | billable |
| Endpoint service in front of the NLB | free; blocked the NLB delete in M5 |
| Network interface with no instance | free |
| Elastic IP on that interface | billable |

| Time (UTC) | Event |
|---|---|
| 02:47:03 | Setup starts |
| 02:49:32 | NLB active, endpoint service created |
| 02:49:45 | `handler.py --list`: load-balancer `active`, elastic-ip `stranded`; reaper invoked |
| 02:49:52 | Reaper returns: both deleted, 1 pass, no errors, nothing remaining |
| 02:50:22 | Free leftovers deleted by hand (interface, gateway, subnet, VPC) |

Afterwards, the NLB, the endpoint service and the Elastic IP no longer
existed, and `handler.py --check` found 0 billable lab resources.

Reaper output:

```json
{"deleted": [{"kind": "load-balancer", "id": "arn:aws:elasticloadbalancing:eu-west-1:<acct>:loadbalancer/net/nwlab-reaper-test/518dbc654d0a5565", "reason": "expired at 2026-11-24T01:47:04Z"}, {"kind": "elastic-ip", "id": "eipalloc-0e1804578a6a93a3c", "reason": "expired at 2026-11-24T01:47:04Z"}], "remaining": [], "errors": {}, "passes": 1, "dry_run": false}
```

Billable time: about 3 minutes for the NLB and Elastic IP, under $0.01.

Still to prove: a full lab cleared by the scheduled reaper alone, in the next
session.
