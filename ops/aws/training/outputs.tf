output "bucket" {
  description = "Private bucket for research tables, models and logs."
  value       = aws_s3_bucket.research.id
}

output "launch_template_id" {
  description = "Spot template tickr-training (cheap, can be taken back)."
  value       = aws_launch_template.training["spot"].id
}

output "ondemand_launch_template_id" {
  description = "On-demand template tickr-training-ondemand (regular price, ondemand_instance_types only)."
  value       = aws_launch_template.training["ondemand"].id
}

output "research_user" {
  description = "IAM user whose access key the owner creates by hand for the research thread."
  value       = aws_iam_user.research.name
}

output "massive_secret" {
  description = "Empty secret the owner fills with the Massive flat-file key (README); only the machine role reads it."
  value       = aws_secretsmanager_secret.massive.name
}
