output "bucket" {
  description = "Private bucket for research tables, models and logs."
  value       = aws_s3_bucket.research.id
}

output "launch_template_id" {
  description = "The only template the research user may start a machine from."
  value       = aws_launch_template.training.id
}

output "research_user" {
  description = "IAM user whose access key the owner creates by hand for the research thread."
  value       = aws_iam_user.research.name
}

output "massive_secret" {
  description = "Empty secret the owner fills with the Massive flat-file key (README); only the machine role reads it."
  value       = aws_secretsmanager_secret.massive.name
}
