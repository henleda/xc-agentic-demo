# idp: demo identity provider

This is **not** a real IdP. It is about 200 lines of FastAPI that mint RS256 JWTs. Identity semantics are in `docs/02-agents.md`. It runs on the CE as `svc-idp`, behind `idp-lb`, `shop-lb`, `ops-lb` and `partner-lb`.

## Endpoints

| Endpoint | Input | Output |
|---|---|---|
| `POST /auth/demo-login` | JSON `{"sub": str, "run_salt": str?}`. `sub` must be a persona in `personas.yaml`. If the persona is `salted: true` and `run_salt` is non-empty, the token `sub` becomes `<sub>-<run_salt>` | `200 {"access_token", "token_type":"Bearer", "expires_in":1800, "sub", "role", "name"}`. The user token is valid for 30 min |
| `POST /auth/token` | Form: `grant_type=client_credentials`, `client_id`, `client_secret`, `audience` | `200 {"access_token","token_type":"Bearer","expires_in"}`. Claims depend on the client kind (see below) |
| `POST /token/exchange` | Form: `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, `subject_token`, `subject_token_type` (optional), `client_id`, `client_secret`, `requested_audience` | `200 {"access_token","issued_token_type":"urn:ietf:params:oauth:token-type:jwt","token_type":"Bearer","expires_in":300}` |
| `GET /.well-known/jwks.json` | — | The public JWKS: one RS256 key with a `kid` |
| `GET /healthz` | — | `200 {"status":"ok","service":"idp"}` |

There is no other route. `idp-lb` forwards only the three token paths and `/.well-known/*`, so anything else is a 404 from the IdP. That includes FastAPI's `/docs`, `/redoc` and `/openapi.json`, which are turned off. A 404 or 405 has the same JSON shape as a refusal: `{"error": "not_found" | "method_not_allowed", "error_description": str}`.

**`run_salt`** (Mallory's per-run suffix, so XC malicious-user state never carries over between runs) must match `[A-Za-z0-9_]{1,32}` when the persona is `salted: true`. Anything else is 400 `invalid_request`. A `-` is not allowed, because `Personas.base_sub()` splits `mallory.x-<salt>` on the **last** `-`, and a dash in the salt would stop the MCP servers from mapping her back to customer `C-1066`. For a persona without `salted: true`, `run_salt` is ignored and not checked. `reset_demo.sh` should generate it with something like `secrets.token_hex(4)`.

Every token response, including refusals, carries `Cache-Control: no-store` (RFC 6749 §5.1).

## Token claims (all flat strings; XC matches on these)

| Token | Claims (plus `iss`, `iat`, `exp`, `jti` on every token) | `aud` | TTL |
|---|---|---|---|
| User (`/auth/demo-login`) | `sub`, `role`, `name`, `customer_id` (only when the persona has one) | the persona's first `audiences` entry: `copperline-shop` or `copperline-ops` | 1800 s |
| Agent (`/token/exchange`) | `sub` = `agent_id` = the client's `agent_id`, `agent_role`, `obo_sub` = the user token's `sub` (salt kept), `obo_role` = the user token's `role` | `requested_audience` | 300 s |
| Machine-mode agent (`/auth/token`, agent client) | same as an agent token, but `obo_sub` = `"none"` and `obo_role` = `"none"`, as **literal strings** (docs/08 R35) | `audience` | 300 s |
| Service (`/auth/token`, service client) | `sub` = the client's `sub`, `svc_role` | `audience` | 300 s |
| Partner (`/auth/token`, partner client) | `sub` = the client's `sub`, `role` | `audience` | 300 s |

The claim lists are exhaustive: a token carries exactly these claims and nothing else from `personas.yaml` (for example, Jordan Lee's `store_id` is not in his user token).

`iss` comes from env `ISSUER` (default `https://idp.copperline.demo`, which matches the XC JWT profiles in `deploy/xc/intent/20-load-balancers.yaml`).

## Rules and error responses (OAuth 2.0 style: `{"error": str, "error_description": str}`)

Checks run in this order. The first failure wins.

| Situation | Status | `error` |
|---|---|---|
| Form body missing a required field, or the wrong `grant_type` | 400 | `invalid_request` / `unsupported_grant_type` |
| `client_id` unknown, or `client_secret` wrong or missing (constant-time compare) | 401 | `invalid_client` |
| `/auth/token`: `audience` not allowed for this client (see below) | 400 | `invalid_target` |
| `/token/exchange`: the client is not an agent client | 400 | `unauthorized_client` |
| `/token/exchange`: `subject_token` fails verification (signature against our own key, `iss`, `exp`; any audience) | 400 | `invalid_grant` |
| `/token/exchange`: `subject_token` is not a user token (it has an `agent_id` or `svc_role` claim) | 400 | `invalid_grant` |
| `/token/exchange`: `requested_audience` is not `copperline-tools` or `copperline-llm` | 400 | `invalid_target` |
| `/token/exchange`: the user's `role` is not in the agent's `allowed_user_roles` | **400** | **`unauthorized_user_role`** |
| `/auth/demo-login`: the body is not a JSON object with a non-empty string `sub` (and, if present, a string `run_salt`) | 400 | `invalid_request` |
| `/auth/demo-login`: unknown `sub`, or a `sub` that already carries a salt (the match is exact, so `mallory.x-r7` and `alex.rivera-r7` are both unknown) | 404 | `unknown_user` |
| `/auth/demo-login`: the persona is `salted: true` and `run_salt` is non-empty but not `[A-Za-z0-9_]{1,32}` | 400 | `invalid_request` |

How the form checks (first row) read:
- **Required fields.** `/auth/token`: `grant_type`, `client_id`, `audience`. `/token/exchange`: `grant_type`, `subject_token`, `client_id`, `requested_audience`. An empty value counts as missing. A body that is not a form (JSON, for example) reads as an empty form.
- **`grant_type`.** Missing or empty is `invalid_request`. Present but not this endpoint's grant is `unsupported_grant_type`.
- **`client_secret`** is not a form-check field. A missing or empty secret is 401 `invalid_client` (second row).

What counts as a user token (the `invalid_grant` row): **only** the absence of `agent_id` and `svc_role`, plus a non-empty string `sub` and `role` (a token without them is `invalid_grant` too). A partner token has neither `agent_id` nor `svc_role`, so it passes this check. It is still refused, with `unauthorized_user_role`, because no agent's `allowed_user_roles` contains `partner`. Don't add `partner` to an agent's `allowed_user_roles`.

**Allowed audiences per client kind:**
- **agent:** `copperline-tools` and `copperline-llm`. Machine mode is allowed: real IdPs allow it, and S5 depends on it.
- **service:** the client's `audiences` list from `personas.yaml`.
- **partner:** the client's `audiences` list from `personas.yaml`.
- **bench:** not issued in Phase 1. Client authentication runs first, and `gen_keys.py` creates no bench secret, so in Phase 1 bench actually gets 401 `invalid_client`. If a bench secret is configured, it gets 400 `unauthorized_client` (checked after `invalid_client` and before `invalid_target`) until Phase 4.

## Where values come from

| Value | Source |
|---|---|
| Personas and clients | `${CONFIG_DIR}/personas.yaml` |
| An agent's `allowed_user_roles` | `${CONFIG_DIR}/tool-access-matrix.yaml`, `agents.<agent_id>.allowed_user_roles`. If that file does not exist, `${CONFIG_DIR}/policy/tool-access-matrix.yaml` is used, so `CONFIG_DIR=config` works from a repo checkout. If neither exists, the IdP refuses to start. An agent client whose `agent_id` is not in the matrix has no allowed user roles, so every exchange for it fails |
| Client secrets | Env `CLIENT_SECRETS_JSON` (`{"agent-shopper": "...", ...}`). If it is unset or empty, each client's secret comes from `credentials.client_secret(client_id)`, i.e. env `AGENT_SHOPPER_SECRET` and so on. If it is set but is not a JSON object of strings, the IdP refuses to start, and the error never echoes the value. A client with no secret configured always fails with `invalid_client` |
| Signing key | Env `SIGNING_KEY_PEM` (the PEM text), or else a file at `SIGNING_KEY_PATH`. An empty `SIGNING_KEY_PEM` counts as unset. The IdP refuses to start without a usable key |
| Issuer | Env `ISSUER` |

`deploy/vk8s/30-platform.yaml` also sets `PERSONAS`. The IdP does not read it: it uses `CONFIG_DIR`, which defaults to `/config`, the ConfigMap mount.

## Logging

Every issued token is logged as `token_issued`, with `grant`, `client_id`, `aud`, `sub`, `agent_role`, `obo_sub`, `obo_role` and `jti`. Every refusal is logged as `token_refused`, with `grant`, `client_id`, `error` and `status`. Tokens and secrets are never logged.

- **`grant`** is `demo_login`, `client_credentials` or `token_exchange`.
- **`client_id`** is `null` for `demo_login`, which has no client.
- **Extra fields.** `token_issued` also carries `agent_id` and `status: 200`. `token_refused` also carries `reason`, which is the `error_description`.
- **Startup.** One `idp_ready` line carries the issuer, the `kid` and `unconfigured_clients`: issuable clients with no secret, which will always get `invalid_client`.
