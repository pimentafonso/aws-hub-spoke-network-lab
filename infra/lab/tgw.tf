# -----------------------------------------------------------------------------
# Transit Gateway with one route table per role. Nothing is associated or
# propagated by default, so every path in the lab is a deliberate one.
#
#   rt-hub     associated: hub        propagated: spoke-a, spoke-b
#   rt-spokes  associated: spokes     propagated: hub
#              static 0.0.0.0/0 -> hub, private ranges blackholed
#
# A spoke reaches the hub and, through the hub's NAT, the IPv4 internet. It
# never reaches the other spoke: the more specific blackhole beats the default
# route. IPv6 has no default route here; it leaves each VPC directly.
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
  for_each = local.vpcs

  transit_gateway_id                              = aws_ec2_transit_gateway.main.id
  vpc_id                                          = module.vpc[each.key].id
  subnet_ids                                      = module.vpc[each.key].subnet_ids["tgw"]
  ipv6_support                                    = "enable"
  dns_support                                     = "enable"
  transit_gateway_default_route_table_association = false
  transit_gateway_default_route_table_propagation = false

  tags = { Name = "nwlab-${each.key}" }
}

resource "aws_ec2_transit_gateway_route_table" "this" {
  for_each = toset(["hub", "spokes"])

  transit_gateway_id = aws_ec2_transit_gateway.main.id
  tags               = { Name = "nwlab-rt-${each.key}" }
}

resource "aws_ec2_transit_gateway_route_table_association" "this" {
  for_each = local.vpcs

  transit_gateway_attachment_id  = aws_ec2_transit_gateway_vpc_attachment.this[each.key].id
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this[each.key == "hub" ? "hub" : "spokes"].id
}

# The hub's routes go to the spokes; the spokes' routes go to the hub.
resource "aws_ec2_transit_gateway_route_table_propagation" "this" {
  for_each = local.vpcs

  transit_gateway_attachment_id  = aws_ec2_transit_gateway_vpc_attachment.this[each.key].id
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this[each.key == "hub" ? "spokes" : "hub"].id
}

resource "aws_ec2_transit_gateway_route" "spokes_default" {
  destination_cidr_block         = "0.0.0.0/0"
  transit_gateway_attachment_id  = aws_ec2_transit_gateway_vpc_attachment.this["hub"].id
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this["spokes"].id
}

resource "aws_ec2_transit_gateway_route" "spokes_blackhole" {
  for_each = toset(local.private_ranges)

  destination_cidr_block         = each.key
  blackhole                      = true
  transit_gateway_route_table_id = aws_ec2_transit_gateway_route_table.this["spokes"].id
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
# NAT (the public subnets carry the return path of centralised egress).
resource "aws_route" "hub_private_ranges" {
  for_each = toset(["private", "public"])

  route_table_id         = module.vpc["hub"].route_table_ids[each.key]
  destination_cidr_block = "10.0.0.0/8"
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
