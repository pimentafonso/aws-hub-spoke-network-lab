# A dual-stack VPC with three subnet tiers, one route table per tier:
#
#   private  workloads; IPv6 leaves through the VPC's own egress-only gateway,
#            unless var.ipv6_egress is off (an isolated VPC)
#   tgw      small subnets that hold the Transit Gateway attachment's ENIs
#   public   only when var.public is set (the hub's NAT and internet gateway)
#
# Routes towards the Transit Gateway or a NAT are added by the root module,
# because they depend on resources outside the VPC.

locals {
  az_index = { for i, az in var.azs : az => i }

  # IPv4: private /24s from the bottom, public /24s from the middle, TGW /28s
  # at the top. IPv6: one /64 per subnet out of the VPC's Amazon-provided /56.
  subnets = merge(
    { for az, i in local.az_index : "private-${az}" => {
      tier = "private", az = az, v4 = cidrsubnet(var.cidr, 8, i), v6 = i
    } },
    { for az, i in local.az_index : "tgw-${az}" => {
      tier = "tgw", az = az, v4 = cidrsubnet(var.cidr, 12, 4080 + i), v6 = 100 + i
    } },
    var.public ? { for az, i in local.az_index : "public-${az}" => {
      tier = "public", az = az, v4 = cidrsubnet(var.cidr, 8, 128 + i), v6 = 200 + i
    } } : {},
  )

  tiers = var.public ? ["private", "tgw", "public"] : ["private", "tgw"]
}

resource "aws_vpc" "this" {
  #checkov:skip=CKV2_AWS_11:The root module adds flow logs to S3 for every VPC (flowlogs.tf); Checkov cannot follow the module.
  cidr_block                       = var.cidr
  assign_generated_ipv6_cidr_block = true
  enable_dns_support               = true
  enable_dns_hostnames             = true

  tags = { Name = var.name }
}

# No rules: nothing may use the default security group.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-default-unused" }
}

resource "aws_subnet" "this" {
  for_each = local.subnets

  vpc_id                          = aws_vpc.this.id
  availability_zone               = each.value.az
  cidr_block                      = each.value.v4
  ipv6_cidr_block                 = cidrsubnet(aws_vpc.this.ipv6_cidr_block, 8, each.value.v6)
  assign_ipv6_address_on_creation = true
  map_public_ip_on_launch         = false

  tags = { Name = "${var.name}-${each.key}", Tier = each.value.tier }
}

resource "aws_route_table" "this" {
  for_each = toset(local.tiers)

  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-${each.key}" }
}

resource "aws_route_table_association" "this" {
  for_each = local.subnets

  subnet_id      = aws_subnet.this[each.key].id
  route_table_id = aws_route_table.this[each.value.tier].id
}

# Egress-only internet gateways have no hourly charge. IPv6 egress cannot be
# centralised through the hub: replies are routed to the VPC that owns the
# address, so each VPC needs its own (docs/adr/0005).
resource "aws_egress_only_internet_gateway" "this" {
  count = var.ipv6_egress ? 1 : 0

  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_route" "private_ipv6_egress" {
  count = var.ipv6_egress ? 1 : 0

  route_table_id              = aws_route_table.this["private"].id
  destination_ipv6_cidr_block = "::/0"
  egress_only_gateway_id      = aws_egress_only_internet_gateway.this[0].id
}

resource "aws_internet_gateway" "this" {
  count = var.public ? 1 : 0

  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_route" "public_ipv4" {
  count = var.public ? 1 : 0

  route_table_id         = aws_route_table.this["public"].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}

resource "aws_route" "public_ipv6" {
  count = var.public ? 1 : 0

  route_table_id              = aws_route_table.this["public"].id
  destination_ipv6_cidr_block = "::/0"
  gateway_id                  = aws_internet_gateway.this[0].id
}
