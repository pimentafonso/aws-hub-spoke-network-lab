# -----------------------------------------------------------------------------
# Test hosts: one t4g.micro per VPC, no public IPv4, reached only through SSM.
# Security groups open web to every lab range, on-prem included, so the
# matrix's on-prem denies can only come from routing.
# Each listens on TCP 80 (web, open to the lab) and 8443 (admin, hub only),
# the two ports tests/matrix.yaml probes.
# -----------------------------------------------------------------------------

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  hosts = {
    "hub-svc"  = "hub"
    "spoke-a"  = "spoke-a"
    "spoke-b"  = "spoke-b"
    "onprem"   = "onprem"   # reaches SSM through the on-prem router (vpn.tf)
    "consumer" = "consumer" # reaches SSM through interface endpoints (privatelink.tf)
  }

  # Spoke A and the consumer share 10.1.0.0/16, so two addresses are pinned.
  # The consumer's direct probe goes to spoke A's host address, which must be
  # unused in the consumer VPC: nothing else is placed in the consumer's first
  # private subnet (its endpoints use the second), and spoke A's NLB nodes have
  # fixed addresses too (privatelink.tf). Otherwise the probe could reach a
  # consumer ENI, and the flow logs could mistake an NLB node for the consumer.
  host_ips = {
    "spoke-a"  = cidrhost(local.vpcs["spoke-a"].cidr, 10)  # 10.1.0.10
    "consumer" = cidrhost(local.vpcs["consumer"].cidr, 20) # 10.1.0.20
  }

  # Probe ports: what the matrix tests, and what the hosts' egress allows.
  web_port   = 80
  admin_port = 8443
}

# --- instance profile: SSM only, capped by the account's boundary ------------

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "host" {
  name                 = "nwlab-test-host"
  description          = "Lab test hosts: SSM Run Command only"
  assume_role_policy   = data.aws_iam_policy_document.ec2_trust.json
  permissions_boundary = "arn:${local.partition}:iam::${local.account_id}:policy/nwlab-workload-boundary"
}

resource "aws_iam_role_policy_attachment" "host_ssm" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "host" {
  name = "nwlab-test-host"
  role = aws_iam_role.host.name
}

# --- security groups -----------------------------------------------------------

resource "aws_security_group" "host" {
  #checkov:skip=CKV2_AWS_5:Attached to the test host in each VPC (aws_instance.host); Checkov cannot follow the for_each key.
  for_each = local.vpcs

  name        = "nwlab-${each.key}-host"
  description = "Test host: web from the lab, admin from the hub"
  vpc_id      = module.vpc[each.key].id
  tags        = { Name = "nwlab-${each.key}-host" }
}

locals {
  # One rule per VPC, port, source VPC and family. Keys are built from names,
  # because the IPv6 ranges are only known after apply. The consumer's own
  # rules are in privatelink.tf.
  families = ["ipv4", "ipv6"]
  routed   = keys(local.routed_vpcs)
  cidr_of = {
    for name, _ in local.routed_vpcs : name => {
      ipv4 = module.vpc[name].ipv4_cidr
      ipv6 = module.vpc[name].ipv6_cidr
    }
  }

  # Web from every lab VPC, admin from the hub only.
  host_ingress = merge(
    { for t in setproduct(local.routed, local.routed, local.families) :
      "${t[0]}/web/${t[1]}/${t[2]}" => { vpc = t[0], port = local.web_port, family = t[2], cidr = local.cidr_of[t[1]][t[2]] }
    },
    { for t in setproduct(local.routed, local.families) :
      "${t[0]}/admin/hub/${t[1]}" => { vpc = t[0], port = local.admin_port, family = t[1], cidr = local.cidr_of["hub"][t[1]] }
    },
  )

  host_egress = merge(
    { for t in setproduct(local.routed, local.families) :
      "${t[0]}/https/internet/${t[1]}" => { vpc = t[0], port = 443, family = t[1], cidr = t[1] == "ipv4" ? "0.0.0.0/0" : "::/0" }
    },
    { for t in setproduct(local.routed, [local.web_port, local.admin_port], local.routed, local.families) :
      "${t[0]}/${t[1]}/${t[2]}/${t[3]}" => { vpc = t[0], port = t[1], family = t[3], cidr = local.cidr_of[t[2]][t[3]] }
    },
  )
}

resource "aws_vpc_security_group_ingress_rule" "host" {
  for_each = local.host_ingress

  security_group_id = aws_security_group.host[each.value.vpc].id
  description       = each.value.port == local.admin_port ? "Admin from the hub" : "Web from the lab"
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.family == "ipv4" ? each.value.cidr : null
  cidr_ipv6         = each.value.family == "ipv6" ? each.value.cidr : null
}

# HTTPS out for SSM and the internet probe; the probe ports towards the lab.
resource "aws_vpc_security_group_egress_rule" "host" {
  for_each = local.host_egress

  security_group_id = aws_security_group.host[each.value.vpc].id
  description       = each.value.port == 443 ? "HTTPS: SSM and the internet probe" : "Probes to the lab"
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.family == "ipv4" ? each.value.cidr : null
  cidr_ipv6         = each.value.family == "ipv6" ? each.value.cidr : null
}

# --- instances -----------------------------------------------------------------

resource "aws_instance" "host" {
  #checkov:skip=CKV_AWS_126:Detailed monitoring costs extra and a lab session is a few hours.
  for_each = local.hosts

  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = "t4g.micro"
  subnet_id                   = module.vpc[each.value].subnet_ids["private"][0]
  private_ip                  = lookup(local.host_ips, each.key, null)
  vpc_security_group_ids      = [aws_security_group.host[each.value].id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  ipv6_address_count          = 1
  ebs_optimized               = true
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/templates/test-host.sh", {
    name = each.key
  })

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    encrypted   = true
    volume_type = "gp3"
    volume_size = 8
  }

  tags = { Name = "nwlab-${each.key}" }
}
