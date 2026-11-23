# -----------------------------------------------------------------------------
# Site-to-site VPN with BGP to a simulated on-premises network (M3).
#
# The on-prem VPC has no Transit Gateway attachment. A router in its public
# subnet (Ubuntu, strongSwan and FRR on a t4g.micro) is the customer gateway:
# its Elastic IP terminates both tunnels of one AWS VPN connection, and it runs
# one eBGP session over each. The on-prem test host sits in the private subnet
# and routes everything through the router, which also masquerades its
# internet traffic (SSM) behind the Elastic IP.
#
# The tunnels are active/standby, chosen with BGP attributes rather than ECMP:
#   on-prem -> AWS   tunnel 1 routes get local-pref 200, tunnel 2 routes 100
#   AWS -> on-prem   on-prem prepends its AS three times on tunnel 2, and the
#                    TGW prefers the shorter AS path
# so traffic uses tunnel 1 until it fails, and the failover is one clear move
# (docs/adr/0006).
#
#   VPN connection        $0.05/h
#   its TGW attachment    $0.05/h
#   router                t4g.micro $0.0092/h plus its public IPv4 $0.005/h
# -----------------------------------------------------------------------------

data "aws_ssm_parameter" "ubuntu_arm64" {
  # AL2023 has neither strongSwan nor FRR in its repositories; Ubuntu has both.
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

locals {
  onprem_asn = 65001

  # Inside addresses: a /30 per tunnel from 169.254.0.0/16, fixed so the
  # router's BGP neighbours and the failover script always know them.
  tunnel_inside_cidrs = ["169.254.10.0/30", "169.254.11.0/30"]

  router_subnet_cidr = module.vpc["onprem"].subnet_ipv4_cidrs["public"][0]

  tunnels = [
    {
      outside = aws_vpn_connection.onprem.tunnel1_address
      cgw     = aws_vpn_connection.onprem.tunnel1_cgw_inside_address
      aws     = aws_vpn_connection.onprem.tunnel1_vgw_inside_address
    },
    {
      outside = aws_vpn_connection.onprem.tunnel2_address
      cgw     = aws_vpn_connection.onprem.tunnel2_cgw_inside_address
      aws     = aws_vpn_connection.onprem.tunnel2_vgw_inside_address
    },
  ]
}

# --- the router's network interface and address ----------------------------
# Created before the instance, so the Elastic IP (the customer gateway's
# address) exists before the VPN does, and the router has internet access
# from its first boot.

resource "aws_security_group" "router" {
  name        = "nwlab-onprem-router"
  description = "On-prem router: IPsec from the two AWS tunnel endpoints, forwarding for on-prem"
  vpc_id      = module.vpc["onprem"].id
  tags        = { Name = "nwlab-onprem-router" }
}

# IKE, NAT-T and ESP from each tunnel's AWS endpoint only.
resource "aws_vpc_security_group_ingress_rule" "router_ipsec" {
  for_each = merge([
    for i in [0, 1] : {
      "tunnel${i + 1}/ike"   = { tunnel = i, protocol = "udp", port = 500 }
      "tunnel${i + 1}/nat-t" = { tunnel = i, protocol = "udp", port = 4500 }
      "tunnel${i + 1}/esp"   = { tunnel = i, protocol = "50", port = null }
    }
  ]...)

  security_group_id = aws_security_group.router.id
  description       = "IPsec from the AWS endpoint of tunnel ${each.value.tunnel + 1}"
  ip_protocol       = each.value.protocol
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = "${local.tunnels[each.value.tunnel].outside}/32"
}

resource "aws_vpc_security_group_ingress_rule" "router_from_onprem" {
  security_group_id = aws_security_group.router.id
  description       = "Traffic from on-prem to route into the tunnels or to the internet"
  ip_protocol       = "-1"
  cidr_ipv4         = local.vpcs.onprem.cidr
}

resource "aws_vpc_security_group_egress_rule" "router_out" {
  #checkov:skip=CKV_AWS_382:A router forwards whatever on-prem sends; the on-prem host's own security group limits what that is.
  security_group_id = aws_security_group.router.id
  description       = "Tunnels, forwarded traffic, package installs and SSM"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_network_interface" "router" {
  subnet_id          = module.vpc["onprem"].subnet_ids["public"][0]
  security_groups    = [aws_security_group.router.id]
  source_dest_check  = false # it forwards packets that are not its own
  ipv6_address_count = 1     # the subnet assigns one anyway; 0 would drift
  tags               = { Name = "nwlab-onprem-router" }
}

resource "aws_eip" "router" {
  domain            = "vpc"
  network_interface = aws_network_interface.router.id
  tags              = { Name = "nwlab-onprem-router" }

  # The address can only be associated once the VPC has an internet gateway.
  depends_on = [module.vpc]
}

# --- the VPN -------------------------------------------------------------------

resource "aws_customer_gateway" "onprem" {
  bgp_asn    = tostring(local.onprem_asn)
  ip_address = aws_eip.router.public_ip
  type       = "ipsec.1"
  tags       = { Name = "nwlab-onprem" }
}

# Both tunnels use the same settings: IKEv2, AES-256, SHA-256 and DH group 14,
# with AWS's default 30 s dead peer detection. The router initiates; AWS waits.
# AWS generates the pre-shared keys; they go to the router through SSM
# Parameter Store, never through user data.
resource "aws_vpn_connection" "onprem" {
  customer_gateway_id = aws_customer_gateway.onprem.id
  transit_gateway_id  = aws_ec2_transit_gateway.main.id
  type                = "ipsec.1"
  static_routes_only  = false

  tunnel1_inside_cidr                  = local.tunnel_inside_cidrs[0]
  tunnel1_ike_versions                 = ["ikev2"]
  tunnel1_phase1_encryption_algorithms = ["AES256"]
  tunnel1_phase1_integrity_algorithms  = ["SHA2-256"]
  tunnel1_phase1_dh_group_numbers      = [14]
  tunnel1_phase2_encryption_algorithms = ["AES256"]
  tunnel1_phase2_integrity_algorithms  = ["SHA2-256"]
  tunnel1_phase2_dh_group_numbers      = [14]
  tunnel1_dpd_timeout_seconds          = 30
  tunnel1_startup_action               = "add"

  tunnel2_inside_cidr                  = local.tunnel_inside_cidrs[1]
  tunnel2_ike_versions                 = ["ikev2"]
  tunnel2_phase1_encryption_algorithms = ["AES256"]
  tunnel2_phase1_integrity_algorithms  = ["SHA2-256"]
  tunnel2_phase1_dh_group_numbers      = [14]
  tunnel2_phase2_encryption_algorithms = ["AES256"]
  tunnel2_phase2_integrity_algorithms  = ["SHA2-256"]
  tunnel2_phase2_dh_group_numbers      = [14]
  tunnel2_dpd_timeout_seconds          = 30
  tunnel2_startup_action               = "add"

  tags = { Name = "nwlab-onprem" }
}

# The VPN's attachment is created by AWS, so the provider's default tags never
# reach it. Tagged here so its hourly charge shows under the project in Cost
# Explorer. The reaper ignores VPN attachments: they go with the connection.
resource "aws_ec2_tag" "vpn_attachment" {
  for_each = merge(local.default_tags, { Name = "nwlab-vpn" })

  resource_id = aws_vpn_connection.onprem.transit_gateway_attachment_id
  key         = each.key
  value       = each.value
}

resource "aws_ssm_parameter" "psk" {
  #checkov:skip=CKV_AWS_337:It is a SecureString, encrypted with the AWS managed key; a customer managed key would cost $1 a month.
  count = 2

  name        = "/nwlab/vpn/tunnel${count.index + 1}-psk"
  description = "Pre-shared key of VPN tunnel ${count.index + 1}, read by the on-prem router at boot"
  type        = "SecureString"
  value       = [aws_vpn_connection.onprem.tunnel1_preshared_key, aws_vpn_connection.onprem.tunnel2_preshared_key][count.index]
}

# --- the router's role: SSM, and the two keys ------------------------------------

resource "aws_iam_role" "router" {
  name                 = "nwlab-onprem-router"
  description          = "On-prem router: SSM Run Command and the VPN pre-shared keys"
  assume_role_policy   = data.aws_iam_policy_document.ec2_trust.json
  permissions_boundary = "arn:${local.partition}:iam::${local.account_id}:policy/nwlab-workload-boundary"
}

resource "aws_iam_role_policy_attachment" "router_ssm" {
  role       = aws_iam_role.router.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "router_keys" {
  statement {
    sid       = "ReadVpnKeys"
    actions   = ["ssm:GetParameter"]
    resources = [for p in aws_ssm_parameter.psk : p.arn]
  }
  statement {
    sid       = "DecryptThroughSsm"
    actions   = ["kms:Decrypt"]
    resources = ["arn:${local.partition}:kms:${var.region}:${local.account_id}:key/*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "router_keys" {
  name   = "vpn-keys"
  role   = aws_iam_role.router.id
  policy = data.aws_iam_policy_document.router_keys.json
}

resource "aws_iam_instance_profile" "router" {
  name = "nwlab-onprem-router"
  role = aws_iam_role.router.name
}

# --- the router ------------------------------------------------------------------

resource "aws_instance" "router" {
  #checkov:skip=CKV_AWS_126:Detailed monitoring costs extra and a lab session is a few hours.
  ami                         = data.aws_ssm_parameter.ubuntu_arm64.value
  instance_type               = "t4g.micro"
  iam_instance_profile        = aws_iam_instance_profile.router.name
  ebs_optimized               = true
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/templates/vpn-router.sh", {
    region        = var.region
    eip           = aws_eip.router.public_ip
    router_ip     = aws_network_interface.router.private_ip
    vpc_router    = cidrhost(local.router_subnet_cidr, 1)
    onprem_cidr   = local.vpcs.onprem.cidr
    onprem_asn    = local.onprem_asn
    aws_asn       = aws_ec2_transit_gateway.main.amazon_side_asn
    tunnels       = local.tunnels
    psk_names     = [for p in aws_ssm_parameter.psk : p.name]
    prepend_times = 3
  })

  primary_network_interface {
    network_interface_id = aws_network_interface.router.id
  }

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    encrypted   = true
    volume_type = "gp3"
    volume_size = 8
  }

  # A newer Ubuntu image must not replace the router in the middle of a
  # session (an extend re-applies the lab). source_dest_check belongs to the
  # interface; the instance reads it back as false and must not reset it.
  lifecycle {
    ignore_changes = [ami, source_dest_check]
  }

  # The keys must be readable when the router boots.
  depends_on = [aws_iam_role_policy.router_keys, aws_iam_role_policy_attachment.router_ssm]

  tags = { Name = "nwlab-onprem-router" }
}

# On-prem's private subnets send everything to the router: the lab ranges it
# learns over BGP, and the internet, which it masquerades.
resource "aws_route" "onprem_default" {
  route_table_id         = module.vpc["onprem"].route_table_ids["private"]
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_network_interface.router.id
}
