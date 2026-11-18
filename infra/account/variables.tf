variable "region" {
  description = "Region of the lab and of every account resource."
  type        = string
  default     = "eu-west-1"
}

variable "prefix" {
  description = "Prefix of every resource name in this project."
  type        = string
  default     = "nwlab"
}

variable "project_tag" {
  description = "Value of the Project tag on every lab resource; the reaper deletes only resources that carry it."
  type        = string
  default     = "aws-hub-spoke-network-lab"
}

variable "state_bucket" {
  description = "Terraform state bucket (contains the account ID; passed by scripts/account.sh)."
  type        = string
}

variable "alert_email" {
  description = "Address for reaper reports and budget alerts (passed by scripts/account.sh from ALERT_EMAIL; never committed)."
  type        = string
  sensitive   = true
}

variable "max_age_hours" {
  description = "Hard cap: the reaper deletes a lab resource this many hours after it was created, whatever its ExpiresAt tag says."
  type        = number
  default     = 10
}

variable "monthly_budget_usd" {
  description = "Monthly budget for resources tagged with the project, before credits."
  type        = number
  default     = 10
}

variable "github_subject_prefix" {
  description = <<-EOT
    Start of the OIDC sub claim for this repository. The repository uses
    GitHub's immutable subjects (owner@owner_id/repo@repo_id), so a renamed or
    re-created repository with the same name cannot assume the plan role.
  EOT
  type        = string
  default     = "repo:pimentafonso@350777569/aws-hub-spoke-network-lab@1463085720"
}
