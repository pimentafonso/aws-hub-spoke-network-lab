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
