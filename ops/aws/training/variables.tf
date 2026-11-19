variable "region" {
  description = "AWS region for the training bucket and machine (us-east-1, next to the bot)."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "Training machine type: CPU only, 32-64 vCPU and 128-256 GB RAM (training spec, stage 4)."
  type        = string
  default     = "c7a.16xlarge"
  validation {
    condition     = contains(var.allowed_instance_types, var.instance_type)
    error_message = "instance_type must be one of allowed_instance_types."
  }
}

variable "allowed_instance_types" {
  description = "The only machine types the research user may start (IAM condition): no GPU, no larger sizes."
  type        = list(string)
  default     = ["c7a.16xlarge", "m7a.16xlarge"]
}

variable "subnet_id" {
  description = "Subnet for the training machine; null = the first default subnet of the default VPC."
  type        = string
  default     = null
}

variable "disk_gb" {
  description = "Encrypted gp3 root disk of the training machine, in GB; deleted with the machine."
  type        = number
  default     = 500
}

variable "max_runtime_hours" {
  description = "Hard limit, in hours: the machine shuts itself down (and so terminates) this long after boot, whatever runs."
  type        = number
  default     = 24
}

variable "monthly_budget_usd" {
  description = "Monthly AWS budget for the whole account in USD: the bot (~80) plus the training sum the owner named."
  type        = number
  default     = 280
}

variable "budget_email" {
  description = "Address the budget alerts go to. Personal data: pass it at apply time (-var or an untracked tfvars file), never commit it."
  type        = string
}
