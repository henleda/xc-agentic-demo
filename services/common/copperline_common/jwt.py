"""RS256 JWT helpers: sign, publish a JWKS, verify against a JWKS, read claims without verifying."""

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm

ALGORITHM = "RS256"


class JwtError(Exception):
    """Token rejected. str(e) is a short reason such as "expired" or "unknown kid"."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_uint(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8 or 1, "big"))


def load_private_key(pem: str | bytes) -> RSAPrivateKey:
    data = pem.encode() if isinstance(pem, str) else pem
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("expected an RSA private key")
    return key


def _public(key) -> RSAPublicKey:
    return key.public_key() if isinstance(key, RSAPrivateKey) else key


def key_id(public_key) -> str:
    """Stable kid: base64url(sha256(DER SubjectPublicKeyInfo))[:16]. Accepts a private key too."""
    der = _public(public_key).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return _b64url(hashlib.sha256(der).digest())[:16]


def public_jwks(private_key) -> dict:
    public_key = _public(private_key)
    numbers = public_key.public_numbers()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": ALGORITHM,
        "kid": key_id(public_key),
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }
    return {"keys": [jwk]}


def sign(claims: dict, private_key, *, ttl_s: int, issuer: str, now: int | None = None) -> str:
    """Sign RS256. Adds iss, iat, exp and jti unless the caller already set them."""
    issued_at = int(time.time()) if now is None else now
    payload = dict(claims)
    payload.setdefault("iss", issuer)
    payload.setdefault("iat", issued_at)
    payload.setdefault("exp", issued_at + ttl_s)
    payload.setdefault("jti", uuid.uuid4().hex)
    return pyjwt.encode(payload, private_key, algorithm=ALGORITHM, headers={"kid": key_id(private_key)})


def unverified_claims(token: str) -> dict:
    """Claims WITHOUT checking anything. {} if the token cannot be decoded."""
    try:
        claims = pyjwt.decode(token, options={"verify_signature": False})
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def bearer_token(authorization_header: str | None) -> str | None:
    """'Bearer abc' -> 'abc' (scheme is case-insensitive). None for anything else."""
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


class JwksVerifier:
    """Verifies RS256 tokens. Keys come from a dict, a file or a URL, loaded on first use."""

    def __init__(
        self,
        jwks: dict | None = None,
        *,
        jwks_path: str | None = None,
        jwks_url: str | None = None,
        issuer: str | None = None,
    ) -> None:
        self._static_jwks = jwks
        self._jwks_path = jwks_path
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._keys: dict[str, RSAPublicKey] | None = None

    def verify(self, token: str, audience: str | list[str] | None = None) -> dict:
        kid = self._header_kid(token)
        key = self._key_for(kid)
        options = {"require": ["exp"], "verify_aud": audience is not None}
        try:
            return pyjwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=audience,
                issuer=self._issuer,
                options=options,
            )
        except pyjwt.ExpiredSignatureError as e:
            raise JwtError("expired") from e
        except pyjwt.InvalidAudienceError as e:
            raise JwtError("bad audience") from e
        except pyjwt.InvalidIssuerError as e:
            raise JwtError("bad issuer") from e
        except pyjwt.InvalidSignatureError as e:
            raise JwtError("bad signature") from e
        except pyjwt.PyJWTError as e:
            raise JwtError(f"invalid token: {e}") from e

    def _header_kid(self, token: str) -> str | None:
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.PyJWTError as e:
            raise JwtError("malformed token") from e
        if header.get("alg") != ALGORITHM:
            raise JwtError(f"unsupported alg {header.get('alg')!r}")
        return header.get("kid")

    def _key_for(self, kid: str | None) -> RSAPublicKey:
        if self._keys is None:
            self._keys = self._load_keys()
        if kid in self._keys:
            return self._keys[kid]
        if kid is None and len(self._keys) == 1:
            return next(iter(self._keys.values()))
        # Unknown kid: the IdP may have rotated its key. Re-fetch once.
        self._keys = self._load_keys()
        if kid in self._keys:
            return self._keys[kid]
        raise JwtError("unknown kid")

    def _load_keys(self) -> dict[str, RSAPublicKey]:
        jwks = self._fetch_jwks()
        keys: dict[str, RSAPublicKey] = {}
        for jwk in jwks.get("keys", []):
            if jwk.get("kty") != "RSA":
                continue
            try:
                public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
            except pyjwt.PyJWTError as e:
                raise JwtError(f"bad jwk: {e}") from e
            keys[jwk.get("kid") or key_id(public_key)] = public_key
        return keys

    def _fetch_jwks(self) -> dict:
        try:
            if self._static_jwks is not None:
                return self._static_jwks
            if self._jwks_path:
                return json.loads(Path(self._jwks_path).read_text(encoding="utf-8"))
            if self._jwks_url:
                response = httpx.get(self._jwks_url, timeout=5.0)
                response.raise_for_status()
                return response.json()
        except (OSError, ValueError, httpx.HTTPError) as e:
            raise JwtError(f"jwks unavailable: {e}") from e
        raise JwtError("no jwks configured")
