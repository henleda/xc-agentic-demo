"""Black-box contract tests for jwt.py (sign, verify, JWKS, kid)."""
import base64
import json
import time

import pytest
from copperline_common.jwt import (
    JwksVerifier,
    JwtError,
    bearer_token,
    key_id,
    load_private_key,
    public_jwks,
    sign,
    unverified_claims,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.copperline.test"


def new_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


@pytest.fixture(scope="module")
def key():
    return load_private_key(new_pem())


@pytest.fixture(scope="module")
def other_key():
    return load_private_key(new_pem())


def b64json(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def header_of(token: str) -> dict:
    return b64json(token.split(".")[0])


def agent_token(key, *, aud="copperline-tools", ttl_s=300, now=None, issuer=ISSUER):
    claims = {"sub": "shopper", "agent_id": "shopper", "agent_role": "shopper", "obo_sub": "alex.rivera",
              "obo_role": "customer", "aud": aud}
    return sign(claims, key, ttl_s=ttl_s, issuer=issuer, now=now)


def test_load_private_key_accepts_str_and_bytes():
    pem = new_pem()
    assert key_id(load_private_key(pem).public_key()) == key_id(load_private_key(pem.decode()).public_key())


def test_sign_and_verify_round_trip(key):
    now = int(time.time())
    token = agent_token(key, now=now)
    claims = JwksVerifier(public_jwks(key), issuer=ISSUER).verify(token, audience="copperline-tools")
    assert claims["agent_role"] == "shopper"
    assert claims["obo_sub"] == "alex.rivera"
    assert claims["iss"] == ISSUER
    assert claims["iat"] == now
    assert claims["exp"] == now + 300
    assert isinstance(claims["jti"], str) and claims["jti"]


def test_sign_keeps_a_jti_that_is_already_present(key):
    assert unverified_claims(sign({"sub": "x", "jti": "fixed-jti"}, key, ttl_s=60, issuer=ISSUER))["jti"] == "fixed-jti"


def test_each_signed_token_gets_a_fresh_jti(key):
    assert unverified_claims(agent_token(key))["jti"] != unverified_claims(agent_token(key))["jti"]


def test_verify_accepts_audience_list_and_no_audience(key):
    token = agent_token(key)
    verifier = JwksVerifier(public_jwks(key), issuer=ISSUER)
    assert verifier.verify(token, audience=["copperline-llm", "copperline-tools"])["aud"] == "copperline-tools"
    assert verifier.verify(token)["sub"] == "shopper"


def test_wrong_audience_raises(key):
    with pytest.raises(JwtError):
        JwksVerifier(public_jwks(key), issuer=ISSUER).verify(agent_token(key), audience="copperline-llm")


def test_expired_token_raises(key):
    token = agent_token(key, ttl_s=60, now=int(time.time()) - 3600)
    with pytest.raises(JwtError):
        JwksVerifier(public_jwks(key), issuer=ISSUER).verify(token, audience="copperline-tools")


def test_wrong_issuer_raises(key):
    token = agent_token(key, issuer="https://evil.test")
    with pytest.raises(JwtError):
        JwksVerifier(public_jwks(key), issuer=ISSUER).verify(token, audience="copperline-tools")


def test_issuer_is_not_checked_when_verifier_has_none(key):
    token = agent_token(key, issuer="https://any.test")
    assert JwksVerifier(public_jwks(key)).verify(token)["iss"] == "https://any.test"


def test_tampered_payload_raises(key):
    header, payload, signature = agent_token(key).split(".")
    claims = b64json(payload)
    claims["agent_role"] = "refunds"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    with pytest.raises(JwtError):
        JwksVerifier(public_jwks(key), issuer=ISSUER).verify(f"{header}.{forged}.{signature}")


def test_tampered_signature_raises(key):
    header, payload, signature = agent_token(key).split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(JwtError):
        JwksVerifier(public_jwks(key), issuer=ISSUER).verify(f"{header}.{payload}.{flipped}")


def test_token_signed_by_another_key_raises(key, other_key):
    with pytest.raises(JwtError):
        JwksVerifier(public_jwks(key), issuer=ISSUER).verify(agent_token(other_key))


def test_garbage_token_raises_jwt_error(key):
    with pytest.raises(JwtError):
        JwksVerifier(public_jwks(key)).verify("not-a-jwt")


def test_kid_is_present_stable_and_matches_jwks(key, other_key):
    token = agent_token(key)
    header = header_of(token)
    assert header["alg"] == "RS256"
    kid = header["kid"]
    assert kid == key_id(key.public_key())
    assert kid == key_id(key.public_key())                 # stable across calls
    assert header_of(agent_token(key))["kid"] == kid       # stable across tokens
    assert len(kid) == 16
    assert all(c.isalnum() or c in "-_" for c in kid)      # base64url alphabet
    assert key_id(other_key.public_key()) != kid
    assert public_jwks(key)["keys"][0]["kid"] == kid


def test_public_jwks_has_only_public_fields(key):
    jwks = public_jwks(key)
    (jwk,) = jwks["keys"]
    assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}
    assert jwk["kty"] == "RSA" and jwk["use"] == "sig" and jwk["alg"] == "RS256"
    for private_field in ("d", "p", "q", "dp", "dq", "qi"):
        assert private_field not in jwk


def test_verifier_loads_jwks_from_file(key, tmp_path):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(public_jwks(key)))
    claims = JwksVerifier(jwks_path=str(path), issuer=ISSUER).verify(agent_token(key), audience="copperline-tools")
    assert claims["sub"] == "shopper"


def test_unverified_claims(key):
    assert unverified_claims(agent_token(key))["obo_role"] == "customer"
    assert unverified_claims("garbage") == {}
    assert unverified_claims("") == {}


@pytest.mark.parametrize("header, expected", [
    ("Bearer abc.def.ghi", "abc.def.ghi"),
    ("bearer abc", "abc"),
    ("BEARER abc", "abc"),
    (None, None),
    ("", None),
    ("Basic dXNlcjpwYXNz", None),
])
def test_bearer_token(header, expected):
    assert bearer_token(header) == expected
