"""Read the XC intent for ONE load balancer and turn it into what the local-gateway enforces.

The intent files under deploy/xc/intent/ are the same ones Phase 3 turns into Terraform, so a local denial comes from
the same rules XC will get. Only some intent fields are emulated (EMULATED below). The XC controls in NOT_EMULATED
are skipped and named in the startup log. A field in neither list stops the gateway: a new control in the intent
must be a conscious decision here, never silently ignored.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from copperline_common.config import load_yaml

LB_FILE = "20-load-balancers.yaml"
SECURITY_FILE = "30-security.yaml"
GENERATED_POLICIES_FILE = "service-policies.generated.yaml"

EMULATED = {"routes", "jwt", "service_policies", "request_headers_to_remove", "request_headers_to_add"}
WHERE_SERVED = {"name", "domains", "advertise", "protocol", "port"}  # docker compose decides this locally
NOT_EMULATED = {
    "waf",
    "user_identification",
    "malicious_user_detection",
    "rate_limit",
    "rate_limiter_policies",
    "data_guard",
    "api_definition",
    "openapi_validation",
    "api_discovery",
    "sensitive_data_discovery",
}
ROUTE_KEYS = {"prefix", "path_exact", "pool", "prefix_rewrite"}
JWT_LB_KEYS = {"profile", "target_paths"}
JWT_PROFILE_KEYS = {"issuer", "audience", "action", "mandatory_claims"}
POLICY_KEYS = {"name", "attached_to", "rules"}
MATCHERS = {"path_exact", "path_prefix", "method", "jwt_claims", "headers"}
# A request_headers_to_add entry names an XC secret; locally its value comes from this env var.
SECRET_ENV = {"aigw-api-key": "GATEWAY_API_KEY"}


class IntentError(Exception):
    """The intent asks for something the local-gateway cannot do faithfully. The message says what."""


@dataclass(frozen=True)
class Route:
    pool: str
    upstream: str  # base URL of the pool, from deploy/local/pools.yaml
    prefix: str | None = None
    path_exact: str | None = None
    prefix_rewrite: str | None = None

    def matches(self, path: str) -> bool:
        if self.path_exact is not None:
            return path == self.path_exact
        return path.startswith(self.prefix)

    def upstream_path(self, path: str) -> str:
        if self.prefix_rewrite is None:
            return path
        return self.prefix_rewrite + path[len(self.prefix) :]


@dataclass(frozen=True)
class JwtCheck:
    profile: str
    issuer: str
    audience: str
    mandatory_claims: tuple[str, ...]
    target_paths: tuple[str, ...]

    def applies_to(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.target_paths)


@dataclass
class Rule:
    """One service-policy rule. Every matcher that is set must match (AND); a rule with none matches everything."""

    name: str
    action: str  # "allow" or "deny"
    path_exact: str | None = None
    path_prefix: str | None = None
    methods: tuple[str, ...] | None = None
    jwt_claims: dict[str, tuple[str, ...]] = field(default_factory=dict)
    headers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # lowercased header name -> allowed values


@dataclass
class Policy:
    name: str
    rules: list[Rule]


@dataclass
class LbConfig:
    name: str
    routes: list[Route]
    jwt: JwtCheck | None
    policy: Policy | None
    headers_to_remove: list[str]  # lowercased
    headers_to_add: list[tuple[str, str]]  # (name, value); values may be secrets, never log them
    not_emulated: list[str]


def load_pools(path: str | Path) -> dict[str, str]:
    """deploy/local/pools.yaml: origin pool name -> base URL inside docker compose."""
    data = load_yaml(path)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise IntentError(f"{path} must map pool names to URLs, e.g. 'op-idp: http://idp:8080'")
    return data


def load_lb(
    name: str, intent_dir: str | Path, pools: Mapping[str, str], env: Mapping[str, str] = os.environ
) -> LbConfig:
    intent_dir = Path(intent_dir)
    lb_intent = load_yaml(intent_dir / LB_FILE)
    lbs = {lb["name"]: lb for lb in lb_intent["http_load_balancers"]}
    if name not in lbs:
        raise IntentError(f"no load balancer named {name!r} in {LB_FILE} (have: {', '.join(lbs)})")
    lb = lbs[name]
    unknown = set(lb) - EMULATED - WHERE_SERVED - NOT_EMULATED
    if unknown:
        raise IntentError(
            f"{name}: unknown intent field(s) {sorted(unknown)}; decide in local_gateway/intent.py whether to "
            "emulate them or list them as NOT_EMULATED"
        )
    return LbConfig(
        name=name,
        routes=[_route(name, raw, pools) for raw in lb["routes"]],
        jwt=_jwt(name, lb.get("jwt"), lb_intent.get("jwt_profiles") or {}),
        policy=_policy(name, lb.get("service_policies"), intent_dir),
        headers_to_remove=[header.lower() for header in lb.get("request_headers_to_remove") or []],
        headers_to_add=[_header_to_add(name, raw, env) for raw in lb.get("request_headers_to_add") or []],
        not_emulated=sorted(set(lb) & NOT_EMULATED),
    )


def _unknown_keys(where: str, raw: dict, known: set[str]) -> None:
    unknown = set(raw) - known
    if unknown:
        raise IntentError(f"{where}: unknown key(s) {sorted(unknown)}")


def _route(lb: str, raw: dict, pools: Mapping[str, str]) -> Route:
    where = f"{lb} route {raw}"
    _unknown_keys(where, raw, ROUTE_KEYS)
    if ("prefix" in raw) == ("path_exact" in raw):
        raise IntentError(f"{where}: needs exactly one of prefix or path_exact")
    if "prefix_rewrite" in raw and "prefix" not in raw:
        raise IntentError(f"{where}: prefix_rewrite only works on a prefix route")
    pool = raw.get("pool")
    if pool not in pools:
        raise IntentError(f"{where}: pool {pool!r} has no entry in deploy/local/pools.yaml")
    return Route(
        pool=pool,
        upstream=pools[pool].rstrip("/"),
        prefix=raw.get("prefix"),
        path_exact=raw.get("path_exact"),
        prefix_rewrite=raw.get("prefix_rewrite"),
    )


def _jwt(lb: str, raw: dict | None, profiles: dict) -> JwtCheck | None:
    if raw is None:
        return None
    _unknown_keys(f"{lb} jwt", raw, JWT_LB_KEYS)
    profile_name = raw.get("profile")
    profile = profiles.get(profile_name)
    if profile is None:
        raise IntentError(f"{lb}: jwt profile {profile_name!r} is not in jwt_profiles")
    _unknown_keys(f"jwt profile {profile_name}", profile, JWT_PROFILE_KEYS)
    if profile.get("action") != "block":
        raise IntentError(f"jwt profile {profile_name}: only action 'block' is emulated, got {profile.get('action')!r}")
    target_paths = raw.get("target_paths")
    if not target_paths:
        raise IntentError(f"{lb} jwt: target_paths must list at least one path prefix")
    return JwtCheck(
        profile=profile_name,
        issuer=profile["issuer"],
        audience=profile["audience"],
        mandatory_claims=tuple(profile.get("mandatory_claims") or ()),
        target_paths=tuple(target_paths),
    )


def _policy(lb: str, names: list[str] | None, intent_dir: Path) -> Policy | None:
    if not names:
        return None
    if len(names) > 1:
        raise IntentError(
            f"{lb}: lists {len(names)} service policies; the local-gateway emulates one per LB "
            "(how XC combines several is not modelled)"
        )
    all_policies = _all_policies(intent_dir)
    if names[0] not in all_policies:
        raise IntentError(
            f"{lb}: service policy {names[0]!r} is in neither {GENERATED_POLICIES_FILE} nor {SECURITY_FILE}"
        )
    raw = all_policies[names[0]]
    _unknown_keys(f"service policy {names[0]}", raw, POLICY_KEYS)
    return Policy(name=names[0], rules=[_rule(names[0], i, rule) for i, rule in enumerate(raw["rules"])])


def _all_policies(intent_dir: Path) -> dict[str, dict]:
    generated = load_yaml(intent_dir / GENERATED_POLICIES_FILE).get("service_policies") or []
    static = load_yaml(intent_dir / SECURITY_FILE).get("service_policies_static") or []
    policies: dict[str, dict] = {}
    for policy in [*generated, *static]:
        if policy["name"] in policies:
            raise IntentError(f"service policy {policy['name']!r} is defined twice")
        policies[policy["name"]] = policy
    return policies


def _rule(policy: str, index: int, raw: dict) -> Rule:
    """Two shapes: generated `{name, action, match: {...} | any}`, and static `{action, <matchers...>}` or
    `{action, match: any}`."""
    where = f"service policy {policy} rule {index}"
    _unknown_keys(where, raw, {"name", "action", "match"} | MATCHERS)
    action = raw.get("action")
    if action not in ("allow", "deny"):
        raise IntentError(f"{where}: action must be allow or deny, got {action!r}")
    inline = {key: raw[key] for key in MATCHERS if key in raw}
    match = raw.get("match")
    if match is not None and inline:
        raise IntentError(f"{where}: has both 'match' and inline matchers")
    if match == "any":
        matchers = {}
    elif isinstance(match, dict):
        matchers = match
    elif match is None and inline:
        matchers = inline
    else:
        raise IntentError(f"{where}: needs 'match: any', a 'match' mapping or inline matchers")
    _unknown_keys(where, matchers, MATCHERS)
    return Rule(
        name=raw.get("name") or f"{policy}[{index}]",
        action=action,
        path_exact=_optional_str(where, matchers, "path_exact"),
        path_prefix=_optional_str(where, matchers, "path_prefix"),
        methods=_methods(where, matchers.get("method")),
        jwt_claims=_value_lists(where, matchers.get("jwt_claims"), lower_keys=False),
        headers=_value_lists(where, matchers.get("headers"), lower_keys=True),
    )


def _optional_str(where: str, matchers: dict, key: str) -> str | None:
    value = matchers.get(key)
    if value is not None and not isinstance(value, str):
        raise IntentError(f"{where}: {key} must be a string")
    return value


def _methods(where: str, raw) -> tuple[str, ...] | None:
    if raw is None:
        return None
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise IntentError(f"{where}: method must be a string or a list of strings")
    return tuple(v.upper() for v in values)


def _value_lists(where: str, raw, *, lower_keys: bool) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise IntentError(f"{where}: expected a mapping of name -> list of values")
    result = {}
    for key, values in raw.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise IntentError(f"{where}: {key} must list string values")
        result[key.lower() if lower_keys else key] = tuple(values)
    return result


def _header_to_add(lb: str, raw: dict, env: Mapping[str, str]) -> tuple[str, str]:
    _unknown_keys(f"{lb} request_headers_to_add", raw, {"name", "secret", "value"})
    if "value" in raw:
        return raw["name"], str(raw["value"])
    secret = raw.get("secret")
    env_name = SECRET_ENV.get(secret)
    if env_name is None:
        raise IntentError(f"{lb}: no local env var for secret {secret!r}; add it to SECRET_ENV in intent.py")
    if not env.get(env_name):
        raise IntentError(f"{lb}: adds header {raw['name']} from {env_name}, which is not set")
    return raw["name"], env[env_name]
