# AWS (F5 account): Terraform to be written by Claude Code

## Resources

- **VPC** `copperline-demo` with CIDR `10.50.0.0/16` and three subnets:
  - `ce-outside` (public, with IGW): `10.50.1.0/24`
  - `ce-inside` (private): `10.50.2.0/24`
  - `core` (private): `10.50.3.0/24`
- **NAT gateway** for `core`. The cockpit needs outbound internet (public XC LBs for login, chat and partner calls; the vK8s API for S5), and the host pulls images. S5 does not depend on it, because vK8s pods sit on the CE, not in `core`.
- **Interface VPC endpoints** in `core`, with private DNS on:
  - `com.amazonaws.<region>.bedrock-runtime`
  - optionally `ecr.api`, `ecr.dkr` and `s3`, if ECR is the registry
- **EC2** `copperline-core`:
  - t3.large, AL2023, Docker plus compose, in `core`
  - SSM access only (no SSH)
  - Instance role allows `bedrock:InvokeModel*` (this also authorizes Converse; there is no `bedrock:Converse` action) on the three route inference profiles `arn:aws:bedrock:us-east-1:<account>:inference-profile/us.anthropic.claude-{haiku,sonnet,opus}-5-5` **and** on `arn:aws:bedrock:{us-east-1,us-east-2,us-west-2}::foundation-model/anthropic.claude-{haiku,sonnet,opus}-5-5`, the Regions the profiles route to (`aws bedrock get-inference-profile`). Both are checked on every call (docs/07 Phase 0 step 3; `deploy/devbox/main.tf` is a working, broader policy). No AWS Marketplace permissions: the account's model subscriptions already exist (docs/08 R24)
- **Security group** `sg-core`: ingress 8081, 8090 and 8095 from `10.50.2.0/24` (CE inside) only.
- **S3 bucket** `copperline-demo-xc-logs` (optional, for the XC global log receiver).

## Notes

- **The CE itself** is created by XC (`volterra_aws_vpc_site`) inside this VPC, using the subnets above. See `deploy/xc/intent/00-infra.yaml` and R1.
- **Outputs** go to `.env`: VPC ID, subnet IDs and `CORE_EC2_PRIVATE_IP`.
- **Tag everything** `owner=<you>`, `project=xc-agentic-demo`, `expires=<date>`, following F5 AWS account hygiene.
