"""One local-gateway process plays ONE XC HTTP load balancer from deploy/xc/intent/. NOT XC.

Per request, in this order (first failure wins):
  1. JWT validation, on the LB's target_paths: RS256 signature, exp, iss, aud, mandatory claims present -> else 401
  2. Service policy: rules in order, AND within a rule, first match wins, no match is deny -> else 403
  3. Route: first route whose prefix / path_exact matches -> else 404
  4. Header changes (request_headers_to_remove, then request_headers_to_add), then proxy to the pool.
     Pool unreachable -> 503, pool timeout -> 504.

Every response this process makes itself carries `x-local-gateway: <lb>` and says "local-gateway — NOT XC" in the
body, and never `x-copperline-origin`, so copperline_common.xcblock classifies it as a fabric block labelled
"local-gateway (NOT XC)". Responses from the pool are passed through unchanged, origin header included.
"""

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from copperline_common.jwt import JwksVerifier, JwtError, bearer_token
from copperline_common.logging import log_event, setup_logging
from copperline_common.requestid import REQUEST_ID_HEADER, agent_id_var, new_request_id, obo_sub_var, request_id_var
from copperline_common.xcblock import LOCAL_GATEWAY_HEADER
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route as StarletteRoute

from local_gateway.intent import LbConfig, load_lb, load_pools
from local_gateway.policy import RequestFacts, decide

SERVICE = "local-gateway"
LABEL = "local-gateway — NOT XC"
HEALTH_PATH = "/__local-gateway/healthz"  # not an XC path; only the compose healthcheck uses it
PATH_SAFE = "/:@!$&'()*+,;=-._~"  # RFC 3986 pchar characters that stay as they are in a path
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Never forwarded in either direction. content-length and date are recomputed by us and by uvicorn.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "content-length", "date", "server",
}


@dataclass
class Settings:
    lb_name: str
    intent_dir: Path
    pools_file: Path
    jwks_path: Path
    upstream_timeout_s: float = 60.0  # model calls through llm-lb can be slow

    @classmethod
    def from_env(cls) -> "Settings":
        lb_name = os.environ.get("LB_NAME", "")
        if not lb_name:
            raise RuntimeError("set LB_NAME to the XC load balancer this gateway plays, e.g. tools-lb")
        return cls(
            lb_name=lb_name,
            intent_dir=Path(os.environ.get("INTENT_DIR") or "/gateway"),
            pools_file=Path(os.environ.get("POOLS_FILE") or "/gateway/pools.yaml"),
            jwks_path=Path(os.environ.get("JWKS_PATH") or "/keys/jwks.json"),
            upstream_timeout_s=float(os.environ.get("UPSTREAM_TIMEOUT_S") or 60),
        )


class Denied(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.error = error
        self.reason = reason


def create_app(settings: Settings, *, http: httpx.AsyncClient | None = None, env=os.environ) -> Starlette:
    """http lets tests route the pools to in-process apps. Raises IntentError/RuntimeError on bad config."""
    setup_logging(SERVICE)
    lb = load_lb(settings.lb_name, settings.intent_dir, load_pools(settings.pools_file), env)
    verifier = None
    if lb.jwt is not None:
        try:
            json.loads(settings.jwks_path.read_text(encoding="utf-8"))  # fail at startup, not on the first request
        except (OSError, ValueError) as e:
            raise RuntimeError(f"{lb.name} validates JWTs but cannot read JWKS {settings.jwks_path}: {e}") from None
        # From the file, not a copy: on an unknown kid (gen_keys.py --rotate) the verifier re-reads it once.
        verifier = JwksVerifier(jwks_path=str(settings.jwks_path), issuer=lb.jwt.issuer)
    client = http or httpx.AsyncClient(timeout=settings.upstream_timeout_s)
    gateway = Gateway(lb, verifier, client)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        if http is None:  # we created the client, so we close it
            await client.aclose()

    app = Starlette(routes=[StarletteRoute("/{path:path}", gateway.handle, methods=ALL_METHODS)], lifespan=lifespan)
    log_event(
        "gateway_ready",
        note=LABEL,
        lb=lb.name,
        routes=[f"{r.prefix or r.path_exact} -> {r.pool}" for r in lb.routes],
        jwt_profile=lb.jwt.profile if lb.jwt else None,
        service_policy=lb.policy.name if lb.policy else None,
        headers_removed=lb.headers_to_remove,
        headers_added=[name for name, _ in lb.headers_to_add],  # names only: values can be secrets
        not_emulated=lb.not_emulated,
    )
    return app


class Gateway:
    def __init__(self, lb: LbConfig, verifier: JwksVerifier | None, http: httpx.AsyncClient) -> None:
        self.lb = lb
        self.verifier = verifier
        self.http = http

    async def handle(self, request: Request) -> Response:
        started = time.monotonic()
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        context = [request_id_var.set(request_id), agent_id_var.set(None), obo_sub_var.set(None)]
        path = request.scope["path"]  # decoded; request.url.path would cut a decoded "?" (%3F) out of the path
        outcome: dict = {}
        try:
            if path == HEALTH_PATH and request.method == "GET":
                return JSONResponse({"status": "ok", "service": SERVICE, "lb": self.lb.name, "note": LABEL})
            try:
                response = await self._admit_and_forward(request, request_id, outcome)
            except Denied as denied:
                outcome.update(decision="denied", reason=denied.reason)
                response = self._denial(denied, request_id)
            log_event(
                "lb_request",
                level="warning" if outcome.get("decision") == "denied" else "info",
                lb=self.lb.name,
                method=request.method,
                path=path,
                status=response.status_code,
                latency_ms=int((time.monotonic() - started) * 1000),
                **outcome,
            )
            return response
        finally:
            for var, token in zip((request_id_var, agent_id_var, obo_sub_var), context, strict=True):
                var.reset(token)

    async def _admit_and_forward(self, request: Request, request_id: str, outcome: dict) -> Response:
        path = request.scope["path"]
        if any(segment in (".", "..") for segment in path.split("/")):
            raise Denied(400, "bad_request", "path has . or .. segments")
        headers = _header_lists(request)
        claims = self._validate_jwt(path, headers, outcome)

        if self.lb.policy is not None:
            action, rule = decide(self.lb.policy, RequestFacts(request.method, path, headers, claims))
            outcome["policy_rule"] = f"{self.lb.policy.name}/{rule}"
            if action != "allow":
                raise Denied(403, "forbidden", f"service policy {self.lb.policy.name}: {rule}")

        route = next((r for r in self.lb.routes if r.matches(path)), None)
        if route is None:
            raise Denied(404, "not_found", "no route on this load balancer")
        outcome.update(decision="allowed", pool=route.pool)
        # path is the decoded path the policy just judged. Re-encode it ourselves (a decoded "?" becomes %3F) so the
        # pool sees exactly that path and nothing else, then append the original query string unchanged.
        raw_path = quote(route.upstream_path(path), safe=PATH_SAFE)
        query = request.scope["query_string"].decode("latin-1")
        if query:
            raw_path += "?" + query
        target = httpx.URL(route.upstream).copy_with(raw_path=raw_path.encode("latin-1"))
        return await self._forward(request, target, request_id, outcome)

    def _validate_jwt(self, path: str, headers: dict[str, list[str]], outcome: dict) -> dict | None:
        """Claims of a valid token, or None when this path is not a JWT target. Raises Denied(401)."""
        jwt = self.lb.jwt
        if jwt is None or not jwt.applies_to(path):
            return None
        authorization = headers.get("authorization", [])
        if len(authorization) > 1:
            raise Denied(401, "unauthorized", "jwt validation: repeated Authorization header")
        token = bearer_token(authorization[0] if authorization else None)
        if token is None:
            raise Denied(401, "unauthorized", "jwt validation: missing bearer token")
        try:
            claims = self.verifier.verify(token, audience=jwt.audience)
        except JwtError as e:
            raise Denied(401, "unauthorized", f"jwt validation: {e}") from None
        missing = [claim for claim in jwt.mandatory_claims if claim not in claims]
        if missing:
            raise Denied(401, "unauthorized", f"jwt validation: missing mandatory claim(s) {', '.join(missing)}")
        agent_id_var.set(claims.get("agent_id"))
        obo_sub_var.set(claims.get("obo_sub") if "agent_id" in claims else claims.get("sub"))
        outcome["jwt"] = "valid"
        return claims

    async def _forward(self, request: Request, url: httpx.URL, request_id: str, outcome: dict) -> Response:
        drop = HOP_BY_HOP | set(self.lb.headers_to_remove) | {name.lower() for name, _ in self.lb.headers_to_add}
        headers = [(name, value) for name, value in request.headers.items() if name not in drop]
        headers += list(self.lb.headers_to_add)
        if REQUEST_ID_HEADER not in request.headers:
            headers.append((REQUEST_ID_HEADER, request_id))
        upstream = self.http.build_request(request.method, url, headers=headers, content=await request.body())
        try:
            reply = await self.http.send(upstream, stream=True)
            try:
                body = b"".join([chunk async for chunk in reply.aiter_raw()])
            finally:
                await reply.aclose()
        except httpx.TimeoutException:
            raise Denied(504, "upstream_timeout", f"pool {outcome['pool']} did not answer in time") from None
        except httpx.HTTPError as e:
            reason = f"pool {outcome['pool']} unreachable: {type(e).__name__}"
            raise Denied(503, "upstream_unavailable", reason) from None
        outcome["upstream_status"] = reply.status_code
        response = Response(content=body, status_code=reply.status_code)  # sets content-length for the raw body
        response.raw_headers += [
            (name, value) for name, value in reply.headers.raw if name.decode("latin-1").lower() not in HOP_BY_HOP
        ]
        return response

    def _denial(self, denied: Denied, request_id: str) -> JSONResponse:
        body = {
            "error": denied.error,
            "reason": denied.reason,
            "lb": self.lb.name,
            LOCAL_GATEWAY_HEADER: True,
            "note": f"{LABEL}: a rough stand-in for this XC load balancer",
        }
        headers = {LOCAL_GATEWAY_HEADER: self.lb.name, REQUEST_ID_HEADER: request_id}
        if denied.status == 401:
            headers["www-authenticate"] = "Bearer"
        return JSONResponse(body, status_code=denied.status, headers=headers)


def _header_lists(request: Request) -> dict[str, list[str]]:
    """Lowercased name -> every value, so a header sent twice is visible to the policy check."""
    result: dict[str, list[str]] = {}
    for name, value in request.headers.raw:
        result.setdefault(name.decode("latin-1").lower(), []).append(value.decode("latin-1"))
    return result
