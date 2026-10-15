# Developer workstation for building and running the demo (NOT part of the demo topology).
# No inbound ports: SSH is tunneled through SSM Session Manager. Destroy with `make devbox-destroy`.
# See deploy/devbox/README.md.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  type    = string
  default = "t3.xlarge"
}

variable "owner" {
  type        = string
  description = "Tag value for owner (F5 AWS account hygiene)"
}

variable "expires" {
  type        = string
  description = "Tag value for expires, YYYY-MM-DD"
}

variable "ssh_public_key" {
  type        = string
  description = "Public key allowed for the ubuntu user (private key stays on the developer's laptop)"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project = "xc-agentic-demo"
      owner   = var.owner
      expires = var.expires
      purpose = "devbox"
    }
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "devbox" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = "${var.region}a"
  default_for_az    = true
}

# Ubuntu 24.04 LTS x86_64: same CPU architecture as XC REs and the AWS CE.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_iam_role" "devbox" {
  name = "copperline-devbox-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.devbox.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Bedrock for the Phase 1 close-out (S0 against real models). Converse is authorized by the InvokeModel actions.
resource "aws_iam_role_policy" "bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.devbox.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      Resource = [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.me.account_id}:inference-profile/*anthropic.*",
      ]
    }]
  })
}

data "aws_caller_identity" "me" {}

resource "aws_iam_instance_profile" "devbox" {
  name = "copperline-devbox-profile"
  role = aws_iam_role.devbox.name
}

# No ingress at all. Egress is open so the box can pull packages and images and reach Bedrock.
resource "aws_security_group" "devbox" {
  name        = "copperline-devbox-sg"
  description = "copperline devbox: no inbound, SSM only"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "devbox" {
  ami                    = data.aws_ssm_parameter.ubuntu.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.devbox.id
  vpc_security_group_ids = [aws_security_group.devbox.id]
  iam_instance_profile   = aws_iam_instance_profile.devbox.name
  user_data              = templatefile("${path.module}/cloud-init.sh", { ssh_public_key = var.ssh_public_key })

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 2          # containers on the box can use the instance role (ai-gw -> Bedrock)
  }

  root_block_device {
    volume_size = 100
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "copperline-devbox" }

  lifecycle {
    ignore_changes = [ami] # a newer Ubuntu image must not replace a box with work on it
  }
}

output "instance_id" {
  value = aws_instance.devbox.id
}
