# Read by scripts/matrix.py run (through scripts/lab.sh matrix).

output "stage" {
  description = "The milestone stage this code deploys; the matrix runs its rows."
  value       = local.stage
}

output "egress" {
  description = "Centralised IPv4 egress in use: nat-gateway or nat-instance."
  value       = var.egress
}

output "hosts" {
  description = "Test hosts by matrix name: instance ID, VPC and addresses."
  value = {
    for name, vpc in local.hosts : name => {
      instance_id = aws_instance.host[name].id
      vpc         = vpc
      ipv4        = aws_instance.host[name].private_ip
      ipv6        = one(aws_instance.host[name].ipv6_addresses)
      eni         = aws_instance.host[name].primary_network_interface_id
    }
  }
}

output "vpcs" {
  description = "VPC IDs and ranges."
  value = {
    for name, _ in local.vpcs : name => {
      id   = module.vpc[name].id
      ipv4 = module.vpc[name].ipv4_cidr
      ipv6 = module.vpc[name].ipv6_cidr
    }
  }
}

output "transit_gateway_id" {
  description = "Transit Gateway ID."
  value       = aws_ec2_transit_gateway.main.id
}

# Read by scripts/failover.py. The tunnels' outside addresses are AWS's
# endpoints; the failover cuts tunnel 1 by dropping its endpoint's traffic.
output "vpn" {
  description = "The VPN connection, the on-prem router and each tunnel's addresses."
  value = {
    connection_id      = aws_vpn_connection.onprem.id
    attachment_id      = aws_vpn_connection.onprem.transit_gateway_attachment_id
    router_instance_id = aws_instance.router.id
    tunnels = [
      for i, t in local.tunnels : {
        name    = "tunnel${i + 1}"
        outside = t.outside
        cgw     = t.cgw
        aws     = t.aws
      }
    ]
  }
}

# Read by scripts/matrix.py: the consumer's probe connects to this name.
output "privatelink_endpoint_dns" {
  description = "DNS name of the consumer's interface endpoint for spoke A's service."
  value       = aws_vpc_endpoint.privatelink.dns_entry[0].dns_name
}

# Read by scripts/evidence.py.
output "privatelink" {
  description = "The endpoint's network interfaces and the NLB's fixed node addresses."
  value = {
    endpoint_enis = aws_vpc_endpoint.privatelink.network_interface_ids
    nlb_ips       = local.nlb_ips
  }
}

output "flow_logs" {
  description = "Where the flow logs land and how to query them."
  value = {
    bucket    = aws_s3_bucket.flow_logs.bucket
    database  = aws_glue_catalog_database.flow_logs.name
    table     = aws_glue_catalog_table.flow_logs.name
    workgroup = aws_athena_workgroup.lab.name
  }
}

output "reachability_paths" {
  description = "Reachability Analyzer path IDs by name, with the matrix row each repeats."
  value = {
    for name, p in local.reachability_paths : name => merge(p, { id = aws_ec2_network_insights_path.matrix[name].id })
  }
}
