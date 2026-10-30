"""Tests for SessionContext: how Fast Query targets reach their server.

A front end (an iOS app, the debugger's NiceGUI app) holds its own
connections, trust store, prompt and SaaS sign-ins; the orchestrator must use
those instead of the config file and an implicit trust store. Everything
here is offline: httpx's MockTransport stands in for the server through
``SessionContext.http_transport``, and the trust tests use a TLS server on
127.0.0.1 with a throwaway self-signed certificate, since the session
library only consults the trust policy for a real TLS handshake.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import ssl
import threading
import warnings
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from bigfix_remote_session_relevance import (
    ClientQueryResponse,
    ClientQueryRow,
    Config,
    ConnectionConfig,
    KnownCerts,
    SaasSessionPool,
    TrustStore,
)

from bigfix_remote_client_relevance import SessionContext
from bigfix_remote_client_relevance.orchestrate import (
    Target,
    default_transport_factory,
    evaluate_client_relevance,
    evaluate_client_relevance_stream,
    evaluate_many,
    evaluate_many_stream,
    fastquery_transport,
    require_fastquery_connection,
)
from bigfix_remote_client_relevance.results import ERROR_KIND_TRANSPORT
from bigfix_remote_client_relevance.transports import fastquery
from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery

SUBMITTED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<BESAPI><ClientQuery Resource="https://x/api/clientquery/7"><ID>7</ID></ClientQuery></BESAPI>'
)
ANSWERED = {
    "results": [{"computerID": 11, "computerName": "web", "isFailure": False, "result": "Linux"}]
}


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    # The first results check would otherwise wait POLL_FIRST_S (2 s).
    monkeypatch.setattr(fastquery, "POLL_FIRST_S", 0.0)


def rest(name: str = "lab", **kwargs) -> ConnectionConfig:
    fields = {
        "backend": "rest",
        "server": "bigfix.example",
        "username": "op",
        "password": "<PASSWORD>",
    }
    fields.update(kwargs)
    return ConnectionConfig(name=name, **fields)  # type: ignore[arg-type]


def saas(name: str = "cloud", **kwargs) -> ConnectionConfig:
    fields = {
        "backend": "saas",
        "server": "login.bigfix.com",
        "username": "operator@example.com",
        "password": "<PASSWORD>",
    }
    fields.update(kwargs)
    return ConnectionConfig(name=name, **fields)  # type: ignore[arg-type]


def fastquery_target(connection: str = "lab", ids: tuple[int, ...] = (11,)) -> Target:
    return Target(kind="fastquery", name=connection, connection=connection, computer_ids=ids)


class FakeRestServer:
    """BESAPI's ClientQuery calls, answering computer 11 with "Linux"."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self, conn: ConnectionConfig | None = None) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/api/clientquery":
            return httpx.Response(200, text=SUBMITTED)
        if request.url.path == "/api/clientqueryresults/7":
            return httpx.Response(200, json=ANSWERED)
        return httpx.Response(404)


class FakeSaas:
    """BigFix SaaS sign-in and one-computer ClientQuery, counting sign-ins."""

    def __init__(self) -> None:
        self.logins = 0
        self.passwords: list[str] = []

    def transport(self, conn: ConnectionConfig | None = None) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/v1/user/login":
            self.logins += 1
            self.passwords.append(json.loads(request.content)["data"]["password"])
            return httpx.Response(
                200,
                json={"status": 200, "data": {"iat": 0, "exp": 3600, "pids": {"bf": {}}}},
                headers=[
                    ("set-cookie", "accessToken=ACCESS; Path=/; HttpOnly; Secure"),
                    ("set-cookie", "refreshToken=REFRESH; Path=/; HttpOnly; Secure"),
                ],
            )
        fixtures = Path(__file__).parent.parent / "fixtures" / "saas_fastquery"
        name = {
            "/core/v2/clientquery/create": "create_ok.json",
            "/bfproxy/v1/clientquery/listresults": "listresults_answered.json",
        }.get(request.url.path)
        if name is None:
            return httpx.Response(404)
        return httpx.Response(
            200,
            text=(fixtures / name).read_text(encoding="utf-8"),
            headers={"content-type": "application/json"},
        )


def _no_config_file(monkeypatch) -> list[object]:
    """Make reading the config file fail loudly, and record any attempt."""
    import bigfix_remote_session_relevance

    calls: list[object] = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("load_config must not be called when a Config is given")

    monkeypatch.setattr(bigfix_remote_session_relevance, "load_config", refuse)
    return calls


# --- the caller's Config ---------------------------------------------------


async def test_a_session_config_is_used_without_reading_the_file(monkeypatch):
    calls = _no_config_file(monkeypatch)
    server = FakeRestServer()
    session = SessionContext(config=Config(connections=(rest(),)), http_transport=server.transport)

    results = await evaluate_client_relevance(
        "name of operating system", [fastquery_target()], session=session
    )

    assert calls == []
    assert [(r.host, r.answers) for r in results] == [("web", ["Linux"])]
    assert ("POST", "/api/clientquery") in [(r.method, r.url.path) for r in server.requests]
    assert all(r.headers.get("authorization") for r in server.requests)


@pytest.mark.parametrize(
    ("conn", "name", "message"),
    [
        (rest("other"), "lab", "no connection named 'lab'; run --setup-fastquery"),
        (rest(enabled=False), "lab", "connection 'lab' is disabled; enable it to use Fast Query"),
        (
            rest(backend="webreports"),
            "lab",
            (
                "connection 'lab' is a webreports connection, and Fast Query only works "
                "through REST or SaaS; run --setup-fastquery"
            ),
        ),
    ],
    ids=["unknown", "disabled", "wrong-backend"],
)
def test_name_lookup_errors_are_unchanged_with_a_caller_config(monkeypatch, conn, name, message):
    _no_config_file(monkeypatch)

    with pytest.raises(ValueError) as caught:
        require_fastquery_connection(name, config=Config(connections=(conn,)))

    assert str(caught.value).startswith(message)


def test_no_connection_name_is_refused_before_any_lookup(monkeypatch):
    _no_config_file(monkeypatch)

    with pytest.raises(ValueError, match="Fast Query needs a connection"):
        require_fastquery_connection(None, config=Config())


def test_http_transport_is_asked_once_per_target_with_its_connection(monkeypatch):
    _no_config_file(monkeypatch)
    seen: list[ConnectionConfig] = []
    conn = rest()

    def per_connection(c: ConnectionConfig) -> httpx.AsyncBaseTransport:
        seen.append(c)
        return FakeRestServer().transport()

    fastquery_transport(
        fastquery_target(),
        session=SessionContext(config=Config(connections=(conn,)), http_transport=per_connection),
    )

    assert seen == [conn]


# --- the prompt ------------------------------------------------------------


async def test_the_prompt_answers_a_missing_saas_password(monkeypatch):
    _no_config_file(monkeypatch)
    service = FakeSaas()
    asked: list[str] = []

    def prompt(question: str) -> str:
        asked.append(question)
        return "<TYPED>"

    session = SessionContext(
        config=Config(connections=(saas(password=None),)),
        prompt=prompt,
        http_transport=service.transport,
    )

    results = await evaluate_client_relevance("true", [fastquery_target("cloud")], session=session)

    assert len(asked) == 1
    assert service.passwords == ["<TYPED>"]
    assert results[0].answers == ["Win11"]


# --- SaaS sign-ins ---------------------------------------------------------


async def test_a_session_pool_shares_one_saas_sign_in_across_runs(monkeypatch):
    """An app keeps one pool for its lifetime (on one event loop): a second
    run must not sign in again, since failed sign-ins lock the account."""
    _no_config_file(monkeypatch)
    service = FakeSaas()
    session = SessionContext(
        config=Config(connections=(saas(),)),
        saas_sessions=SaasSessionPool(),
        http_transport=service.transport,
    )

    for _ in range(2):
        results = await evaluate_client_relevance(
            "true", [fastquery_target("cloud")], session=session
        )
        assert results[0].answers == ["Win11"]

    assert service.logins == 1


async def test_without_a_session_pool_each_run_signs_in_again(monkeypatch):
    """The per-run pool is still the default, so the test above is not
    passing by accident."""
    _no_config_file(monkeypatch)
    service = FakeSaas()
    session = SessionContext(config=Config(connections=(saas(),)), http_transport=service.transport)

    for _ in range(2):
        await evaluate_client_relevance("true", [fastquery_target("cloud")], session=session)

    assert service.logins == 2


def test_an_explicit_pool_wins_over_the_sessions(monkeypatch):
    _no_config_file(monkeypatch)
    ours, theirs = SaasSessionPool(), SaasSessionPool()
    session = SessionContext(config=Config(connections=(saas(),)), saas_sessions=theirs)
    target = fastquery_target("cloud")

    first = fastquery_transport(target, session=session, saas_sessions=ours)
    second = fastquery_transport(target, session=session, saas_sessions=ours)
    other = fastquery_transport(target, session=session)

    assert first._backend._session is second._backend._session  # type: ignore[attr-defined]
    assert other._backend._session is not first._backend._session  # type: ignore[attr-defined]


# --- the default (no SessionContext) ---------------------------------------


def _write_connections(text: str) -> None:
    import os

    Path(os.environ["BFRSR_CONFIG"]).write_text(text, encoding="utf-8")


def test_no_session_reads_the_config_file_and_trusts_on_first_use_explicitly(monkeypatch):
    """Today's behaviour, minus the session library's implicit TrustStore:
    an explicit "auto" store pins a first-use certificate as before and
    does not change when session 2.0 flips its default to refuse."""
    import bigfix_remote_session_relevance
    from bigfix_remote_session_relevance import trust

    _write_connections(
        "[[connections]]\n"
        'name = "lab"\nbackend = "rest"\nserver = "bigfix.example"\n'
        'username = "op"\npassword = "<PASSWORD>"\n'
    )
    loads: list[object] = []
    real_load = bigfix_remote_session_relevance.load_config

    def counting_load(*args, **kwargs):
        loads.append(args)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(bigfix_remote_session_relevance, "load_config", counting_load)
    # The warning fires once per process; make this test see it regardless of order.
    monkeypatch.setattr(trust, "_implicit_auto_warned", False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        transport = default_transport_factory(fastquery_target())

    assert len(loads) == 1
    store = transport._backend.trust  # type: ignore[attr-defined]
    assert isinstance(store, TrustStore)
    assert store.trust_on_first_use == "auto"
    assert store.callback is None


def test_an_empty_session_is_the_default(monkeypatch):
    from bigfix_remote_session_relevance import trust

    _write_connections(
        "[[connections]]\n"
        'name = "lab"\nbackend = "rest"\nserver = "bigfix.example"\n'
        'username = "op"\npassword = "<PASSWORD>"\n'
    )
    monkeypatch.setattr(trust, "_implicit_auto_warned", False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        transport = fastquery_transport(fastquery_target(), session=SessionContext())

    assert transport._backend.trust.trust_on_first_use == "auto"  # type: ignore[attr-defined]


def test_the_session_trust_policy_reaches_the_backend(monkeypatch):
    _no_config_file(monkeypatch)
    store = TrustStore(trust_on_first_use=False)

    transport = fastquery_transport(
        fastquery_target(),
        session=SessionContext(config=Config(connections=(rest(),)), trust=store),
    )

    assert transport._backend.trust is store  # type: ignore[attr-defined]


def test_default_transport_factory_passes_the_session_on(monkeypatch):
    _no_config_file(monkeypatch)
    store = TrustStore(trust_on_first_use=False)
    session = SessionContext(config=Config(connections=(rest(),)), trust=store)

    transport = default_transport_factory(fastquery_target(), session=session)

    assert isinstance(transport, TransportFastQuery)
    assert transport._backend.trust is store  # type: ignore[attr-defined]


# --- plumbing --------------------------------------------------------------


async def test_every_evaluate_entry_point_takes_a_session(monkeypatch):
    _no_config_file(monkeypatch)
    server = FakeRestServer()
    session = SessionContext(config=Config(connections=(rest(),)), http_transport=server.transport)
    targets = [fastquery_target()]

    async def drain(stream):
        return [result async for result in stream]

    outcomes = [
        await evaluate_client_relevance("x", targets, session=session),
        await drain(evaluate_client_relevance_stream("x", targets, session=session)),
        await evaluate_many(["x"], targets, session=session),
        await drain(evaluate_many_stream(["x"], targets, session=session)),
    ]

    for results in outcomes:
        results = [r[1] if isinstance(r, tuple) else r for r in results]
        assert [(r.host, r.answers) for r in results] == [("web", ["Linux"])]


async def test_a_session_with_a_custom_factory_is_refused():
    """The custom factory would silently ignore the session's trust policy,
    so a front end that asked for deferred trust would not get it."""
    with pytest.raises(ValueError, match="fastquery_transport"):
        await evaluate_client_relevance(
            "true",
            [fastquery_target()],
            transport_factory=lambda t: pytest.fail("never built"),
            session=SessionContext(),
        )


def test_session_context_is_exported():
    import bigfix_remote_client_relevance as package
    from bigfix_remote_client_relevance import orchestrate

    for name in ("SessionContext", "fastquery_transport"):
        assert name in package.__all__
        assert name in orchestrate.__all__


def test_session_context_is_frozen_and_keyword_only():
    with pytest.raises(TypeError):
        SessionContext(Config())  # type: ignore[misc]
    session = SessionContext()
    with pytest.raises(AttributeError):
        session.trust = None  # type: ignore[misc]


# --- the backend's target limit --------------------------------------------


class LimitedBackend:
    """The ClientQuery calls, with a ``client_query_max_targets`` limit."""

    def __init__(self, limit: int | None) -> None:
        self.client_query_max_targets = limit
        self.submitted: list[list[int]] = []

    @staticmethod
    def _response(query_id: int, rows: list) -> ClientQueryResponse:
        return ClientQueryResponse(
            query_id=query_id,
            rows=rows,
            error=None,
            connection="lab",
            server="bigfix.example:52311",
            backend="rest",
            elapsed_ms=1.0,
        )

    async def submit_client_query(self, client_relevance, computer_ids, *, use_client_context):
        self.submitted.append(list(computer_ids))
        return self._response(len(self.submitted), [])

    async def client_query_results(self, query_id):
        ids = self.submitted[query_id - 1]
        return self._response(query_id, [ClientQueryRow(i, f"pc{i}", False, "ok") for i in ids])

    async def evaluate(self, session_relevance):  # pragma: no cover - computer_query unused
        raise AssertionError


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(1, [[1], [2], [3]]), (None, [[1, 2, 3]])],
    ids=["one-per-query", "no-limit"],
)
async def test_the_backends_target_limit_decides_the_queries(limit, expected):
    backend = LimitedBackend(limit)

    results = await TransportFastQuery(
        backend,  # type: ignore[arg-type]
        computer_ids=[1, 2, 3],
    ).evaluate_client_relevance_fanout("true", timeout_s=5)

    assert sorted(backend.submitted) == expected
    assert [r.host for r in results] == ["pc1", "pc2", "pc3"]


# --- trust, against a local TLS server -------------------------------------


class _TLSServer:
    def __init__(self, httpd: ThreadingHTTPServer, seen: list[tuple[str, str, bool]]) -> None:
        self.httpd = httpd
        self.seen = seen
        self.host_port = f"127.0.0.1:{httpd.server_address[1]}"


def _self_signed(directory: Path) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_server(tmp_path) -> Iterator[_TLSServer]:
    """BESAPI's ClientQuery calls over HTTPS on 127.0.0.1, self-signed.

    Records (method, path, had Authorization) for every request that got
    past the handshake: an untrusted certificate must mean none did.
    """
    cert, key = _self_signed(tmp_path)
    seen: list[tuple[str, str, bool]] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: str, content_type: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append(("POST", self.path, "Authorization" in self.headers))
            if self.path == "/api/clientquery":
                self._reply(200, SUBMITTED, "application/xml")
            else:
                self._reply(404, "", "text/plain")

        def do_GET(self) -> None:
            seen.append(("GET", self.path, "Authorization" in self.headers))
            if self.path.startswith("/api/clientqueryresults/7"):
                self._reply(200, json.dumps(ANSWERED), "application/json")
            else:
                self._reply(404, "", "text/plain")

        def log_message(self, format: str, *args: object) -> None:
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address) -> None:
            # Refused handshakes (the client rejecting our cert) are expected.
            pass

    httpd = Server(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield _TLSServer(httpd, seen)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _tls_session(server: _TLSServer, trust) -> SessionContext:
    conn = rest(server=f"https://{server.host_port}", verify="tofu")
    return SessionContext(config=Config(connections=(conn,)), trust=trust)


async def test_the_session_trust_policy_decides_a_first_use_certificate(tls_server, monkeypatch):
    """A refusal from the caller's policy means no credentialed request."""
    _no_config_file(monkeypatch)

    class Refusing:
        def __init__(self) -> None:
            self.asked: list[str] = []
            self._store = TrustStore(trust_on_first_use=False, record_refused=False)

        def is_pinned(self, address):
            return False

        def ssl_context(self, address):
            return self._store.ssl_context(address)

        async def on_untrusted(self, address):
            self.asked.append(address.host_port)
            return False

        async def mismatch_error(self, address, backend):  # pragma: no cover
            raise AssertionError("nothing is pinned")

    policy = Refusing()

    results = await evaluate_client_relevance(
        "true", [fastquery_target()], session=_tls_session(tls_server, policy)
    )

    assert policy.asked == [tls_server.host_port]
    assert tls_server.seen == []
    assert results[0].error_kind == ERROR_KIND_TRANSPORT
    assert tls_server.host_port in (results[0].error or "")


async def test_deferred_trust_refuses_then_runs_after_accept(tls_server, tmp_path, monkeypatch):
    """A front end's flow: refuse and record, show the fingerprint, accept,
    run again -- with no callback blocking the run."""
    _no_config_file(monkeypatch)
    store = TrustStore(KnownCerts(tmp_path / "known_certs.toml"), trust_on_first_use=False)
    session = _tls_session(tls_server, store)

    refused = await evaluate_client_relevance("true", [fastquery_target()], session=session)

    assert tls_server.seen == []
    assert list(store.pending) == [tls_server.host_port]
    fingerprint = store.pending[tls_server.host_port].fingerprint
    assert refused[0].error_kind == ERROR_KIND_TRANSPORT
    assert tls_server.host_port in (refused[0].error or "")
    assert fingerprint in (refused[0].error or "")

    store.accept(tls_server.host_port)
    answered = await evaluate_client_relevance("true", [fastquery_target()], session=session)

    assert [(r.host, r.answers) for r in answered] == [("web", ["Linux"])]
    assert tls_server.seen and all(had_auth for _, _, had_auth in tls_server.seen)
