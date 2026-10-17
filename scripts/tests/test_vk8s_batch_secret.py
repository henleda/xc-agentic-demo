"""The insights-nightly CronJob (S5) gets the env its scenario plan needs to log in at the real IdP.

S5's token steps run in plan_executor, which reads the client secret as credentials.client_secret(client_id),
i.e. AGENT_STOREOPS_SECRET for agent-storeops. The container env here is built from the real manifest
(deploy/vk8s/40-insights-cronjob.yaml), with secretKeyRef values taken from a fake agent-credentials Secret.
Test keys are generated here; nothing reads .secrets/.
"""

import asyncio
import dataclasses
import os
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from copperline_common.outbound import new_http_client
from copperline_common.plan_executor import ExecContext, PlanRun, run_plan
from copperline_common.scenarios import Scenario, by_id, load_scenarios
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from idp.app import create_app
from idp.settings import Settings

REPO = Path(__file__).resolve().parents[2]
CRONJOB = REPO / "deploy/vk8s/40-insights-cronjob.yaml"
IDP_BASE_URL = "http://idp.agentic-demo"  # runtime-env ConfigMap in deploy/vk8s/00-config.yaml
ISSUER = "https://idp.batch.test"
# The agent-credentials Secret (deploy/vk8s/secrets/agent-credentials.template.yaml): one key per client_id.
AGENT_CREDENTIALS = {
    "agent-shopper": "shopper-secret-0123456789",
    "agent-storeops": "storeops-secret-0123456789",
    "agent-refunds": "refunds-secret-0123456789",
    "svc-mcp": "svc-mcp-secret-0123456789",
    "granite-supply": "granite-secret-0123456789",
}


def cronjob_container_env(manifest: Path, secret: dict[str, str]) -> dict[str, str]:
    """The env of the CronJob's container: plain values, and secretKeyRef values read from `secret`."""
    cronjob = yaml.safe_load(manifest.read_text())
    container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    env: dict[str, str] = {}
    for var in container["env"]:
        if "value" in var:
            env[var["name"]] = var["value"]
            continue
        ref = var["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "agent-credentials", f"{var['name']} reads an unexpected Secret"
        env[var["name"]] = secret[ref["key"]]
    return env


@pytest.fixture
def idp_settings(tmp_path) -> Settings:
    """The IdP as deployed: flat ConfigMap layout, client secrets from agent-credentials _all_json."""
    shutil.copy(REPO / "config/personas.yaml", tmp_path / "personas.yaml")
    shutil.copy(REPO / "config/policy/tool-access-matrix.yaml", tmp_path / "tool-access-matrix.yaml")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return Settings(
        config_dir=tmp_path, issuer=ISSUER, signing_key_pem=pem.decode(), client_secrets=dict(AGENT_CREDENTIALS)
    )


@pytest.fixture
def batch_env(monkeypatch) -> dict[str, str]:
    """Process env = exactly the secrets the CronJob container would get (none leak in from the test runner)."""
    for name in list(os.environ):
        if name.endswith("_SECRET"):
            monkeypatch.delenv(name)
    env = cronjob_container_env(CRONJOB, AGENT_CREDENTIALS)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


def token_steps_only(scenario: Scenario) -> Scenario:
    """S5's token steps are its first two plan entries, so their indexes (i=0, i=1) stay the same."""
    return dataclasses.replace(scenario, plan=[step for step in scenario.plan if "token" in step])


def run_against_idp(scenario: Scenario, settings: Settings, env: dict[str, str]) -> PlanRun:
    async def go() -> PlanRun:
        transport = httpx.ASGITransport(app=create_app(settings))
        async with new_http_client(transport=transport) as http:
            ctx = ExecContext(
                http=http,
                tools_base_url=None,
                llm_base_url=None,
                idp_base_url=IDP_BASE_URL,
                tokens={},
                lb_base_urls=(IDP_BASE_URL,),
                scenario_id=scenario.id,
                env=env,
            )
            return await run_plan(scenario, ctx)

    return asyncio.run(go())


def test_insights_cronjob_sets_the_secret_variable_s5_token_steps_read(batch_env):
    assert batch_env.get("AGENT_STOREOPS_SECRET") == AGENT_CREDENTIALS["agent-storeops"]  # the copied credential


def test_s5_token_steps_get_machine_tokens_with_the_cronjob_env(batch_env, idp_settings):
    s5 = by_id(load_scenarios(REPO / "scenarios"), "S5")
    expected = {step["i"]: step["status"] for step in s5.expect["tenant"]["steps"] if step["i"] in (0, 1)}

    run = run_against_idp(token_steps_only(s5), idp_settings, batch_env)

    assert {step.i: step.status for step in run.steps} == expected == {0: 200, 1: 200}
    assert [step.blocked_by for step in run.steps] == [None, None]
    for step in run.steps:
        assert step.identity["agent_role"] == "storeops"
        assert (step.identity["obo_sub"], step.identity["obo_role"]) == ("none", "none")
    assert [step.identity["aud"] for step in run.steps] == ["copperline-tools", "copperline-llm"]
