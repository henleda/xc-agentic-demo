#!/usr/bin/env bash
# docker compose for the local stack (NOT XC). Pass any compose arguments:
#   deploy/local/compose.sh up -d --build --wait     (what `make local-up` runs)
#   deploy/local/compose.sh ps | logs -f tools-lb | down -v
#
# Before every call it:
#   1. creates the IdP key and client secrets if they are missing (scripts/gen_keys.py --if-missing);
#   2. stages deploy/local/.run/ (gitignored) with the files the containers mount, made world-readable, because the
#      containers run as an arbitrary UID and the checkout's files may be 0600. config/ is flat, like the vK8s
#      ConfigMap. Nothing secret is staged: the JWKS there is the PUBLIC key;
#   3. hands the secrets to compose as variables only: .secrets/secrets.env via --env-file, and the IdP signing key
#      via the environment. docker-compose.yaml gives each container only the secrets it needs;
#   4. exports the few non-secret .env settings ai-gw uses (LOCAL_STUB_MODEL, AWS_REGION, ROUTE_*, BUDGET_TOKENS_*;
#      see local_env.py). The shell wins over .env. LOCAL_STUB_MODEL=false switches ai-gw to real Bedrock, which
#      works on the devbox only: its credentials come from the instance role.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUN="$ROOT/deploy/local/.run"
cd "$ROOT"

uv run python scripts/gen_keys.py --if-missing

# Copy over the files in place, never delete the directories: running containers have them bind-mounted.
mkdir -p "$RUN/config" "$RUN/gateway" "$RUN/keys"
cp config/personas.yaml config/tools/tools.yaml config/policy/tool-access-matrix.yaml config/agents/*.yaml "$RUN/config/"
cp deploy/xc/intent/20-load-balancers.yaml deploy/xc/intent/30-security.yaml \
   deploy/xc/intent/service-policies.generated.yaml deploy/local/pools.yaml "$RUN/gateway/"
cp deploy/xc/generated/jwks.json "$RUN/keys/"
chmod -R a+rX "$RUN"

SIGNING_KEY_PEM="$(cat .secrets/idp-signing-key.pem)"
export SIGNING_KEY_PEM
settings="$(uv run python deploy/local/local_env.py .env)"   # exits 2 on a half-configured Bedrock switch
while IFS= read -r line; do [ -n "$line" ] && export "$line"; done <<< "$settings"
exec docker compose -f deploy/local/docker-compose.yaml --env-file .secrets/secrets.env "$@"
