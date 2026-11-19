output "id" {
  description = "VPC ID."
  value       = aws_vpc.this.id
}

output "ipv4_cidr" {
  description = "IPv4 CIDR of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "ipv6_cidr" {
  description = "Amazon-provided IPv6 /56 of the VPC."
  value       = aws_vpc.this.ipv6_cidr_block
}

output "subnet_ids" {
  description = "Subnet IDs by tier, in the order of var.azs."
  value = {
    for tier in local.tiers : tier => [
      for az in var.azs : aws_subnet.this["${tier}-${az}"].id
    ]
  }
}

output "route_table_ids" {
  description = "Route table ID by tier."
  value       = { for tier, rt in aws_route_table.this : tier => rt.id }
}

output "internet_gateway_id" {
  description = "Internet gateway ID, or null without public subnets."
  value       = one(aws_internet_gateway.this[*].id)
}
