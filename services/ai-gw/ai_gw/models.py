"""The two model backends behind /v1/converse. Both return the same normalized shape:

    {"output": {"message": {"role": "assistant", "content": [...]}}, "stop_reason": str,
     "usage": {"inputTokens", "outputTokens", "totalTokens"}, "route": str, "model_id": str}

Request bodies use our wire format (copperline_common.converse), which follows Bedrock Converse except for tools:
we send {"name", "description", "input_schema"}, and BedrockModel turns that into Bedrock's toolConfig.
"""

import re
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from copperline_common.config import load_yaml

STUB_REPLIES = Path(__file__).resolve().parent / "stub_replies.yaml"
STUB_MODEL_ID = "stub"


class ModelError(Exception):
    """The model call failed. status is what /v1/converse answers: 502 upstream error, 504 timeout."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


def _words(text: str) -> int:
    return len(text.split())


def _message_text(messages: list[dict]) -> list[str]:
    return [block["text"] for m in messages for block in m.get("content", []) if isinstance(block.get("text"), str)]


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            texts = [b["text"] for b in message.get("content", []) if isinstance(b.get("text"), str)]
            if texts:
                return " ".join(texts)
    return ""


def _has_tool_results(messages: list[dict]) -> bool:
    return any("toolResult" in block for m in messages for block in m.get("content", []))


class StubModel:
    """Deterministic, no network. Token counts are word counts, so budgets and usage still move.

    - Scripted turns (x-demo-scenario names a scenario in stub_replies.yaml): that canned reply.
    - Model-driven turns with tools offered and no tool result yet: call the first offered tool whose name shares a
      word with the last user message (e.g. "order" -> orders__get_order_status), with empty input. This is enough
      for agent-runtime to exercise its tool loop offline; it is not trying to be smart.
    - Everything else: the default reply.
    """

    def __init__(self, replies_path: str | Path | None = None) -> None:
        data = load_yaml(replies_path or STUB_REPLIES) or {}
        self.default = str(data.get("default") or "(stub model) OK.")
        self.scenarios = {str(k): str(v) for k, v in (data.get("scenarios") or {}).items()}

    def converse(self, route: str, body: dict, scenario: str | None) -> dict:
        messages = body.get("messages") or []
        content: list[dict]
        stop_reason = "end_turn"
        tool = None if scenario in self.scenarios else self._pick_tool(body, messages)
        if tool:
            content = [{"toolUse": {"toolUseId": f"stub-{len(messages)}", "name": tool, "input": {}}}]
            stop_reason = "tool_use"
            text_out = tool
        else:
            text_out = self.scenarios.get(scenario or "", self.default)
            content = [{"text": text_out}]
        tokens_in = sum(_words(t) for t in _message_text(messages)) + _words(str(body.get("system") or ""))
        tokens_out = _words(text_out)
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stop_reason": stop_reason,
            "usage": {"inputTokens": tokens_in, "outputTokens": tokens_out, "totalTokens": tokens_in + tokens_out},
            "route": route,
            "model_id": STUB_MODEL_ID,
        }

    @staticmethod
    def _pick_tool(body: dict, messages: list[dict]) -> str | None:
        tools = body.get("tools") or []
        if not tools or _has_tool_results(messages):
            return None
        asked = {w.rstrip("s") for w in re.findall(r"[a-z]+", _last_user_text(messages).lower()) if len(w) > 3}
        for tool in tools:
            name_words = {w.rstrip("s") for w in re.split(r"[_\W]+", str(tool.get("name", "")).lower()) if w}
            if asked & name_words:
                return tool["name"]
        return None


class BedrockModel:
    """Bedrock Converse through boto3 and the instance role (on EC2, through the bedrock-runtime VPC endpoint)."""

    def __init__(self, region: str, route_models: dict[str, str], *, timeout_s: float = 30.0, client=None) -> None:
        self.route_models = dict(route_models)
        self.client = client or boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=timeout_s, connect_timeout=5, retries={"max_attempts": 2, "mode": "standard"}),
        )

    def converse(self, route: str, body: dict, scenario: str | None) -> dict:
        model_id = self.route_models[route]
        request: dict = {
            "modelId": model_id,
            "messages": body["messages"],
            "inferenceConfig": {"maxTokens": body["max_tokens"]},
        }
        if body.get("system"):
            request["system"] = [{"text": body["system"]}]
        if body.get("tools"):
            request["toolConfig"] = {"tools": [
                {"toolSpec": {"name": t["name"], "description": t.get("description") or t["name"],
                              "inputSchema": {"json": t["input_schema"]}}}
                for t in body["tools"]
            ]}
        try:
            response = self.client.converse(**request)
        except ClientError as e:
            details = e.response.get("Error", {})
            code = details.get("Code", "ClientError")
            status = 429 if code == "ThrottlingException" else 502
            raise ModelError(status, "bedrock_error", f"{code}: {details.get('Message', '')}") from None
        except BotoCoreError as e:
            timeout = "timeout" in type(e).__name__.lower()
            raise ModelError(504 if timeout else 502, "bedrock_unreachable", type(e).__name__) from None
        usage = response.get("usage") or {}
        return {
            "output": response.get("output") or {},
            "stop_reason": response.get("stopReason"),
            "usage": {key: int(usage.get(key, 0)) for key in ("inputTokens", "outputTokens", "totalTokens")},
            "route": route,
            "model_id": model_id,
        }
