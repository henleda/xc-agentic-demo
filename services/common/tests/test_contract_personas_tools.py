"""Black-box contract tests for personas.py and tool_schemas.py against the real config files."""
import re
from pathlib import Path

import pytest
import yaml
from copperline_common.personas import Personas, salted_sub
from copperline_common.tool_schemas import (
    ToolDef,
    fq_from_model_name,
    load_tools,
    tools_for_server,
    validate_args,
)

REPO = Path(__file__).resolve().parents[3]
PERSONAS_YAML = REPO / "config" / "personas.yaml"
TOOLS_YAML = REPO / "config" / "tools" / "tools.yaml"


@pytest.fixture(scope="module")
def personas():
    return Personas.load(PERSONAS_YAML)


@pytest.fixture(scope="module")
def tools_yaml(tmp_path_factory):
    """The real tools.yaml with `type?` values quoted.

    PyYAML cannot parse an unquoted `integer?` inside a flow mapping ({ ... }), so the real file fails
    yaml.safe_load. test_real_tools_yaml_is_valid_yaml reports that separately; everything else runs on this copy.
    """
    text = re.sub(r"(?<=: )(\w+\?)(?=[ ,}])", r'"\1"', TOOLS_YAML.read_text())
    path = tmp_path_factory.mktemp("config") / "tools.yaml"
    path.write_text(text)
    return path


@pytest.fixture(scope="module")
def tools(tools_yaml):
    return load_tools(tools_yaml)


# --- personas --------------------------------------------------------------------------------------


@pytest.mark.parametrize("sub, expected", [
    ("mallory.x-ab12", "mallory.x"),         # salted persona: suffix stripped
    ("mallory.x", "mallory.x"),
    ("alex.rivera-x", "alex.rivera-x"),      # Alex is not salted: unchanged
    ("alex.rivera", "alex.rivera"),
    ("sam.patel-ab12", "sam.patel-ab12"),
    ("nobody-ab12", "nobody-ab12"),
    ("granite-supply", "granite-supply"),
    ("job:insights-nightly", "job:insights-nightly"),
])
def test_base_sub_strips_the_run_salt_only_for_salted_personas(personas, sub, expected):
    assert personas.base_sub(sub) == expected


def test_base_sub_uses_the_last_dash(personas):
    assert personas.base_sub("mallory.x-ab-12") == "mallory.x-ab-12"   # text before the last "-" is "mallory.x-ab"


def test_get_is_exact_and_resolve_strips_the_salt(personas):
    assert personas.get("mallory.x")["name"] == "Mallory"
    assert personas.get("mallory.x-ab12") is None
    assert personas.resolve("mallory.x-ab12")["sub"] == "mallory.x"
    assert personas.resolve("alex.rivera-ab12") is None


def test_customer_id(personas):
    assert personas.customer_id("alex.rivera") == "C-1001"
    assert personas.customer_id("mallory.x-ab12") == "C-1066"
    assert personas.customer_id("sam.patel") is None          # persona without customer_id
    assert personas.customer_id("ghost.user") is None         # unknown persona


def test_client_lookup(personas):
    assert personas.client("agent-storeops")["agent_role"] == "storeops"
    assert personas.client("granite-supply")["kind"] == "partner"
    assert personas.client("agent-unknown") is None


def test_salted_sub():
    assert salted_sub("mallory.x", "ab12") == "mallory.x-ab12"
    assert salted_sub("mallory.x", None) == "mallory.x"
    assert salted_sub("mallory.x", "") == "mallory.x"


def test_load_defaults_to_config_dir(monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(PERSONAS_YAML.parent))
    assert Personas.load().get("alex.rivera")["customer_id"] == "C-1001"


# --- tool_schemas ----------------------------------------------------------------------------------


def test_real_tools_yaml_is_valid_yaml():
    try:
        yaml.safe_load(TOOLS_YAML.read_text())
    except yaml.YAMLError as exc:
        pytest.fail(f"config/tools/tools.yaml is not valid YAML; quote the optional types (\"integer?\"): {exc}")


def test_load_tools_reads_the_real_config_file():
    assert load_tools(TOOLS_YAML)["catalog.search_products"].input_schema["required"] == ["query"]


def test_load_tools_is_keyed_by_fully_qualified_name(tools):
    assert len(tools) == 11
    tool = tools["orders.get_order_status"]
    assert isinstance(tool, ToolDef)
    assert tool.server == "orders" and tool.name == "get_order_status"
    assert tool.description == "Order status (masked; no PII)"
    assert tool.fq == "orders.get_order_status"


def test_shorthand_becomes_strict_json_schema_with_optional_marker(tools):
    schema = tools["inventory.adjust_stock"].input_schema
    assert set(schema) == {"type", "properties", "required", "additionalProperties"}
    assert schema["type"] == "object"
    assert schema["properties"] == {
        "sku": {"type": "string"},
        "store_id": {"type": "string"},
        "delta": {"type": "integer"},
        "reason": {"type": "string"},
    }
    assert sorted(schema["required"]) == ["delta", "sku", "store_id"]
    assert schema["additionalProperties"] is False
    search = tools["catalog.search_products"].input_schema
    assert search["properties"]["limit"] == {"type": "integer"}
    assert search["required"] == ["query"]
    assert tools["refunds.issue_refund"].input_schema["properties"]["amount"] == {"type": "number"}


def test_every_schema_is_a_closed_object(tools):
    for tool in tools.values():
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])


def test_load_tools_parses_boolean_and_number_shorthand(tmp_path):
    path = tmp_path / "tools.yaml"
    path.write_text("demo:\n  t:\n    description: d\n    input:\n      flag: boolean\n      ratio: number?\n"
                    "      label: string\n")
    schema = load_tools(path)["demo.t"].input_schema
    assert schema["properties"] == {
        "flag": {"type": "boolean"}, "ratio": {"type": "number"}, "label": {"type": "string"},
    }
    assert sorted(schema["required"]) == ["flag", "label"]


def test_model_name_and_fq_round_trip(tools):
    for fq, tool in tools.items():
        assert tool.model_name == fq.replace(".", "__")
        assert fq_from_model_name(tool.model_name) == fq
        assert all(c.isalnum() or c in "_-" for c in tool.model_name) and len(tool.model_name) <= 64
    assert tools["orders.get_order_status"].model_name == "orders__get_order_status"
    assert fq_from_model_name("orders__get_order_status") == "orders.get_order_status"


def test_tools_for_server(tools):
    names = sorted(t.name for t in tools_for_server(tools, "refunds"))
    assert names == ["get_refund_status", "issue_refund"]
    assert tools_for_server(tools, "nope") == []


def test_load_tools_defaults_to_config_dir(monkeypatch, tools_yaml):
    monkeypatch.setenv("CONFIG_DIR", str(tools_yaml.parent))
    assert "catalog.get_reviews" in load_tools()


def test_validate_args_accepts_valid_arguments(tools):
    assert validate_args(tools["inventory.adjust_stock"], {"sku": "X", "store_id": "S-001", "delta": 2}) is None
    assert validate_args(tools["refunds.issue_refund"], {"order_id": "CO-1", "amount": 120}) is None
    assert validate_args(tools["refunds.issue_refund"], {"order_id": "CO-1", "amount": 99.5, "reason": "x"}) is None


@pytest.mark.parametrize("fq, args", [
    ("inventory.adjust_stock", {"sku": "X", "store_id": "S-1", "delta": True}),     # bool is not an integer
    ("inventory.adjust_stock", {"sku": "X", "store_id": "S-1", "delta": 2.5}),      # float is not an integer
    ("inventory.adjust_stock", {"sku": "X", "store_id": "S-1", "delta": "2"}),      # str is not an integer
    ("refunds.issue_refund", {"order_id": "CO-1", "amount": False}),                # bool is not a number
    ("refunds.issue_refund", {"order_id": "CO-1", "amount": "120"}),                # str is not a number
    ("orders.get_order_status", {"order_id": 1042}),                                # int is not a string
    ("orders.get_order_status", {"order_id": "CO-1", "extra": 1}),                  # unknown key
    ("orders.get_order_status", {}),                                                 # missing required
    ("inventory.adjust_stock", {"sku": "X", "delta": 1}),                           # missing store_id
])
def test_validate_args_rejects_with_a_message(tools, fq, args):
    message = validate_args(tools[fq], args)
    assert isinstance(message, str) and message
