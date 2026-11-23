# The session-only lab. M2: a hub and two spokes on a Transit Gateway, with
# centralised IPv4 egress through the hub and a test host in each VPC.
# M3: a simulated on-premises network joined over a site-to-site VPN with BGP.
# M4: a consumer VPC that reuses spoke A's range and reaches spoke A's service
# only through PrivateLink; flow logs from every VPC, queried with Athena.
#
#   hub      10.0.0.0/16    shared services, NAT, internet gateway
#   spoke-a  10.1.0.0/16    workload, reachable from on-prem
#   spoke-b  10.2.0.0/16    workload, isolated from spoke-a and from on-prem
#   onprem   172.16.0.0/16  not attached to the TGW: its only way in is the VPN
#   consumer 10.1.0.0/16    overlaps spoke-a; no TGW, no internet, interface
#                           endpoints only (privatelink.tf)
#
# Every VPC is dual-stack with an Amazon-provided IPv6 /56. The design and the
# expected results are in tests/matrix.yaml; scripts/lab.sh runs it after
# every deploy.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # On every resource through the provider, and on the few the provider cannot
  # tag (the VPN's Transit Gateway attachment, vpn.tf).
  default_tags = {
    Project   = "aws-hub-spoke-network-lab"
    ManagedBy = "terraform"
    ExpiresAt = var.expires_at
  }

  # The milestone this code deploys; the matrix runner runs its rows.
  stage = "m4"

  # Two zones for the subnets and the TGW attachments, which cost the same
  # either way. Hosts, the NAT gateway and the NAT instance sit in the first
  # zone only, to keep one of each.
  # Pinned rather than read from the API, so the set never changes under a
  # running lab.
  azs = ["${var.region}a", "${var.region}b"]

  vpcs = {
    hub        = { cidr = "10.0.0.0/16", public = true, tgw = true, isolated = false }
    "spoke-a"  = { cidr = "10.1.0.0/16", public = false, tgw = true, isolated = false }
    "spoke-b"  = { cidr = "10.2.0.0/16", public = false, tgw = true, isolated = false }
    onprem     = { cidr = "172.16.0.0/16", public = true, tgw = false, isolated = false }
    "consumer" = { cidr = "10.1.0.0/16", public = false, tgw = false, isolated = true }
  }
  tgw_vpcs = { for name, v in local.vpcs : name => v if v.tgw }

  # The VPCs whose ranges are unique and routed. The consumer reuses spoke A's
  # range, so it is left out of every rule built from VPC ranges: a rule for
  # 10.1.0.0/16 "from the consumer" would duplicate spoke A's and fail.
  routed_vpcs = { for name, v in local.vpcs : name => v if !v.isolated }
  spokes      = ["spoke-a", "spoke-b"]

  # Everything the lab uses or may use for private addressing. rt-spokes
  # blackholes these, so the default route to the hub never carries
  # spoke-to-spoke traffic (it would hairpin through the NAT otherwise).
  private_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
}

module "vpc" {
  source   = "./modules/vpc"
  for_each = local.vpcs

  name        = "nwlab-${each.key}"
  cidr        = each.value.cidr
  azs         = local.azs
  public      = each.value.public
  ipv6_egress = !each.value.isolated
}
