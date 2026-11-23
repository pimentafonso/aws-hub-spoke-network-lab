# Reachability Analyzer: m4

Run at 2026-11-23 21:42:12 UTC by `scripts/evidence.py reachability`. One analysis per path, each repeating an IPv4 matrix row.

| Result | Path | Matrix row | Expected | Path found | Layer blamed | Explanation codes |
|---|---|---|---|---|---|---|
| pass | hub-to-spoke-a-admin | hub-to-spoke-a-admin | allow | yes | - | - |
| pass | spoke-a-to-hub-admin | spoke-a-to-hub-admin | deny (security-group) | no | security-group | ENI_SG_RULES_MISMATCH |
| pass | spoke-a-to-spoke-b-web | spoke-a-to-spoke-b | deny (routing) | no | routing | TGW_RTB_NO_ROUTE_TO_TGW_ATTACHMENT |

Components named in the explanations:

- spoke-a-to-hub-admin: SecurityGroups sg-0408d0995f8338d71
- spoke-a-to-spoke-b-web: TransitGatewayRouteTable tgw-rtb-0511cc8f0b5f5cc20

Note, added after the session: `rt-spoke-a` does match 10.2.0.0/16, with its 10.0.0.0/8 blackhole route; the table's routes were listed during the session to confirm it. The analyzer reports a blackhole match with the same code as a missing route.
