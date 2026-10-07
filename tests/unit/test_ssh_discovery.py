"""Tests for interactive SSH host discovery (--auto-discovery-ssh).

Every outside process is injected: ``ssh -G`` (the resolver), ``ssh-keyscan``
and the evaluator. No host is ever contacted and the real ``~/.ssh`` is never
read or written.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import threading
import tomllib
from pathlib import Path

import pytest

from bigfix_remote_client_relevance import ssh_discovery as ssh_discovery_module
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_QNA,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
)
from bigfix_remote_client_relevance.ssh_discovery import (
    KnownHost,
    MdnsService,
    Resolved,
    default_browse,
    describe,
    fingerprint,
    gather_candidates,
    known_hosts_line,
    parse_avahi_browse,
    parse_dns_sd_browse,
    parse_dns_sd_lookup,
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

MDNS_FIXTURES = Path(__file__).parent.parent / "fixtures" / "mdns"


def _fixture(name: str) -> str:
    return (MDNS_FIXTURES / name).read_text(encoding="utf-8")


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


# --- mDNS / DNS-SD ----------------------------------------------------------


def test_dns_sd_browse_lists_each_name_once_and_honors_removals():
    assert parse_dns_sd_browse(_fixture("dns_sd_browse.txt")) == [
        "THIS-MAC (796)",
        "build-box",
        "Alex\u2019s Mac mini",
        "Alex\u2019s iMac",
    ]


def test_dns_sd_browse_of_nothing_is_empty():
    assert parse_dns_sd_browse("Browsing for _ssh._tcp.local.\n") == []


def test_dns_sd_lookup_gives_host_without_trailing_dot_and_port():
    assert parse_dns_sd_lookup(_fixture("dns_sd_lookup.txt")) == ("Alexs-Mac-mini.local", 22)


def test_dns_sd_lookup_without_an_answer_is_none():
    assert parse_dns_sd_lookup("Lookup x._ssh._tcp.local.\n...STARTING...\n") is None


def test_avahi_browse_unescapes_labels_and_merges_addresses():
    assert parse_avahi_browse(_fixture("avahi_browse.txt")) == [
        MdnsService(
            "build-box", "build-box.local", 22, ("fe80::1234:5678:9abc:def0", "192.168.4.20")
        ),
        MdnsService("Alex\u2019s Mac mini", "Alexs-Mac-mini.local", 22, ("192.168.4.115",)),
        MdnsService("odd; name.v2", "odd-port.local", 2222, ("192.168.4.30",)),
    ]


def test_mdns_host_is_offered_with_its_label_and_needs_a_key():
    candidates = gather_candidates(
        aliases=[],
        known=[],
        resolve=_resolver(),
        mdns=[MdnsService("Alex\u2019s iMac", "Alexs-iMac.local")],
    )

    assert _names(candidates) == ["Alexs-iMac.local"]
    assert candidates[0].source == "mDNS"
    assert candidates[0].label == "Alex\u2019s iMac"
    assert candidates[0].needs_host_key


def test_mdns_host_already_in_known_hosts_merges_into_that_entry():
    known = parse_known_hosts(
        f"alexs-mac-mini.local ssh-ed25519 {KEY_A}\n192.168.4.115 ssh-ed25519 {KEY_A}\n"
    )

    candidates = gather_candidates(
        aliases=[],
        known=known,
        resolve=_resolver(),
        mdns=[MdnsService("Alex\u2019s Mac mini", "Alexs-Mac-mini.local")],
    )

    assert len(candidates) == 1
    assert candidates[0].name == "alexs-mac-mini.local"
    assert candidates[0].label == "Alex\u2019s Mac mini"
    assert not candidates[0].needs_host_key


def test_mdns_ssh_and_sftp_adverts_of_one_host_are_one_candidate():
    candidates = gather_candidates(
        aliases=[],
        known=[],
        resolve=_resolver(),
        mdns=[MdnsService("box", "box.local"), MdnsService("box", "box.local")],
    )

    assert _names(candidates) == ["box.local"]


def test_several_adverts_of_one_host_do_not_list_its_own_name_as_also():
    # Real avahi output: one container advertising three service names.
    candidates = gather_candidates(
        aliases=[],
        known=[],
        resolve=_resolver(),
        mdns=[
            MdnsService("adv-box", "adv-box.local"),
            MdnsService("Alex’s Mac mini", "adv-box.local"),
        ],
    )

    assert _names(candidates) == ["adv-box.local"]
    assert candidates[0].also == []


def test_mdns_this_machine_inventory_hosts_and_odd_ports_are_dropped():
    candidates = gather_candidates(
        aliases=[],
        known=[],
        resolve=_resolver(),
        existing={"Build-Box.local": {"transport": "ssh"}},
        self_names={"this-mac-2.local"},
        mdns=[
            MdnsService("THIS-MAC (796)", "THIS-MAC-2.local"),
            MdnsService("build-box", "build-box.local"),
            MdnsService("odd-port", "odd-port.local", 2222),
            MdnsService("keeper", "keeper.local"),
        ],
    )

    assert _names(candidates) == ["keeper.local"]


def test_listing_groups_mdns_last_and_shows_its_label(tmp_path):
    candidates = gather_candidates(
        aliases=["web"],
        known=parse_known_hosts(f"alpha ssh-ed25519 {KEY_A}\n"),
        resolve=_resolver(),
        mdns=[MdnsService("Alex\u2019s iMac", "Alexs-iMac.local")],
    )

    lines = describe(candidates, tmp_path)
    numbered = [line for line in lines if ". " in line and line.strip()[0].isdigit()]

    assert [line.split(". ")[1].split()[0] for line in numbered] == [
        "web",
        "alpha",
        "Alexs-iMac.local",
    ]
    assert any("mDNS" in line for line in lines)
    assert "Alex\u2019s iMac" in numbered[2]


def test_dns_sd_browse_runs_both_types_then_looks_each_name_up(monkeypatch):
    calls: list[tuple[list[str], float]] = []
    lookup = _fixture("dns_sd_lookup.txt")

    def fake_run_for(args, seconds, until=None):
        calls.append((list(args), seconds))
        if args[1] == "-B" and args[2] == "_ssh._tcp":
            return _fixture("dns_sd_browse.txt")
        if args[1] == "-B":
            return ""
        if args[2] == "Alex\u2019s Mac mini":
            return lookup
        return ""  # nothing resolves for the rest

    monkeypatch.setattr(ssh_discovery_module, "_run_for", fake_run_for)

    services = ssh_discovery_module._browse_dns_sd(4.0)

    assert services == [MdnsService("Alex\u2019s Mac mini", "Alexs-Mac-mini.local", 22)]
    browses = [args for args, _ in calls if args[1] == "-B"]
    assert sorted(b[2] for b in browses) == ["_sftp-ssh._tcp", "_ssh._tcp"]
    assert all(seconds == 4.0 for args, seconds in calls if args[1] == "-B")
    assert ["dns-sd", "-L", "build-box", "_ssh._tcp", "local."] in [a for a, _ in calls]


def test_default_browse_skips_windows_without_running_anything(monkeypatch):
    monkeypatch.setattr(ssh_discovery_module.sys, "platform", "win32")
    monkeypatch.setattr(ssh_discovery_module, "_run_for", pytest.fail)

    assert default_browse(1.0) == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_default_browse_without_the_tool_is_not_fatal(monkeypatch, platform):
    monkeypatch.setattr(ssh_discovery_module.sys, "platform", platform)
    monkeypatch.setattr(ssh_discovery_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(ssh_discovery_module, "_run_for", pytest.fail)

    assert default_browse(1.0) == []


@pytest.mark.skipif(sys.platform == "win32", reason="mDNS (and _run_for) is POSIX-only")
def test_run_for_stops_at_its_deadline_and_returns_what_it_read():
    script = "import sys,time; print('first', flush=True); time.sleep(30)"
    text = ssh_discovery_module._run_for([sys.executable, "-c", script], 0.5)

    assert text.strip() == "first"


@pytest.mark.skipif(sys.platform == "win32", reason="mDNS (and _run_for) is POSIX-only")
def test_run_for_stops_early_when_until_is_satisfied():
    import time

    script = "import sys,time; print('answer', flush=True); time.sleep(30)"
    started = time.monotonic()
    text = ssh_discovery_module._run_for(
        [sys.executable, "-c", script], 20.0, until=lambda out: "answer" in out
    )

    assert "answer" in text
    assert time.monotonic() - started < 10


def test_run_for_a_missing_program_is_empty():
    assert ssh_discovery_module._run_for(["no-such-program-xyz"], 0.5) == ""


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
        self.mdns: list[MdnsService] = []
        self.browse_windows: list[float] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def tell(self, text: str) -> None:
        self.told.append(text)

    def resolve(self, host: str) -> Resolved:
        return self.resolved.get(host, Resolved(hostname=host.lower()))

    def browse(self, window_s: float) -> list[MdnsService]:
        self.browse_windows.append(window_s)
        return list(self.mdns)

    def keyscan(self, hostname: str, port: int) -> list[tuple[str, str]]:
        self.scanned.append((hostname, port))
        return self.scan_keys.get(hostname, [])

    async def evaluate(self, client_relevance, targets, **kwargs):
        (target,) = targets
        become = bool(target.become)
        self.probed.append((target.name, become))
        default = [_ok(target.name)]
        return self.outcomes.get(target.name, {}).get(become, default)

    def run(self, **kwargs):
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
            browse=self.browse,
            **kwargs,
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


def test_mdns_only_host_is_listed_and_picking_it_accepts_its_key(tmp_path):
    session = Session(tmp_path, answers=["1"])
    session.mdns = [MdnsService("Alex\u2019s iMac", "Alexs-iMac.local")]
    session.scan_keys["alexs-imac.local"] = [("ssh-ed25519", KEY_C)]

    assert session.run() == ["Alexs-iMac.local"]
    assert "Alex\u2019s iMac" in session.output
    assert "accept" in session.output.lower()
    assert session.scanned == [("alexs-imac.local", 22)]
    known = (session.ssh_dir / "known_hosts").read_text(encoding="utf-8")
    assert f"alexs-imac.local ssh-ed25519 {KEY_C}" in known
    assert session.probed == [("Alexs-iMac.local", False)]


def test_mdns_window_is_passed_to_the_browse(tmp_path):
    session = Session(tmp_path)

    session.run(mdns_window_s=2.5)

    assert session.browse_windows == [2.5]


def test_mdns_can_be_turned_off(tmp_path):
    session = Session(tmp_path)
    session.mdns = [MdnsService("box", "box.local")]

    assert session.run(mdns_window_s=0) == []
    assert session.browse_windows == []
    assert session.prompts == []


def test_mdns_browse_overlaps_reading_and_resolving_the_files(tmp_path):
    """The browse is still running while ssh -G resolves the file candidates."""
    session = Session(tmp_path, known=f"alpha ssh-ed25519 {KEY_A}\n")
    resolving = threading.Event()
    overlapped: list[bool] = []
    plain_resolve = session.resolve

    def resolve(host):
        resolving.set()
        return plain_resolve(host)

    def browse(window_s):
        # Run sequentially before the files, this would wait the full timeout
        # and record False.
        overlapped.append(resolving.wait(timeout=5))
        return []

    session.resolve = resolve  # type: ignore[method-assign]
    session.browse = browse  # type: ignore[method-assign]

    session.run()

    assert overlapped == [True]


def test_a_failing_browse_is_not_fatal(tmp_path):
    session = Session(tmp_path, known=f"alpha ssh-ed25519 {KEY_A}\n", answers=["none"])

    def browse(window_s):
        raise OSError("boom")

    session.browse = browse  # type: ignore[method-assign]

    assert session.run() == []
    assert "alpha" in session.output
