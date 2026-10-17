"""Tool definitions from tools.yaml (shorthand types) turned into MCP / JSON Schema form."""

import math
from dataclasses import dataclass
from pathlib import Path

from copperline_common.config import config_file, load_yaml

SHORTHAND_TYPES = ("string", "integer", "number", "boolean")
MODEL_NAME_SEPARATOR = "__"


@dataclass(frozen=True)
class ToolDef:
    server: str
    name: str
    description: str
    input_schema: dict

    @property
    def fq(self) -> str:
        return f"{self.server}.{self.name}"

    @property
    def model_name(self) -> str:
        # Bedrock tool names must match ^[a-zA-Z0-9_-]{1,64}$, so no dots.
        return f"{self.server}{MODEL_NAME_SEPARATOR}{self.name}"


def shorthand_to_schema(shorthand: dict | None) -> dict:
    """{'query': 'string', 'limit': 'integer?'} -> a strict JSON Schema object."""
    properties: dict[str, dict] = {}
    required: list[str] = []
    for arg, type_text in (shorthand or {}).items():
        type_text = str(type_text).strip()
        optional = type_text.endswith("?")
        type_name = type_text.rstrip("?").strip()
        if type_name not in SHORTHAND_TYPES:
            raise ValueError(f"unknown shorthand type {type_text!r} for argument {arg!r}")
        properties[arg] = {"type": type_name}
        if not optional:
            required.append(arg)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def load_tools(path: str | Path | None = None) -> dict[str, ToolDef]:
    """All tools keyed by fully-qualified name, e.g. 'orders.get_order_status'."""
    data = load_yaml(path or config_file("tools.yaml")) or {}
    tools: dict[str, ToolDef] = {}
    for server, server_tools in data.items():
        for name, spec in (server_tools or {}).items():
            spec = spec or {}
            tool = ToolDef(
                server=server,
                name=name,
                description=str(spec.get("description") or ""),
                input_schema=shorthand_to_schema(spec.get("input")),
            )
            tools[tool.fq] = tool
    return tools


def tools_for_server(tools: dict[str, ToolDef], server: str) -> list[ToolDef]:
    return [tool for tool in tools.values() if tool.server == server]


def fq_from_model_name(model_name: str) -> str:
    """'orders__get_order_status' -> 'orders.get_order_status'."""
    return model_name.replace(MODEL_NAME_SEPARATOR, ".", 1)


def _type_ok(value: object, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    if type_name == "boolean":
        return isinstance(value, bool)
    return True


def validate_args(tool: ToolDef, args: dict) -> str | None:
    """None if the arguments fit the tool's schema, otherwise a message the model can act on."""
    if not isinstance(args, dict):
        return f"{tool.name}: arguments must be an object"
    properties = tool.input_schema.get("properties", {})
    missing = [name for name in tool.input_schema.get("required", []) if name not in args]
    if missing:
        return f"{tool.name}: missing required argument(s): {', '.join(missing)}"
    unknown = [name for name in args if name not in properties]
    if unknown:
        return f"{tool.name}: unknown argument(s): {', '.join(unknown)}"
    for name, value in args.items():
        expected = properties[name].get("type", "")
        if not _type_ok(value, expected):
            return f"{tool.name}: argument {name!r} must be of type {expected}"
    return None
