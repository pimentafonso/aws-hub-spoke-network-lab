terraform {
  # The bucket name contains the account ID, so scripts/account.sh passes it
  # at init time: -backend-config="bucket=<account-id>-nwlab-tfstate"
  backend "s3" {
    key          = "account/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
