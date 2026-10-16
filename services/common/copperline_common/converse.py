"""Helpers for the Bedrock Converse message shape that the AI gateway accepts on POST /v1/converse."""

from copperline_common.tool_schemas import ToolDef

CONVERSATION_START = "(conversation start)"


def text_message(role: str, text: str) -> dict:
    return {"role": role, "content": [{"text": text}]}


def chat_to_converse(messages: list[dict]) -> list[dict]:
    """[{"role", "content": str}] -> Converse messages.

    Drops empty messages, merges consecutive messages from the same role, and makes sure the
    conversation starts with a user message (Bedrock rejects anything else).
    """
    converse: list[dict] = []
    for message in messages:
        role = message.get("role")
        text = message.get("content")
        if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
            continue
        if converse and converse[-1]["role"] == role:
            converse[-1]["content"].append({"text": text})
        else:
            converse.append(text_message(role, text))
    if converse and converse[0]["role"] != "user":
        converse.insert(0, text_message("user", CONVERSATION_START))
    return converse


def _content_blocks(converse_response: dict | None) -> list[dict]:
    if not isinstance(converse_response, dict):
        return []
    output = converse_response.get("output")
    message = output.get("message") if isinstance(output, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def reply_text(converse_response: dict | None) -> str | None:
    """All text blocks of output.message.content joined with newlines; None if there are none."""
    texts = [block["text"] for block in _content_blocks(converse_response) if isinstance(block.get("text"), str)]
    return "\n".join(texts) if texts else None


def tool_uses(converse_response: dict | None) -> list[dict]:
    uses = []
    for block in _content_blocks(converse_response):
        tool_use = block.get("toolUse")
        if isinstance(tool_use, dict):
            uses.append(
                {
                    "toolUseId": tool_use.get("toolUseId"),
                    "name": tool_use.get("name"),
                    "input": tool_use.get("input") or {},
                }
            )
    return uses


def tool_specs(tools: list[ToolDef]) -> list[dict]:
    return [
        {"name": tool.model_name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in tools
    ]
