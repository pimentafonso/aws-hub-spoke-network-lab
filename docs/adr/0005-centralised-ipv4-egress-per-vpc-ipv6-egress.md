# 5. Centralised IPv4 egress, per-VPC IPv6 egress

- Status: accepted
- Date: 2026-11-18

## Context

The spokes need outbound internet access on both IP families. A NAT in every
VPC costs one NAT per VPC per hour; a hub-and-spoke network usually puts one
NAT in the hub and sends every spoke's default route there through the
Transit Gateway.

The plan was to do the same for IPv6, with an egress-only internet gateway in
the hub. That does not work. IPv6 has no translation: a spoke's packet leaves
with its own global address, from the spoke VPC's /56. The reply is routed by
the internet to the VPC that owns that prefix, the spoke, not back through the
hub, and the spoke has no gateway to receive it. AWS has no managed NAT66; the
[AWS multi-VPC whitepaper](https://docs.aws.amazon.com/whitepapers/latest/building-scalable-secure-multi-vpc-network-infrastructure/centralized-egress-for-ipv6.html)
describes centralised IPv6 egress only with self-managed NAT or proxy
instances, and treats an egress-only gateway in each VPC as the usual design.

## Decision

- **IPv4 egress is centralised** in the hub. Each spoke's private route table
  sends `0.0.0.0/0` to the Transit Gateway; `rt-spokes` sends it to the hub
  attachment; the hub's TGW subnets send it to the NAT; the NAT's public
  subnet routes `10.0.0.0/8` back to the Transit Gateway.
- **The default route never carries private traffic.** `rt-spokes`
  blackholes `10.0.0.0/8`, `172.16.0.0/12` and `192.168.0.0/16`. Without that,
  spoke A's packets for spoke B would follow the default route to the hub, hit
  the NAT's subnet and come back through `rt-hub` to spoke B: segmentation
  undone by a hairpin. The propagated hub /16 is more specific than the
  blackhole, so the hub stays reachable.
- **IPv6 egress is per VPC**, through each VPC's own egress-only internet
  gateway, which has no hourly charge. Spokes route only the hub's /56 to the
  Transit Gateway; `rt-spokes` has no IPv6 default route, so the TGW carries
  no IPv6 internet traffic.
- **The NAT is either a NAT gateway or a NAT instance**, chosen by the lab
  variable `egress`. The matrix runs against both.

| | NAT gateway | NAT instance (t4g.micro) |
|---|---|---|
| Hourly (eu-west-1) | $0.048 + $0.005 public IPv4 | $0.0092 + $0.005 public IPv4 |
| Per GB processed | $0.048 | none |
| Availability | managed, scales to 100 Gbps | one instance, no failover |
| Operations | none | AMI updates, iptables, source/destination check off |

## Consequences

- One NAT for three VPCs. With a NAT per VPC, the NAT gateway option would
  cost three times as much each hour.
- The NAT instance costs about a quarter of the NAT gateway each hour and has
  no per-GB charge, but it is a single instance I have to patch, and it fails
  without anything taking over. That is fine for a lab and not for
  production.
- IPv6 egress cannot be inspected or filtered centrally in this design. Doing
  that would need NAT66 or proxy instances in the hub, which is out of scope.
- The matrix row `spoke-a-to-internet-v6` tests the spoke's own egress-only
  gateway, not the hub's.
