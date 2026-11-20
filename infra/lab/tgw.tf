# -----------------------------------------------------------------------------
# Transit Gateway with one route table per attachment. Nothing is associated or
# propagated by default, so every path in the lab is a deliberate one.
#
#   rt-hub      associated: hub      propagated: spoke-a, spoke-b, vpn
#   rt-spoke-a  associated: spoke-a  propagated: hub, vpn
#   rt-spoke-b  associated: spoke-b  propagated: hub
#   rt-vpn      associated: vpn      propagated: hub, spoke-a
#   both spoke tables: static 0.0.0.0/0 -> hub, private ranges blackholed
#
# A spoke reaches the hub and, through the hub's NAT, the IPv4 internet. It
# never reaches the other spoke: the more specific blackhole beats the default
# route. Spoke A also learns on-prem's range over BGP, which beats the
# 172.16.0.0/12 blackhole; spoke B never learns it, so its packets for on-prem
# are dropped here rather than reaching on-prem. One table per spoke is what
# makes that possible: a shared table would give both spokes the same routes.
# IPv6 has no default route here; it leaves each VPC directly.
# The TGW itself has no hourly charge; each attachment costs $0.05 an hour.
# -----------------------------------------------------------------------------

resource "aws_ec2_transit_gateway" "main" {
  description                     = "nwlab hub-and-spoke"
  amazon_side_asn                 = 64512
  default_route_table_association = "disable"
  default_route_table_propagation = "disable"
  auto_accept_shared_attachments  = "disable"
  dns_support                     = "enable"
  vpn_ecmp_support                = "enable"

  tags = { Name = "nwlab-tgw" }
}

resource "aws_ec2_transit_gateway_vpc_attachment" "this" {
  for_each = local.tgw_vpcs

  transit_gateway_id                              = aws_ec2_transit_gateway.main.id
  vpc_id                                          = module.vpc[each.key].id
  subnet_ids                                      = module.vpc[each.key].subnet_ids["tgw"]
  ipv6_support                                    = "enable"
  dns_support                                     = "enable"
  transit_gateway_default_route_table_association = false
  transit_gateway_default_route_table_propagation = false

  tags = { Name = "nwlab-${each.key}" }
}

locals {
  # Attachment IDs by name: the VPCs and the VPN (vpn.tf).
  tgw_attachments = merge(
    { for name, a in aws_ec2_transit_gateway_vpc_attachment.this : name => a.id },
    { vpn = aws_vpn_connection.onprem.transit_gateway_attachment_id },
  )

  # Route table => the attachments whose routes it learns.
  tgw_propagations = {
    hub       = ["spoke-a", "spoke-b", "vpn"]
    "spoke-a" = ["hub", "vpn"]
    "spoke-b" = ["hub"]
    vpn       = ["hub", "spoke-a"]
  }
}

# One table per attachment, named after it; each attachment is associated
# with its own table.
resource "aws_ec2_transit_gateway_route_table" "this" {
  for_each = local.tgw_propagations

  transit_gateway_id = aws_ec2_transit_gateway.main.id
  tags               = { Name = "nwlab-rt-${each.key}" }
}

resource "aws_ec2_transit_gateway_route_table_association" "this" {
  for_each = local.tgw_propagations

  transit_gateway_attachment_id  = local.tgw_attachments[each.key]
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this[each.key].id
}

resource "aws_ec2_transit_gateway_route_table_propagation" "this" {
  for_each = merge([
    for table, sources in local.tgw_propagations : {
      for source in sources : "${table}/${source}" => { table = table, source = source }
    }
  ]...)

  transit_gateway_attachment_id  = local.tgw_attachments[each.value.source]
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this[each.value.table].id
}

resource "aws_ec2_transit_gateway_route" "spokes_default" {
  for_each = toset(local.spokes)

  destination_cidr_block         = "0.0.0.0/0"
  transit_gateway_attachment_id  = aws_ec2_transit_gateway_vpc_attachment.this["hub"].id
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this[each.key].id
}

resource "aws_ec2_transit_gateway_route" "spokes_blackhole" {
  for_each = { for p in setproduct(local.spokes, local.private_ranges) : "${p[0]}/${p[1]}" => p }

  destination_cidr_block         = each.value[1]
  blackhole                      = true
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this[each.value[0]].id
}

# -----------------------------------------------------------------------------
# VPC routes towards the Transit Gateway
# -----------------------------------------------------------------------------

# Spokes: all IPv4 to the TGW (the hub, or a blackhole); IPv6 to the hub only.
resource "aws_route" "spoke_ipv4_default" {
  for_each = toset(local.spokes)

  route_table_id         = module.vpc[each.key].route_table_ids["private"]
  destination_cidr_block = "0.0.0.0/0"
  transit_gateway_id     = aws_ec2_transit_gateway.main.id

  depends_on = [aws_ec2_transit_gateway_vpc_attachment.this]
}

resource "aws_route" "spoke_ipv6_hub" {
  for_each = toset(local.spokes)

  route_table_id              = module.vpc[each.key].route_table_ids["private"]
  destination_ipv6_cidr_block = module.vpc["hub"].ipv6_cidr
  transit_gateway_id          = aws_ec2_transit_gateway.main.id

  depends_on = [aws_ec2_transit_gateway_vpc_attachment.this]
}

# Hub: the private ranges back through the TGW, from the hosts and from the
# NAT (the public subnets carry the return path of centralised egress). Only
# the hosts talk to on-prem; the NAT never sees its traffic.
resource "aws_route" "hub_private_ranges" {
  for_each = {
    "private/lab"    = { tier = "private", cidr = "10.0.0.0/8" }
    "public/lab"     = { tier = "public", cidr = "10.0.0.0/8" }
    "private/onprem" = { tier = "private", cidr = local.vpcs.onprem.cidr }
  }

  route_table_id         = module.vpc["hub"].route_table_ids[each.value.tier]
  destination_cidr_block = each.value.cidr
  transit_gateway_id     = aws_ec2_transit_gateway.main.id

  depends_on = [aws_ec2_transit_gateway_vpc_attachment.this]
}

resource "aws_route" "hub_ipv6_spokes" {
  for_each = toset(local.spokes)

  route_table_id              = module.vpc["hub"].route_table_ids["private"]
  destination_ipv6_cidr_block = module.vpc[each.key].ipv6_cidr
  transit_gateway_id          = aws_ec2_transit_gateway.main.id

  depends_on = [aws_ec2_transit_gateway_vpc_attachment.this]
}
