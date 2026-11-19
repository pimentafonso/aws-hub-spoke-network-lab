# -----------------------------------------------------------------------------
# Centralised IPv4 egress: one NAT in the hub for every VPC. var.egress picks
# the managed NAT gateway or a NAT instance; the matrix runs against either.
#
#   nat-gateway   $0.048/h + $0.048/GB processed, plus its public IPv4
#   nat-instance  t4g.micro $0.0092/h plus its public IPv4, no per-GB charge;
#                 one instance, no failover, patched by me
#
# Traffic from a spoke arrives on the hub's TGW subnets, whose route table
# sends it to the NAT. The NAT's public subnet routes 10.0.0.0/8 back to the
# TGW (tgw.tf).
# -----------------------------------------------------------------------------

locals {
  nat_gateway  = var.egress == "nat-gateway"
  nat_instance = var.egress == "nat-instance"
}

resource "aws_eip" "nat" {
  count = local.nat_gateway ? 1 : 0

  domain = "vpc"
  tags   = { Name = "nwlab-nat" }
}

resource "aws_nat_gateway" "hub" {
  count = local.nat_gateway ? 1 : 0

  subnet_id     = module.vpc["hub"].subnet_ids["public"][0]
  allocation_id = aws_eip.nat[0].id
  tags          = { Name = "nwlab-nat" }
}

resource "aws_security_group" "nat_instance" {
  count = local.nat_instance ? 1 : 0

  name        = "nwlab-nat-instance"
  description = "NAT instance: forwards IPv4 from the lab ranges to the internet"
  vpc_id      = module.vpc["hub"].id
  tags        = { Name = "nwlab-nat-instance" }
}

resource "aws_vpc_security_group_ingress_rule" "nat_from_lab" {
  count = local.nat_instance ? 1 : 0

  security_group_id = aws_security_group.nat_instance[0].id
  description       = "Any traffic from the lab to be translated"
  ip_protocol       = "-1"
  cidr_ipv4         = "10.0.0.0/8"
}

resource "aws_vpc_security_group_egress_rule" "nat_to_internet" {
  #checkov:skip=CKV_AWS_382:A NAT forwards whatever the lab sends to the internet; the hosts' own security groups limit what that is.
  count = local.nat_instance ? 1 : 0

  security_group_id = aws_security_group.nat_instance[0].id
  description       = "Translated traffic to the internet"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "nat" {
  #checkov:skip=CKV_AWS_88:A NAT instance needs a public IPv4 address; it is the egress point.
  #checkov:skip=CKV_AWS_126:Detailed monitoring costs extra and a lab session is a few hours.
  #checkov:skip=CKV2_AWS_41:The NAT instance calls no AWS API, so it has no instance profile.
  count = local.nat_instance ? 1 : 0

  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = "t4g.micro"
  subnet_id                   = module.vpc["hub"].subnet_ids["public"][0]
  vpc_security_group_ids      = [aws_security_group.nat_instance[0].id]
  associate_public_ip_address = true
  ipv6_address_count          = 0
  source_dest_check           = false # it forwards packets that are not its own
  ebs_optimized               = true
  user_data_replace_on_change = true
  user_data                   = file("${path.module}/templates/nat-instance.sh")

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    encrypted   = true
    volume_type = "gp3"
    volume_size = 8
  }

  tags = { Name = "nwlab-nat-instance" }
}

# The default route of the hub's hosts and of traffic arriving from the spokes.
resource "aws_route" "hub_ipv4_egress" {
  for_each = toset(["private", "tgw"])

  route_table_id         = module.vpc["hub"].route_table_ids[each.key]
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = local.nat_gateway ? aws_nat_gateway.hub[0].id : null
  network_interface_id   = local.nat_instance ? aws_instance.nat[0].primary_network_interface_id : null
}
