# -----------------------------------------------------------------------------
# Reachability Analyzer paths. A path costs nothing; each analysis of one costs
# $0.10, so scripts/evidence.py runs them only when asked
# (bash scripts/lab.sh reachability). Each repeats a matrix row and should name
# the same layer the row does.
# -----------------------------------------------------------------------------

locals {
  reachability_paths = {
    # Blocked by routing: rt-spoke-a blackholes 10.0.0.0/8 (spoke-a-to-spoke-b).
    "spoke-a-to-spoke-b-web" = { from = "spoke-a", to = "spoke-b", port = local.web_port }
    # Blocked by the hub host's security group (spoke-a-to-hub-admin).
    "spoke-a-to-hub-admin" = { from = "spoke-a", to = "hub-svc", port = local.admin_port }
    # Allowed: the hub reaches the admin port everywhere (hub-to-spoke-a-admin).
    "hub-to-spoke-a-admin" = { from = "hub-svc", to = "spoke-a", port = local.admin_port }
  }
}

resource "aws_ec2_network_insights_path" "matrix" {
  for_each = local.reachability_paths

  source           = aws_instance.host[each.value.from].id
  destination      = aws_instance.host[each.value.to].id
  protocol         = "tcp"
  destination_port = each.value.port
  tags             = { Name = "nwlab-${each.key}" }
}
