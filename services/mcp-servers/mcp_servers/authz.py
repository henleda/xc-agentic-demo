"""Defense-in-depth authorization: the same decision XC's sp-tools makes, from the same access matrix.

XC (or the local-gateway) validates the JWT and enforces the generated service policy first. This check exists so a
broken fabric policy is caught and logged (server_authz_deny) instead of silently trusted. Claims are read without
signature verification on purpose: the fabric is the verifier, and RE pods have no JWKS.
"""

from copperline_common.logging import log_event
from copperline_common.mcp import FORBIDDEN, UNAUTHORIZED, McpDenied, McpRequestContext

DISCOVERY_METHODS = ("tools/list", "server/discover")


def _tool_entry(entry) -> tuple[str, list[str] | None]:
    """Same semantics as scripts/gen_policy.py norm_tool_entry: "server.tool" or {tool, require_obo_roles}."""
    if isinstance(entry, str):
        return entry, None
    return entry["tool"], entry.get("require_obo_roles")


class Matrix:
    """Answers "may agent_role call this tool for a human with obo_role?" from tool-access-matrix.yaml."""

    def __init__(self, matrix: dict) -> None:
        # agent_role -> {fq tool: allowed obo roles}
        self.grants: dict[str, dict[str, list[str]]] = {}
        for agent in matrix["agents"].values():
            users = list(agent["allowed_user_roles"])
            tools = {}
            for entry in agent["tools"]:
                fq, require = _tool_entry(entry)
                tools[fq] = list(require or users)
            self.grants[agent["agent_role"]] = tools
        self.users = {a["agent_role"]: list(a["allowed_user_roles"]) for a in matrix["agents"].values()}

    def may_call(self, agent_role: str, fq_tool: str, obo_role: str) -> bool:
        return obo_role in self.grants.get(agent_role, {}).get(fq_tool, [])

    def may_discover(self, agent_role: str, server: str, obo_role: str) -> bool:
        has_tool = any(fq.split(".", 1)[0] == server for fq in self.grants.get(agent_role, {}))
        return has_tool and obo_role in self.users.get(agent_role, [])


def make_authorize(server: str, matrix: Matrix):
    """The authorize hook mcp_router calls after the protocol checks and before any tool runs."""

    async def authorize(ctx: McpRequestContext) -> None:
        agent_role = ctx.claims.get("agent_role")
        obo_role = ctx.claims.get("obo_role")
        if not ctx.token or not isinstance(agent_role, str) or not isinstance(obo_role, str):
            reason = "no bearer token" if not ctx.token else "token undecodable or missing agent_role/obo_role"
            log_event("server_authz_deny", level="warning", status=401,
                      agent_role=agent_role if isinstance(agent_role, str) else None,
                      obo_role=obo_role if isinstance(obo_role, str) else None, method=ctx.method,
                      tool=f"{server}.{ctx.tool}" if ctx.tool else None, reason=reason)
            raise McpDenied(401, UNAUTHORIZED, "Unauthorized")

        if ctx.method in DISCOVERY_METHODS:
            allowed = matrix.may_discover(agent_role, server, obo_role)
            tool = None
        else:
            tool = f"{server}.{ctx.tool}"
            allowed = matrix.may_call(agent_role, tool, obo_role)
        if not allowed:
            log_event("server_authz_deny", level="warning", status=403, agent_role=agent_role, obo_role=obo_role,
                      method=ctx.method, tool=tool, reason="not granted by tool-access-matrix")
            raise McpDenied(403, FORBIDDEN, "Forbidden")

    return authorize
