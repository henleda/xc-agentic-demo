# scripts

| Script | Status | Purpose |
|---|---|---|
| `gen_policy.py` | **working** | Matrix → generated service-policy intent and consistency check |
| `gen_keys.py` | to build | Create the IdP RSA key and write `deploy/xc/generated/jwks.json` |
| `render_secrets.py` | to build | `.env` + generated client secrets → `deploy/vk8s/secrets/rendered/*.yaml` |
| `render_manifests.py` | to build | Substitute `${REGISTRY}`/`${IMAGE_TAG}`, render the `agent-configs` ConfigMap from `config/`, write `deploy/vk8s/rendered/` |
| `build_images.sh` | to build | Build multi-arch images, run each with `--user 1000770000:0` as a smoke check, push |
| `check_bedrock.py` | to build | Run on EC2: one Converse call per route through the VPC endpoint |
| `smoke_test.py` | to build | `--env local\|tenant [--no-security]`: health checks via LBs, one login, one tool call, one model call, ERP not resolvable from RE (via debug Job) |
| `run_scenario.py` | to build | `--all \| S1 S3 ... --env local\|tenant [--dry] [--discovery-seed]`: runs scenarios through the cockpit backend API or directly, asserts `expect`, writes `results/scenarios-<ts>.json` |
| `measure_latency.py` | to build | Measures RE→CE vs RE-local tool calls per RE and the 30-call loop overhead → `results/latency.json` |
| `reset_demo.sh` | to build | ERP reset, gateway usage reset, new `RUN_SALT`, wait for rate-limit windows; under 60 s |
| `preflight.sh` | to build | Reset + smoke + `run_scenario --all --dry` + print the go/no-go table |
