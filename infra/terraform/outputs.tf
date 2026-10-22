output "app_url" {
  description = "Participant entrance (linked from the Qualtrics survey)"
  value       = "https://${local.app_fqdn}"
}

output "ecr_repository" {
  description = "Push the image here, pin <uri>:<tag> as container_image in terraform.tfvars by PR, and release it through tools/deploy.sh"
  value       = aws_ecr_repository.app.repository_url
}

output "rater_data_bucket" {
  value = aws_s3_bucket.rater_data.bucket
}

output "rater_kms_alias" {
  value = aws_kms_alias.rater.name
}

output "step1_bucket_read_only" {
  value = data.aws_s3_bucket.step1.bucket
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "task_execution_role_arn" {
  value = aws_iam_role.task_execution.arn
}
