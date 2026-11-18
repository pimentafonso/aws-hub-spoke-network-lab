terraform {
  # The bucket name contains the account ID, so scripts/lab.sh passes it at
  # init time: -backend-config="bucket=<account-id>-nwlab-tfstate"
  backend "s3" {
    key          = "lab/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
