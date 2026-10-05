"""OAuth 2.0 / OpenID Connect logins for the GUI (the authorization code
flow with PKCE).

``OidcClient.start`` gives the address at the provider to send the browser
to; the provider sends it back to ``/login/oidc/callback`` with a code,
which ``OidcClient.finish`` swaps for an ID token. The token's signature is
checked against the provider's published keys, and its issuer, audience,
expiry and nonce against what this login expects. The user's name and
groups are read from its claims (and from the provider's userinfo endpoint
when the token has no groups).

The provider's endpoints come from its discovery document
(``<issuer>/.well-known/openid-configuration``). They must be https,
except on this machine (a test provider).
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import ssl
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlencode

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .directory import (
    DirectoryError, DirectoryUser, LoginRefused, OidcConfig, check_name, is_loopback_url,
)

logger = logging.getLogger(__name__)

PENDING_TTL = 600        # seconds a started login may take at the provider
MAX_PENDING = 1000       # started logins kept; the oldest go first
METADATA_TTL = 3600      # seconds the discovery document and keys are kept
KEYS_REFRESH = 60        # seconds between key fetches for an unknown key id
CLOCK_SKEW = 60          # seconds the provider's clock may differ from ours
HTTP_TIMEOUT = 10        # seconds for each request to the provider
MAX_RESPONSE = 1 << 20   # bytes read of a provider's answer
MIN_RSA_BITS = 2048

HASHES = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}
CURVES = {"ES256": ("P-256", ec.SECP256R1), "ES384": ("P-384", ec.SECP384R1),
          "ES512": ("P-521", ec.SECP521R1)}


def _b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _uint(text: str) -> int:
    return int.from_bytes(_b64(text), "big")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _verify(alg: str, key: dict, signature: bytes, signed: bytes) -> None:
    """Raise unless ``signature`` is ``key``'s over ``signed``."""
    digest = HASHES[alg[2:]]()
    if alg.startswith("ES"):
        name, curve = CURVES[alg]
        if key.get("kty") != "EC" or key.get("crv") != name:
            raise InvalidSignature
        public = ec.EllipticCurvePublicNumbers(_uint(key["x"]), _uint(key["y"]),
                                               curve()).public_key()
        half = len(signature) // 2  # the two numbers of the signature, side by side
        der = encode_dss_signature(int.from_bytes(signature[:half], "big"),
                                   int.from_bytes(signature[half:], "big"))
        public.verify(der, signed, ec.ECDSA(digest))
        return
    if key.get("kty") != "RSA":
        raise InvalidSignature
    public = rsa.RSAPublicNumbers(_uint(key["e"]), _uint(key["n"])).public_key()
    if public.key_size < MIN_RSA_BITS:
        raise InvalidSignature
    pad = (padding.PSS(padding.MGF1(digest), digest.digest_size) if alg.startswith("PS")
           else padding.PKCS1v15())
    public.verify(signature, signed, pad, digest)


def jwt_header(token: str) -> dict:
    try:
        header = json.loads(_b64(token.split(".")[0]))
    except (ValueError, AttributeError) as exc:
        raise LoginRefused("the ID token is not a JWT") from exc
    if not isinstance(header, dict):
        raise LoginRefused("the ID token is not a JWT")
    return header


def verify_jwt(token: str, keys: list) -> dict:
    """The claims of a JWT signed by one of ``keys`` (JWKs); LoginRefused
    otherwise. Only public-key signatures count: never "none", and never
    HS256, where the signing key would be something we hold."""
    header = jwt_header(token)
    alg, kid = header.get("alg"), header.get("kid")
    if (not isinstance(alg, str) or alg[:2] not in ("RS", "PS", "ES") or alg[2:] not in HASHES):
        raise LoginRefused(f"the ID token is signed with {alg!r}, which is not accepted")
    parts = token.split(".")
    if len(parts) != 3:
        raise LoginRefused("the ID token is not a signed JWT")
    try:
        signature = _b64(parts[2])
    except ValueError as exc:
        raise LoginRefused("the ID token is not a signed JWT") from exc
    signed = f"{parts[0]}.{parts[1]}".encode()
    for key in keys:
        if (not isinstance(key, dict) or (kid and key.get("kid") != kid)
                or key.get("use", "sig") != "sig" or key.get("alg", alg) != alg):
            continue
        try:
            _verify(alg, key, signature, signed)
        except (InvalidSignature, KeyError, ValueError, TypeError):
            continue
        try:
            claims = json.loads(_b64(parts[1]))
        except ValueError as exc:
            raise LoginRefused("the ID token's claims are not JSON") from exc
        if not isinstance(claims, dict):
            raise LoginRefused("the ID token's claims are not JSON")
        return claims
    raise LoginRefused("the ID token's signature is not from the provider's keys")


def check_id_token(claims: dict, issuer: str, client_id: str, nonce: str,
                   now: Optional[float] = None) -> None:
    """Raise LoginRefused unless the ID token is from ``issuer``, for this
    client, in date and the answer to the login that sent ``nonce``."""
    now = time.time() if now is None else now
    if claims.get("iss") != issuer:
        raise LoginRefused(f"the ID token is from {claims.get('iss')!r}, not {issuer}")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if client_id not in audiences:
        raise LoginRefused("the ID token is for another application")
    if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != client_id:
        raise LoginRefused("the ID token was issued to another application")
    exp, iat = claims.get("exp"), claims.get("iat")
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (exp, iat))
    if not numbers or exp <= now - CLOCK_SKEW:
        raise LoginRefused("the ID token has expired")
    nbf = claims.get("nbf", iat)
    if iat > now + CLOCK_SKEW or not isinstance(nbf, (int, float)) or nbf > now + CLOCK_SKEW:
        raise LoginRefused("the ID token is not valid yet (check this machine's clock)")
    got = claims.get("nonce")
    if not isinstance(got, str) or not hmac.compare_digest(got.encode(), nonce.encode()):
        raise LoginRefused("the ID token does not belong to this login")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise LoginRefused("the ID token names no subject")


def claim(claims: dict, name: str):
    """A claim by name: ``groups``, or a path such as ``realm_access.roles``.
    The whole name is tried first, as claim names may hold dots themselves
    (``https://example.com/groups``)."""
    if name in claims:
        return claims[name]
    value = claims
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def claim_groups(claims: dict, name: str) -> Optional[list]:
    """The groups in a claim: a list, or one name. None if it is missing."""
    value = claim(claims, name)
    if value is None:
        return None
    values = value if isinstance(value, list) else [value]
    return [str(v) for v in values if isinstance(v, (str, int)) and not isinstance(v, bool)]


def user_from_claims(config: OidcConfig, claims: dict) -> DirectoryUser:
    """Who an ID token's claims are, with the role their groups give them."""
    groups = claim_groups(claims, config.groups_claim)
    if groups is None:
        sources = claims.get("_claim_names")
        if isinstance(sources, dict) and config.groups_claim in sources:
            # Microsoft Entra ID leaves the groups out when there are too many
            raise LoginRefused(
                "the provider left the groups out of the token (too many); have it send "
                "only the groups assigned to the application", authenticated=True)
        groups = []
    name = next((claims[c] for c in (config.username_claim, "email", "sub")
                 if isinstance(claims.get(c), str) and claims[c].strip()), None)
    name = check_name(name)
    role, hits = config.roles.resolve(groups)
    if not role:
        raise LoginRefused(f"{name} is in no group that may use this GUI", authenticated=True)
    return DirectoryUser(name, role, hits, str(claims.get("sub", "")))


@dataclass
class _Pending:
    nonce: str
    verifier: str
    redirect_uri: str
    created: float


class OidcClient:
    """The provider side of OIDC logins: one per GUI."""

    def __init__(self, config: OidcConfig):
        self.config = config
        self._http: Optional[aiohttp.ClientSession] = None
        self._metadata: Optional[dict] = None
        self._metadata_at = 0.0
        self._keys: list = []
        self._keys_at = 0.0
        self._pending: dict[str, _Pending] = {}  # by the hash of the state

    async def close(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None

    # --- talking to the provider ---

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None:
            context = (ssl.create_default_context(cafile=self.config.ca_file)
                       if self.config.ca_file else None)
            # trust_env: go through HTTPS_PROXY where the network needs it
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT), trust_env=True,
                connector=aiohttp.TCPConnector(ssl=context) if context else None)
        return self._http

    def _check_url(self, url, what: str) -> str:
        if not isinstance(url, str) or not (
                url.startswith("https://") or (url.startswith("http://") and is_loopback_url(url))):
            raise DirectoryError(f"the provider's {what} is not an https:// URL")
        return url

    async def _request(self, method: str, url: str, what: str, **kwargs) -> tuple[int, object]:
        """(status, JSON body) of a request to the provider."""
        try:
            async with self._session().request(method, url, allow_redirects=False,
                                               **kwargs) as resp:
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    raw += chunk
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError("the answer is too large")
                return resp.status, json.loads(bytes(raw))
        except (aiohttp.ClientError, TimeoutError, ValueError, OSError) as exc:
            raise DirectoryError(f"{what} ({url}): {exc or type(exc).__name__}") from exc

    async def metadata(self) -> dict:
        """The provider's discovery document."""
        now = time.monotonic()
        if self._metadata is None or now - self._metadata_at > METADATA_TTL:
            url = f"{self.config.issuer}/.well-known/openid-configuration"
            status, data = await self._request("GET", url, "provider discovery")
            if status != 200 or not isinstance(data, dict):
                raise DirectoryError(f"provider discovery ({url}): HTTP {status}")
            if str(data.get("issuer", "")).rstrip("/") != self.config.issuer:
                raise DirectoryError(f"provider discovery ({url}): it is for the issuer "
                                     f"{data.get('issuer')!r}, not {self.config.issuer}")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                self._check_url(data.get(key), key)
            self._metadata, self._metadata_at = data, now
        return self._metadata

    async def _signing_keys(self, kid: Optional[str]) -> list:
        """The provider's keys, fetched again for a key id not seen before
        (providers change keys), but not on every bad token."""
        now = time.monotonic()
        age = now - self._keys_at
        known = not kid or any(isinstance(k, dict) and k.get("kid") == kid for k in self._keys)
        if not self._keys or age > METADATA_TTL or (not known and age > KEYS_REFRESH):
            url = (await self.metadata())["jwks_uri"]
            status, data = await self._request("GET", url, "provider keys")
            keys = data.get("keys") if isinstance(data, dict) else None
            if status != 200 or not isinstance(keys, list):
                raise DirectoryError(f"provider keys ({url}): HTTP {status}")
            self._keys, self._keys_at = keys, now
        return self._keys

    # --- the flow ---

    async def start(self, redirect_uri: str) -> tuple[str, str]:
        """(address at the provider to send the browser to, state)."""
        meta = await self.metadata()
        now = time.time()
        self._pending = {k: p for k, p in self._pending.items() if now - p.created < PENDING_TTL}
        while len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]
        state = secrets.token_urlsafe(32)
        pending = _Pending(secrets.token_urlsafe(32), secrets.token_urlsafe(48), redirect_uri, now)
        self._pending[hashlib.sha256(state.encode()).hexdigest()] = pending
        challenge = _b64url(hashlib.sha256(pending.verifier.encode()).digest())
        query = urlencode({
            "response_type": "code", "client_id": self.config.client_id,
            "redirect_uri": redirect_uri, "scope": " ".join(self.config.scopes),
            "state": state, "nonce": pending.nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        endpoint = meta["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}", state

    async def finish(self, state: str, code: str) -> DirectoryUser:
        """The user a provider's answer is for. Each state works once."""
        pending = self._pending.pop(hashlib.sha256(state.encode()).hexdigest(), None)
        if not pending or time.time() - pending.created > PENDING_TTL:
            raise LoginRefused("the login took too long or was already used; try again")
        cfg, meta = self.config, await self.metadata()
        form = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": pending.redirect_uri, "code_verifier": pending.verifier}
        headers = {}
        methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if cfg.client_secret and "client_secret_basic" in methods:
            # RFC 6749: the id and the secret are form-encoded, then Basic
            basic = f"{quote(cfg.client_id, safe='')}:{quote(cfg.client_secret, safe='')}"
            headers["Authorization"] = f"Basic {base64.b64encode(basic.encode()).decode()}"
        else:
            form["client_id"] = cfg.client_id
            if cfg.client_secret:
                form["client_secret"] = cfg.client_secret
        status, tokens = await self._request("POST", meta["token_endpoint"], "token request",
                                             data=form, headers=headers)
        if status != 200 or not isinstance(tokens, dict) or not isinstance(
                tokens.get("id_token"), str):
            detail = tokens.get("error_description") or tokens.get("error") if isinstance(
                tokens, dict) else None
            raise LoginRefused(f"the provider gave no ID token: {str(detail or status)[:200]}")
        id_token = tokens["id_token"]
        keys = await self._signing_keys(jwt_header(id_token).get("kid"))
        claims = verify_jwt(id_token, keys)
        check_id_token(claims, cfg.issuer, cfg.client_id, pending.nonce)
        if claim(claims, cfg.groups_claim) is None and "_claim_names" not in claims:
            claims = {**await self._userinfo(meta, tokens, claims["sub"]), **claims}
        return user_from_claims(cfg, claims)

    async def _userinfo(self, meta: dict, tokens: dict, sub: str) -> dict:
        """The userinfo endpoint's claims: where some providers keep the
        groups. {} if there are none to be had."""
        url, access = meta.get("userinfo_endpoint"), tokens.get("access_token")
        if not url or not isinstance(access, str):
            return {}
        try:
            status, info = await self._request(
                "GET", self._check_url(url, "userinfo_endpoint"), "userinfo request",
                headers={"Authorization": f"Bearer {access}"})
        except DirectoryError as exc:
            logger.warning("oidc: %s", exc)
            return {}
        # It must be about the same person as the ID token
        if status != 200 or not isinstance(info, dict) or info.get("sub") != sub:
            return {}
        return info
