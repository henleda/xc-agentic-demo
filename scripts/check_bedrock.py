#!/usr/bin/env python3
"""Phase 0 acceptance (docs/07): Bedrock Converse works from EC2 copperline-core through the VPC endpoint.

Usage:
    uv run python scripts/check_bedrock.py                 # from the laptop; needs the AWS CLI, SSM access and .env

For each route model in .env (ROUTE_FAST / ROUTE_STANDARD / ROUTE_PREMIUM) it checks, and prints one line per check:

    agreement   the account's AWS Marketplace agreement for the model is AVAILABLE (docs/08 R24)
    endpoint    bedrock-runtime resolves to a private address on the host (the interface endpoint's private DNS)
    converse    on the host, as its instance role, a Converse call with ai-gw's request shape (maxTokens 400, one
                tool) returns a toolUse block; done twice, PAUSE_S apart, because a model whose Marketplace
                subscription failed answers its first call and is then denied (R24)

The host has Docker but no Python SDK, so the calls run in a throwaway python container (which also shows that
containers on the host get the instance role, as ai-gw does). Exit 0 only if every check is ok.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import read_env_file  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ROUTES = ("ROUTE_FAST", "ROUTE_STANDARD", "ROUTE_PREMIUM")
PAUSE_S = 150
PYTHON_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"

# Runs on the host inside the container. Prints one JSON line per model.
CONVERSE = r'''
import json, sys, boto3, socket
region = sys.argv[1]
client = boto3.client("bedrock-runtime", region_name=region)
host = client.meta.endpoint_url.split("//")[1]
print(json.dumps({"endpoint": host, "address": socket.gethostbyname(host)}))
schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
tool = {"toolSpec": {"name": "catalog_search", "description": "Search the catalog", "inputSchema": {"json": schema}}}
message = [{"role": "user", "content": [{"text": "Find me a 2-person tent."}]}]
for model in sys.argv[2:]:
    try:
        r = client.converse(modelId=model, messages=message, inferenceConfig={"maxTokens": 400},
                            toolConfig={"tools": [tool]})
        kinds = [list(block)[0] for block in r["output"]["message"]["content"]]
        print(json.dumps({"model": model, "ok": "toolUse" in kinds, "detail": f"{r['stopReason']} {kinds}"}))
    except Exception as e:  # noqa: BLE001 - every failure is a result line
        print(json.dumps({"model": model, "ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"}))
'''


def aws(env: dict[str, str], *args: str) -> str:
    command = ["aws", *args, "--region", env["AWS_REGION"], "--output", "json"]
    if env.get("AWS_PROFILE"):
        command += ["--profile", env["AWS_PROFILE"]]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def core_instance_id(env: dict[str, str]) -> str:
    out = json.loads(aws(env, "ec2", "describe-instances", "--filters", "Name=tag:Name,Values=copperline-core",
                         "Name=instance-state-name,Values=running"))
    ids = [i["InstanceId"] for r in out["Reservations"] for i in r["Instances"]]
    if len(ids) != 1:
        raise SystemExit(f"expected one running copperline-core instance in {env['AWS_REGION']}, found {ids}")
    return ids[0]


def run_on_host(env: dict[str, str], instance: str, models: list[str]) -> list[dict]:
    """The CONVERSE script in a python container on the host, through SSM. Returns its JSON lines."""
    script = base64.b64encode(CONVERSE.encode()).decode()
    inner = (f"echo {script} | base64 -d > /tmp/converse.py && docker run --rm -v /tmp/converse.py:/c.py:ro "
             f"{PYTHON_IMAGE} sh -c 'pip -q install boto3 >/dev/null 2>&1; python /c.py {env['AWS_REGION']} "
             + " ".join(models) + "'")
    sent = json.loads(aws(env, "ssm", "send-command", "--instance-ids", instance, "--document-name",
                          "AWS-RunShellScript", "--timeout-seconds", "300",
                          "--parameters", json.dumps({"commands": [inner]})))
    command_id = sent["Command"]["CommandId"]
    for _ in range(60):
        time.sleep(5)
        try:
            result = json.loads(aws(env, "ssm", "get-command-invocation", "--command-id", command_id,
                                    "--instance-id", instance))
        except subprocess.CalledProcessError:
            continue  # not registered yet
        if result["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
            lines = [line for line in result["StandardOutputContent"].splitlines() if line.startswith("{")]
            if not lines:
                error = result["StandardErrorContent"][-400:]
                raise SystemExit(f"no result from the host ({result['Status']}): {error}")
            return [json.loads(line) for line in lines]
    raise SystemExit("the host did not answer within 5 minutes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pause", type=int, default=PAUSE_S, help="seconds between the two Converse rounds")
    args = parser.parse_args(argv)

    env = read_env_file(REPO / ".env")
    models = [env[r] for r in ROUTES if env.get(r)]
    if not env.get("AWS_REGION") or not models:
        print("FAIL  .env needs AWS_REGION and at least one ROUTE_*", flush=True)
        return 1
    ok = True

    for model in models:
        base = model.split(".", 1)[1] if model.startswith(("us.", "eu.", "apac.", "global.")) else model
        status = json.loads(aws(env, "bedrock", "get-foundation-model-availability", "--model-id", base))
        agreed = status.get("agreementAvailability", {}).get("status")
        ok &= agreed == "AVAILABLE"
        print(f"{'ok  ' if agreed == 'AVAILABLE' else 'FAIL'}  agreement  {base}: {agreed}", flush=True)

    instance = core_instance_id(env)
    for round_ in (1, 2):
        if round_ == 2:
            print(f"      (waiting {args.pause} s before the second round)", flush=True)
            time.sleep(args.pause)
        lines = run_on_host(env, instance, models)
        if round_ == 1:
            where = lines[0]
            private = where["address"].startswith(("10.", "172.", "192.168."))
            ok &= private
            print(f"{'ok  ' if private else 'FAIL'}  endpoint   {where['endpoint']} -> {where['address']}", flush=True)
        for line in lines[1:]:
            ok &= line["ok"]
            print(f"{'ok  ' if line['ok'] else 'FAIL'}  converse   round {round_} {line['model']}: {line['detail']}",
                  flush=True)

    print("all ok" if ok else "FAILED", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
