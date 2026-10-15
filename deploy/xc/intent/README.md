# XC intent files

This folder holds human-readable, provider-agnostic intent for every XC object. Read it alongside `docs/04-xc-configuration.md`.

Claude Code translates these files into Terraform under `../terraform/` using the `volterraedge/volterra` provider. Before writing any resource, check the provider schema for it (see `CLAUDE.md` rule 2).

- `service-policies.generated.yaml` is **generated** by `scripts/gen_policy.py`. Do not edit it by hand.
- Placeholders written `${...}` come from `.env`.
- Fields marked `verify: true` are not confirmed against a live tenant. See `docs/08-risks-and-verification.md`.
