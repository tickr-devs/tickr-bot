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
  description = "The only machine types the research user may start (IAM condition): no GPU, no larger sizes. c7a.2xlarge (8 vCPU, 16 GB) is the small machine for downloads and runs that fit the 8 vCPU spot quota."
  type        = list(string)
  default     = ["c7a.16xlarge", "m7a.16xlarge", "c7a.2xlarge"]
}

variable "ondemand_instance_types" {
  description = "Machine types the research user may rent at the regular (on-demand) price, from the tickr-training-ondemand template; the first is that template's default. Small only: on-demand costs 3-4 times spot."
  type        = list(string)
  default     = ["c7a.2xlarge"]
  validation {
    condition     = length(var.ondemand_instance_types) > 0
    error_message = "ondemand_instance_types must name at least one type."
  }
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

variable "jobs" {
  description = "Research jobs run by an auto-restart group: script = boot script the job machine fetches from the bucket and runs; disk_gb = root disk in GB; disk_mbps = gp3 throughput in MB/s (null = AWS default 125); machines = most machines the group may run at once (the job script shares the work between them); instance_types = spot types the group may pick (first = template default)."
  type = map(object({
    script         = string
    disk_gb        = number
    disk_mbps      = optional(number)
    machines       = optional(number, 1)
    instance_types = list(string)
  }))
  default = {
    download = {
      script    = "scripts/tickboot.sh"
      disk_gb   = 300
      disk_mbps = 500
      # 4 x 16 vCPU = the 64 vCPU spot quota.
      machines       = 4
      instance_types = ["c7a.4xlarge", "m7a.4xlarge", "c6a.4xlarge", "m6a.4xlarge", "c7i.4xlarge", "c6i.4xlarge", "m6i.4xlarge"]
    }
    train = {
      script         = "scripts/l1boot.sh"
      disk_gb        = 500
      instance_types = ["c7a.16xlarge", "m7a.16xlarge", "c6a.16xlarge", "m6a.16xlarge", "c7i.16xlarge", "m7i.16xlarge", "c6i.16xlarge", "m6i.16xlarge"]
    }
  }

  validation {
    condition     = alltrue([for j in values(var.jobs) : j.machines >= 1])
    error_message = "Every job needs machines >= 1."
  }
}

variable "job_max_failures" {
  description = "Failed runs of a job script (non-zero exit) after which its auto-restart group stops itself; read by the boot wrapper."
  type        = number
  default     = 3
}
