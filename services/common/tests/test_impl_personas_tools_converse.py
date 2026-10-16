import pytest
from copperline_common.converse import chat_to_converse, reply_text, text_message, tool_specs, tool_uses
from copperline_common.personas import Personas, salted_sub
from copperline_common.tool_schemas import ToolDef, fq_from_model_name, load_tools, tools_for_server, validate_args

TOOLS_YAML = """
catalog:
  search_products: { description: Search, input: { query: string, limit: "integer?" } }
refunds:
  issue_refund: { description: Refund, input: { order_id: string, amount: number, reason: "string?" } }
  flagger: { description: Flags, input: { enabled: boolean } }
"""


@pytest.fixture
def personas() -> Personas:
    return Personas(
        personas=[
            {"sub": "alex.rivera", "role": "customer", "customer_id": "C-1001"},
            {"sub": "mallory.x", "role": "customer", "customer_id": "C-1066", "salted": True},
            {"sub": "sam.patel", "role": "care_agent"},
        ],
        clients=[{"client_id": "agent-shopper", "agent_id": "shopper"}],
    )


@pytest.fixture
def tools(tmp_path):
    path = tmp_path / "tools.yaml"
    path.write_text(TOOLS_YAML)
    return load_tools(path)


@pytest.mark.parametrize(
    "sub, expected",
    [
        ("mallory.x-ab12", "mallory.x"),  # salted persona: salt stripped
        ("mallory.x", "mallory.x"),
        ("mallory.x-ab-12", "mallory.x-ab-12"),  # only the LAST dash counts; "mallory.x-ab" is not a persona
        ("alex.rivera-x", "alex.rivera-x"),  # Alex is not salted
        ("sam.patel-ab12", "sam.patel-ab12"),
        ("nobody-ab12", "nobody-ab12"),
        ("-ab12", "-ab12"),
    ],
)
def test_base_sub_salting_rules(personas, sub, expected):
    assert personas.base_sub(sub) == expected


def test_resolve_customer_id_and_client(personas):
    assert personas.customer_id("mallory.x-run7") == "C-1066"
    assert personas.customer_id("alex.rivera") == "C-1001"
    assert personas.customer_id("alex.rivera-x") is None
    assert personas.customer_id("sam.patel") is None
    assert personas.get("mallory.x-run7") is None
    assert personas.client("agent-shopper")["agent_id"] == "shopper"
    assert personas.client("nope") is None
    assert salted_sub("mallory.x", "ab12") == "mallory.x-ab12"
    assert salted_sub("mallory.x", None) == "mallory.x" == salted_sub("mallory.x", "")


def test_personas_load_real_file(repo_root):
    loaded = Personas.load(repo_root / "config/personas.yaml")
    assert loaded.base_sub("mallory.x-zz") == "mallory.x"
    assert loaded.client("svc-mcp")["svc_role"] == "tool-server"


def test_shorthand_schema(tools):
    schema = tools["catalog.search_products"].input_schema
    assert schema == {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    assert [t.name for t in tools_for_server(tools, "refunds")] == ["issue_refund", "flagger"]
    tool = tools["refunds.issue_refund"]
    assert tool.fq == "refunds.issue_refund" and tool.model_name == "refunds__issue_refund"
    assert fq_from_model_name(tool.model_name) == tool.fq


@pytest.mark.parametrize(
    "fq, args, problem",
    [
        ("catalog.search_products", {"query": "tent"}, None),
        ("catalog.search_products", {"query": "tent", "limit": 5}, None),
        ("catalog.search_products", {}, "missing"),
        ("catalog.search_products", {"query": "tent", "extra": 1}, "unknown"),
        ("catalog.search_products", {"query": 3}, "string"),
        ("catalog.search_products", {"query": "t", "limit": True}, "integer"),
        ("catalog.search_products", {"query": "t", "limit": 2.5}, "integer"),
        ("refunds.issue_refund", {"order_id": "CO-1", "amount": 1}, None),
        ("refunds.issue_refund", {"order_id": "CO-1", "amount": 1.5}, None),
        ("refunds.issue_refund", {"order_id": "CO-1", "amount": False}, "number"),
        ("refunds.flagger", {"enabled": True}, None),
        ("refunds.flagger", {"enabled": 1}, "boolean"),
    ],
)
def test_validate_args(tools, fq, args, problem):
    message = validate_args(tools[fq], args)
    if problem is None:
        assert message is None
    else:
        assert message is not None and problem in message


def test_chat_to_converse_merges_drops_and_starts_with_user():
    messages = [
        {"role": "assistant", "content": "Hi, how can I help?"},
        {"role": "user", "content": "where is CO-1042"},
        {"role": "user", "content": "   "},
        {"role": "user", "content": "and the tent?"},
        {"role": "assistant", "content": ""},
    ]
    assert chat_to_converse(messages) == [
        text_message("user", "(conversation start)"),
        text_message("assistant", "Hi, how can I help?"),
        {"role": "user", "content": [{"text": "where is CO-1042"}, {"text": "and the tent?"}]},
    ]
    assert chat_to_converse([]) == []


def test_reply_text_tool_uses_and_specs():
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "Let me check."},
                    {"toolUse": {"toolUseId": "t1", "name": "orders__get_order_status", "input": {"order_id": "X"}}},
                    {"text": "One moment."},
                ],
            }
        },
        "stop_reason": "tool_use",
    }
    assert reply_text(response) == "Let me check.\nOne moment."
    assert tool_uses(response) == [{"toolUseId": "t1", "name": "orders__get_order_status", "input": {"order_id": "X"}}]
    assert reply_text(None) is None and tool_uses({"output": {}}) == []
    tool = ToolDef("orders", "get_order_status", "Status", {"type": "object"})
    assert tool_specs([tool]) == [{"name": "orders__get_order_status", "description": "Status",
                                   "input_schema": {"type": "object"}}]
