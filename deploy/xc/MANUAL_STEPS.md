# Console-only or not-yet-automated XC steps

Claude Code: whenever a step can't be expressed in Terraform, add it here with the exact console path. Remove it once it has been automated.

1. **Blindfold the AI gateway key.** Run `vesctl request secrets get-public-key` and `vesctl request secrets get-policy-document --namespace shared --name ves-io-allow-volterra`, then `vesctl request secrets encrypt ...`. Save the result as secret `aigw-api-key` and reference it from llm-lb `request_headers_to_add`.
   - `TODO(verify-xc)`: exact commands for the tenant's vesctl version.
2. **Paste the JWKS** from `deploy/xc/generated/jwks.json` into the JWT validation config of each of the six LBs. Use the JWKS URI option only if the tenant can reach a public JWKS URL.
3. **Pre-seed API discovery.** At least 24 hours (2 hours minimum) before the first rehearsal, run `scripts/run_scenario.py S6 S2 --env tenant --discovery-seed`, with partner-lb and tools-lb API validation set to Report.
4. **Console deep links.** After the LBs exist, copy the security-events and request-log URLs for tools-lb, llm-lb and partner-lb into `.env` (`XC_CONSOLE_LINKS`).
