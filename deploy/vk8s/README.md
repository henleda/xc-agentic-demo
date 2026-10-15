# vK8s manifests (namespace `agentic-demo`)

**Never apply this folder directly**: the files contain `${REGISTRY}`/`${IMAGE_TAG}` placeholders and secret templates. Run `make deploy-vk8s`, which renders secrets to `deploy/vk8s/secrets/rendered/` and manifests to `deploy/vk8s/rendered/` (both gitignored), then applies secrets first and manifests second.

The rules below are hard requirements. They are repeated from `CLAUDE.md` so nobody misses them.

- **Placement:** every Deployment and CronJob must carry `ves.io/virtual-sites` with exactly one virtual site.
- **Security context:** run as non-root with an arbitrary UID; no privilege escalation; drop all capabilities; read-only root filesystem where the image allows it.
- **Networking:** container port 8080. No hostNetwork, hostPID or hostIPC.
- **Sizing:**
  - On REs, size workloads with `ves.io/workload-flavor`.
  - On the CE, resource requests apply instead. `TODO(verify-xc)`: confirm that the flavor annotation is ignored on CE.
- **Naming:** Service names are `svc-*`, so they never collide with internal LB names (`tools`, `llm`, `idp`, `erp`).
- **Images:** `${REGISTRY}` and `${IMAGE_TAG}` are substituted by `scripts/render_manifests.py` (envsubst-style). Never hard-code them.
