"""Tests for interactive SSH host discovery (--auto-discovery-ssh).

Every outside process is injected: ``ssh -G`` (the resolver), ``ssh-keyscan``
and the evaluator. No host is ever contacted and the real ``~/.ssh`` is never
read or written.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import tomllib

import pytest

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_QNA,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
)
from bigfix_remote_client_relevance.ssh_discovery import (
    KnownHost,
    Resolved,
    fingerprint,
    gather_candidates,
    known_hosts_line,
    parse_known_hosts,
    parse_selection,
    parse_ssh_g,
    read_config_aliases,
    run_ssh_discovery,
)

# Distinct fake public-key blobs (base64 of arbitrary bytes is all the code needs).
KEY_A = base64.b64encode(b"key-a").decode()
KEY_B = base64.b64encode(b"key-b").decode()
KEY_C = base64.b64encode(b"key-c").decode()
KEY_SELF = base64.b64encode(b"key-self").decode()


# --- ~/.ssh/config -----------------------------------------------------------


def test_config_aliases_skip_wildcards_and_negations(tmp_path):
    config = tmp_path / "config"
    config.write_text(
        "Host *\n  ServerAliveInterval 30\n"
        "Host web db\n  User admin\n"
        "Host coder.*\n  ProxyCommand x\n"
        "Host build? !bad good\n",
        encoding="utf-8",
    )

    assert read_config_aliases(config) == ["web", "db", "good"]


def test_config_keywords_are_case_insensitive_and_accept_equals(tmp_path):
    config = tmp_path / "config"
    config.write_text('host=alpha\nHOST "beta"\n  # Host commented\n', encoding="utf-8")

    assert read_config_aliases(config) == ["alpha", "beta"]


def test_config_match_blocks_add_no_aliases(tmp_path):
    config = tmp_path / "config"
    config.write_text("Match host foo exec true\n  User x\nHost after\n", encoding="utf-8")

    assert read_config_aliases(config) == ["after"]


def test_config_follows_includes_relative_to_ssh_dir_with_globs(tmp_path):
    (tmp_path / "config.d").mkdir()
    (tmp_path / "config.d" / "a.conf").write_text("Host from-a\n", encoding="utf-8")
    (tmp_path / "config.d" / "b.conf").write_text("Host from-b\n", encoding="utf-8")
    absolute = tmp_path / "abs.conf"
    absolute.write_text("Host from-abs\n", encoding="utf-8")
    config = tmp_path / "config"
    config.write_text(
        f"Host first\nInclude config.d/*.conf {absolute}\nInclude missing.conf\n",
        encoding="utf-8",
    )

    assert read_config_aliases(config) == ["first", "from-a", "from-b", "from-abs"]


def test_config_include_loop_does_not_recurse_forever(tmp_path):
    config = tmp_path / "config"
    config.write_text(f"Host one\nInclude {config}\n", encoding="utf-8")

    assert read_config_aliases(config) == ["one"]


def test_missing_config_has_no_aliases(tmp_path):
    assert read_config_aliases(tmp_path / "nope") == []


# --- known_hosts -------------------------------------------------------------


def test_known_hosts_splits_names_and_bracketed_ports():
    text = (
        f"web,192.168.1.5 ssh-ed25519 {KEY_A}\n"
        f"[gitbox]:2222 ssh-rsa {KEY_B} comment here\n"
        "\n# a comment\n"
    )

    assert parse_known_hosts(text) == [
        KnownHost("web", 22, "ssh-ed25519", KEY_A),
        KnownHost("192.168.1.5", 22, "ssh-ed25519", KEY_A),
        KnownHost("gitbox", 2222, "ssh-rsa", KEY_B),
    ]


def test_known_hosts_skips_hashed_markers_wildcards_and_junk():
    text = (
        f"|1|c2FsdA==|aGFzaA== ssh-ed25519 {KEY_A}\n"
        f"@cert-authority *.example.com ssh-rsa {KEY_B}\n"
        f"@revoked bad ssh-rsa {KEY_B}\n"
        f"*.lan,!x.lan ssh-rsa {KEY_C}\n"
        "truncated-line\n"
    )

    assert parse_known_hosts(text) == []


def test_known_hosts_lowercases_names():
    assert parse_known_hosts(f"Mac-Mini.local ssh-ed25519 {KEY_A}\n") == [
        KnownHost("mac-mini.local", 22, "ssh-ed25519", KEY_A)
    ]


# --- ssh -G ------------------------------------------------------------------


def test_parse_ssh_g_reads_the_fields_discovery_uses():
    out = (
        "user jgstew\nhostname 192.168.4.115\nport 22\n"
        "hashknownhosts yes\nhostkeyalias minibox\nserveraliveinterval 0\n"
    )

    assert parse_ssh_g(out) == Resolved(
        hostname="192.168.4.115", port=22, user="jgstew", hostkeyalias="minibox", hashed=True
    )


def test_parse_ssh_g_defaults_when_fields_missing():
    assert parse_ssh_g("hostname box\n") == Resolved(hostname="box")


# --- candidates and duplicates ----------------------------------------------


def _resolver(table: dict[str, Resolved] | None = None):
    table = table or {}

    def resolve(host: str) -> Resolved:
        return table.get(host, Resolved(hostname=host.lower()))

    return resolve


def _names(candidates):
    return [c.name for c in candidates]


def test_known_hosts_names_sharing_a_key_are_one_machine():
    known = parse_known_hosts(
        f"192.168.4.115 ssh-ed25519 {KEY_A}\n"
        f"mini.local ecdsa-sha2-nistp256 {KEY_B}\n"
        f"mini.local ssh-ed25519 {KEY_A}\n"
    )

    candidates = gather_candidates(aliases=[], known=known, resolve=_resolver())

    assert _names(candidates) == ["mini.local"]
    assert candidates[0].also == ["192.168.4.115"]


def test_config_alias_wins_and_absorbs_its_known_hosts_entries():
    known = parse_known_hosts(f"10.0.0.9 ssh-ed25519 {KEY_A}\nweb.example ssh-ed25519 {KEY_A}\n")

    candidates = gather_candidates(
        aliases=["web"],
        known=known,
        resolve=_resolver({"web": Resolved(hostname="10.0.0.9")}),
    )

    assert _names(candidates) == ["web"]
    assert candidates[0].source == "ssh config"
    assert set(candidates[0].also) == {"10.0.0.9", "web.example"}
    assert not candidates[0].needs_host_key


def test_config_alias_found_by_host_key_alias():
    known = parse_known_hosts(f"boxkey ssh-ed25519 {KEY_A}\n")

    candidates = gather_candidates(
        aliases=["box"],
        known=known,
        resolve=_resolver({"box": Resolved(hostname="10.1.1.1", hostkeyalias="boxkey")}),
    )

    assert _names(candidates) == ["box"]
    assert not candidates[0].needs_host_key


def test_distinct_keys_stay_distinct_and_ips_sort_after_names():
    known = parse_known_hosts(f"10.0.0.2 ssh-ed25519 {KEY_A}\nalpha ssh-ed25519 {KEY_B}\n")

    assert _names(gather_candidates(aliases=[], known=known, resolve=_resolver())) == [
        "alpha",
        "10.0.0.2",
    ]


def test_non_default_port_known_hosts_entries_are_left_out():
    # The inventory has no `port`; such a host is only reachable through an alias.
    known = parse_known_hosts(f"[gitbox]:2222 ssh-ed25519 {KEY_A}\n")

    assert gather_candidates(aliases=[], known=known, resolve=_resolver()) == []


def test_config_alias_with_no_key_needs_one():
    candidates = gather_candidates(aliases=["newbox"], known=[], resolve=_resolver())

    assert candidates[0].needs_host_key


def test_existing_inventory_host_is_dropped_by_name_and_by_key():
    known = parse_known_hosts(
        f"mini.local ssh-ed25519 {KEY_A}\n"
        f"192.168.4.115 ssh-ed25519 {KEY_A}\n"
        f"other ssh-ed25519 {KEY_B}\n"
        f"Third ssh-ed25519 {KEY_C}\n"
    )

    candidates = gather_candidates(
        aliases=[],
        known=known,
        resolve=_resolver(),
        existing={
            # same machine as 192.168.4.115, by key
            "Mini.local": {"transport": "ssh"},
            # same name, different case
            "third": {},
            # not an SSH host, so its name says nothing
            "other": {"transport": "container", "image": "x"},
        },
    )

    assert _names(candidates) == ["other"]


def test_this_machine_is_dropped_by_key_and_by_name():
    known = parse_known_hosts(
        f"10.0.0.50 ssh-ed25519 {KEY_SELF}\n"
        f"localhost ssh-ed25519 {KEY_B}\n"
        f"mylaptop.local ssh-ed25519 {KEY_C}\n"
        f"elsewhere ssh-ed25519 {KEY_A}\n"
    )

    candidates = gather_candidates(
        aliases=[],
        known=known,
        resolve=_resolver(),
        self_keys={KEY_SELF},
        self_names={"mylaptop.local"},
    )

    assert _names(candidates) == ["elsewhere"]


@pytest.mark.parametrize(
    "host",
    ["github.com", "ssh.github.com", "gitlab.com", "bitbucket.org", "ssh.dev.azure.com"],
)
def test_public_git_hosts_are_never_offered(host):
    known = parse_known_hosts(f"{host} ssh-ed25519 {KEY_A}\nkeeper ssh-ed25519 {KEY_B}\n")

    assert _names(gather_candidates(aliases=[], known=known, resolve=_resolver())) == ["keeper"]


def test_git_host_is_skipped_under_an_alias_or_another_address():
    # `Host gh` -> github.com, and an IP sharing github.com's key.
    known = parse_known_hosts(f"github.com ssh-ed25519 {KEY_A}\n140.82.112.3 ssh-ed25519 {KEY_A}\n")

    candidates = gather_candidates(
        aliases=["gh"],
        known=known,
        resolve=_resolver({"gh": Resolved(hostname="github.com")}),
    )

    assert candidates == []


def test_git_host_match_is_exact_not_by_prefix():
    known = parse_known_hosts(f"github01.example.com ssh-ed25519 {KEY_A}\n")

    assert _names(gather_candidates(aliases=[], known=known, resolve=_resolver())) == [
        "github01.example.com"
    ]


# --- selection ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,4-6", [0, 3, 4, 5]),
        (" 2 , 1 ", [0, 1]),
        ("3 1", [0, 2]),
        ("all", [0, 1, 2, 3, 4, 5]),
        ("ALL", [0, 1, 2, 3, 4, 5]),
        ("none", []),
        ("", []),
        ("2,2", [1]),
    ],
)
def test_parse_selection(text, expected):
    assert parse_selection(text, 6) == expected


@pytest.mark.parametrize("text", ["0", "7", "1-9", "x", "3-1", "1,,"])
def test_parse_selection_rejects_bad_input(text):
    with pytest.raises(ValueError):
        parse_selection(text, 6)


# --- known_hosts lines -------------------------------------------------------


def test_known_hosts_line_plain_and_bracketed():
    assert known_hosts_line("box", 22, "ssh-ed25519", KEY_A) == f"box ssh-ed25519 {KEY_A}"
    assert known_hosts_line("box", 2222, "ssh-ed25519", KEY_A) == f"[box]:2222 ssh-ed25519 {KEY_A}"


def test_known_hosts_line_hashed_matches_openssh_format():
    salt = b"0123456789abcdefghij"
    line = known_hosts_line("box", 22, "ssh-ed25519", KEY_A, hashed=True, salt=salt)
    digest = hmac.new(salt, b"box", hashlib.sha1).digest()

    assert line == (
        f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"
        f" ssh-ed25519 {KEY_A}"
    )


def test_fingerprint_is_openssh_sha256_style():
    expected = base64.b64encode(hashlib.sha256(b"key-a").digest()).decode().rstrip("=")

    assert fingerprint(KEY_A) == f"SHA256:{expected}"


# --- the whole interactive run ----------------------------------------------


def _ok(host: str, answer: str = "Linux") -> ClientRelevanceResult:
    return ClientRelevanceResult(
        host=host,
        transport="fake",
        client_relevance="x",
        answers=[answer],
        answer_types=["string"],
    )


def _failed(host: str, kind: str) -> ClientRelevanceResult:
    return ClientRelevanceResult(
        host=host, transport="fake", client_relevance="x", error="boom", error_kind=kind
    )


class Session:
    """The injected world for run_ssh_discovery: a fake ~/.ssh and fake processes."""

    def __init__(self, tmp_path, *, config="", known="", answers=("none",)) -> None:
        self.ssh_dir = tmp_path / ".ssh"
        self.ssh_dir.mkdir()
        (self.ssh_dir / "config").write_text(config, encoding="utf-8")
        (self.ssh_dir / "known_hosts").write_text(known, encoding="utf-8")
        self.inventory = tmp_path / ".bigfix" / "remote_clients.toml"
        self.answers = list(answers)
        self.told: list[str] = []
        self.prompts: list[str] = []
        self.probed: list[tuple[str, bool]] = []
        self.scanned: list[tuple[str, int]] = []
        self.resolved: dict[str, Resolved] = {}
        self.scan_keys: dict[str, list[tuple[str, str]]] = {}
        # name -> list of results per attempt; become attempt is the second
        self.outcomes: dict[str, dict[bool, list[ClientRelevanceResult]]] = {}

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def tell(self, text: str) -> None:
        self.told.append(text)

    def resolve(self, host: str) -> Resolved:
        return self.resolved.get(host, Resolved(hostname=host.lower()))

    def keyscan(self, hostname: str, port: int) -> list[tuple[str, str]]:
        self.scanned.append((hostname, port))
        return self.scan_keys.get(hostname, [])

    async def evaluate(self, client_relevance, targets, **kwargs):
        (target,) = targets
        become = bool(target.become)
        self.probed.append((target.name, become))
        default = [_ok(target.name)]
        return self.outcomes.get(target.name, {}).get(become, default)

    def run(self):
        return run_ssh_discovery(
            self.inventory,
            ask=self.ask,
            tell=self.tell,
            ssh_dir=self.ssh_dir,
            resolve=self.resolve,
            keyscan=self.keyscan,
            evaluate=self.evaluate,
            self_keys=set(),
            self_names=set(),
        )

    def hosts(self) -> dict:
        return tomllib.loads(self.inventory.read_text(encoding="utf-8"))["hosts"]

    @property
    def output(self) -> str:
        return "\n".join(self.told)


def test_nothing_is_probed_or_written_when_user_picks_none(tmp_path):
    session = Session(tmp_path, known=f"alpha ssh-ed25519 {KEY_A}\n", answers=["none"])

    assert session.run() == []
    assert session.probed == []
    assert session.scanned == []
    assert not session.inventory.exists()
    assert "alpha" in session.output


def test_eof_at_the_prompt_means_none(tmp_path):
    session = Session(tmp_path, known=f"alpha ssh-ed25519 {KEY_A}\n", answers=[])

    assert session.run() == []
    assert session.probed == []


def test_no_candidates_asks_nothing(tmp_path):
    session = Session(tmp_path)

    assert session.run() == []
    assert session.prompts == []


def test_only_picked_hosts_are_probed_and_only_working_ones_written(tmp_path):
    session = Session(
        tmp_path,
        known=f"alpha ssh-ed25519 {KEY_A}\nbeta ssh-ed25519 {KEY_B}\ngamma ssh-ed25519 {KEY_C}\n",
        answers=["1,3"],
    )
    session.outcomes["gamma"] = {
        False: [_failed("gamma", ERROR_KIND_TRANSPORT)],
    }

    assert session.run() == ["alpha"]
    assert {name for name, _ in session.probed} == {"alpha", "gamma"}
    assert session.hosts() == {"alpha": {"transport": "ssh"}}


def test_invalid_selection_is_asked_again(tmp_path):
    session = Session(tmp_path, known=f"alpha ssh-ed25519 {KEY_A}\n", answers=["9", "1"])

    assert session.run() == ["alpha"]
    assert len(session.prompts) == 2
    assert "9" in session.output


def test_become_is_tried_when_plain_run_fails_short_of_transport(tmp_path):
    # e.g. macOS, where qna needs root: connects fine, fails without sudo.
    session = Session(tmp_path, known=f"mac ssh-ed25519 {KEY_A}\n", answers=["all"])
    session.outcomes["mac"] = {
        False: [_failed("mac", ERROR_KIND_QNA)],
        True: [_ok("mac", "Mac OS X")],
    }

    assert session.run() == ["mac"]
    assert session.probed == [("mac", False), ("mac", True)]
    assert session.hosts() == {"mac": {"transport": "ssh", "become": True}}


def test_become_is_not_tried_when_host_is_unreachable(tmp_path):
    session = Session(tmp_path, known=f"gone ssh-ed25519 {KEY_A}\n", answers=["all"])
    session.outcomes["gone"] = {False: [_failed("gone", ERROR_KIND_TRANSPORT)]}

    assert session.run() == []
    assert session.probed == [("gone", False)]


def test_keyless_host_says_picking_it_accepts_its_key(tmp_path):
    session = Session(tmp_path, config="Host newbox\n", answers=["none"])

    session.run()

    listing = session.output
    assert "newbox" in listing
    assert "known_hosts" in listing
    assert "accept" in listing.lower()
    assert session.scanned == []


def test_picking_keyless_host_scans_appends_key_then_probes(tmp_path):
    session = Session(tmp_path, config="Host newbox\n", answers=["1"])
    session.resolved["newbox"] = Resolved(hostname="10.9.9.9")
    session.scan_keys["10.9.9.9"] = [("ssh-ed25519", KEY_C)]

    assert session.run() == ["newbox"]
    assert session.scanned == [("10.9.9.9", 22)]
    known = (session.ssh_dir / "known_hosts").read_text(encoding="utf-8")
    assert f"10.9.9.9 ssh-ed25519 {KEY_C}" in known
    assert fingerprint(KEY_C) in session.output
    assert session.probed == [("newbox", False)]


def test_appended_key_is_hashed_when_ssh_config_says_so(tmp_path):
    session = Session(tmp_path, config="Host newbox\n", answers=["1"])
    session.resolved["newbox"] = Resolved(hostname="10.9.9.9", hashed=True)
    session.scan_keys["10.9.9.9"] = [("ssh-ed25519", KEY_C)]

    session.run()

    known = (session.ssh_dir / "known_hosts").read_text(encoding="utf-8")
    assert known.startswith("|1|")
    assert "10.9.9.9" not in known


def test_appended_key_goes_on_its_own_line_after_unterminated_file(tmp_path):
    session = Session(
        tmp_path, config="Host newbox\n", known=f"alpha ssh-ed25519 {KEY_A}", answers=["1"]
    )
    session.scan_keys["newbox"] = [("ssh-ed25519", KEY_C)]

    session.run()

    lines = (session.ssh_dir / "known_hosts").read_text(encoding="utf-8").splitlines()
    assert lines == [f"alpha ssh-ed25519 {KEY_A}", f"newbox ssh-ed25519 {KEY_C}"]


def test_keyless_host_whose_key_cannot_be_fetched_is_not_probed(tmp_path):
    session = Session(tmp_path, config="Host newbox\n", answers=["1"])

    assert session.run() == []
    assert session.probed == []
    assert "newbox" in session.output


def test_existing_inventory_hosts_are_not_offered(tmp_path):
    session = Session(
        tmp_path,
        known=f"mini.local ssh-ed25519 {KEY_A}\n192.168.4.115 ssh-ed25519 {KEY_A}\n",
        answers=["all"],
    )
    session.inventory.parent.mkdir()
    session.inventory.write_text(
        '# mine\n[hosts."mini.local"]\ntransport = "ssh"\n', encoding="utf-8"
    )

    assert session.run() == []
    assert session.prompts == []
    assert session.inventory.read_text(encoding="utf-8").startswith("# mine")
