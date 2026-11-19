# Training stack for the owner's model research (docs/backlog.md T15, training spec stage 4).
# Isolated from the bot: the research user below can only run the training machine from this
# launch template, open SSM sessions to it and use the training bucket. It has no access to
# Secrets Manager, the bot server, IAM or billing. Market data derived from the owner's personal
# Massive plan stays in this private bucket and on this private machine (licence: personal use;
# delete within 30 days after the subscription ends).

data "aws_caller_identity" "me" {}

data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.me.account_id
  partition = data.aws_partition.current.partition
  role_tag  = "training"
}

# --- Bucket ---------------------------------------------------------------------------------

resource "aws_s3_bucket" "research" {
  bucket = "tickr-research-${local.account}"
}

resource "aws_s3_bucket_public_access_block" "research" {
  bucket                  = aws_s3_bucket.research.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "research" {
  bucket = aws_s3_bucket.research.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "research" {
  bucket = aws_s3_bucket.research.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "research_bucket" {
  statement {
    sid     = "TlsOnly"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.research.arn,
      "${aws_s3_bucket.research.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "research" {
  bucket     = aws_s3_bucket.research.id
  policy     = data.aws_iam_policy_document.research_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.research]
}

data "aws_iam_policy_document" "bucket_rw" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.research.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.research.arn}/*"]
  }
}

# --- Training machine -----------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "machine" {
  name               = "tickr-training-machine"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Only what the SSM agent needs for Run Command and Session Manager. Not the managed
# AmazonSSMManagedInstanceCore: it also reads every Parameter Store value of the account, and a
# shell on this machine would then reach them.
data "aws_iam_policy_document" "machine_agent" {
  statement {
    sid = "SsmAgent"
    actions = [
      "ssm:UpdateInstanceInformation",
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
      "ec2messages:AcknowledgeMessage",
      "ec2messages:DeleteMessage",
      "ec2messages:FailMessage",
      "ec2messages:GetEndpoint",
      "ec2messages:GetMessages",
      "ec2messages:SendReply",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "NoAccountSecrets"
    effect    = "Deny"
    actions   = ["ssm:GetParameter*", "ssm:DescribeParameters", "secretsmanager:*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "machine_agent" {
  name   = "ssm-agent-only"
  role   = aws_iam_role.machine.id
  policy = data.aws_iam_policy_document.machine_agent.json
}

resource "aws_iam_role_policy" "machine_bucket" {
  name   = "research-bucket"
  role   = aws_iam_role.machine.id
  policy = data.aws_iam_policy_document.bucket_rw.json
}

resource "aws_iam_instance_profile" "machine" {
  name = "tickr-training-machine"
  role = aws_iam_role.machine.name
}

data "aws_vpc" "default" {
  default = true
}

# No inbound rule at all: the machine is reached only through SSM, which it dials out to.
resource "aws_security_group" "machine" {
  name        = "tickr-training"
  description = "Training machine: no inbound, outbound for SSM, S3 and packages"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_launch_template" "training" {
  name                                 = "tickr-training"
  image_id                             = data.aws_ssm_parameter.ubuntu.value
  instance_type                        = var.instance_type
  instance_initiated_shutdown_behavior = "terminate"
  update_default_version               = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.machine.arn
  }

  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  network_interfaces {
    # A public address only for outbound traffic (SSM, S3, packages); no inbound rule exists.
    associate_public_ip_address = true
    subnet_id                   = coalesce(var.subnet_id, sort(data.aws_subnets.default.ids)[0])
    security_groups             = [aws_security_group.machine.id]
    delete_on_termination       = true
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = var.disk_gb
      volume_type           = "gp3"
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
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      "tickr:role" = local.role_tag
      Name         = "tickr-training"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      "tickr:role" = local.role_tag
    }
  }
}

# --- Research user --------------------------------------------------------------------------
# Its access key is created by the owner by hand (README), so no secret ever sits in Terraform
# state or in this repository.

data "aws_iam_policy_document" "research" {
  statement {
    sid = "ReadOnlyDescribe"
    # No ec2:DescribeInstanceAttribute: it returns the user data of every instance, the bot's too.
    # The SSM command-output reads cannot be limited by resource (README).
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeSpotInstanceRequests",
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeLaunchTemplates",
      "ec2:DescribeLaunchTemplateVersions",
      "ec2:DescribeImages",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeVolumes",
      "ssm:DescribeInstanceInformation",
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
      "ssm:DescribeSessions",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "RunFromTemplateOnly"
    actions   = ["ec2:RunInstances"]
    resources = ["*"]
    condition {
      test     = "ArnLike"
      variable = "ec2:LaunchTemplate"
      values   = [aws_launch_template.training.arn]
    }
    condition {
      test     = "Bool"
      variable = "ec2:IsLaunchTemplateResource"
      values   = ["true"]
    }
    # The instance resource carries ec2:InstanceType; the others pass this check.
    condition {
      test     = "StringEqualsIfExists"
      variable = "ec2:InstanceType"
      values   = var.allowed_instance_types
    }
  }

  statement {
    sid       = "TagAtLaunch"
    actions   = ["ec2:CreateTags"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  statement {
    sid       = "SpotServiceRoleOnce"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["spot.amazonaws.com"]
    }
  }

  statement {
    sid       = "PassMachineRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.machine.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  statement {
    sid       = "StopOwnMachines"
    actions   = ["ec2:TerminateInstances", "ec2:StopInstances"]
    resources = ["arn:${local.partition}:ec2:${var.region}:${local.account}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/tickr:role"
      values   = [local.role_tag]
    }
  }

  statement {
    sid       = "SsmToOwnMachines"
    actions   = ["ssm:StartSession", "ssm:SendCommand"]
    resources = ["arn:${local.partition}:ec2:${var.region}:${local.account}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/tickr:role"
      values   = [local.role_tag]
    }
  }

  statement {
    sid     = "SsmDocuments"
    actions = ["ssm:StartSession", "ssm:SendCommand"]
    resources = [
      "arn:${local.partition}:ssm:${var.region}::document/AWS-RunShellScript",
      "arn:${local.partition}:ssm:${var.region}:${local.account}:document/SSM-SessionManagerRunShell",
    ]
  }

  statement {
    sid       = "OwnSessions"
    actions   = ["ssm:TerminateSession", "ssm:ResumeSession"]
    resources = ["arn:${local.partition}:ssm:*:*:session/$${aws:username}-*"]
  }
}

resource "aws_iam_user" "research" {
  name = "tickr-research"
}

# Managed policies, not inline ones: AWS caps all inline policies of one user at 2048 bytes
# together, and the training rules are longer. A managed policy may hold 6144 characters.
resource "aws_iam_policy" "research" {
  name   = "tickr-research-training-only"
  policy = data.aws_iam_policy_document.research.json
}

resource "aws_iam_user_policy_attachment" "research" {
  user       = aws_iam_user.research.name
  policy_arn = aws_iam_policy.research.arn
}

resource "aws_iam_policy" "research_bucket" {
  name   = "tickr-research-bucket"
  policy = data.aws_iam_policy_document.bucket_rw.json
}

resource "aws_iam_user_policy_attachment" "research_bucket" {
  user       = aws_iam_user.research.name
  policy_arn = aws_iam_policy.research_bucket.arn
}

# --- Budget ---------------------------------------------------------------------------------

resource "aws_budgets_budget" "account" {
  name         = "tickr-account-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
