# Auto-restart groups: one Auto Scaling group per research job that keeps at most `machines` spot
# machines alive (1 unless the job script shares its work between machines), so a machine AWS
# takes back (or the 24-hour power-off) is replaced within minutes and the job resumes from its
# checkpoints in S3. Desired capacity is 0 by default: a group runs only after someone sets it AND a deadline file exists for the job
# (jobs/<job>/until in the bucket, written after the owner's yes on a named sum). The boot
# wrapper in user-data.sh enforces both and sets the group back to 0 when the job is done or the
# deadline has passed, so a finished job is never relaunched, and after job_max_failures failed
# runs (a job script that cannot be fetched counts), so a permanent error is not relaunched forever. The backstop that needs no machine:
# whoever starts a group also schedules "desired 0" at the deadline on the group itself (README).

locals {
  # Static names: the boot wrapper needs its own group name before the group exists.
  job_groups = { for job, cfg in var.jobs : job => "tickr-${job}" }
}

resource "aws_launch_template" "job" {
  for_each                             = var.jobs
  name                                 = "tickr-${each.key}-auto"
  image_id                             = data.aws_ssm_parameter.ubuntu.value
  instance_type                        = each.value.instance_types[0]
  instance_initiated_shutdown_behavior = "terminate"
  update_default_version               = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.machine.arn
  }

  # No subnet here: the group spreads over every default subnet (all zones).
  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.machine.id]
    delete_on_termination       = true
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = each.value.disk_gb
      volume_type           = "gp3"
      throughput            = each.value.disk_mbps
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  user_data = base64encode(templatefile("${path.module}/user-data.sh", {
    max_runtime_minutes = var.max_runtime_hours * 60
    bucket              = aws_s3_bucket.research.id
    region              = var.region
    job                 = each.key
    job_script          = each.value.script
    group               = local.job_groups[each.key]
    max_failures        = var.job_max_failures
    aws_cli_version     = var.installers.aws_cli_version
    aws_cli_sha256      = var.installers.aws_cli_sha256
    uv_version          = var.installers.uv_version
    uv_wheel_url        = var.installers.uv_wheel_url
    uv_sha256           = var.installers.uv_sha256
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      "tickr:role" = local.role_tag
      "tickr:job"  = each.key
      Name         = "tickr-${each.key}"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      "tickr:role" = local.role_tag
    }
  }
}

resource "aws_autoscaling_group" "job" {
  for_each            = var.jobs
  name                = local.job_groups[each.key]
  min_size            = 0
  max_size            = each.value.machines
  desired_capacity    = 0
  vpc_zone_identifier = data.aws_subnets.default.ids
  # No early replacement on a rebalance hint: the group never runs more than max_size machines,
  # so a job never gets an extra machine writing the same checkpoints.
  capacity_rebalance = false

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "capacity-optimized"
    }
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.job[each.key].id
        version            = "$Latest"
      }
      dynamic "override" {
        for_each = each.value.instance_types
        content {
          instance_type = override.value
        }
      }
    }
  }

  tag {
    key                 = "tickr:role"
    value               = local.role_tag
    propagate_at_launch = true
  }

  # Started and stopped by the research user and the boot wrapper, never by an apply.
  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

# Lets a job machine set its group back to 0 when the job is done. It is the shared machine role,
# so a training machine could also start a group; same owner, same max_size limit.
data "aws_iam_policy_document" "machine_groups" {
  statement {
    sid     = "StopOwnGroups"
    actions = ["autoscaling:SetDesiredCapacity"]
    resources = [for name in values(local.job_groups) :
    "arn:${local.partition}:autoscaling:${var.region}:${local.account}:autoScalingGroup:*:autoScalingGroupName/${name}"]
  }
}

resource "aws_iam_role_policy" "machine_groups" {
  name   = "job-groups"
  role   = aws_iam_role.machine.id
  policy = data.aws_iam_policy_document.machine_groups.json
}
