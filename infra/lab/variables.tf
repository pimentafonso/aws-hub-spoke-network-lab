variable "region" {
  description = "AWS region for every resource in the lab."
  type        = string
  default     = "eu-west-1"
}

variable "expires_at" {
  description = "UTC time after which the TTL reaper may delete the lab, as YYYY-MM-DDTHH:MM:SSZ. Set by the deploy script (deploy time plus the session length); there is no default, so the lab cannot be deployed without an expiry."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.expires_at)) && can(timeadd(var.expires_at, "0s"))
    error_message = "Use an RFC 3339 UTC time such as 2026-11-20T18:00:00Z."
  }

  # The reaper's hard cap is 10 hours after creation; an expiry beyond it
  # would only be a promise the reaper will not keep.
  validation {
    condition     = !can(timeadd(var.expires_at, "0s")) || (timecmp(var.expires_at, plantimestamp()) > 0 && timecmp(var.expires_at, timeadd(plantimestamp(), "10h")) <= 0)
    error_message = "expires_at must be in the future and at most 10 hours away."
  }
}
