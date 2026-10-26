# The AWS cloud credential for the XC CE site, as an assume role: F5 XC's own AWS account assumes this role to build
# the CE (EC2, its IAM role, security groups) in our VPC. No access key exists anywhere. docs/08 R45; the SSO role
# can create IAM roles but not IAM users or keys, and an SSO session key cannot be an XC credential.
#
# Created only when var.f5xc_aws_account_id is set (F5 publishes it only through a support ticket). Then, in the XC
# console: Cloud Credentials -> Add -> AWS Assume Role, IAM Role ARN = output xc_assume_role_arn, and set
# XC_AWS_CRED_NAME in .env to that credential's name.

variable "f5xc_aws_account_id" {
  type        = string
  default     = ""
  description = "F5 Distributed Cloud's AWS account number (12 digits, from F5 support). Empty: no role is created"
}

variable "xc_tenant_id" {
  type        = string
  default     = "f5-amer-ent-qyyfhhfj"
  description = "XC tenant ID (not the tenant name): XC sends it as the sts:ExternalId"
}

locals {
  xc_role = var.f5xc_aws_account_id == "" ? 0 : 1
}

resource "aws_iam_role" "xc_site" {
  count = local.xc_role
  name  = "copperline-xc-site"
  # Trust policy as on F5's "AWS VPC Policies and Permissions Reference" page.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.f5xc_aws_account_id}:root" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = { StringEquals = { "sts:ExternalId" = var.xc_tenant_id } }
    }]
  })
}

# F5's AWS VPC site policy (4 statements, 181 actions), from the same page. It must be this wide: XC creates the CE's
# own instance, IAM role and security groups. The IAM statement is the one the console-made policy copperline-xc lacks.
resource "aws_iam_role_policy" "xc_site" {
  count  = local.xc_role
  name   = "f5xc-aws-vpc-site"
  role   = aws_iam_role.xc_site[0].id
  policy = file("${path.module}/xc-site-policy.json")
}

output "xc_assume_role_arn" {
  value       = local.xc_role == 1 ? aws_iam_role.xc_site[0].arn : "not created: set f5xc_aws_account_id"
  description = "Paste into XC: Cloud Credentials -> AWS Assume Role -> IAM Role ARN"
}
