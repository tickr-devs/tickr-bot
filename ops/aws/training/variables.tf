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
    # Layer 1 settings search: several LightGBM fits side by side over the development package,
    # so only the 256 GB types (m-type .16xlarge). On a 128 GB c-type the job script refuses and
    # stops the job, which would make a start depend on which pool AWS picks.
    train = {
      script         = "scripts/l1boot.sh"
      disk_gb        = 500
      disk_mbps      = 500
      instance_types = ["m7i.16xlarge", "m7a.16xlarge", "m6i.16xlarge", "m6a.16xlarge"]
    }
    # Label tables and the tick content check: one process per core, about 6 GB each, so the
    # memory-rich .16xlarge types (64 vCPU, 512 GB) let every core work. One machine = the
    # 64 vCPU spot quota, so it runs only while tickr-download and tickr-train are at 0.
    labels = {
      script         = "scripts/labelboot.sh"
      disk_gb        = 500
      disk_mbps      = 500
      instance_types = ["r7a.16xlarge", "r6a.16xlarge", "r6i.16xlarge", "r7i.16xlarge", "r5a.16xlarge", "r5.16xlarge"]
    }
  }

  validation {
    condition     = alltrue([for j in values(var.jobs) : j.machines >= 1])
    error_message = "Every job needs machines >= 1."
  }
}

variable "job_max_failures" {
  description = "Failed runs of a job (job script not fetched, or a non-zero exit) after which its auto-restart group stops itself; read by the boot wrapper."
  type        = number
  default     = 3
}

variable "installers" {
  description = "The installers user-data.sh downloads and runs as root on every training machine, each pinned to one version and refused unless its file has this sha256. aws_cli_version: AWS CLI v2 release (https://awscli.amazonaws.com/awscli-exe-linux-x86_64-<version>.zip); aws_cli_sha256: sha256 of that zip. uv_version: uv release; uv_wheel_url: its manylinux x86_64 wheel on PyPI (only the uv binary inside is used); uv_sha256: sha256 of that wheel (PyPI shows it). To move a version: download the new file, run sha256sum on it, change version, URL and sha256 together."
  type = object({
    aws_cli_version = string
    aws_cli_sha256  = string
    uv_version      = string
    uv_wheel_url    = string
    uv_sha256       = string
  })
  default = {
    aws_cli_version = "2.39.0"
    aws_cli_sha256  = "6c89ce7ed7bc0a2520e2bc3eeedfea4e2eb2e7da2836860c9471871408404036"
    uv_version      = "0.11.32"
    uv_wheel_url    = "https://files.pythonhosted.org/packages/90/b0/114463d056b6b328d45557001e848b8ab15539bd8f4fa7a457ccb83e2b5d/uv-0.11.32-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    uv_sha256       = "3da76cd4e2697de30928b8a8524bd39183ac1e08cb7e72833807c022b7cba6c4"
  }
  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.installers.aws_cli_sha256)) && can(regex("^[0-9a-f]{64}$", var.installers.uv_sha256))
    error_message = "installers: every sha256 must be 64 lowercase hex characters."
  }
}
