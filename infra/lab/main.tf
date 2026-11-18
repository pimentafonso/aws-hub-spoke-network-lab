# The session-only lab. M2: a hub and two spokes on a Transit Gateway, with
# centralised IPv4 egress through the hub and a test host in each VPC.
#
#   hub      10.0.0.0/16  shared services, NAT, internet gateway
#   spoke-a  10.1.0.0/16  workload
#   spoke-b  10.2.0.0/16  workload, isolated from spoke-a
#
# Every VPC is dual-stack with an Amazon-provided IPv6 /56. The design and the
# expected results are in tests/matrix.yaml; scripts/lab.sh runs it after
# every deploy.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # The milestone this code deploys; the matrix runner runs its rows.
  stage = "m2"

  # Two zones for the subnets and the TGW attachments, which cost the same
  # either way. Hosts, the NAT gateway and the NAT instance sit in the first
  # zone only, to keep one of each.
  # Pinned rather than read from the API, so the set never changes under a
  # running lab.
  azs = ["${var.region}a", "${var.region}b"]

  vpcs = {
    hub       = { cidr = "10.0.0.0/16", public = true }
    "spoke-a" = { cidr = "10.1.0.0/16", public = false }
    "spoke-b" = { cidr = "10.2.0.0/16", public = false }
  }
  spokes = ["spoke-a", "spoke-b"]

  # Everything the lab uses or may use for private addressing. rt-spokes
  # blackholes these, so the default route to the hub never carries
  # spoke-to-spoke traffic (it would hairpin through the NAT otherwise).
  private_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
}

module "vpc" {
  source   = "./modules/vpc"
  for_each = local.vpcs

  name   = "nwlab-${each.key}"
  cidr   = each.value.cidr
  azs    = local.azs
  public = each.value.public
}
