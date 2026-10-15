# Terraform (to be written by Claude Code)

Use provider `volterraedge/volterra`. Check the latest version in the registry before pinning it.

Resources the intent maps to (all confirmed to exist in the provider docs):

- `volterra_aws_vpc_site` (or `volterra_securemesh_site_v2`; see R1)
- `volterra_virtual_site`
- `volterra_virtual_k8s`
- `volterra_origin_pool`
- `volterra_http_loadbalancer`
- `volterra_service_policy`
- `volterra_app_firewall`
- `volterra_rate_limiter_policy`: confirm the schema
- `volterra_api_definition`
- `volterra_forward_proxy_policy`: fallback for S5 egress

Suggested layout:

- `providers.tf`
- `infra.tf`
- `pools.tf`
- `lbs_public.tf`
- `lbs_internal.tf`
- `security.tf`
- `policies_generated.tf`: rendered from `../intent/service-policies.generated.yaml` by `scripts/gen_policy.py --emit terraform`, once the schema is confirmed

Credentials: an API certificate (p12) path and password, taken from `.env`. Never commit them.

Apply in this order: `infra.tf` → `pools.tf` → load balancers with security blocks commented out (Phase 2) → security (Phase 3).
