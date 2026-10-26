# XC Terraform (tenant f5-amer-ent)

Provider `volterraedge/volterra` **0.13.2** (pinned in `providers.tf`; the lock file is committed). Every field was
mapped against the provider's real schema (CLAUDE.md rule 2); the dump of the resources in use is written to
`../generated/schema-0.13.2/` (gitignored) by:

```bash
terraform -chdir=deploy/xc/terraform providers schema -json \
  | jq '.provider_schemas[].resource_schemas["volterra_aws_vpc_site"]'
```

## Run it

Credentials never go in a file in git. The API certificate is `~/.xc/api-creds.p12` (mode 600); its password is in
the repo-root `.env` (`scripts/set_xc_password.sh` puts it there). From the repo root:

```bash
source scripts/xc_env.sh                     # exports VES_P12_PASSWORD and the TF_VAR_* from .env (bash or zsh)
terraform -chdir=deploy/xc/terraform init
terraform -chdir=deploy/xc/terraform plan    # then apply
```

## Files

| File | What | Phase |
|---|---|---|
| `providers.tf` | Provider, API URL and certificate path from variables | 0 |
| `infra.tf` | Namespace `agentic-demo`, AWS CE site `copperline-aws-ce` (App Stack, one interface: docs/08 R30) and the action that launches it, virtual sites `vs-ce-aws` and `vs-re-agents`, vK8s `agentic-demo-vk8s` | 0 |
| `pools.tf`, `lbs_public.tf`, `lbs_internal.tf` | Origin pools and load balancers, routing only (security blocks off) | 2 |
| `security.tf`, `policies_generated.tf` | JWT, service policies (rendered by `scripts/gen_policy.py --emit terraform`), WAF, rate limits, API definitions | 3 |

Apply in this order: `infra.tf` → pools → load balancers with security blocks off (Phase 2) → security (Phase 3).

## Phase 0 status (2026-10-26)

Not up yet (docs/08 R45). The site object `copperline-aws-ce` exists in `system` but failed validation: the AWS cloud
credential it named (`henley-3g-aws`) has a deleted access key. And the certificate may not create namespaces (403),
so `agentic-demo` needs a tenant admin. Nothing was built in AWS. With a working credential in `XC_AWS_CRED_NAME` and
the namespace in place, `terraform apply` updates the site in place and continues.

The AWS cloud credential is made in the console, not here: in Terraform its secret would be stored in state. It needs
either an access key (an IAM user, which the SSO `Users` role cannot create) or Assume Role, whose trust policy names
F5 XC's AWS account (a support ticket) with external ID = the tenant ID `f5-amer-ent-qyyfhhfj`. The IAM policy XC
needs is on F5's "AWS VPC Policies and Permissions Reference" page.

Console-only steps are in `../MANUAL_STEPS.md`.
