provider "aws" {
  region = var.region

  # Every resource carries the project tag and an expiry time. The TTL reaper
  # (from M1) deletes anything with this project tag once ExpiresAt has passed.
  default_tags {
    tags = local.default_tags
  }
}
