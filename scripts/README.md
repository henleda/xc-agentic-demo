# scripts

| Script | Status | Purpose |
|---|---|---|
| `gen_policy.py` | **working** | Matrix → generated service-policy intent and consistency check (agent configs, rate-limit sync). Tests in `scripts/tests/` |
| `gen_keys.py` | **working** | Create the IdP RSA key, every client secret and the AI gateway key in `.secrets/` (gitignored), and write `deploy/xc/generated/jwks.json`. See below |
| `render_secrets.py` | **working** | `.secrets/` + `.env` → `deploy/vk8s/secrets/rendered/*.yaml` (gitignored), from the templates in `deploy/vk8s/secrets/` |
| `render_manifests.py` | to build (Phase 2) | Substitute `${REGISTRY}`, `${IMAGE_TAG}` and `${EXFIL_TEST_URL}` (S5 exits 2 without it) from `.env`, render the `agent-configs` ConfigMap from `config/`, write `deploy/vk8s/rendered/`. Fail on any `${...}` left unrendered |
| `build_images.sh` | to build (Phase 2) | Build multi-arch images, run each with `--user 1000770000:0` as a smoke check, push |
| `check_bedrock.py` | to build | Run on EC2: one Converse call per route through the VPC endpoint |
| `smoke_test.py` | **working** (`--env local`); tenant in Phase 2 | Go/no-go for every hop: every LB answers, admin ports on loopback, one login, one chat whose token exchange, tool call and model call all succeed, ERP admin not reachable through erp-lb. `make local-smoke`. Tenant (Phase 2): the same through the public front doors, plus ERP not resolvable from an RE pod and `--no-security` |
| `run_scenario.py` | **working** (`--env local`); tenant in Phase 2 | `--all \| S1 S3a ... --env local [--runs N] [--dry]`: runs each scenario the way the stage does it (agent chats through the front doors with `scenario_hint`, S5 as the compose job, S6 played by this script through partner-lb), checks `expect.<env>`, writes `results/scenarios-<ts>.json`. S4 and S6 run last; between runs it waits 65 s after the last S4/S6 call. "No expectations" never counts as a pass. `make local-scenarios` (`RUNS=3` for three runs in a row locally; the definition of done is the same against the tenant, `make scenarios`). `--dry` expands every plan and sends nothing. Tenant runs and `--discovery-seed` come with the cockpit (Phase 2) |
| `measure_latency.py` | to build (Phase 4) | Measures RE→CE vs RE-local tool calls per RE and the 30-call loop overhead → `results/latency.json` |
| `reset_demo.sh` | to build | ERP reset, gateway usage reset, new `RUN_SALT`, wait for rate-limit windows; under 60 s |
| `preflight.sh` | to build | Reset + smoke + `run_scenario --all --dry` + print the go/no-go table |

## `gen_keys.py`

```bash
uv run python scripts/gen_keys.py               # create anything missing; never overwrites (except the .env override below)
uv run python scripts/gen_keys.py --if-missing  # same; quiet when everything already exists (make local-up uses this)
uv run python scripts/gen_keys.py --rotate      # replace the key and every secret (all tokens become invalid)
uv run python scripts/gen_keys.py --root DIR    # work on another checkout (tests use a tmp dir); also on render_secrets.py
```

`--if-missing` and `--rotate` can't be combined. Exit codes: 0 means OK; 2 means bad input, and then **nothing is written**. Bad input is one of these:

- `personas.yaml` is missing, or lists no client of kind `agent`, `service` or `partner`. A typo such as `clientz:` must not wipe every secret.
- An existing key won't load and `--rotate` wasn't given.
- A value can't go in an env file.

It writes the following files. Everything under `.secrets/` is mode 0600, inside a 0700 directory.

| File | Content | Used by |
|---|---|---|
| `.secrets/idp-signing-key.pem` | RSA 2048 private key (PKCS#8 PEM) | idp (`SIGNING_KEY_PEM` / `SIGNING_KEY_PATH`) |
| `.secrets/secrets.env` | One `NAME=value` line per secret: `AGENT_SHOPPER_SECRET`, `AGENT_STOREOPS_SECRET`, `AGENT_REFUNDS_SECRET`, `SVC_MCP_SECRET`, `GRANITE_SUPPLY_SECRET`, `GATEWAY_API_KEY`, plus `CLIENT_SECRETS_JSON` (all client secrets as one JSON object, for the idp) | `deploy/local/compose.sh` (`--env-file`: compose variables only; `docker-compose.yaml` gives each container just the secrets it needs, so never use a service-level `env_file:` for it), `render_secrets.py`, `run_scenario.py`, `make local-check` |
| `deploy/xc/generated/jwks.json` | The public JWKS (gitignored; pasted into the XC JWT validation configs in Phase 3) | XC LBs, local-gateway, erp-mock |

- **Client list.** The secret names come from `config/personas.yaml` `clients` (kinds `agent`, `service` and `partner`), via `credentials.secret_env_name`. A new client therefore gets a secret automatically.
- **Values.** Secrets are `secrets.token_urlsafe(32)`.
- **`.env` override.** If `.env` already sets `GRANITE_SUPPLY_SECRET` or `GATEWAY_API_KEY`, that value is used instead of a generated one, so the cockpit and the vK8s Secret always agree.
  - This is the one exception to "never overwrites". On every run, including `--rotate`, a non-empty `.env` value replaces whatever `secrets.env` holds for that name, and `CLIENT_SECRETS_JSON` is rebuilt to match.
  - An empty value, such as the `GRANITE_SUPPLY_SECRET=   # comment` line copied from `.env.example`, does not count as set.
- **"Never overwrites"** means the following:
  - A run without `--rotate` keeps the existing key and every existing secret value byte for byte.
  - A file that is missing is recreated. If `secrets.env` or `jwks.json` is deleted, it is rebuilt from the existing key.
  - A client added to `personas.yaml` later gets a new secret, and every other value is kept.
  - A secret whose client is no longer in `personas.yaml` (removed, or its `client_id` mistyped) is **kept** in `secrets.env`, but left out of `CLIENT_SECRETS_JSON`, so the idp stops accepting it. Putting the client back restores the same value, so nothing is accidentally rotated. Each run prints a warning on stderr that names the kept secrets, never their values. Only `--rotate` drops them, and it says so too.
  - `jwks.json` is always rewritten from the current key when its content differs.
- **`secrets.env` format.** It starts with a `#` header, followed by one unquoted `NAME=value` line per secret: client secrets in `personas.yaml` order, then `GATEWAY_API_KEY`, then any kept secrets of clients no longer in `personas.yaml`, then `CLIENT_SECRETS_JSON` as compact JSON. Lines are unquoted because `docker run --env-file` keeps quotes literally.
  - A value that can't be written unquoted exits 2. That covers a newline, `$` (compose would interpolate it), ` #`, or surrounding quotes or spaces. In practice this guards the `.env` overrides.
- **Modes and writes.** Existing files are tightened to 0600 and the directory to 0700. Files are written atomically: a 0600 temp file, then a rename. No secret value is ever printed.

## `render_secrets.py`

It fills the `{{ NAME }}` placeholders in `deploy/vk8s/secrets/*.template.yaml` from `.secrets/secrets.env`, the PEM, and `.env`. The output goes to `deploy/vk8s/secrets/rendered/`.

- **Placeholder names.** `{{ ALL_CLIENT_SECRETS_JSON }}` is filled from `CLIENT_SECRETS_JSON`, and `{{ IDP_PRIVATE_KEY_PEM }}` from `.secrets/idp-signing-key.pem`. Every other placeholder name is looked up in `.secrets/secrets.env`, then `.env`. When both have the name, `secrets.env` wins.
- **Missing values.** Any placeholder with no value is an error, and an empty value counts as missing. The error names the placeholder, never the value.
- **Drift.** If `.env`'s `GRANITE_SUPPLY_SECRET` or `GATEWAY_API_KEY` differs from `secrets.env`, `.env` was edited after `gen_keys.py` last ran, and the cockpit would disagree with the Secret. That is an error too; re-run `gen_keys.py`.
- **All or nothing.** On any error, across all templates, the exit code is 1 and nothing is written. Output from an earlier run is left as it was.
- **Formatting.** A placeholder's surroundings decide how its value is escaped:
  - **Alone on its line** (the PEM under `private.pem: |`): every line of the value gets that line's indentation, which fits the YAML block scalar.
  - **Inside `"…"`:** the value is JSON-string escaped. This is required for `_all_json: "{{ ALL_CLIENT_SECRETS_JSON }}"`, whose JSON is full of `"`.
  - **Inside `'…'`:** single quotes are doubled.
  - **Elsewhere:** inserted as is, and it must be a single line.
  - Each rendered file is re-parsed with PyYAML before anything is written, and every value must come back out unchanged.
- **Output.** One `<name>.yaml` per template in `deploy/vk8s/secrets/rendered/` (mode 0700), each file 0600. Files rendered from a template that has since been removed are not deleted.
- **Phase 1.** Nothing in this phase applies the output. `make deploy-vk8s` uses it in Phase 2.

## `run_scenario.py` and `smoke_test.py`

Both read endpoints from `scripts/demo_env.py`: locally the published 127.0.0.1 ports of the compose stack (front doors 8001–8003, internal LBs 8011–8014, admin 8181/8190). S6's client secret comes from `GRANITE_SUPPLY_SECRET` in the shell, else `.secrets/secrets.env`; nothing is printed.

```bash
make local-up                         # on the devbox: make devbox-run CMD="make local-up"
make local-smoke                      # every hop, a few seconds
make local-scenarios                  # every scenario once
RUNS=3 make local-scenarios           # three runs in a row, as the definition of done asks of the tenant (~4 min)
uv run python scripts/run_scenario.py S1 S3a --env local
```

A scenario's verdict is:

- `PASS`: every check holds, the agent ran the scenario that was asked for, and the scripted steps match the plan in `scenarios/`.
- `FAIL`: it ran and a check did not hold. Each failing check is printed, followed by the steps behind it (status, who blocked it, why), so a null status caused by a refused token earlier reads as that.
- `ERROR`: it could not run properly: a login failed, a hop was unreachable or timed out, the S5 job exited non-zero, the agent ran a different scenario or its normal loop, or **the container runs another version of the plan** (an image older than an edit to `scenarios/`: rebuild with `make local-up`). Never counted as a pass, whatever a partial trace shows.
- `--`: no expectations for this environment (S7 locally). Never counted as a pass.

The exit code is 0 only if at least one scenario was checked and all of them passed. Each `--runs` run logs in with a fresh `RUN_SALT` (printed), so XC's malicious-user state never carries over. The results file (every check and the full trace) is written after every scenario, so an interrupted run keeps what it had. On the devbox, `make devbox-results` copies it to the laptop; the sync never deletes it on the box.

`smoke_test.py` sends a real request through each load balancer and requires the service behind it to answer, so `ok` means the hop works: every agent, MCP server, partner-api, the IdP and the ERP. Its chat is S0 (scripted), so the same steps run in stub mode and on real Bedrock, and nothing depends on an agent's token cache, so running it twice in a row gives the same result.
