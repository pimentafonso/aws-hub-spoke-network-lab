# -----------------------------------------------------------------------------
# PrivateLink across an overlapping range (docs/adr/0007).
#
# The consumer VPC reuses spoke A's 10.1.0.0/16, so no route can join them.
# Spoke A publishes its web port through an internal NLB and an endpoint
# service; the consumer reaches it through an interface endpoint, an address in
# its own range. Traffic arrives at spoke A from the NLB's addresses, never the
# consumer's.
#
# The consumer has no internet, NAT or Transit Gateway. Its test host reaches
# SSM through three more interface endpoints. Every endpoint sits in one zone
# ($0.011 an hour each), in the consumer's second private subnet, so the first
# holds only the test host (instances.tf explains why).
#
#   NLB                 $0.0252/h + LCU
#   interface endpoint  $0.011/h each, 4 of them, + $0.01/GB
# -----------------------------------------------------------------------------

locals {
  # The zone, by index into local.azs, that the endpoints and the NLB share.
  endpoint_az = 1

  # Fixed NLB node addresses in spoke A, one per zone, clear of the hosts'.
  nlb_ips = [for i, subnet in module.vpc["spoke-a"].subnet_ipv4_cidrs["private"] : cidrhost(subnet, 30)]

  ssm_endpoints = ["ssm", "ssmmessages", "ec2messages"]
}

# --- spoke A: the service --------------------------------------------------------

resource "aws_lb" "spoke_a" {
  #checkov:skip=CKV_AWS_91:Access logs are not needed: the flow logs record every connection.
  #checkov:skip=CKV_AWS_150:Deletion protection would stop the session's destroy.
  #checkov:skip=CKV2_AWS_20:A TCP listener; there is no HTTP to redirect.
  #checkov:skip=CKV2_AWS_28:WAF applies to HTTP load balancers, not a TCP NLB.
  #checkov:skip=CKV_AWS_131:Dropping invalid HTTP headers applies to application load balancers.
  name                             = "nwlab-spoke-a"
  internal                         = true
  load_balancer_type               = "network"
  enable_cross_zone_load_balancing = true # the endpoint's zone has no target

  dynamic "subnet_mapping" {
    for_each = module.vpc["spoke-a"].subnet_ids["private"]
    content {
      subnet_id            = subnet_mapping.value
      private_ipv4_address = local.nlb_ips[subnet_mapping.key]
    }
  }

  tags = { Name = "nwlab-spoke-a" }
}

resource "aws_lb_target_group" "spoke_a_web" {
  name        = "nwlab-spoke-a-web"
  port        = local.web_port
  protocol    = "TCP"
  target_type = "instance"
  vpc_id      = module.vpc["spoke-a"].id

  health_check {
    protocol            = "TCP"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = { Name = "nwlab-spoke-a-web" }
}

resource "aws_lb_target_group_attachment" "spoke_a_web" {
  target_group_arn = aws_lb_target_group.spoke_a_web.arn
  target_id        = aws_instance.host["spoke-a"].id
  port             = local.web_port
}

resource "aws_lb_listener" "spoke_a_web" {
  #checkov:skip=CKV_AWS_2:Plain TCP to a lab test page; the matrix tests reachability, not TLS.
  #checkov:skip=CKV_AWS_103:Plain TCP to a lab test page; there is no TLS to enforce.
  load_balancer_arn = aws_lb.spoke_a.arn
  port              = local.web_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.spoke_a_web.arn
  }
}

resource "aws_vpc_endpoint_service" "spoke_a" {
  #checkov:skip=CKV_AWS_123:Only this account is allowed (allowed_principals), so there is no one to accept or refuse.
  acceptance_required        = false
  network_load_balancer_arns = [aws_lb.spoke_a.arn]
  allowed_principals         = ["arn:${local.partition}:iam::${local.account_id}:root"]
  tags                       = { Name = "nwlab-spoke-a" }
}

# --- the consumer: endpoints and its test host's rules ----------------------------

resource "aws_security_group" "consumer_endpoints" {
  name        = "nwlab-consumer-endpoints"
  description = "Interface endpoints: web and HTTPS from the consumer VPC"
  vpc_id      = module.vpc["consumer"].id
  tags        = { Name = "nwlab-consumer-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "consumer_endpoints" {
  for_each = { web = local.web_port, https = 443 }

  security_group_id = aws_security_group.consumer_endpoints.id
  description       = each.key == "web" ? "Spoke A's service" : "SSM"
  ip_protocol       = "tcp"
  from_port         = each.value
  to_port           = each.value
  cidr_ipv4         = local.vpcs["consumer"].cidr
}

resource "aws_vpc_endpoint" "privatelink" {
  vpc_id              = module.vpc["consumer"].id
  service_name        = aws_vpc_endpoint_service.spoke_a.service_name
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [module.vpc["consumer"].subnet_ids["private"][local.endpoint_az]]
  security_group_ids  = [aws_security_group.consumer_endpoints.id]
  private_dns_enabled = false
  tags                = { Name = "nwlab-consumer-spoke-a" }
}

resource "aws_vpc_endpoint" "ssm" {
  for_each = toset(local.ssm_endpoints)

  vpc_id              = module.vpc["consumer"].id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [module.vpc["consumer"].subnet_ids["private"][local.endpoint_az]]
  security_group_ids  = [aws_security_group.consumer_endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "nwlab-consumer-${each.key}" }
}

# The consumer host: HTTPS out to anywhere, like every host, so it is routing
# and not this group that stops its internet probe; web out within its own
# range, which covers the endpoint and the direct probe to spoke A's address.
# No ingress: nothing needs to reach it.
resource "aws_vpc_security_group_egress_rule" "consumer_host" {
  for_each = {
    https = { port = 443, cidr = "0.0.0.0/0", description = "HTTPS: SSM endpoints and the internet probe" }
    web   = { port = local.web_port, cidr = local.vpcs["consumer"].cidr, description = "Probes within the consumer VPC" }
  }

  security_group_id = aws_security_group.host["consumer"].id
  description       = each.value.description
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}
