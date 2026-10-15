# Remote dev server (devbox)

This is a single EC2 instance used to **build and run** the demo during development. It is not part of the demo topology: it lives in the account's default VPC, not in the demo VPC from `deploy/aws`.

## Why

- **Same CPU architecture as XC.** REs and the AWS CE run x86_64, and so does the box, so images built here match what Phase 2 deploys.
- **Real Bedrock without laptop credentials.** The instance role is allowed to invoke Anthropic models on Bedrock.
- **The laptop stays light.** Docker and the roughly 15 demo containers run on the box.

## What it is

| Setting | Value |
|---|---|
| Instance | `copperline-devbox`, t3.xlarge (4 vCPU, 16 GB), Ubuntu 24.04 x86_64, 100 GB gp3 (encrypted), us-east-1a |
| Network | Default VPC, public subnet for outbound only. The security group has **no inbound rules**, and IMDSv2 is required |
| Access | SSH **tunneled through SSM Session Manager** (`AWS-StartSSHSession`). The key `~/.ssh/copperline-devbox` never leaves the laptop |
| IAM role | `copperline-devbox-role`: `AmazonSSMManagedInstanceCore` plus `bedrock:InvokeModel*` on Anthropic models and inference profiles |
| Software | Docker Engine with compose, uv, git, make, AWS CLI v2 (installed by `cloud-init.sh` on first boot) |
| Tags | `project=xc-agentic-demo`, `owner`, `expires`, `purpose=devbox` |
| Cost | About $0.17/hour (about $4/day) while running. Stop it from the EC2 console when it isn't needed, or destroy it |

## Workflow

Edit code and run git on the laptop. Run Docker, integration tests and Bedrock calls on the box.

```bash
make devbox-up                                         # create (or update) and wait until ready
make devbox-run CMD="make local-up"                    # rsync the tree, then run a command in the repo on the box
make devbox-run CMD="uv run python scripts/run_scenario.py --all --env local"
make devbox-tunnel                                     # browse the cockpit at http://localhost:8095
make devbox-ssh                                        # interactive shell
make devbox-destroy                                    # delete everything this created
```

- `rsync` mirrors the working tree to the box, excluding `.git`, `.venv` and caches. `.env` and `.secrets/` **are** copied, because the services need them. The box is private to you.
- Terraform state is local, in `deploy/devbox/terraform.tfstate`, and gitignored. Only the person who created the box can update or destroy it this way.
