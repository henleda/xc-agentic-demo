import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from copperline_common import jwt as cjwt
from copperline_common.credentials import client_secret, secret_env_name
from copperline_common.tokens import TokenStore, client_credentials, exchange


def test_key_id_is_stable_and_jwks_shape(rsa_key, rsa_pem):
    loaded = cjwt.load_private_key(rsa_pem.decode())
    assert cjwt.key_id(loaded.public_key()) == cjwt.key_id(rsa_key.public_key()) == cjwt.key_id(rsa_key)
    assert len(cjwt.key_id(rsa_key)) == 16
    (jwk,) = cjwt.public_jwks(rsa_key)["keys"]
    assert {k: jwk[k] for k in ("kty", "use", "alg", "kid")} == {
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": cjwt.key_id(rsa_key)
    }
    assert jwk["e"] == "AQAB" and "=" not in jwk["n"]


def test_sign_adds_standard_claims_and_kid(rsa_key):
    token = cjwt.sign({"sub": "x"}, rsa_key, ttl_s=60, issuer="iss-1", now=1000)
    claims = cjwt.unverified_claims(token)
    assert claims["iss"] == "iss-1" and claims["iat"] == 1000 and claims["exp"] == 1060
    assert len(claims["jti"]) == 32
    import jwt as pyjwt

    assert pyjwt.get_unverified_header(token)["kid"] == cjwt.key_id(rsa_key)
    kept = cjwt.unverified_claims(cjwt.sign({"jti": "fixed", "iss": "mine"}, rsa_key, ttl_s=1, issuer="other"))
    assert kept["jti"] == "fixed" and kept["iss"] == "mine"


def test_verify_happy_path_and_audience(rsa_key):
    verifier = cjwt.JwksVerifier(cjwt.public_jwks(rsa_key), issuer="idp")
    token = cjwt.sign({"sub": "a", "aud": "copperline-tools"}, rsa_key, ttl_s=60, issuer="idp")
    assert verifier.verify(token, audience="copperline-tools")["sub"] == "a"
    assert verifier.verify(token, audience=["x", "copperline-tools"])["sub"] == "a"
    assert verifier.verify(token)["sub"] == "a"  # audience not checked when not given
    with pytest.raises(cjwt.JwtError, match="audience"):
        verifier.verify(token, audience="copperline-llm")


@pytest.mark.parametrize(
    "case",
    ["expired", "issuer", "signature", "garbage", "alg"],
)
def test_verify_rejections(rsa_key, other_rsa_key, case):
    verifier = cjwt.JwksVerifier(cjwt.public_jwks(rsa_key), issuer="idp")
    if case == "expired":
        token = cjwt.sign({"sub": "a"}, rsa_key, ttl_s=10, issuer="idp", now=int(time.time()) - 100)
    elif case == "issuer":
        token = cjwt.sign({"sub": "a"}, rsa_key, ttl_s=60, issuer="evil")
    elif case == "signature":
        # Signed by another key but claiming our kid.
        import jwt as pyjwt

        token = pyjwt.encode({"sub": "a", "exp": int(time.time()) + 60, "iss": "idp"}, other_rsa_key,
                             algorithm="RS256", headers={"kid": cjwt.key_id(rsa_key)})
    elif case == "garbage":
        token = "not-a-token"
    else:
        import jwt as pyjwt

        token = pyjwt.encode({"sub": "a", "exp": int(time.time()) + 60}, "k" * 32, algorithm="HS256")
    with pytest.raises(cjwt.JwtError):
        verifier.verify(token)


def test_unknown_kid_refetches_once(rsa_key, other_rsa_key, tmp_path):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(cjwt.public_jwks(rsa_key)))
    verifier = cjwt.JwksVerifier(jwks_path=str(path))
    assert verifier.verify(cjwt.sign({"sub": "a"}, rsa_key, ttl_s=60, issuer="i"))["sub"] == "a"
    # Key rotation: the file now has the new key; the verifier re-reads on the unknown kid.
    path.write_text(json.dumps(cjwt.public_jwks(other_rsa_key)))
    assert verifier.verify(cjwt.sign({"sub": "b"}, other_rsa_key, ttl_s=60, issuer="i"))["sub"] == "b"
    stranger = cjwt.sign({"sub": "c"}, cjwt.load_private_key(_new_pem()), ttl_s=60, issuer="i")
    with pytest.raises(cjwt.JwtError, match="unknown kid"):
        verifier.verify(stranger)


def _new_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def test_jwks_url_is_loaded_lazily(monkeypatch, rsa_key):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return httpx.Response(200, json=cjwt.public_jwks(rsa_key), request=httpx.Request("GET", url))

    monkeypatch.setattr(cjwt.httpx, "get", fake_get)
    verifier = cjwt.JwksVerifier(jwks_url="http://idp.test/.well-known/jwks.json")
    assert calls == []
    verifier.verify(cjwt.sign({"sub": "a"}, rsa_key, ttl_s=60, issuer="i"))
    verifier.verify(cjwt.sign({"sub": "b"}, rsa_key, ttl_s=60, issuer="i"))
    assert calls == ["http://idp.test/.well-known/jwks.json"]


def test_unverified_claims_and_bearer():
    assert cjwt.unverified_claims("junk") == {}
    assert cjwt.bearer_token("Bearer abc") == "abc"
    assert cjwt.bearer_token("bEaReR   abc ") == "abc"
    assert cjwt.bearer_token("Basic abc") is None
    assert cjwt.bearer_token("Bearer") is None
    assert cjwt.bearer_token(None) is None


def test_secret_env_name(monkeypatch):
    assert secret_env_name("agent-shopper") == "AGENT_SHOPPER_SECRET"
    assert secret_env_name("svc-mcp") == "SVC_MCP_SECRET"
    assert secret_env_name("granite-supply") == "GRANITE_SUPPLY_SECRET"
    monkeypatch.setenv("AGENT_STOREOPS_SECRET", "shh")
    assert client_secret("agent-storeops") == "shh"
    monkeypatch.delenv("AGENT_STOREOPS_SECRET")
    assert client_secret("agent-storeops") is None


def test_client_credentials_and_exchange_forms(make_token):
    seen = []
    token = make_token(sub="storeops", agent_id="storeops", aud="copperline-tools")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), {k: v[0] for k, v in parse_qs(request.content.decode()).items()}))
        return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            first = await client_credentials(http, "http://idp/auth/token", "agent-storeops", "s", "copperline-tools")
            second = await exchange(http, "http://idp/", client_id="agent-shopper", client_secret="t",
                                    subject_token="user.jwt", requested_audience="copperline-llm")
            return first, second

    first, second = asyncio.run(go())
    assert first.token == token and first.claims["agent_id"] == "storeops" and first.expires_at > 0
    assert seen[0] == ("http://idp/auth/token", {
        "grant_type": "client_credentials", "client_id": "agent-storeops", "client_secret": "s",
        "audience": "copperline-tools",
    })
    assert seen[1] == ("http://idp/token/exchange", {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": "user.jwt",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt", "client_id": "agent-shopper",
        "client_secret": "t", "requested_audience": "copperline-llm",
    })
    assert second.token == token


def test_token_error_response_has_no_token():
    def handler(request):
        return httpx.Response(400, json={"error": "unauthorized_user_role"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await exchange(http, "http://idp", client_id="c", client_secret=None, subject_token="u",
                                  requested_audience="copperline-tools")

    result = asyncio.run(go())
    assert result.token is None and result.claims == {} and result.expires_at == 0 and result.http.status == 400


def test_token_store_skew():
    now = [1000.0]
    store = TokenStore(clock=lambda: now[0])
    store.put("alex.rivera", "copperline-tools", "tok", exp=1100)
    assert store.get("alex.rivera", "copperline-tools") == "tok"
    assert store.get("alex.rivera", "copperline-llm") is None
    now[0] = 1069.0
    assert store.get("alex.rivera", "copperline-tools") == "tok"
    now[0] = 1070.0  # exp - 30 == now: no longer served
    assert store.get("alex.rivera", "copperline-tools") is None
