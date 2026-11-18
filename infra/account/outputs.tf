output "plan_role_arn" {
  description = "Role that pull requests assume to run terraform plan."
  value       = aws_iam_role.github_plan.arn
}

output "workload_boundary_arn" {
  description = "Permissions boundary every role in this project must carry."
  value       = aws_iam_policy.workload_boundary.arn
}

output "reaper_function_name" {
  description = "The TTL reaper Lambda function."
  value       = aws_lambda_function.reaper.function_name
}

output "alert_topic_arn" {
  description = "SNS topic for reaper reports and failures."
  value       = aws_sns_topic.alerts.arn
}
