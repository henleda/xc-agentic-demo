# The demo's AWS side (docs/07 Phase 0 steps 1-3, deploy/aws/README.md): the VPC the XC CE runs in, the private
# "core" subnet with the EC2 host for the customer estate (ERP mock, AI gateway, cockpit), and Bedrock access.
# The CE instance itself is created by XC (volterra_aws_vpc_site) inside this VPC, not here.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "region" {
  type        = string
  default     = "us-east-2"
  description = "us-east-2, not us-east-1: the account's us-east-1 Elastic IP quota (5) is used up by other teams"
}

variable "az" {
  type        = string
  default     = "us-east-2a"
  description = "One zone for all three subnets: offers t3.large and m5.2xlarge, no cross-zone traffic CE <-> core"
}

variable "owner" {
  type        = string
  description = "Tag value for owner (F5 AWS account hygiene)"
}

variable "expires" {
  type        = string
  description = "Tag value for expires, YYYY-MM-DD"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project = "xc-agentic-demo"
      owner   = var.owner
      expires = var.expires
    }
  }
}

data "aws_caller_identity" "me" {}

# ---- Network: 10.50.0.0/16, three subnets in one zone ------------------------------------------------------------

resource "aws_vpc" "demo" {
  cidr_block           = "10.50.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true # needed for the Bedrock endpoint's private DNS
  tags                 = { Name = "copperline-demo" }
}

resource "aws_internet_gateway" "demo" {
  vpc_id = aws_vpc.demo.id
  tags   = { Name = "copperline-demo" }
}

resource "aws_subnet" "ce_outside" {
  vpc_id            = aws_vpc.demo.id
  cidr_block        = "10.50.1.0/24"
  availability_zone = var.az
  tags              = { Name = "copperline-ce-outside" }
}

resource "aws_subnet" "ce_inside" {
  vpc_id            = aws_vpc.demo.id
  cidr_block        = "10.50.2.0/24"
  availability_zone = var.az
  tags              = { Name = "copperline-ce-inside" }
}

resource "aws_subnet" "core" {
  vpc_id            = aws_vpc.demo.id
  cidr_block        = "10.50.3.0/24"
  availability_zone = var.az
  tags              = { Name = "copperline-core" }
}

# The CE's own subnet. XC creates and owns its route table (default route to the internet gateway, for the IPsec
# tunnels to the REs), so it must have NO route table association here: XC refuses a subnet with a custom route table
# ("The deployment will fail if the subnets have existing custom route table associations", F5 "Create AWS Site").
# ce-outside could not be used: it carries the NAT gateway for core, so it needs our own route to the internet.
resource "aws_subnet" "ce_site" {
  vpc_id            = aws_vpc.demo.id
  cidr_block        = "10.50.4.0/24"
  availability_zone = var.az
  tags              = { Name = "copperline-ce-site" }
}

# CE outside: public, through the internet gateway. The NAT gateway for core lives here too.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.demo.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.demo.id
  }
  tags = { Name = "copperline-public" }
}

resource "aws_route_table_association" "ce_outside" {
  subnet_id      = aws_subnet.ce_outside.id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "copperline-nat" }
}

resource "aws_nat_gateway" "core" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.ce_outside.id
  tags          = { Name = "copperline-core-nat" }
  depends_on    = [aws_internet_gateway.demo]
}

# Core: private, outbound through NAT (the cockpit reaches the public XC LBs and the vK8s API; the host pulls images).
resource "aws_route_table" "core" {
  vpc_id = aws_vpc.demo.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.core.id
  }
  tags = { Name = "copperline-core" }
}

resource "aws_route_table_association" "core" {
  subnet_id      = aws_subnet.core.id
  route_table_id = aws_route_table.core.id
}

# CE inside: private, no default route here. The XC site manages what it needs on its own interfaces; the inside
# subnet only has to reach core (same VPC, local route).
resource "aws_route_table" "ce_inside" {
  vpc_id = aws_vpc.demo.id
  tags   = { Name = "copperline-ce-inside" }
}

resource "aws_route_table_association" "ce_inside" {
  subnet_id      = aws_subnet.ce_inside.id
  route_table_id = aws_route_table.ce_inside.id
}

# ---- Bedrock interface endpoint in core (private DNS: bedrock-runtime resolves to it inside the VPC) ---------------

resource "aws_security_group" "bedrock_endpoint" {
  name        = "copperline-bedrock-endpoint"
  description = "bedrock-runtime interface endpoint: HTTPS from core only"
  vpc_id      = aws_vpc.demo.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_subnet.core.cidr_block]
  }
}

resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = aws_vpc.demo.id
  service_name        = "com.amazonaws.${var.region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.core.id]
  security_group_ids  = [aws_security_group.bedrock_endpoint.id]
  private_dns_enabled = true
  tags                = { Name = "copperline-bedrock-runtime" }
}

# ---- EC2 copperline-core ------------------------------------------------------------------------------------------

resource "aws_iam_role" "core" {
  name = "copperline-core-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "core_ssm" {
  role       = aws_iam_role.core.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# The three route models only (docs/07 Phase 0 step 3). A us. inference-profile call is authorized against the
# profile AND the foundation model in every Region the profile routes to; Converse is covered by InvokeModel.
# No AWS Marketplace permissions: the account's model subscriptions already exist (docs/08 R24).
locals {
  route_models    = ["anthropic.claude-haiku-5-5", "anthropic.claude-sonnet-5-5", "anthropic.claude-opus-5-5"]
  profile_regions = ["us-east-1", "us-east-2", "us-west-2"] # aws bedrock get-inference-profile --inference-profile-identifier us.<model>
}

resource "aws_iam_role_policy" "core_bedrock" {
  name = "bedrock-route-models"
  role = aws_iam_role.core.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      Resource = concat(
        [for m in local.route_models :
        "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.me.account_id}:inference-profile/us.${m}"],
        flatten([for r in local.profile_regions : [for m in local.route_models :
        "arn:aws:bedrock:${r}::foundation-model/${m}"]]),
      )
    }]
  })
}

resource "aws_iam_instance_profile" "core" {
  name = "copperline-core-profile"
  role = aws_iam_role.core.name
}

# Ingress from the CE subnets only: 8081 ERP, 8090 AI gateway, 8095 cockpit (the XC origin pools).
# The App Stack CE has ONE interface (docs/08 R30), in ce-site, so its origin traffic comes from 10.50.4.0/24.
# ce-outside and ce-inside stay allowed (an earlier layout and a two-interface site).
# The admin ports 8181/8190 are bound to 127.0.0.1 by compose and never opened here.
# (The description still says "inside": changing it would replace the group under the running instance.)
resource "aws_security_group" "core" {
  name        = "copperline-core-sg" # AWS refuses names that begin with sg-
  description = "copperline-core: ERP, AI gateway and cockpit from the CE inside subnet only"
  vpc_id      = aws_vpc.demo.id
  dynamic "ingress" {
    for_each = [8081, 8090, 8095]
    content {
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = [aws_subnet.ce_site.cidr_block, aws_subnet.ce_outside.cidr_block, aws_subnet.ce_inside.cidr_block]
    }
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "core" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = "t3.large"
  subnet_id              = aws_subnet.core.id
  vpc_security_group_ids = [aws_security_group.core.id]
  iam_instance_profile   = aws_iam_instance_profile.core.name
  user_data              = file("${path.module}/cloud-init.sh")

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 2          # containers use the instance role (ai-gw -> Bedrock)
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "copperline-core" }

  lifecycle {
    ignore_changes = [ami] # a newer AL2023 image must not replace a host with the demo on it
  }

  depends_on = [aws_route_table_association.core] # cloud-init needs NAT to install Docker
}

# ---- Outputs: copy into .env (deploy/aws/README.md) ---------------------------------------------------------------

output "env" {
  description = "Lines for the repo-root .env"
  value       = <<-EOT
    AWS_VPC_ID=${aws_vpc.demo.id}
    AWS_SUBNET_CE_OUTSIDE=${aws_subnet.ce_outside.id}
    AWS_SUBNET_CE_SITE=${aws_subnet.ce_site.id}
    AWS_SUBNET_CE_INSIDE=${aws_subnet.ce_inside.id}
    AWS_SUBNET_CORE=${aws_subnet.core.id}
    CORE_EC2_PRIVATE_IP=${aws_instance.core.private_ip}
  EOT
}

output "core_instance_id" {
  value = aws_instance.core.id
}
