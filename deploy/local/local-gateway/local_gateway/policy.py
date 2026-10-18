"""Service-policy evaluation as the intent files describe it: rules in order, every matcher in a rule must match
(AND), the first matching rule wins, and no match means deny. Whether XC behaves exactly like this is docs/08 R6."""

from dataclasses import dataclass

from local_gateway.intent import Policy, Rule

NO_MATCH = "no rule matched"


@dataclass
class RequestFacts:
    method: str
    path: str  # as the client sent it, before any prefix_rewrite
    headers: dict[str, list[str]]  # lowercased name -> every value sent under that name
    claims: dict | None  # claims of a token this LB validated; None when it validated none


def rule_matches(rule: Rule, facts: RequestFacts) -> bool:
    if rule.path_exact is not None and facts.path != rule.path_exact:
        return False
    if rule.path_prefix is not None and not facts.path.startswith(rule.path_prefix):
        return False
    if rule.methods is not None and facts.method.upper() not in rule.methods:
        return False
    claims = facts.claims or {}
    for claim, allowed in rule.jwt_claims.items():
        value = claims.get(claim)
        if not isinstance(value, str) or value not in allowed:
            return False
    for header, allowed in rule.headers.items():
        values = facts.headers.get(header, [])
        # A header sent twice never matches here. (How XC treats repeats is unverified; the MCP server rejects them.)
        if len(values) != 1 or values[0] not in allowed:
            return False
    return True


def decide(policy: Policy, facts: RequestFacts) -> tuple[str, str]:
    """(action, rule name)."""
    for rule in policy.rules:
        if rule_matches(rule, facts):
            return rule.action, rule.name
    return "deny", NO_MATCH
