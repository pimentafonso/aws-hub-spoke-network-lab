provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "aws-hub-spoke-network-lab"
      Scope     = "account"
      ManagedBy = "terraform"
    }
  }
}
