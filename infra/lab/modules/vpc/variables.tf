variable "name" {
  description = "Name prefix for the VPC and everything in it."
  type        = string
}

variable "cidr" {
  description = "IPv4 CIDR of the VPC, a /16. The IPv6 /56 is assigned by Amazon."
  type        = string

  validation {
    condition     = can(cidrhost(var.cidr, 0)) && endswith(var.cidr, "/16")
    error_message = "cidr must be a valid /16."
  }
}

variable "azs" {
  description = "Availability Zones to create subnets in (one subnet per tier per zone)."
  type        = list(string)
}

variable "ipv6_egress" {
  description = "Give the private subnets an egress-only internet gateway for IPv6."
  type        = bool
  default     = true
}

variable "public" {
  description = "Create public subnets and an internet gateway."
  type        = bool
  default     = false
}
