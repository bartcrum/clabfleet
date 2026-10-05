"""Directory logins: OIDC and LDAP / Active Directory next to the users file."""

import asyncio
import base64
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("cryptography")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402

from clabfleet import cli  # noqa: E402
from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import auth, directory, oidc, server  # noqa: E402
from clabfleet.gui.auth import AuditLog, UserStore  # noqa: E402
from clabfleet.gui.directory import (  # noqa: E402
    DirectoryError, InvalidCredentials, LdapDirectory, LoginRefused, load_directory,
)
from clabfleet.gui.state import Workspace  # noqa: E402

TOPO = """\
name: t
topology:
  nodes:
    a: {kind: linux, image: alpine}
"""
PASSWORD = "correct horse battery"
ADMINS = "CN=NetLab Admins,OU=Groups,DC=example,DC=com"
READERS = "CN=NetLab Readers,OU=Groups,DC=example,DC=com"
LDAP_SECTION = {
    "url": "ldaps://dc1.example.com",
    "bind_dn": "CN=svc-clabfleet,OU=Service,DC=example,DC=com",
    "bind_password": "service password",
    "user_base": "DC=example,DC=com",
    "roles": {"operator": [ADMINS], "viewer": [READERS]},
}


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 10)


def _workspace(tmp_path):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    return Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _write(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


# ----------------------------------------------------------------------
# The directory file
# ----------------------------------------------------------------------

def test_directory_file(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLABFLEET_TEST_SECRET", "from the environment")
    path = _write(tmp_path / "directory.yaml", f"""\
oidc:
  issuer: https://login.example.com/tenant/v2.0/
  client_id: clabfleet
  client_secret_env: CLABFLEET_TEST_SECRET
  label: Example ID
  groups_claim: roles
  roles:
    operator: netlab-admins
    viewer: [netlab-readers, 42]
ldap:
  url: ldaps://dc1.example.com
  bind_dn: CN=svc,DC=example,DC=com
  bind_password: service password
  user_base: DC=example,DC=com
  nested_groups: true
  roles:
    operator: ["{ADMINS}"]
""")
    found = load_directory(path)
    assert found.oidc.issuer == "https://login.example.com/tenant/v2.0"
    assert found.oidc.client_secret == "from the environment"
    assert found.oidc.scopes == ("openid", "profile", "email")
    assert found.oidc.roles.resolve(["netlab-readers", "netlab-admins"]) == (
        "operator", ("netlab-admins",))
    assert found.oidc.roles.resolve(["42"]) == ("viewer", ("42",))
    assert found.oidc.roles.resolve(["Netlab-Admins"]) == (None, ())  # names as they are
    ldap = found.ldap.config
    assert ldap.user_filter == directory.AD_USER_FILTER and not ldap.start_tls
    assert ldap.group_base == "DC=example,DC=com" and ldap.recheck == 300
    assert ldap.roles.resolve([ADMINS.lower()])[0] == "operator"  # DNs have no case
    assert found.describe() == [
        "Single sign-on: Example ID (https://login.example.com/tenant/v2.0)",
        "Directory logins: directory (ldaps://dc1.example.com)"]
    assert "readable by other users" not in caplog.text

    # Secrets and labels are not part of who gets in; roles are
    before = found.fingerprint("ldap")
    text = path.read_text()
    _write(path, text.replace("service password", "a new password"))
    assert load_directory(path).fingerprint("ldap") == before
    _write(path, text.replace(ADMINS, READERS))
    assert load_directory(path).fingerprint("ldap") != before
    assert directory.Directory().fingerprint("ldap") is None

    path.chmod(0o644)
    load_directory(path)
    assert "holds ldap.bind_password and is readable by other users" in caplog.text
    path.chmod(0o660)
    with pytest.raises(ValueError, match="writable by other users"):
        load_directory(path)
    with pytest.raises(FileNotFoundError):
        load_directory(tmp_path / "missing.yaml")


OIDC_OK = {"issuer": "https://id.example.com", "client_id": "c", "roles": {"operator": ["a"]}}


@pytest.mark.parametrize("section, change, error", [
    ("oidc", {"issuer": "http://id.example.com"}, "must be an https:// URL"),
    ("oidc", {"client_id": None}, "'client_id' is missing"),
    ("oidc", {"roles": {}}, "names no group"),
    ("oidc", {"roles": {"admin": ["a"]}}, "'roles' maps operator and viewer"),
    ("oidc", {"roles": {"operator": [["a"]]}}, "roles.operator must be a list"),
    ("oidc", {"client_secrt": "x"}, "unknown setting client_secrt"),
    ("oidc", {"client_secret_env": "CLABFLEET_NOT_SET"}, "CLABFLEET_NOT_SET"),
    ("oidc", {"session_hours": 0}, "'session_hours' must be a number above 0"),
    ("oidc", {"ca_file": "/nonexistent/ca.pem"}, "not found"),
    ("ldap", {"url": "dc1.example.com"}, "must be ldaps://"),
    ("ldap", {"start_tls": True}, "ldaps:// is TLS already"),
    ("ldap", {"user_filter": "(uid=alice)"}, "needs {username}"),
    ("ldap", {"roles": {"operator": ["NetLab Admins"]}}, "full DNs"),
    ("ldap", {"bind_password": ""}, "'bind_dn' needs 'bind_password'"),
    ("ldap", {"user_base": None}, "'user_base' is missing"),
    ("ldap", {"nested_groups": "yes"}, "must be true or false"),
])
def test_directory_file_mistakes(tmp_path, section, change, error):
    data = {**(OIDC_OK if section == "oidc" else LDAP_SECTION), **change}
    data = {k: v for k, v in data.items() if v is not None}
    path = _write(tmp_path / "directory.yaml", json.dumps({section: data}))
    with pytest.raises(ValueError, match=re.escape(error)):
        load_directory(path)


def test_directory_file_needs_a_section(tmp_path):
    for text in ("", "saml: {}", "oidc: yes", "- a"):
        with pytest.raises(ValueError, match="'oidc:'"):
            load_directory(_write(tmp_path / "directory.yaml", text))


def test_plain_ldap_is_warned_about(tmp_path, caplog):
    section = {**LDAP_SECTION, "url": "ldap://dc1.example.com"}
    path = _write(tmp_path / "directory.yaml", json.dumps({"ldap": section}))
    assert load_directory(path).ldap.config.start_tls  # the default for ldap://
    assert "clear text" not in caplog.text
    _write(path, json.dumps({"ldap": {**section, "start_tls": False}}))
    load_directory(path)
    assert "passwords cross the network in clear text" in caplog.text


# ----------------------------------------------------------------------
# LDAP / Active Directory
# ----------------------------------------------------------------------

class FakeLdap:
    """A directory of ``{dn: {"password", "sAMAccountName", "memberOf", "nested"}}``."""

    def __init__(self, entries):
        self.entries = entries
        self.config = directory._ldap_config(LDAP_SECTION)
        self.binds, self.queries = [], []
        self.down = False

    def directory(self, **changes):
        self.config = directory._ldap_config({**LDAP_SECTION, **changes})
        return LdapDirectory(self.config, connect=self.connect)

    def connect(self, config, user, password):
        if self.down:
            raise DirectoryError("dc1.example.com: connection refused")
        self.binds.append(user)
        expected = (LDAP_SECTION["bind_password"] if user == config.bind_dn
                    else self.entries.get(user, {}).get("password"))
        if expected is None or password != expected:
            raise InvalidCredentials("invalidCredentials")
        return self

    def search(self, base, query, attributes=()):
        self.queries.append(query)
        by_name = re.search(r"\(sAMAccountName=([^)]*)\)", query)
        if by_name:
            return [(dn, {"sAMAccountName": [e["sAMAccountName"]],
                          "memberOf": list(e.get("memberOf", []))})
                    for dn, e in self.entries.items()
                    if e["sAMAccountName"].lower() == by_name.group(1).lower()]
        chain = re.fullmatch(r"\(member:1\.2\.840\.113556\.1\.4\.1941:=(.*)\)", query)
        return [(g, {}) for g in self.entries[chain.group(1)].get("nested", [])]

    def close(self):
        pass


ALICE = "CN=Alice Admin,OU=People,DC=example,DC=com"
BOB = "CN=Bob Reader,OU=People,DC=example,DC=com"
CAROL = "CN=Carol Nobody,OU=People,DC=example,DC=com"


def _people():
    return FakeLdap({
        ALICE: {"password": PASSWORD, "sAMAccountName": "alice", "memberOf": [ADMINS.upper()],
                "nested": [ADMINS, READERS]},
        BOB: {"password": PASSWORD, "sAMAccountName": "bob", "memberOf": [READERS]},
        CAROL: {"password": PASSWORD, "sAMAccountName": "carol",
                "memberOf": ["CN=Other,DC=example,DC=com"], "nested": [ADMINS]},
    })


def test_ldap_logins_and_roles():
    fake = _people()
    ldap = fake.directory()
    alice = ldap.authenticate("Alice", PASSWORD)  # the directory's spelling of the name
    assert (alice.name, alice.role, alice.subject) == ("alice", "operator", ALICE)
    assert alice.groups == (ADMINS.upper(),)
    assert fake.binds == [fake.config.bind_dn, ALICE]
    assert ldap.authenticate("bob", PASSWORD).role == "viewer"

    for name, password in (("alice", "wrong"), ("nobody", PASSWORD), ("alice\n", PASSWORD)):
        with pytest.raises(LoginRefused, match="invalid user name or password") as refused:
            ldap.authenticate(name, password)
        assert not refused.value.authenticated
    # Right password, no group with a role: they may be told
    with pytest.raises(LoginRefused, match="carol is in no group") as refused:
        ldap.authenticate("carol", PASSWORD)
    assert refused.value.authenticated

    assert ldap.lookup("alice").role == "operator"
    assert ldap.lookup("carol").role is None
    assert ldap.lookup("nobody") is None


def test_ldap_empty_password_never_reaches_the_directory():
    fake = _people()
    with pytest.raises(LoginRefused):
        fake.directory().authenticate("alice", "")  # it would be an anonymous bind
    assert fake.binds == []


def test_ldap_names_are_escaped_in_the_filter():
    fake = _people()
    with pytest.raises(LoginRefused):
        fake.directory().authenticate("*)(sAMAccountName=alice", PASSWORD)
    assert "(sAMAccountName=\\2a\\29\\28sAMAccountName=alice)" in fake.queries[0]
    assert directory.escape_filter("a\\b(c)*\x00") == "a\\5cb\\28c\\29\\2a\\00"


def test_ldap_nested_groups():
    fake = _people()
    ldap = fake.directory(nested_groups=True)
    assert ldap.authenticate("carol", PASSWORD).role == "operator"  # through a nested group
    assert fake.queries[-1] == f"(member:1.2.840.113556.1.4.1941:={CAROL})"
    assert ldap.authenticate("alice", PASSWORD).groups == (ADMINS,)


def test_ldap_directory_trouble_is_not_a_wrong_password():
    fake = _people()
    ldap = fake.directory()
    fake.down = True
    with pytest.raises(DirectoryError, match="connection refused"):
        ldap.authenticate("alice", PASSWORD)
    fake.down = False
    with pytest.raises(DirectoryError, match="bind account .* was refused"):
        fake.directory(bind_password="not it").lookup("alice")
    fake.entries["CN=Alice Twin,DC=example,DC=com"] = dict(fake.entries[ALICE])
    with pytest.raises(LoginRefused):  # two entries with one name: neither
        fake.directory().authenticate("alice", PASSWORD)


def test_ldap_through_ldap3(monkeypatch):
    """The real connection class, against ldap3's in-memory server."""
    ldap3 = pytest.importorskip("ldap3")
    service = LDAP_SECTION["bind_dn"]
    real = ldap3.Connection

    def connection(server, **kwargs):
        conn = real(server, **kwargs)
        person = {"objectClass": "person"}
        conn.strategy.add_entry(service, {**person, "userPassword": "service password",
                                          "sAMAccountName": "svc"})
        conn.strategy.add_entry(ALICE, {**person, "userPassword": PASSWORD,
                                        "sAMAccountName": "alice", "memberOf": [ADMINS]})
        return conn

    monkeypatch.setattr(ldap3, "Connection", connection)
    config = directory._ldap_config({
        **LDAP_SECTION, "url": "ldap://dc1.example.com", "start_tls": False,
        "user_filter": "(&(objectClass=person)(sAMAccountName={username}))"})
    ldap = LdapDirectory(config, connect=lambda c, user, password: directory.Ldap3Connection(
        c, user, password, strategy=ldap3.MOCK_SYNC))
    alice = ldap.authenticate("alice", PASSWORD)
    assert (alice.name, alice.role, alice.subject) == ("alice", "operator", ALICE)
    assert ldap.lookup("nobody") is None
    with pytest.raises(LoginRefused):
        ldap.authenticate("alice", "wrong")
    with pytest.raises(InvalidCredentials):
        directory.Ldap3Connection(config, service, "wrong", strategy=ldap3.MOCK_SYNC)


# ----------------------------------------------------------------------
# ID tokens
# ----------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _num(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
RSA_JWK = {"kty": "RSA", "kid": "rsa-1", "use": "sig",
           "n": _num(RSA_KEY.public_key().public_numbers().n),
           "e": _num(RSA_KEY.public_key().public_numbers().e)}
EC_JWK = {"kty": "EC", "kid": "ec-1", "crv": "P-256",
          "x": _num(EC_KEY.public_key().public_numbers().x),
          "y": _num(EC_KEY.public_key().public_numbers().y)}


def _jwt(claims: dict, alg: str = "RS256", kid: str = "rsa-1", key=None) -> str:
    signed = (f"{_b64(json.dumps({'alg': alg, 'kid': kid}).encode())}."
              f"{_b64(json.dumps(claims).encode())}")
    if alg == "RS256":
        sig = (key or RSA_KEY).sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    elif alg == "PS256":
        sig = RSA_KEY.sign(signed.encode(), padding.PSS(padding.MGF1(hashes.SHA256()), 32),
                           hashes.SHA256())
    elif alg == "ES256":
        r, s = decode_dss_signature(EC_KEY.sign(signed.encode(), ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    else:
        sig = b"anything"
    return f"{signed}.{_b64(sig)}"


def test_id_token_signatures():
    claims = {"sub": "1", "name": "x"}
    keys = [EC_JWK, RSA_JWK]
    for alg, kid in (("RS256", "rsa-1"), ("PS256", "rsa-1"), ("ES256", "ec-1")):
        assert oidc.verify_jwt(_jwt(claims, alg, kid), keys) == claims

    token = _jwt(claims)
    head, body, sig = token.split(".")
    forged = _b64(json.dumps({"sub": "2", "name": "x"}).encode())
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad, error in (
            (f"{head}.{forged}.{sig}", "not from the provider's keys"),
            (_jwt(claims, key=stranger), "not from the provider's keys"),
            (_jwt(claims, kid="rsa-2"), "not from the provider's keys"),
            (_jwt(claims, "ES256", "rsa-1"), "not from the provider's keys"),
            (_jwt(claims, "none"), "'none', which is not accepted"),
            (_jwt(claims, "HS256"), "'HS256', which is not accepted"),
            (f"{head}.{body}", "not a signed JWT"),
            ("not a token", "not a JWT")):
        with pytest.raises(LoginRefused, match=error):
            oidc.verify_jwt(bad, keys)
    # A short RSA key signs nothing we accept
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_jwk = {"kty": "RSA", "kid": "rsa-1", "n": _num(weak.public_key().public_numbers().n),
                "e": _num(65537)}
    with pytest.raises(LoginRefused):
        oidc.verify_jwt(_jwt(claims, key=weak), [weak_jwk])


def test_id_token_claims():
    now = 1_800_000_000
    good = {"iss": "https://id.example.com", "aud": "clabfleet", "sub": "u1", "nonce": "n1",
            "iat": now - 5, "exp": now + 300}

    def check(**changes):
        claims = {k: v for k, v in {**good, **changes}.items() if v is not None}
        oidc.check_id_token(claims, "https://id.example.com", "clabfleet", "n1", now)

    check()
    check(aud=["clabfleet"], azp="clabfleet")
    check(exp=now - 30)  # within the clocks' allowance
    for changes, error in (
            ({"iss": "https://evil.example.com"}, "not https://id.example.com"),
            ({"aud": "another"}, "for another application"),
            ({"aud": ["clabfleet", "another"]}, "issued to another application"),
            ({"azp": "another"}, "issued to another application"),
            ({"exp": now - 61}, "expired"),
            ({"exp": None}, "expired"),
            ({"exp": "tomorrow"}, "expired"),
            ({"iat": now + 600}, "not valid yet"),
            ({"nbf": now + 600}, "not valid yet"),
            ({"nonce": "n2"}, "does not belong to this login"),
            ({"nonce": None}, "does not belong to this login"),
            ({"sub": ""}, "names no subject")):
        with pytest.raises(LoginRefused, match=error):
            check(**changes)


def test_claims_to_user(tmp_path):
    def config(**changes):
        path = _write(tmp_path / "directory.yaml", json.dumps({"oidc": {
            **OIDC_OK, "roles": {"operator": ["admins"], "viewer": ["readers"]}, **changes}}))
        return load_directory(path).oidc

    who = oidc.user_from_claims(config(), {
        "sub": "u1", "preferred_username": "alice@example.com", "groups": ["x", "admins"]})
    assert (who.name, who.role, who.groups, who.subject) == (
        "alice@example.com", "operator", ("admins",), "u1")
    # One group as text; the name falls back to the email, then the subject
    assert oidc.user_from_claims(config(), {"sub": "u2", "email": "b@example.com",
                                            "groups": "readers"}).name == "b@example.com"
    assert oidc.user_from_claims(config(), {"sub": "u3", "groups": ["readers"]}).name == "u3"
    # Claims by path, and claim names that hold dots themselves
    nested = config(groups_claim="realm_access.roles")
    assert oidc.user_from_claims(nested, {"sub": "u", "realm_access": {"roles": ["admins"]}}).role
    dotted = config(groups_claim="https://example.com/groups")
    assert oidc.user_from_claims(dotted, {"sub": "u", "https://example.com/groups": ["admins"]})

    for claims, error in (
            ({"sub": "u", "groups": ["x"]}, "u is in no group that may use this GUI"),
            ({"sub": "u"}, "in no group"),
            ({"sub": "u", "groups": {"admins": True}}, "in no group"),
            ({"sub": "u", "_claim_names": {"groups": "src1"}}, "left the groups out"),
            ({"sub": "u\x00", "groups": ["admins"]}, "no usable user name")):
        with pytest.raises(LoginRefused, match=error) as refused:
            oidc.user_from_claims(config(), claims)
        assert refused.value.authenticated


# ----------------------------------------------------------------------
# OIDC logins in the GUI
# ----------------------------------------------------------------------

class Provider:
    """A small OpenID Connect provider. ``claims``: what the next login is
    told about the user; ``userinfo``: what its userinfo endpoint adds."""

    def __init__(self):
        self.claims = {"sub": "u-alice", "preferred_username": "alice@example.com",
                       "groups": ["netlab-admins"]}
        self.userinfo = None
        self.codes = {}  # code -> the authorization request it answers
        self.token_requests = []
        self.jwks_fetches = 0
        self.issuer = ""
        self.sign = _jwt

    def app(self):
        app = web.Application()
        app.router.add_get("/.well-known/openid-configuration", self._discovery)
        app.router.add_get("/keys", self._keys)
        app.router.add_post("/token", self._token)
        app.router.add_get("/userinfo", self._userinfo)
        return app

    async def _discovery(self, request):
        return web.json_response({
            "issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token", "jwks_uri": f"{self.issuer}/keys",
            "userinfo_endpoint": f"{self.issuer}/userinfo"})

    async def _keys(self, request):
        self.jwks_fetches += 1
        # More than arrives in one read
        return web.json_response({"keys": [RSA_JWK, EC_JWK], "padding": "x" * 300_000})

    def authorize(self, url: str) -> str:
        """What the provider's login page does once the user is in: the
        address it sends the browser back to."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert url.startswith(f"{self.issuer}/authorize?")
        assert query["response_type"] == "code" and query["client_id"] == "clabfleet"
        assert query["scope"].split()[0] == "openid" and query["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = query
        return f"{query['redirect_uri']}?code={code}&state={query['state']}"

    async def _token(self, request):
        form = dict(await request.post())
        self.token_requests.append((form, request.headers.get("Authorization")))
        asked = self.codes.pop(form.get("code"), None)
        challenge = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
        if (not asked or challenge != asked["code_challenge"]
                or form["redirect_uri"] != asked["redirect_uri"]):
            return web.json_response({"error": "invalid_grant"}, status=400)
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": "clabfleet", "iat": now, "exp": now + 300,
                  "nonce": asked["nonce"], **self.claims}
        return web.json_response({"id_token": self.sign(claims), "access_token": "at-1",
                                  "token_type": "Bearer"})

    async def _userinfo(self, request):
        if request.headers.get("Authorization") != "Bearer at-1" or self.userinfo is None:
            raise web.HTTPUnauthorized()
        return web.json_response(self.userinfo)


def _oidc_directory(tmp_path, issuer, **changes):
    path = _write(tmp_path / "directory.yaml", json.dumps({"oidc": {
        "issuer": issuer, "client_id": "clabfleet", "client_secret": "s3cr3t/+",
        "label": "Example ID",
        "roles": {"operator": ["netlab-admins"], "viewer": ["netlab-readers"]}, **changes}}))
    return load_directory(path)


async def _sso(client, provider):
    """Log in through the provider; the response to its answer."""
    resp = await client.get("/login/oidc", allow_redirects=False)
    assert resp.status == 302, await resp.text()
    back = provider.authorize(resp.headers["Location"])
    return await client.get(urlsplit(back)._replace(scheme="", netloc="").geturl(),
                            allow_redirects=False)


def _login_error(resp) -> str:
    assert resp.status == 302 and resp.headers["Location"].startswith("/#login_error=")
    return unquote(resp.headers["Location"].split("=", 1)[1])


def _oidc_scenario(tmp_path, body, **changes):
    """Run ``body(client, provider, app)`` against a GUI with OIDC logins."""
    users = UserStore(tmp_path / "users.yaml")
    if not users.path.exists():
        users.add("admin", password=PASSWORD)
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        provider = Provider()
        async with TestServer(provider.app()) as idp:
            provider.issuer = str(idp.make_url("")).rstrip("/")
            found = _oidc_directory(tmp_path, provider.issuer, **changes)
            app = server.create_app(_workspace(tmp_path), users=users, directory=found,
                                    audit=AuditLog(audit_path), instance="8650",
                                    session_file=auth.SessionFile(tmp_path / "sessions.json"))
            async with TestClient(TestServer(app)) as client:
                await body(client, provider, app)

    asyncio.run(scenario())
    return _events(audit_path) if audit_path.exists() else []


def test_oidc_login(tmp_path):
    async def body(client, provider, app):
        # The login form learns that it may offer single sign-on
        resp = await client.get("/api/me")
        assert resp.status == 401 and resp.headers["X-Clabfleet-Login"] == "password"
        assert resp.headers["X-Clabfleet-SSO"] == "Example%20ID"
        assert "X-Clabfleet-Directory" not in resp.headers

        resp = await client.get("/login/oidc", allow_redirects=False)
        state_cookie = resp.headers["Set-Cookie"]
        assert "clabfleet_oidc_8650=" in state_cookie and "HttpOnly" in state_cookie
        assert "SameSite=Lax" in state_cookie and "Max-Age=600" in state_cookie
        back = provider.authorize(resp.headers["Location"])
        asked = provider.codes["code-0"]
        assert asked["redirect_uri"] == str(client.make_url("/login/oidc/callback"))
        assert asked["scope"] == "openid profile email" and len(asked["nonce"]) > 20
        resp = await client.get(urlsplit(back)._replace(scheme="", netloc="").geturl(),
                                allow_redirects=False)
        assert resp.status == 302 and resp.headers["Location"] == "/"
        cookies = resp.headers.getall("Set-Cookie")
        session = next(c for c in cookies if c.startswith("clabfleet_session_8650="))
        assert "HttpOnly" in session and "SameSite=Strict" in session
        assert any(c.startswith('clabfleet_oidc_8650="";') for c in cookies)  # used up
        # The client secret went in the Authorization header, the PKCE verifier in the form
        form, authorization = provider.token_requests[0]
        assert base64.b64decode(authorization.split()[1]).decode() == "clabfleet:s3cr3t%2F%2B"
        assert "client_secret" not in form and len(form["code_verifier"]) >= 43

        me = await (await client.get("/api/me")).json()
        assert (me["user"], me["role"]) == ("alice@example.com", "operator")
        assert not me["has_password"] and me["can_manage_users"]
        assert (await client.post("/api/validate/t.clab.yml", json={})).status != 403
        # Directory users are not in the users file
        listed = await (await client.get("/api/users")).json()
        assert [u["name"] for u in listed["users"]] == ["admin"]
        assert (await client.post("/api/password",
                                  json={"current": "x", "new": PASSWORD})).status == 400

        assert (await client.post("/logout")).status == 200
        assert (await client.get("/api/me")).status == 401
        # A viewer by their group
        provider.claims = {"sub": "u-bob", "preferred_username": "bob",
                           "groups": ["netlab-readers"]}
        assert (await _sso(client, provider)).headers["Location"] == "/"
        assert (await (await client.get("/api/me")).json())["role"] == "viewer"
        assert (await client.post("/api/jobs", json={})).status == 403
        assert provider.jwks_fetches == 1  # the keys are kept
        # A local user who takes the name ends the directory user's login
        app[server.AUTH].users.add("Bob")
        assert (await client.get("/api/me")).status == 401

    events = _oidc_scenario(tmp_path, body)
    logins = [e for e in events if e["event"] == "login"]
    assert [(e["user"], e["role"], e["details"]) for e in logins] == [
        ("alice@example.com", "operator",
         {"method": "oidc", "groups": ["netlab-admins"], "subject": "u-alice"}),
        ("bob", "viewer", {"method": "oidc", "groups": ["netlab-readers"], "subject": "u-bob"})]
    assert "s3cr3t" not in (tmp_path / "audit.jsonl").read_text()
    assert "s3cr3t" not in (tmp_path / "sessions.json").read_text()


def test_oidc_login_is_tied_to_the_browser_and_works_once(tmp_path):
    async def body(client, provider, app):
        resp = await client.get("/login/oidc", allow_redirects=False)
        back = provider.authorize(resp.headers["Location"])
        path = urlsplit(back)._replace(scheme="", netloc="").geturl()
        # Another browser (no state cookie) cannot finish someone's login
        async with TestClient(TestServer(app)) as other:
            assert "not started in this browser" in _login_error(
                await other.get(path, allow_redirects=False))
            assert (await other.get("/api/me")).status == 401
        for bad in ("/login/oidc/callback", "/login/oidc/callback?code=code-0&state=guess",
                    "/login/oidc/callback?code=code-0"):
            assert "not started in this browser" in _login_error(
                await client.get(bad, allow_redirects=False))
        # The provider said no
        resp = await client.get("/login/oidc", allow_redirects=False)
        state = parse_qs(urlsplit(resp.headers["Location"]).query)["state"][0]
        refused = await client.get(f"/login/oidc/callback?state={state}&error=access_denied"
                                   "&error_description=Not+assigned", allow_redirects=False)
        assert _login_error(refused) == "The identity provider refused the login: Not assigned"
        # A code the provider does not know
        resp = await client.get("/login/oidc", allow_redirects=False)
        state = parse_qs(urlsplit(resp.headers["Location"]).query)["state"][0]
        resp = await client.get(f"/login/oidc/callback?state={state}&code=made-up",
                                allow_redirects=False)
        assert "gave no ID token: invalid_grant" in _login_error(resp)
        assert (await client.get("/api/me")).status == 401

        resp = await _sso(client, provider)
        assert resp.headers["Location"] == "/"
        # The same answer again starts nothing
        again = resp.request_info.url.path_qs
        client.session.cookie_jar.clear()
        assert _login_error(await client.get(again, allow_redirects=False))
        assert (await client.get("/api/me")).status == 401

    events = _oidc_scenario(tmp_path, body)
    failed = [e["details"] for e in events if e["event"] == "login_failed"]
    assert all(d["method"] == "oidc" for d in failed) and len(failed) == 7


def test_oidc_tokens_that_are_refused(tmp_path):
    async def body(client, provider, app):
        stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        good = provider.claims
        for sign, claims, error in (
                (lambda c: _jwt(c, key=stranger), good, "not from the provider's keys"),
                (lambda c: _jwt(c, "none"), good, "which is not accepted"),
                (_jwt, {**good, "aud": "another-app"}, "for another application"),
                (_jwt, {**good, "iss": "https://evil.example.com"}, "is from"),
                (_jwt, {**good, "nonce": "replayed"}, "does not belong to this login"),
                (_jwt, {**good, "exp": 1}, "expired"),
                (_jwt, {**good, "groups": ["sales"]},
                 "Alice@example.com is in no group that may use this GUI"),
                (_jwt, {**good, "preferred_username": "Admin"},
                 "Admin is also the name of a local user")):
            provider.sign, provider.claims = sign, claims
            assert error in _login_error(await _sso(client, provider)), error
            assert (await client.get("/api/me")).status == 401
        # Tokens signed with ES256 are fine
        provider.sign, provider.claims = (lambda c: _jwt(c, "ES256", "ec-1")), good
        assert (await _sso(client, provider)).headers["Location"] == "/"

    events = _oidc_scenario(tmp_path, body)
    assert [e["event"] for e in events].count("login_failed") == 8


def test_oidc_groups_from_userinfo(tmp_path):
    async def body(client, provider, app):
        provider.claims = {"sub": "u-alice", "preferred_username": "alice"}
        # Userinfo about someone else is not used
        provider.userinfo = {"sub": "u-mallory", "groups": ["netlab-admins"]}
        assert "in no group" in _login_error(await _sso(client, provider))
        provider.userinfo = {"sub": "u-alice", "groups": ["netlab-readers"],
                             "preferred_username": "not used"}
        assert (await _sso(client, provider)).headers["Location"] == "/"
        me = await (await client.get("/api/me")).json()
        assert (me["user"], me["role"]) == ("alice", "viewer")

    _oidc_scenario(tmp_path, body)


def test_oidc_sessions_end(tmp_path):
    async def body(client, provider, app):
        assert (await _sso(client, provider)).headers["Location"] == "/"
        assert (await client.get("/api/me")).status == 200
        (session,) = app[server.AUTH]._sessions.values()
        assert session.source == "oidc" and session.role == "operator"
        assert session.expires == pytest.approx(time.time() + 2 * 3600, abs=5)
        session.expires = time.time() - 1  # session_hours later
        assert (await client.get("/api/me")).status == 401

    _oidc_scenario(tmp_path, body, session_hours=2)


def test_oidc_sessions_survive_a_restart_unless_the_settings_change(tmp_path):
    kept = {}

    async def first(client, provider, app):
        assert (await _sso(client, provider)).headers["Location"] == "/"
        kept["cookies"] = {c.key: c.value for c in client.session.cookie_jar}

    async def same(client, provider, app):
        client.session.cookie_jar.update_cookies(kept["cookies"], client.make_url("/"))
        me = await (await client.get("/api/me")).json()
        assert (me["user"], me["role"]) == ("alice@example.com", "operator")

    async def changed(client, provider, app):
        client.session.cookie_jar.update_cookies(kept["cookies"], client.make_url("/"))
        assert (await client.get("/api/me")).status == 401

    # The fake provider gets a new port per run; the issuer is part of the settings
    _oidc_scenario(tmp_path, first)
    sessions = tmp_path / "sessions.json"
    saved = json.loads(sessions.read_text())
    (entry,) = saved["sessions"].values()
    assert entry["source"] == "oidc" and entry["role"] == "operator"

    def restart(body, **changes):
        async def scenario():
            section = json.loads((tmp_path / "directory.yaml").read_text())["oidc"]
            _write(tmp_path / "directory.yaml", json.dumps({"oidc": {**section, **changes}}))
            app = server.create_app(
                _workspace(tmp_path), users=UserStore(tmp_path / "users.yaml"), instance="8650",
                directory=load_directory(tmp_path / "directory.yaml"),
                session_file=auth.SessionFile(sessions))
            async with TestClient(TestServer(app)) as client:
                await body(client, None, app)
        asyncio.run(scenario())

    restart(same, client_secret="a rotated secret")
    restart(changed, roles={"viewer": ["netlab-admins"]})
    # Without the directory, or with --single-token, its sessions are nobody's
    sessions.write_text(json.dumps(saved))

    async def gone():
        for app in (server.create_app(_workspace(tmp_path), instance="8650",
                                      users=UserStore(tmp_path / "users.yaml"),
                                      session_file=auth.SessionFile(sessions)),
                    server.create_app(_workspace(tmp_path), "tok", instance="8650",
                                      session_file=auth.SessionFile(sessions))):
            async with TestClient(TestServer(app)) as client:
                await changed(client, None, app)
    asyncio.run(gone())


def test_oidc_provider_down_and_not_set_up(tmp_path):
    users = UserStore(tmp_path / "users.yaml")
    users.add("admin", password=PASSWORD)

    async def scenario():
        found = _oidc_directory(tmp_path, "http://127.0.0.1:9")  # nothing listens there
        app = server.create_app(_workspace(tmp_path), users=users, directory=found)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/login/oidc", allow_redirects=False)
            assert "could not be reached" in _login_error(resp)
            # The local users still get in
            resp = await client.post("/login", json={"username": "admin", "password": PASSWORD})
            assert resp.status == 200
        async with TestClient(TestServer(server.create_app(_workspace(tmp_path),
                                                           users=users))) as client:
            assert (await client.get("/login/oidc", allow_redirects=False)).status == 404
            assert (await client.get("/login/oidc/callback")).status == 404
            assert "X-Clabfleet-SSO" not in (await client.get("/api/me")).headers

    asyncio.run(scenario())
    with pytest.raises(ValueError, match="needs named users"):
        server.create_app(_workspace(tmp_path), "tok", directory=directory.Directory())


def test_oidc_discovery_must_be_the_issuers(tmp_path):
    async def body(client, provider, app):
        client_ = app[server.AUTH].oidc
        client_._metadata = None
        real = provider.issuer
        provider.issuer = "https://evil.example.com"
        with pytest.raises(DirectoryError, match="it is for the issuer"):
            await client_.metadata()
        provider.issuer = real
        assert (await client_.metadata())["token_endpoint"] == f"{real}/token"

    _oidc_scenario(tmp_path, body)


# ----------------------------------------------------------------------
# LDAP logins in the GUI
# ----------------------------------------------------------------------

def _ldap_scenario(tmp_path, body, **changes):
    users = UserStore(tmp_path / "users.yaml")
    users.add("admin", password=PASSWORD)
    audit_path = tmp_path / "audit.jsonl"
    fake = _people()
    found = directory.Directory(ldap=fake.directory(label="Example AD", **changes))

    async def scenario():
        app = server.create_app(_workspace(tmp_path), users=users, directory=found,
                                audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            await body(client, fake, app)

    asyncio.run(scenario())
    return _events(audit_path)


def test_ldap_login(tmp_path):
    async def body(client, fake, app):
        resp = await client.get("/api/me")
        assert resp.status == 401 and resp.headers["X-Clabfleet-Directory"] == "Example%20AD"
        assert "X-Clabfleet-SSO" not in resp.headers

        resp = await client.post("/login", json={"username": "Alice", "password": PASSWORD})
        assert resp.status == 200 and await resp.json() == {"user": "alice", "role": "operator"}
        me = await (await client.get("/api/me")).json()
        assert (me["user"], me["role"], me["has_password"]) == ("alice", "operator", False)
        assert (await client.post("/logout")).status == 200

        resp = await client.post("/login", json={"username": "bob", "password": PASSWORD})
        assert (await resp.json())["role"] == "viewer"
        assert (await client.post("/api/jobs", json={})).status == 403
        client.session.cookie_jar.clear()

        for name, password in (("alice", "wrong"), ("nobody", PASSWORD), ("alice", "")):
            resp = await client.post("/login", json={"username": name, "password": password})
            assert resp.status == 401 and await resp.text() == "invalid user name or password"
        # The right password and no role: told so, and not a failed login
        resp = await client.post("/login", json={"username": "carol", "password": PASSWORD})
        assert resp.status == 403
        assert await resp.text() == "Carol is in no group that may use this GUI"

        # A local user's password never goes to the directory
        fake.binds.clear()
        resp = await client.post("/login", json={"username": "admin", "password": "wrong"})
        assert resp.status == 401 and fake.binds == []
        resp = await client.post("/login", json={"username": "admin", "password": PASSWORD})
        assert resp.status == 200 and fake.binds == []

    events = _ldap_scenario(tmp_path, body)
    logins = [e for e in events if e["event"] == "login"]
    assert [(e["user"], e["details"]["method"]) for e in logins] == [
        ("alice", "ldap"), ("bob", "ldap"), ("admin", "password")]
    assert logins[0]["details"] == {"method": "ldap", "groups": [ADMINS.upper()],
                                    "subject": ALICE}
    reasons = [e["details"]["reason"] for e in events if e["event"] == "login_failed"]
    assert reasons.count("invalid user name or password") == 4
    assert "carol is in no group that may use this GUI" in reasons
    assert PASSWORD not in (tmp_path / "audit.jsonl").read_text()


def test_ldap_wrong_passwords_are_throttled(tmp_path):
    async def body(client, fake, app):
        for _ in range(server.LOGIN_FAILURES):
            resp = await client.post("/login", json={"username": "alice", "password": "wrong"})
            assert resp.status == 401
        fake.binds.clear()
        resp = await client.post("/login", json={"username": "alice", "password": PASSWORD})
        assert resp.status == 429 and fake.binds == []  # the directory is not asked

    _ldap_scenario(tmp_path, body)


def test_ldap_name_of_a_local_user_and_a_directory_that_is_down(tmp_path):
    async def body(client, fake, app):
        # "Admin" is not a local user, but the directory's "admin" would be one
        fake.entries["CN=Admin,DC=example,DC=com"] = {
            "password": PASSWORD, "sAMAccountName": "admin", "memberOf": [ADMINS]}
        resp = await client.post("/login", json={"username": "Admin", "password": PASSWORD})
        assert resp.status == 403 and "also the name of a local user" in await resp.text()

        fake.down = True
        for _ in range(server.LOGIN_FAILURES + 1):  # not counted as failed logins
            resp = await client.post("/login", json={"username": "alice", "password": PASSWORD})
            assert resp.status == 503 and "could not be reached" in await resp.text()
        resp = await client.post("/login", json={"username": "admin", "password": PASSWORD})
        assert resp.status == 200  # the local users still get in

    events = _ldap_scenario(tmp_path, body)
    assert "the directory could not be asked" in [
        e["details"].get("reason") for e in events if e["event"] == "login_failed"]


def test_ldap_logins_follow_the_directory(tmp_path, monkeypatch):
    async def body(client, fake, app):
        auth_ = app[server.AUTH]
        resp = await client.post("/login", json={"username": "alice", "password": PASSWORD})
        assert resp.status == 200
        (session,) = auth_._sessions.values()
        assert session.source == "ldap" and auth_.ldap_due() == []

        def later(seconds):
            session.checked -= seconds

        # Moved to the readers: a viewer at the next re-check
        fake.entries[ALICE]["memberOf"] = [READERS]
        await server._recheck_ldap(app)
        assert (await (await client.get("/api/me")).json())["role"] == "operator"  # not due yet
        later(301)
        assert auth_.ldap_due() == ["alice"]
        await server._recheck_ldap(app)
        assert (await (await client.get("/api/me")).json())["role"] == "viewer"
        assert auth_.ldap_due() == []

        # The directory is down: the login holds, and is asked about again later
        fake.down = True
        later(301)
        await server._recheck_ldap(app)
        assert (await client.get("/api/me")).status == 200
        assert auth_.ldap_due() == []  # not hammered
        monkeypatch.setattr(server, "DIRECTORY_RETRY", 0)
        assert auth_.ldap_due() == ["alice"]
        # ... until it has been down for too long
        later(3600)
        assert (await client.get("/api/me")).status == 401
        fake.down = False
        await server._recheck_ldap(app)
        assert (await client.get("/api/me")).status == 200

        # Out of every group with a role, or gone: logged out
        fake.entries[ALICE]["memberOf"] = []
        later(301)
        await server._recheck_ldap(app)
        assert (await client.get("/api/me")).status == 401 and not auth_._sessions
        resp = await client.post("/login", json={"username": "bob", "password": PASSWORD})
        assert resp.status == 200
        del fake.entries[BOB]
        next(iter(auth_._sessions.values())).checked -= 301
        await server._recheck_ldap(app)
        assert (await client.get("/api/me")).status == 401

    events = _ldap_scenario(tmp_path, body)
    ended = [(e["user"], e["details"]["reason"]) for e in events
             if e["event"] == "directory_login_ended"]
    assert ended == [("alice", "no role in the directory any more"),
                     ("bob", "not in the directory any more")]


def test_ldap_logins_from_before_a_restart_are_checked_first(tmp_path):
    users = UserStore(tmp_path / "users.yaml")
    users.add("admin", password=PASSWORD)
    fake = _people()
    found = directory.Directory(ldap=fake.directory())
    sessions = tmp_path / "sessions.json"
    kept = {}

    def app():
        return server.create_app(_workspace(tmp_path), users=users, directory=found,
                                 instance="8650", session_file=auth.SessionFile(sessions))

    async def before():
        async with TestClient(TestServer(app())) as client:
            await client.post("/login", json={"username": "alice", "password": PASSWORD})
            kept["cookies"] = {c.key: c.value for c in client.session.cookie_jar}

    async def after(status):
        async with TestClient(TestServer(app())) as client:
            client.session.cookie_jar.update_cookies(kept["cookies"], client.make_url("/"))
            assert (await client.get("/api/me")).status == status

    asyncio.run(before())
    saved = json.loads(sessions.read_text())
    for entry in saved["sessions"].values():  # the GUI was off for a day
        entry["checked"] -= 24 * 3600
    sessions.write_text(json.dumps(saved))
    asyncio.run(after(200))  # the directory vouches for her again at start-up

    saved = json.loads(sessions.read_text())
    for entry in saved["sessions"].values():
        entry["checked"] -= 24 * 3600
    sessions.write_text(json.dumps(saved))
    del fake.entries[ALICE]  # she left in the meantime
    asyncio.run(after(401))


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

@pytest.fixture
def fake_run(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "run", lambda *a, **kw: calls.append(kw))
    return calls


def test_gui_directory_file_selection(tmp_path, fake_run, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr(auth, "DEFAULT_USERS_FILE", home / "users.yaml")
    monkeypatch.setattr(directory, "DEFAULT_DIRECTORY_FILE", home / "directory.yaml")
    base = ["gui", "--dir", str(tmp_path), "--no-browser"]
    assert cli.main(base) == 0
    assert fake_run[-1]["directory"] is None  # no file, no directory
    capsys.readouterr()

    assert cli.main([*base, "--directory", str(tmp_path / "missing.yaml")]) == 1
    assert "Directory file not found" in capsys.readouterr().err
    _write(home / "directory.yaml", json.dumps({"ldap": LDAP_SECTION}))
    assert cli.main(base) == 0  # the default file is used when it is there
    assert fake_run[-1]["directory"].ldap.config.url == "ldaps://dc1.example.com"
    assert fake_run[-1]["directory"].oidc is None
    other = _write(tmp_path / "d.yaml", json.dumps({"oidc": OIDC_OK}))
    assert cli.main([*base, "--directory", str(other)]) == 0
    assert fake_run[-1]["directory"].oidc.client_id == "c"

    assert cli.main([*base, "--single-token"]) == 0  # the default file is left alone
    assert fake_run[-1]["directory"] is None
    assert cli.main([*base, "--single-token", "--directory", str(other)]) == 1
    assert "exclude each other" in capsys.readouterr().err
    _write(other, json.dumps({"oidc": {**OIDC_OK, "rolez": {}}}))
    assert cli.main([*base, "--directory", str(other)]) == 1
    assert "unknown setting rolez" in capsys.readouterr().err


def test_startup_message_names_the_directories(tmp_path):
    users = UserStore(tmp_path / "users.yaml")
    users.add("admin", password=PASSWORD)
    found = _oidc_directory(tmp_path, "https://id.example.com")
    found.ldap = _people().directory()
    _, text = server.startup_message("https://lab.example.com:8650", None, users, None, found)
    assert "Single sign-on: Example ID (https://id.example.com)" in text
    assert ("Redirect URI to register there: "
            "https://lab.example.com:8650/login/oidc/callback") in text
    assert "Directory logins: directory (ldaps://dc1.example.com)" in text
    assert "s3cr3t" not in text and "service password" not in text


def test_directory_check(tmp_path, monkeypatch, capsys):
    path = _write(tmp_path / "directory.yaml", json.dumps({"ldap": LDAP_SECTION}))
    fake = _people()
    monkeypatch.setattr(directory, "Ldap3Connection", fake.connect)
    base = ["directory", "check", "--directory", str(path)]

    def run(*args):
        rc = cli.main([*base, *args])
        out = capsys.readouterr()
        return rc, out.out + out.err

    rc, text = run()
    assert rc == 0 and "ldaps://dc1.example.com answers; bound as CN=svc-clabfleet" in text
    rc, text = run("--user", "alice")
    assert rc == 0 and f"alice is {ALICE}" in text and "role operator from" in text
    rc, text = run("--user", "carol")
    assert rc == 0 and "carol is in no group with a role and could not log in" in text
    rc, text = run("--user", "nobody")
    assert rc == 1 and "no user 'nobody' under DC=example,DC=com" in text
    fake.down = True
    rc, text = run()
    assert rc == 1 and "connection refused" in text
    assert "service password" not in text

    # OIDC: nothing answers at the issuer
    _write(path, json.dumps({"oidc": {**OIDC_OK, "issuer": "http://127.0.0.1:9"}}))
    rc, text = run()
    assert rc == 1 and "provider discovery" in text
    rc, text = run("--user", "alice")
    assert rc == 1 and "no 'ldap:' section" in text
