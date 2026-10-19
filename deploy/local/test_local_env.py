"""deploy/local/local_env.py: which .env settings reach local compose, and the half-configured Bedrock check."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("local_env", Path(__file__).with_name("local_env.py"))
local_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_env)


def test_only_the_allow_listed_names_pass_and_the_shell_wins(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ROUTE_FAST=from-file\nGATEWAY_API_KEY=never-passed\nAWS_REGION=eu-west-1   # comment\n"
                   "BUDGET_TOKENS_FAST=\n")
    values = local_env.local_settings(env, {"ROUTE_FAST": "from-shell", "AWS_PROFILE": "x"})
    assert values == {"ROUTE_FAST": "from-shell", "AWS_REGION": "eu-west-1"}


def test_a_missing_env_file_is_fine(tmp_path):
    assert local_env.local_settings(tmp_path / "nope", {}) == {}


def test_stub_off_needs_a_route():
    assert "ROUTE_" in local_env.check({"LOCAL_STUB_MODEL": "false"})
    assert local_env.check({"LOCAL_STUB_MODEL": "false", "ROUTE_FAST": "m"}) is None
    assert local_env.check({}) is None  # stub by default
    assert "true or false" in local_env.check({"LOCAL_STUB_MODEL": "maybe"})
