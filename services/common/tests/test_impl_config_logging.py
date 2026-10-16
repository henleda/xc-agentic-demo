import logging
import re

import pytest
from copperline_common import config
from copperline_common.logging import log_event, setup_logging
from copperline_common.requestid import agent_id_var, obo_sub_var, request_id_var

STANDARD_FIELDS = ("ts", "service", "level", "request_id", "agent_id", "obo_sub", "event", "status", "latency_ms")


def test_config_dir_defaults_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv("CONFIG_DIR", raising=False)
    assert config.config_dir() == config.Path("/config")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert config.config_file("tools.yaml") == tmp_path / "tools.yaml"


def test_load_yaml_and_missing_file(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("a: [1, 2]\n")
    assert config.load_yaml(path) == {"a": [1, 2]}
    with pytest.raises(FileNotFoundError):
        config.load_yaml(tmp_path / "missing.yaml")


def test_repo_root_finds_the_checkout(repo_root):
    assert config.repo_root() == repo_root


def test_log_line_has_all_standard_fields(read_logs):
    setup_logging("unit-test")
    log_event("hello", extra_key=5)
    record = [line for line in read_logs() if line.get("event") == "hello"][-1]
    for key in STANDARD_FIELDS:
        assert key in record
    assert record["service"] == "unit-test"
    assert record["level"] == "info"
    assert record["status"] is None and record["latency_ms"] is None
    assert record["extra_key"] == 5
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record["ts"])


def test_log_uses_context_variables(read_logs):
    setup_logging("unit-test")
    tokens = [request_id_var.set("rid-1"), agent_id_var.set("shopper"), obo_sub_var.set("alex.rivera")]
    try:
        log_event("ctx")
    finally:
        for var, token in zip((request_id_var, agent_id_var, obo_sub_var), tokens, strict=True):
            var.reset(token)
    record = [line for line in read_logs() if line.get("event") == "ctx"][-1]
    assert (record["request_id"], record["agent_id"], record["obo_sub"]) == ("rid-1", "shopper", "alex.rivera")


def test_redaction_is_recursive(read_logs):
    setup_logging("unit-test")
    log_event(
        "redact",
        client_secret="s3cr3t",
        Authorization="Bearer abc",
        nested={"access_token": "t", "keep": "visible", "deeper": {"API_KEY": "k", "Password": "p"}},
        items=[{"refresh_token": "r"}],
    )
    records = read_logs()
    record = [line for line in records if line.get("event") == "redact"][-1]
    assert record["client_secret"] == "***"
    assert record["Authorization"] == "***"
    assert record["nested"] == {
        "access_token": "***", "keep": "visible", "deeper": {"API_KEY": "***", "Password": "***"}
    }
    assert record["items"] == [{"refresh_token": "***"}]
    assert "s3cr3t" not in str(records)


def test_setup_logging_is_idempotent_and_reads_level(monkeypatch, read_logs):
    setup_logging("unit-test")
    setup_logging("unit-test")
    root = logging.getLogger()
    ours = [handler for handler in root.handlers if type(handler).__name__ == "_StdoutHandler"]
    assert len(ours) == 1

    monkeypatch.setenv("LOG_LEVEL", "warning")
    setup_logging("unit-test")
    log_event("dropped-info")
    log_event("kept-warning", level="warning")
    events = [line.get("event") for line in read_logs()]
    assert "dropped-info" not in events and "kept-warning" in events
    setup_logging("unit-test", level="info")
