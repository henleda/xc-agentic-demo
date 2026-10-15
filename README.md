# Copperline Outfitters: agentic AI on F5 Distributed Cloud

This is a live demo of F5 Distributed Cloud (XC) vK8s used as a runtime for agentic AI. Several agents, each with a different scope, privilege level and tool set, run on F5 Regional Edges and on a Customer Edge in AWS. The XC data path governs every call those agents make.

**The story in one line:** the model can be fooled and the agent code can be misconfigured, but the fabric still says no.

| | |
|---|---|
| Audience | Internal F5: PM, SEs, field leadership |
| Format | Live, about 20 minutes, scripted, with a recorded backup |
| Infra | One AWS CE (App Stack) in the F5 AWS account, three REs, Amazon Bedrock behind a customer-built AI gateway |
| Status | Spec and skeletons; build with Claude Code (see `CLAUDE.md`) |

Start with `docs/00-overview.md`. To build, open this folder in Claude Code and ask it to follow `CLAUDE.md`.

## Quick start (once built)

```bash
cp .env.example .env              # fill in tenant, domain, AWS, registry values
make check                        # lint, tests, policy consistency
make local-up                     # laptop mode, no XC
make deploy-aws deploy-vk8s deploy-xc
python scripts/smoke_test.py --env tenant
python scripts/run_scenario.py --all --env tenant
scripts/reset_demo.sh             # before every live run
```
