"""Black-box contract tests for scripts/gen_keys.py and scripts/render_secrets.py, from scripts/README.md only.

Each test builds a throwaway repo tree (a copy of config/ and the secret templates), runs the real scripts as
subprocesses with `--root <tree>` from an unrelated working directory, and inspects the files they leave behind.
The subprocess environment has every *_SECRET / key variable removed, so only the tree's files can feed the scripts.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import jwt as pyjwt
import pytest
import yaml
from copperline_common.jwt import JwksVerifier, key_id, load_private_key, sign, unverified_claims
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

PEM = Path(".secrets/idp-signing-key.pem")
SECRETS_ENV = Path(".secrets/secrets.env")
JWKS = Path("deploy/xc/generated/jwks.json")
TEMPLATES = Path("deploy/vk8s/secrets")
RENDERED = Path("deploy/vk8s/secrets/rendered")

CLIENT_SECRET_NAMES = {
    "agent-shopper": "AGENT_SHOPPER_SECRET",
    "agent-storeops": "AGENT_STOREOPS_SECRET",
    "agent-refunds": "AGENT_REFUNDS_SECRET",
    "svc-mcp": "SVC_MCP_SECRET",
    "granite-supply": "GRANITE_SUPPLY_SECRET",
}
GENERATED_NAMES = {*CLIENT_SECRET_NAMES.values(), "GATEWAY_API_KEY"}
EXPECTED_NAMES = GENERATED_NAMES | {"CLIENT_SECRETS_JSON"}
TOKEN_URLSAFE_32 = re.compile(r"[A-Za-z0-9_-]{43}")
PRIVATE_JWK_FIELDS = ("d", "p", "q", "dp", "dq", "qi", "oth")
DROP_FROM_ENV = {"GATEWAY_API_KEY", "CLIENT_SECRETS_JSON", "SIGNING_KEY_PEM", "SIGNING_KEY_PATH", "CONFIG_DIR"}

GRANITE_FROM_DOTENV = "granite-from-dotenv-0123456789abcdef"
GATEWAY_FROM_DOTENV = "gateway-from-dotenv-0123456789abcdef"


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not (k.endswith("_SECRET") or k in DROP_FROM_ENV)}


def run_script(script: str, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Not run from inside the tree, so a script that ignores --root fails loudly instead of passing by accident
    # (and never writes into the real checkout).
    assert (SCRIPTS / script).is_file(), f"scripts/{script} does not exist yet"
    cwd = root.parent / "elsewhere"
    cwd.mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--root", str(root), *args],
        cwd=cwd,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert not list(cwd.iterdir()), f"{script} wrote into its working directory instead of --root"
    if check:
        assert result.returncode == 0, f"{script} {args} exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
    return result


def gen_keys(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run_script("gen_keys.py", root, *args, check=check)


def render(root: Path, check: bool = True) -> subprocess.CompletedProcess:
    return run_script("render_secrets.py", root, check=check)


def unquote(value: str) -> str:
    """docker compose env_file quoting: bare, 'single' (literal) or "double" (with \\" escapes)."""
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        assert sep, f"{path.name}: not a NAME=value line: {line[:20]!r}"
        assert name.strip() not in values, f"{path.name}: duplicate {name.strip()}"
        values[name.strip()] = unquote(value.strip())
    return values


def secrets_env(root: Path) -> dict[str, str]:
    return read_env_file(root / SECRETS_ENV)


def client_secrets_json(root: Path) -> dict[str, str]:
    return json.loads(secrets_env(root)["CLIENT_SECRETS_JSON"])


def jwks(root: Path) -> dict:
    return json.loads((root / JWKS).read_text())


def private_key(root: Path) -> RSAPrivateKey:
    return load_private_key((root / PEM).read_text())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    files = sorted((root / ".secrets").iterdir()) + [root / JWKS]
    return {str(path.relative_to(root)): (path.read_bytes(), mode(path)) for path in files}


def rendered_files(root: Path) -> list[Path]:
    directory = root / RENDERED
    return sorted(directory.glob("*")) if directory.is_dir() else []


def rendered_docs(root: Path) -> dict[str, dict]:
    """Rendered Secrets keyed by metadata.name (file names are not part of the contract)."""
    docs = {}
    for path in rendered_files(root):
        doc = yaml.safe_load(path.read_text())
        docs[doc["metadata"]["name"]] = doc
    return docs


def sensitive_values(root: Path) -> list[str]:
    values = [value for name, value in secrets_env(root).items() if name in GENERATED_NAMES]
    pem_lines = [line for line in (root / PEM).read_text().splitlines() if line and not line.startswith("-----")]
    return values + pem_lines


def add_client(root: Path, client: dict) -> None:
    path = root / "config/personas.yaml"
    personas = yaml.safe_load(path.read_text())
    personas["clients"].append(client)
    path.write_text(yaml.safe_dump(personas, sort_keys=False))


# ---------------------------------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path) -> Path:
    """A fresh repo tree: config/ and the secret templates, nothing generated yet."""
    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "config", root / "config")
    (root / TEMPLATES).mkdir(parents=True)
    for template in sorted((REPO_ROOT / TEMPLATES).glob("*.template.yaml")):
        shutil.copy(template, root / TEMPLATES / template.name)
    return root


@pytest.fixture
def generated(tree) -> Path:
    gen_keys(tree)
    return tree


# ---------------------------------------------------------------------------------------------------------------------
# gen_keys.py
# ---------------------------------------------------------------------------------------------------------------------


def test_gen_keys_creates_0700_dir_with_0600_files(generated):
    assert mode(generated / ".secrets") == 0o700
    assert (generated / PEM).is_file() and (generated / SECRETS_ENV).is_file()
    for path in (generated / ".secrets").iterdir():
        assert mode(path) == 0o600, f"{path.name} is {oct(mode(path))}"


def test_signing_key_is_rsa_2048_pkcs8_pem(generated):
    text = (generated / PEM).read_text()
    assert text.startswith("-----BEGIN PRIVATE KEY-----")  # PKCS#8, not "BEGIN RSA PRIVATE KEY" (PKCS#1)
    assert text.rstrip().endswith("-----END PRIVATE KEY-----")
    assert private_key(generated).key_size == 2048


def test_secrets_env_has_exactly_the_expected_names(generated):
    values = secrets_env(generated)
    assert set(values) == EXPECTED_NAMES
    assert "BENCH_SECRET" not in values


def test_generated_values_are_token_urlsafe_32_and_distinct(generated):
    values = secrets_env(generated)
    for name in GENERATED_NAMES:
        assert TOKEN_URLSAFE_32.fullmatch(values[name]), f"{name} does not look like secrets.token_urlsafe(32)"
    assert len({values[name] for name in GENERATED_NAMES}) == len(GENERATED_NAMES)


def test_client_secrets_json_has_exactly_the_agent_service_partner_clients(generated):
    values = secrets_env(generated)
    all_secrets = client_secrets_json(generated)
    assert set(all_secrets) == set(CLIENT_SECRET_NAMES)
    for client_id, name in CLIENT_SECRET_NAMES.items():
        assert all_secrets[client_id] == values[name]


def test_client_list_comes_from_personas(tree):
    add_client(tree, {"client_id": "agent-pricing", "kind": "agent", "agent_id": "pricing", "agent_role": "pricing"})
    add_client(tree, {"client_id": "bench-2", "kind": "bench", "sub_prefix": "bench2-", "role": "customer"})
    gen_keys(tree)
    values = secrets_env(tree)
    assert TOKEN_URLSAFE_32.fullmatch(values["AGENT_PRICING_SECRET"])
    assert client_secrets_json(tree)["agent-pricing"] == values["AGENT_PRICING_SECRET"]
    assert "BENCH_2_SECRET" not in values and "bench-2" not in client_secrets_json(tree)


def test_second_run_without_rotate_changes_nothing(generated):
    before = snapshot(generated)
    gen_keys(generated)
    assert snapshot(generated) == before


def test_if_missing_creates_everything_on_a_fresh_tree(tree):
    gen_keys(tree, "--if-missing")
    assert set(secrets_env(tree)) == EXPECTED_NAMES
    assert (tree / JWKS).is_file()
    assert private_key(tree).key_size == 2048


def test_if_missing_is_quiet_and_changes_nothing_when_complete(generated):
    before = snapshot(generated)
    result = gen_keys(generated, "--if-missing")
    assert result.stdout.strip() == ""
    assert snapshot(generated) == before


def test_missing_files_are_recreated_without_touching_the_existing_key(generated):
    pem_before = (generated / PEM).read_bytes()
    kid_before = jwks(generated)["keys"][0]["kid"]
    (generated / SECRETS_ENV).unlink()
    (generated / JWKS).unlink()
    gen_keys(generated)
    assert (generated / PEM).read_bytes() == pem_before
    assert jwks(generated)["keys"][0]["kid"] == kid_before == key_id(private_key(generated))
    assert set(secrets_env(generated)) == EXPECTED_NAMES
    assert mode(generated / SECRETS_ENV) == 0o600


def test_client_added_later_gets_a_secret_and_others_keep_theirs(generated):
    before = secrets_env(generated)
    add_client(generated, {"client_id": "agent-pricing", "kind": "agent", "agent_id": "pricing"})
    gen_keys(generated)
    after = secrets_env(generated)
    assert TOKEN_URLSAFE_32.fullmatch(after["AGENT_PRICING_SECRET"])
    assert client_secrets_json(generated)["agent-pricing"] == after["AGENT_PRICING_SECRET"]
    for name in GENERATED_NAMES:
        assert after[name] == before[name], f"{name} changed without --rotate"


def test_rotate_replaces_the_key_and_every_secret(generated):
    before_values = secrets_env(generated)
    before_pem = (generated / PEM).read_bytes()
    before_kid = jwks(generated)["keys"][0]["kid"]
    gen_keys(generated, "--rotate")
    after_values = secrets_env(generated)
    assert (generated / PEM).read_bytes() != before_pem
    assert jwks(generated)["keys"][0]["kid"] == key_id(private_key(generated)) != before_kid
    assert set(after_values) == EXPECTED_NAMES
    for name in GENERATED_NAMES:
        assert after_values[name] != before_values[name], f"{name} was not rotated"
    assert mode(generated / ".secrets") == 0o700
    assert all(mode(path) == 0o600 for path in (generated / ".secrets").iterdir())


def test_dotenv_overrides_granite_supply_and_gateway_key(tree):
    (tree / ".env").write_text(
        f"XC_NAMESPACE=agentic-demo\nGRANITE_SUPPLY_SECRET={GRANITE_FROM_DOTENV}\nGATEWAY_API_KEY={GATEWAY_FROM_DOTENV}\n"
    )
    gen_keys(tree)
    values = secrets_env(tree)
    assert values["GRANITE_SUPPLY_SECRET"] == GRANITE_FROM_DOTENV
    assert values["GATEWAY_API_KEY"] == GATEWAY_FROM_DOTENV
    assert client_secrets_json(tree)["granite-supply"] == GRANITE_FROM_DOTENV
    assert TOKEN_URLSAFE_32.fullmatch(values["AGENT_SHOPPER_SECRET"])


def test_dotenv_override_still_applies_on_rotate(tree):
    (tree / ".env").write_text(f"GRANITE_SUPPLY_SECRET={GRANITE_FROM_DOTENV}\n")
    gen_keys(tree)
    shopper_before = secrets_env(tree)["AGENT_SHOPPER_SECRET"]
    gen_keys(tree, "--rotate")
    values = secrets_env(tree)
    assert values["GRANITE_SUPPLY_SECRET"] == GRANITE_FROM_DOTENV
    assert client_secrets_json(tree)["granite-supply"] == GRANITE_FROM_DOTENV
    assert values["AGENT_SHOPPER_SECRET"] != shopper_before


def test_dotenv_copied_from_example_does_not_override(tree):
    # .env.example has `GRANITE_SUPPLY_SECRET=           # comment` (empty value): that does not "set" it.
    shutil.copy(REPO_ROOT / ".env.example", tree / ".env")
    gen_keys(tree)
    values = secrets_env(tree)
    for name in ("GRANITE_SUPPLY_SECRET", "GATEWAY_API_KEY"):
        assert TOKEN_URLSAFE_32.fullmatch(values[name]), f"{name} = {values[name][:12]!r}..."


def test_jwks_is_public_only_and_matches_the_pem(generated):
    published = jwks(generated)
    assert len(published["keys"]) == 1
    jwk = published["keys"][0]
    assert jwk["kty"] == "RSA" and jwk["alg"] == "RS256"
    assert not [field for field in PRIVATE_JWK_FIELDS if field in jwk]

    key = private_key(generated)
    token = sign({"sub": "contract-test"}, key, ttl_s=60, issuer="https://idp.contract.test")
    assert unverified_claims(token)["sub"] == "contract-test"
    assert pyjwt.get_unverified_header(token)["kid"] == jwk["kid"] == key_id(key)
    claims = JwksVerifier(jwks_path=str(generated / JWKS)).verify(token)
    assert claims["sub"] == "contract-test"


def test_gen_keys_never_prints_secret_values(tree):
    first = gen_keys(tree)
    rotated = gen_keys(tree, "--rotate")
    output = first.stdout + first.stderr + rotated.stdout + rotated.stderr
    for value in sensitive_values(tree):
        assert value not in output


# ---------------------------------------------------------------------------------------------------------------------
# render_secrets.py
# ---------------------------------------------------------------------------------------------------------------------


def test_render_writes_valid_yaml_with_no_placeholders_left(generated):
    render(generated)
    files = rendered_files(generated)
    assert len(files) == len(list((generated / TEMPLATES).glob("*.template.yaml")))
    for path in files:
        text = path.read_text()
        assert "{{" not in text and "}}" not in text, path.name
        assert yaml.safe_load(text)["kind"] == "Secret"
    assert set(rendered_docs(generated)) == {"agent-credentials", "idp-signing-key"}


def test_render_agent_credentials_match_secrets_env(generated):
    render(generated)
    data = rendered_docs(generated)["agent-credentials"]["stringData"]
    values = secrets_env(generated)
    for client_id, name in CLIENT_SECRET_NAMES.items():
        assert data[client_id] == values[name]
    assert json.loads(data["_all_json"]) == client_secrets_json(generated)


def test_render_private_pem_block_contains_the_full_pem(generated):
    render(generated)
    rendered_pem = rendered_docs(generated)["idp-signing-key"]["stringData"]["private.pem"]
    assert rendered_pem.strip() == (generated / PEM).read_text().strip()
    assert key_id(load_private_key(rendered_pem)) == jwks(generated)["keys"][0]["kid"]


def test_render_honours_dotenv_override_end_to_end(tree):
    (tree / ".env").write_text(f"GRANITE_SUPPLY_SECRET={GRANITE_FROM_DOTENV}\n")
    gen_keys(tree)
    render(tree)
    data = rendered_docs(tree)["agent-credentials"]["stringData"]
    assert data["granite-supply"] == GRANITE_FROM_DOTENV
    assert json.loads(data["_all_json"])["granite-supply"] == GRANITE_FROM_DOTENV


def test_render_looks_up_secrets_env_first_then_dotenv(generated):
    (generated / ".env").write_text("DEMO_DOMAIN=demo.contract.test\nAGENT_SHOPPER_SECRET=from-dotenv-must-lose\n")
    (generated / TEMPLATES / "extra.template.yaml").write_text(
        "apiVersion: v1\nkind: Secret\nmetadata: { name: extra }\ntype: Opaque\nstringData:\n"
        '  domain: "{{ DEMO_DOMAIN }}"\n  shopper: "{{ AGENT_SHOPPER_SECRET }}"\n'
    )
    render(generated)
    data = rendered_docs(generated)["extra"]["stringData"]
    assert data["domain"] == "demo.contract.test"
    assert data["shopper"] == secrets_env(generated)["AGENT_SHOPPER_SECRET"]


def drop_line(path: Path, name: str) -> None:
    lines = [line for line in path.read_text().splitlines() if not line.startswith(f"{name}=")]
    path.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("case", ["unknown-placeholder", "name-missing-from-secrets-env", "no-secrets-dir"])
def test_render_with_a_missing_value_fails_and_writes_nothing(generated, case):
    if case == "unknown-placeholder":
        (generated / TEMPLATES / "extra.template.yaml").write_text(
            'apiVersion: v1\nkind: Secret\nmetadata: { name: extra }\nstringData:\n  x: "{{ NOT_SET_ANYWHERE }}"\n'
        )
    elif case == "name-missing-from-secrets-env":
        drop_line(generated / SECRETS_ENV, "GRANITE_SUPPLY_SECRET")
    else:
        shutil.rmtree(generated / ".secrets")

    result = render(generated, check=False)
    assert result.returncode != 0
    assert rendered_files(generated) == []
    if case == "unknown-placeholder":
        assert "NOT_SET_ANYWHERE" in result.stdout + result.stderr
    if case == "name-missing-from-secrets-env":
        assert "GRANITE_SUPPLY_SECRET" in result.stdout + result.stderr


def test_render_failure_leaves_earlier_output_untouched(generated):
    render(generated)
    before = {path.name: path.read_bytes() for path in rendered_files(generated)}
    gen_keys(generated, "--rotate")
    (generated / TEMPLATES / "extra.template.yaml").write_text(
        'apiVersion: v1\nkind: Secret\nmetadata: { name: extra }\nstringData:\n  x: "{{ NOT_SET_ANYWHERE }}"\n'
    )
    assert render(generated, check=False).returncode != 0
    assert {path.name: path.read_bytes() for path in rendered_files(generated)} == before


def test_render_never_prints_secret_values(generated):
    result = render(generated)
    output = result.stdout + result.stderr
    for value in sensitive_values(generated):
        assert value not in output
