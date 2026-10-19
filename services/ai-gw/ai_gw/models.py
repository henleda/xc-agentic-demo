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
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)
from copperline_common.config import load_yaml
from copperline_common.logging import log_event

STUB_REPLIES = Path(__file__).resolve().parent / "stub_replies.yaml"
STUB_MODEL_ID = "stub"


# Bedrock error codes that mean "the model did not run", so trying once more is safe and cannot double-bill.
RETRYABLE_CODES = ("ThrottlingException", "ServiceUnavailableException")
TIMEOUT_CODES = ("ModelTimeoutException",)


class ModelError(Exception):
    """The model call failed. status is what /v1/converse answers: 400 shape rejected before sending, 429 throttled,
    502 upstream error or unreachable, 504 timeout. message never holds request content or AWS account details."""

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
    """Bedrock Converse through boto3 and the instance role (on EC2, through the bedrock-runtime VPC endpoint).

    Timeouts: one read timeout of timeout_s, never retried. A read timeout means the model may already be running
    (and billing), and llm-lb gives up soon after anyway, so a retry would only produce a reply nobody receives.
    Only "the model did not run" failures (connect errors, throttling, service unavailable) get one more try.
    timeout_s must stay below llm-lb's route timeout (docs/08 R39), and the agent's model-call timeout above it.
    """

    ATTEMPTS = 2  # for retryable failures only

    def __init__(self, region: str, route_models: dict[str, str], *, timeout_s: float = 25.0, client=None) -> None:
        self.route_models = dict(route_models)
        self.client = client or boto3.client(
            "bedrock-runtime",
            region_name=region,
            # botocore's own retries are off: _call() decides what is safe to retry.
            config=Config(read_timeout=timeout_s, connect_timeout=5,
                          retries={"total_max_attempts": 1, "mode": "standard"}),
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
        response = self._call(request, route)
        usage = response.get("usage") or {}
        return {
            "output": response.get("output") or {},
            "stop_reason": response.get("stopReason"),
            "usage": {key: int(usage.get(key, 0)) for key in ("inputTokens", "outputTokens", "totalTokens")},
            "route": route,
            "model_id": model_id,
        }

    def _call(self, request: dict, route: str) -> dict:
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                return self.client.converse(**request)
            except ParamValidationError:
                # botocore checked the shape before sending anything. The caller's fault; never echo str(e): it
                # contains the offending request values (message content).
                raise ModelError(400, "bad_request", "message shape rejected by the Bedrock client") from None
            except ClientError as e:
                details = e.response.get("Error", {})
                code = details.get("Code", "ClientError")
                # The full message can name the AWS account, role and instance (an IAM denial does): log it here,
                # return only the code. The trace shows the response body on stage.
                log_event("bedrock_error", level="warning", route=route, code=code, attempt=attempt,
                          message=details.get("Message", ""))
                if code in RETRYABLE_CODES and attempt < self.ATTEMPTS:
                    continue
                if code in TIMEOUT_CODES:
                    raise ModelError(504, "bedrock_timeout", code) from None
                raise ModelError(429 if code == "ThrottlingException" else 502, "bedrock_error", code) from None
            except (ReadTimeoutError, ConnectTimeoutError) as e:
                log_event("bedrock_error", level="warning", route=route, code=type(e).__name__, attempt=attempt)
                if isinstance(e, ConnectTimeoutError) and attempt < self.ATTEMPTS:
                    continue  # never connected, so the model never ran
                raise ModelError(504, "bedrock_timeout", type(e).__name__) from None
            except EndpointConnectionError as e:
                log_event("bedrock_error", level="warning", route=route, code=type(e).__name__, attempt=attempt)
                if attempt < self.ATTEMPTS:
                    continue
                raise ModelError(502, "bedrock_unreachable", type(e).__name__) from None
            except BotoCoreError as e:  # anything else from botocore: report it, never retry
                log_event("bedrock_error", level="warning", route=route, code=type(e).__name__, attempt=attempt)
                raise ModelError(502, "bedrock_unreachable", type(e).__name__) from None
        raise AssertionError("unreachable")  # the loop always returns or raises
