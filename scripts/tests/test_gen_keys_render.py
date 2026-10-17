"""gen_keys.py and render_secrets.py, run against a scratch repo root (never the real .secrets/)."""

import json
import shutil
import stat
from pathlib import Path

import envfile
import gen_keys
import pytest
import render_secrets
import yaml
from copperline_common.jwt import JwksVerifier, load_private_key, sign
from fastapi.testclient import TestClient
from idp.app import create_app
from idp.settings import Settings

REPO = Path(__file__).resolve().parents[2]
CLIENT_SECRET_NAMES = [
    "AGENT_SHOPPER_SECRET",
    "AGENT_STOREOPS_SECRET",
    "AGENT_REFUNDS_SECRET",
    "SVC_MCP_SECRET",
    "GRANITE_SUPPLY_SECRET",
]


@pytest.fixture
def root(tmp_path) -> Path:
    """A minimal checkout: personas.yaml, the matrix and the real Secret templates."""
    (tmp_path / "config" / "policy").mkdir(parents=True)
    shutil.copy(REPO / "config/personas.yaml", tmp_path / "config/personas.yaml")
    shutil.copy(REPO / "config/policy/tool-access-matrix.yaml", tmp_path / "config/policy/tool-access-matrix.yaml")
    templates = tmp_path / "deploy/vk8s/secrets"
    templates.mkdir(parents=True)
    for template in (REPO / "deploy/vk8s/secrets").glob("*.template.yaml"):
        shutil.copy(template, templates / template.name)
    return tmp_path


def secrets_env(root: Path) -> dict[str, str]:
    return envfile.read_env_file(root / ".secrets/secrets.env")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---- gen_keys -------------------------------------------------------------------------------------------------------


def test_gen_keys_creates_everything_with_private_modes(root, capsys):
    assert gen_keys.main(["--root", str(root)]) == 0
    assert mode(root / ".secrets") == 0o700
    assert mode(root / ".secrets/idp-signing-key.pem") == 0o600
    assert mode(root / ".secrets/secrets.env") == 0o600
    assert (root / ".secrets/idp-signing-key.pem").read_text().startswith("-----BEGIN PRIVATE KEY-----")  # PKCS#8
    assert load_private_key((root / ".secrets/idp-signing-key.pem").read_bytes()).key_size == 2048

    values = secrets_env(root)
    assert list(values) == [*CLIENT_SECRET_NAMES, "GATEWAY_API_KEY", "CLIENT_SECRETS_JSON"]
    assert all(len(values[name]) >= 43 for name in [*CLIENT_SECRET_NAMES, "GATEWAY_API_KEY"])  # token_urlsafe(32)
    assert len(set(values.values())) == len(values)
    assert json.loads(values["CLIENT_SECRETS_JSON"]) == {
        "agent-shopper": values["AGENT_SHOPPER_SECRET"],
        "agent-storeops": values["AGENT_STOREOPS_SECRET"],
        "agent-refunds": values["AGENT_REFUNDS_SECRET"],
        "svc-mcp": values["SVC_MCP_SECRET"],
        "granite-supply": values["GRANITE_SUPPLY_SECRET"],
    }  # no bench: it is not issued tokens

    out = capsys.readouterr().out
    assert not [value for value in values.values() if value in out]


def test_token_signed_with_gen_keys_key_verifies_against_its_jwks(root):
    gen_keys.main(["--root", str(root)])
    key = load_private_key((root / ".secrets/idp-signing-key.pem").read_bytes())
    jwks = json.loads((root / "deploy/xc/generated/jwks.json").read_text())
    token = sign({"sub": "alex.rivera", "aud": "copperline-shop"}, key, ttl_s=60, issuer="https://idp.copperline.demo")
    verifier = JwksVerifier(jwks, issuer="https://idp.copperline.demo")
    assert verifier.verify(token, audience="copperline-shop")["sub"] == "alex.rivera"
    assert not {"d", "p", "q"} & set(jwks["keys"][0])


def test_idp_runs_on_gen_keys_output(root):
    """The files gen_keys writes are exactly what the idp consumes, and its tokens verify against jwks.json."""
    gen_keys.main(["--root", str(root)])
    values = secrets_env(root)
    settings = Settings(
        config_dir=root / "config",
        issuer="https://idp.copperline.demo",
        signing_key_pem=(root / ".secrets/idp-signing-key.pem").read_text(),
        client_secrets=json.loads(values["CLIENT_SECRETS_JSON"]),
    )
    form = {
        "grant_type": "client_credentials",
        "client_id": "agent-storeops",
        "client_secret": values["AGENT_STOREOPS_SECRET"],
        "audience": "copperline-tools",
    }
    with TestClient(create_app(settings)) as client:
        response = client.post("/auth/token", data=form)
    assert response.status_code == 200, response.text
    jwks = json.loads((root / "deploy/xc/generated/jwks.json").read_text())
    claims = JwksVerifier(jwks, issuer="https://idp.copperline.demo").verify(response.json()["access_token"])
    assert claims["obo_role"] == "none"


def test_gen_keys_never_overwrites_without_rotate(root, capsys):
    gen_keys.main(["--root", str(root)])
    pem = (root / ".secrets/idp-signing-key.pem").read_bytes()
    values = secrets_env(root)
    jwks = (root / "deploy/xc/generated/jwks.json").read_text()
    capsys.readouterr()

    assert gen_keys.main(["--root", str(root)]) == 0
    assert "nothing changed" in capsys.readouterr().out
    assert gen_keys.main(["--root", str(root), "--if-missing"]) == 0
    assert capsys.readouterr().out == ""  # quiet when everything exists
    assert (root / ".secrets/idp-signing-key.pem").read_bytes() == pem
    assert secrets_env(root) == values
    assert (root / "deploy/xc/generated/jwks.json").read_text() == jwks


def test_gen_keys_fills_in_only_what_is_missing(root):
    gen_keys.main(["--root", str(root)])
    before = secrets_env(root)
    jwks_path = root / "deploy/xc/generated/jwks.json"
    jwks_path.unlink()
    kept = {name: value for name, value in before.items() if name != "SVC_MCP_SECRET"}
    (root / ".secrets/secrets.env").write_text(envfile.format_env_file(kept, "hand-edited"))

    gen_keys.main(["--root", str(root), "--if-missing"])
    after = secrets_env(root)
    assert after["SVC_MCP_SECRET"] and after["SVC_MCP_SECRET"] != before["SVC_MCP_SECRET"]
    assert {k: v for k, v in after.items() if k not in ("SVC_MCP_SECRET", "CLIENT_SECRETS_JSON")} == {
        k: v for k, v in before.items() if k not in ("SVC_MCP_SECRET", "CLIENT_SECRETS_JSON")
    }
    assert json.loads(after["CLIENT_SECRETS_JSON"])["svc-mcp"] == after["SVC_MCP_SECRET"]
    assert jwks_path.is_file()


def test_gen_keys_rotate_replaces_key_and_every_secret(root):
    gen_keys.main(["--root", str(root)])
    pem = (root / ".secrets/idp-signing-key.pem").read_bytes()
    jwks = (root / "deploy/xc/generated/jwks.json").read_text()
    before = secrets_env(root)

    assert gen_keys.main(["--root", str(root), "--rotate"]) == 0
    after = secrets_env(root)
    assert (root / ".secrets/idp-signing-key.pem").read_bytes() != pem
    assert (root / "deploy/xc/generated/jwks.json").read_text() != jwks
    assert all(after[name] != before[name] for name in before)
    assert mode(root / ".secrets/idp-signing-key.pem") == 0o600


def test_dotenv_overrides_granite_and_gateway_even_on_rotate(root):
    (root / ".env").write_text(
        "XC_TENANT=demo\n"
        "GRANITE_SUPPLY_SECRET=granite-from-dotenv   # rendered into agent-credentials\n"
        "GATEWAY_API_KEY=gw-from-dotenv\n"
        "AGENT_SHOPPER_SECRET=ignored-not-an-override\n"
    )
    gen_keys.main(["--root", str(root)])
    values = secrets_env(root)
    assert values["GRANITE_SUPPLY_SECRET"] == "granite-from-dotenv"
    assert values["GATEWAY_API_KEY"] == "gw-from-dotenv"
    assert values["AGENT_SHOPPER_SECRET"] != "ignored-not-an-override"
    assert json.loads(values["CLIENT_SECRETS_JSON"])["granite-supply"] == "granite-from-dotenv"

    gen_keys.main(["--root", str(root), "--rotate"])
    assert secrets_env(root)["GRANITE_SUPPLY_SECRET"] == "granite-from-dotenv"


def test_empty_dotenv_value_does_not_override(root):
    (root / ".env").write_text("GATEWAY_API_KEY=                 # generated; injected by XC llm-lb\n")
    gen_keys.main(["--root", str(root)])
    assert len(secrets_env(root)["GATEWAY_API_KEY"]) >= 43


def test_dotenv_change_is_picked_up_without_rotate(root):
    gen_keys.main(["--root", str(root)])
    (root / ".env").write_text("GRANITE_SUPPLY_SECRET=new-granite-value\n")
    gen_keys.main(["--root", str(root), "--if-missing"])
    assert secrets_env(root)["GRANITE_SUPPLY_SECRET"] == "new-granite-value"


def test_new_client_in_personas_gets_a_secret(root):
    personas_path = root / "config/personas.yaml"
    personas = yaml.safe_load(personas_path.read_text())
    personas["clients"].append({"client_id": "agent-new.thing", "kind": "agent", "agent_id": "x", "agent_role": "x"})
    personas_path.write_text(yaml.safe_dump(personas))
    gen_keys.main(["--root", str(root)])
    values = secrets_env(root)
    assert values["AGENT_NEW_THING_SECRET"]
    assert json.loads(values["CLIENT_SECRETS_JSON"])["agent-new.thing"] == values["AGENT_NEW_THING_SECRET"]


def remove_client(root: Path, client_id: str) -> None:
    personas_path = root / "config/personas.yaml"
    personas = yaml.safe_load(personas_path.read_text())
    personas["clients"] = [c for c in personas["clients"] if c["client_id"] != client_id]
    personas_path.write_text(yaml.safe_dump(personas, sort_keys=False))


def test_client_removed_from_personas_keeps_its_secret_until_rotate(root, capsys):
    """Dropping a client (or mistyping its id) must not delete its secret, or putting it back would rotate it."""
    personas_text = (root / "config/personas.yaml").read_text()
    gen_keys.main(["--root", str(root)])
    original_file = (root / ".secrets/secrets.env").read_text()
    original = secrets_env(root)
    capsys.readouterr()

    remove_client(root, "svc-mcp")
    assert gen_keys.main(["--root", str(root), "--if-missing"]) == 0
    kept = secrets_env(root)
    assert kept["SVC_MCP_SECRET"] == original["SVC_MCP_SECRET"]
    assert "svc-mcp" not in json.loads(kept["CLIENT_SECRETS_JSON"])  # the idp stops accepting it meanwhile
    assert {k: v for k, v in kept.items() if k != "CLIENT_SECRETS_JSON"} == {
        k: v for k, v in original.items() if k != "CLIENT_SECRETS_JSON"
    }
    captured = capsys.readouterr()
    assert "warning: kept SVC_MCP_SECRET" in captured.err
    assert not [value for value in original.values() if value in captured.out + captured.err]

    (root / "config/personas.yaml").write_text(personas_text)
    assert gen_keys.main(["--root", str(root), "--if-missing"]) == 0
    assert "new:" not in capsys.readouterr().out
    assert (root / ".secrets/secrets.env").read_text() == original_file  # back to byte-identical, nothing rotated

    remove_client(root, "svc-mcp")
    assert gen_keys.main(["--root", str(root), "--rotate"]) == 0
    assert "SVC_MCP_SECRET" not in secrets_env(root)
    assert "warning: dropped SVC_MCP_SECRET" in capsys.readouterr().err


def typo_in_clients_key(personas_path: Path) -> None:
    personas_path.write_text(personas_path.read_text().replace("\nclients:", "\nclientz:"))


def only_bench_clients(personas_path: Path) -> None:
    personas = yaml.safe_load(personas_path.read_text())
    personas["clients"] = [c for c in personas["clients"] if c["kind"] == "bench"]
    personas_path.write_text(yaml.safe_dump(personas, sort_keys=False))


@pytest.mark.parametrize("break_personas", [typo_in_clients_key, only_bench_clients])
def test_personas_without_secret_clients_exits_2_and_writes_nothing(root, capsys, break_personas):
    gen_keys.main(["--root", str(root)])
    before = {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()}
    break_personas(root / "config/personas.yaml")

    for args in ([], ["--if-missing"], ["--rotate"]):
        assert gen_keys.main(["--root", str(root), *args]) == 2
        assert "no clients of kind agent, service, partner" in capsys.readouterr().err
    assert {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()} == before


def test_gen_keys_tightens_loose_modes(root):
    gen_keys.main(["--root", str(root)])
    (root / ".secrets").chmod(0o755)
    (root / ".secrets/secrets.env").chmod(0o644)
    gen_keys.main(["--root", str(root), "--if-missing"])
    assert mode(root / ".secrets") == 0o700
    assert mode(root / ".secrets/secrets.env") == 0o600


def test_gen_keys_bad_input_exits_2(root, capsys):
    (root / ".env").write_text("GATEWAY_API_KEY=has$dollar\n")
    assert gen_keys.main(["--root", str(root)]) == 2
    assert "GATEWAY_API_KEY" in capsys.readouterr().err

    (root / ".env").unlink()
    (root / "config/personas.yaml").unlink()
    assert gen_keys.main(["--root", str(root)]) == 2


def test_bad_dotenv_writes_nothing_even_on_rotate(root):
    (root / ".env").write_text("GATEWAY_API_KEY=has$dollar\n")
    assert gen_keys.main(["--root", str(root)]) == 2
    assert not (root / ".secrets").exists() and not (root / "deploy/xc/generated/jwks.json").exists()

    (root / ".env").unlink()
    gen_keys.main(["--root", str(root)])
    before = {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()}
    jwks_before = (root / "deploy/xc/generated/jwks.json").read_bytes()
    (root / ".env").write_text("GATEWAY_API_KEY=has$dollar\n")
    assert gen_keys.main(["--root", str(root), "--rotate"]) == 2
    assert {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()} == before
    assert (root / "deploy/xc/generated/jwks.json").read_bytes() == jwks_before


def test_gen_keys_refuses_a_corrupt_key_unless_rotating(root):
    gen_keys.main(["--root", str(root)])
    (root / ".secrets/idp-signing-key.pem").write_text("not a key")
    assert gen_keys.main(["--root", str(root)]) == 2
    assert gen_keys.main(["--root", str(root), "--rotate"]) == 0


def test_rotate_and_if_missing_are_exclusive(root):
    with pytest.raises(SystemExit):
        gen_keys.main(["--root", str(root), "--rotate", "--if-missing"])


# ---- render_secrets -------------------------------------------------------------------------------------------------


def rendered(root: Path, name: str) -> dict:
    return yaml.safe_load((root / "deploy/vk8s/secrets/rendered" / name).read_text())


def test_render_fills_every_placeholder_and_round_trips(root):
    gen_keys.main(["--root", str(root)])
    assert render_secrets.main(["--root", str(root)]) == 0
    values = secrets_env(root)
    pem = (root / ".secrets/idp-signing-key.pem").read_text()

    creds = rendered(root, "agent-credentials.yaml")
    assert creds["kind"] == "Secret" and creds["metadata"]["name"] == "agent-credentials"
    assert creds["stringData"] == {
        "agent-shopper": values["AGENT_SHOPPER_SECRET"],
        "agent-storeops": values["AGENT_STOREOPS_SECRET"],
        "agent-refunds": values["AGENT_REFUNDS_SECRET"],
        "svc-mcp": values["SVC_MCP_SECRET"],
        "granite-supply": values["GRANITE_SUPPLY_SECRET"],
        "_all_json": values["CLIENT_SECRETS_JSON"],
    }
    assert json.loads(creds["stringData"]["_all_json"])["agent-refunds"] == values["AGENT_REFUNDS_SECRET"]

    key_secret = rendered(root, "idp-signing-key.yaml")
    assert key_secret["stringData"]["private.pem"] == pem  # literal block scalar keeps the trailing newline
    assert load_private_key(key_secret["stringData"]["private.pem"])

    out_dir = root / "deploy/vk8s/secrets/rendered"
    assert mode(out_dir) == 0o700
    assert all(mode(path) == 0o600 for path in out_dir.iterdir())
    assert sorted(path.name for path in out_dir.iterdir()) == ["agent-credentials.yaml", "idp-signing-key.yaml"]


def test_render_missing_value_writes_nothing(root, capsys):
    gen_keys.main(["--root", str(root)])
    values = secrets_env(root)
    del values["SVC_MCP_SECRET"]
    (root / ".secrets/secrets.env").write_text(envfile.format_env_file(values, "partial"))

    assert render_secrets.main(["--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "agent-credentials.template.yaml: no value for SVC_MCP_SECRET" in err
    assert not (root / "deploy/vk8s/secrets/rendered").exists()  # not even the good idp-signing-key file


def test_render_without_gen_keys_reports_every_missing_value(root, capsys):
    assert render_secrets.main(["--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "no value for IDP_PRIVATE_KEY_PEM" in err
    assert "no value for ALL_CLIENT_SECRETS_JSON" in err
    assert not (root / "deploy/vk8s/secrets/rendered").exists()


def test_render_falls_back_to_dotenv(root):
    gen_keys.main(["--root", str(root)])
    template = root / "deploy/vk8s/secrets/extra.template.yaml"
    template.write_text('kind: Secret\nstringData:\n  url: "{{ EXFIL_TEST_URL }}"\n')
    (root / ".env").write_text("EXFIL_TEST_URL=https://bin.example/x?a=1&b=\"q\"\n")
    assert render_secrets.main(["--root", str(root)]) == 0
    assert rendered(root, "extra.yaml")["stringData"]["url"] == 'https://bin.example/x?a=1&b="q"'


def test_render_refuses_dotenv_drift(root, capsys):
    gen_keys.main(["--root", str(root)])
    (root / ".env").write_text("GRANITE_SUPPLY_SECRET=edited-after-gen-keys\n")
    assert render_secrets.main(["--root", str(root)]) == 1
    assert "GRANITE_SUPPLY_SECRET in .env differs" in capsys.readouterr().err


def test_render_rejects_output_that_does_not_parse(root, capsys):
    gen_keys.main(["--root", str(root)])
    (root / "deploy/vk8s/secrets/broken.template.yaml").write_text("a: [\n  {{ SVC_MCP_SECRET }}\n")
    assert render_secrets.main(["--root", str(root)]) == 1
    assert "broken.template.yaml: rendered YAML does not parse" in capsys.readouterr().err
    assert not (root / "deploy/vk8s/secrets/rendered").exists()


def test_render_output_never_printed(root, capsys):
    gen_keys.main(["--root", str(root)])
    capsys.readouterr()
    render_secrets.main(["--root", str(root)])
    out = capsys.readouterr().out
    assert not [value for value in secrets_env(root).values() if value in out]
    assert "PRIVATE KEY" not in out


@pytest.mark.parametrize(
    "line,quote",
    [
        ('  a: "{{ X }}"', '"'),
        ("  a: '{{ X }}'", "'"),
        ("  a: {{ X }}", None),
        ("  note: it's {{ X }}", None),
        ('  a: "x\\"y {{ X }}"', '"'),
        ("  a: 'it''s {{ X }}'", "'"),
    ],
)
def test_open_quote(line, quote):
    prefix = line[: line.index("{{")]
    assert render_secrets.open_quote(prefix) == quote


def test_block_scalar_indentation_and_blank_lines():
    template = "k: |\n    {{ PEM }}\nnext: 1\n"
    out = render_secrets.render_text(template, {"PEM": "line1\n\nline3\n"})
    assert out == "k: |\n    line1\n\n    line3\nnext: 1\n"
    assert yaml.safe_load(out) == {"k": "line1\n\nline3\n", "next": 1}


# ---- envfile --------------------------------------------------------------------------------------------------------


def test_envfile_reads_dotenv_example_style_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "XC_TENANT=                       # e.g. f5-xc-demo\n"
        "XC_API_URL=https://${XC_TENANT}.console.ves.volterra.io/api\n"
        "export QUOTED=\"a # not a comment\"\n"
        "SINGLE='x'\n"
        "HASH_IN_VALUE=abc#def\n"
        "BLANK=\n"
    )
    assert envfile.read_env_file(path) == {
        "XC_TENANT": "",
        "XC_API_URL": "https://${XC_TENANT}.console.ves.volterra.io/api",
        "QUOTED": "a # not a comment",
        "SINGLE": "x",
        "HASH_IN_VALUE": "abc#def",
        "BLANK": "",
    }


@pytest.mark.parametrize("value", ["two\nlines", "has $dollar", "x # y", '"quoted"', " padded"])
def test_envfile_refuses_values_that_would_not_read_back(value):
    with pytest.raises(ValueError):
        envfile.format_env_file({"NAME": value}, "h")


def test_envfile_round_trips_generated_values(tmp_path):
    values = {"A": "abc-_XYZ019", "J": '{"agent-shopper":"x-y_z"}'}
    path = tmp_path / "s.env"
    path.write_text(envfile.format_env_file(values, "header\nline two"))
    assert envfile.read_env_file(path) == values
