# idp: demo identity provider

This is **not** a real IdP. It is about 200 lines of FastAPI with RS256 JWTs. Identity semantics are in `docs/02-agents.md`.

| Endpoint | Input | Output |
|---|---|---|
| `POST /auth/demo-login` | `{sub}`; `sub` must exist in `config/personas.yaml`. `RUN_SALT` is applied to salted personas | User token (aud from persona; 30 min) |
| `POST /auth/token` | Form: `grant_type=client_credentials`, `client_id`, `client_secret`, `audience` | Agent machine-mode token (`obo_sub=none`), service token, or partner token, depending on the client kind |
| `POST /token/exchange` | Form: `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, `subject_token`, `client_id`, `client_secret`, `requested_audience` | Agent token (5 min). **400 `unauthorized_user_role`** if the user's role is not in the agent's `allowed_user_roles` |
| `GET /.well-known/jwks.json` | — | JWKS |
| `GET /healthz` | — | `ok` |

## Agent token claims (flat; XC matches on these)

`iss`, `aud`, `sub` (= `agent_id`), `agent_id`, `agent_role`, `obo_sub`, `obo_role`, `iat`, `exp`, `jti`.

## Where values come from

- `allowed_user_roles` per agent is read from `config/policy/tool-access-matrix.yaml`, mounted into the pod.
- Client secrets come from `CLIENT_SECRETS_JSON`.
