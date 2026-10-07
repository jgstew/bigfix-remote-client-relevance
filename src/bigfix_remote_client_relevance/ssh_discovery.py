"""Interactive SSH host discovery (``--auto-discovery-ssh``).

SSH targets are other people's machines: even a failed login can raise a
login alert, an MFA prompt or a lockout. So nothing is contacted until the
user has picked it from a list, and nothing is written that didn't work.

Nothing is connected to while gathering candidates:

* ``~/.ssh/config`` ``Host`` aliases (wildcards and negations skipped,
  ``Include`` followed).
* ``~/.ssh/known_hosts`` names (hashed entries are unreadable and skipped;
  entries on a port other than 22 are left out, since the inventory has no
  ``port`` and only a config alias can reach them).
* mDNS / DNS-SD: hosts that advertise ``_ssh._tcp`` or ``_sftp-ssh._tcp``
  (macOS Remote Login does) on the local network -- ``dns-sd`` on macOS,
  ``avahi-browse`` on Linux, skipped on Windows or when the tool is missing.
  A single standard query that only hears hosts which chose to advertise;
  no scanning. The browse starts first and collects for a few seconds while
  the files are read and resolved, so its wait overlaps that work. Its
  display names (``Alex's Mac mini``) are looked up to real host names
  (``Alexs-Mac-mini.local``), and this machine, which may advertise itself,
  is dropped like any other.

Public git hosting (``github.com``, ``gitlab.com``, ... -- see ``GIT_HOSTS``)
is never offered, under any name or address that shares its key.

Each is resolved with ``ssh -G`` (config only, no network) and duplicates are
merged by host key: two names whose ``known_hosts`` keys overlap are the same
machine, whatever address each was reached by. Hosts already in the inventory
and this machine itself are dropped the same way.

When keys can't decide -- one side has none, typically a host heard only
over mDNS -- two candidates (or a candidate and an inventory host) whose
addresses overlap are listed as *probable* duplicates, never merged
silently: an IP and a ``.local`` name may be one machine, but a multi-homed
host or a reused DHCP lease can fool an address match. Once picked, host
keys decide: the same key as an inventory host or an earlier pick means the
same machine, so it's skipped; a different key gets a warning and the host
is kept separate.

A picked host with no key in ``known_hosts`` would always fail the test --
the SSH transport (asyncssh) verifies host keys and never prompts -- so the
list says that picking it also accepts its key. After selection its key is
fetched with ``ssh-keyscan``, its fingerprint shown, and it is appended to
``~/.ssh/known_hosts`` before the test.

Each picked host is tested as-is first; when that fails for any reason but
not connecting (e.g. macOS, where qna needs root), it is tried again with
``become`` (``sudo -n``, so never a password prompt).

The library never prints: prompts and the list go through the caller's
``ask``/``tell``, which the CLI sends to stderr.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import glob
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import selectors
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bigfix_remote_client_relevance.discovery import (
    PROBE_RELEVANCE,
    Evaluator,
    ensure_writable,
    read_existing,
    write_discovered,
)
from bigfix_remote_client_relevance.inventory import _target_from_entry
from bigfix_remote_client_relevance.orchestrate import evaluate_client_relevance
from bigfix_remote_client_relevance.results import ERROR_KIND_TRANSPORT

logger = logging.getLogger(__name__)

# ssh -G and ssh-keyscan: the first never touches the network, the second
# only reaches hosts the user picked.
RESOLVE_TIMEOUT_S = 5.0
KEYSCAN_TIMEOUT_S = 5

# Shorter than container discovery's: nothing is pulled, only connected to.
SSH_PROBE_TIMEOUT_S = 120.0

DEFAULT_SSH_PORT = 22

# How long the mDNS browse collects. Adverts nearly all arrive within half a
# second; the odd _sftp-ssh record takes a few.
MDNS_WINDOW_S = 5.0
# Per-name lookup of a display name to its host; answers are near-instant.
MDNS_LOOKUP_S = 3.0
MDNS_SERVICE_TYPES = ("_ssh._tcp", "_sftp-ssh._tcp")

# getaddrinfo, for spotting one machine under two names. Run in parallel;
# a name that hasn't answered by then just has no addresses.
ADDRESS_TIMEOUT_S = 3.0

SOURCE_CONFIG = "ssh config"
SOURCE_KNOWN_HOSTS = "known_hosts"
SOURCE_MDNS = "mDNS"
# Listing order, and which source's name wins when one machine has several.
_SOURCE_RANK = {SOURCE_CONFIG: 0, SOURCE_KNOWN_HOSTS: 1, SOURCE_MDNS: 2}

_ALWAYS_SELF = frozenset({"localhost", "127.0.0.1", "::1"})

# Public git hosting: in nearly everyone's known_hosts, never a BigFix client.
# Exact names only -- a self-hosted `github01.corp` or `gitlab-runner` could
# be a real machine, so nothing here matches by prefix.
GIT_HOSTS = frozenset(
    {
        "github.com",
        "ssh.github.com",
        "gist.github.com",
        "gitlab.com",
        "altssh.gitlab.com",
        "bitbucket.org",
        "altssh.bitbucket.org",
        "ssh.dev.azure.com",
        "vs-ssh.visualstudio.com",
        "codeberg.org",
        "git.sr.ht",
        "source.developers.google.com",
        "git-codecommit.us-east-1.amazonaws.com",
    }
)

Resolver = Callable[[str], "Resolved"]
KeyScanner = Callable[[str, int], list[tuple[str, str]]]
AddressLookup = Callable[[str], Collection[str]]
Browser = Callable[[float], list["MdnsService"]]


@dataclass(frozen=True)
class KnownHost:
    """One host name from one ``known_hosts`` line (lowercased, as ssh compares)."""

    host: str
    port: int
    keytype: str
    key: str


@dataclass(frozen=True)
class MdnsService:
    """One SSH service heard over mDNS: its display label and real host."""

    label: str
    host: str
    port: int = DEFAULT_SSH_PORT
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolved:
    """What ``ssh -G`` says connecting to a name would actually use."""

    hostname: str
    port: int = DEFAULT_SSH_PORT
    user: str | None = None
    hostkeyalias: str | None = None
    hashed: bool = False

    @property
    def key_name(self) -> str:
        """The name ssh looks the host key up under."""
        return (self.hostkeyalias or self.hostname).lower()


@dataclass(frozen=True)
class Probable:
    """Another host sharing an address with a candidate: maybe the same machine."""

    name: str
    keys: frozenset[str]
    in_inventory: bool = False


@dataclass
class SSHCandidate:
    """One machine offered for selection, possibly under several names."""

    name: str
    source: str
    resolved: Resolved
    also: list[str] = field(default_factory=list)
    keys: frozenset[str] = frozenset()
    label: str | None = None
    """The mDNS display name, when the machine advertised itself."""
    addresses: frozenset[str] = frozenset()
    probable: list[Probable] = field(default_factory=list)
    """Hosts sharing an address where keys can't settle it; checked once picked."""

    @property
    def needs_host_key(self) -> bool:
        return not self.keys


# --- reading the files ---------------------------------------------------


def _is_pattern(name: str) -> bool:
    return any(char in name for char in "*?!")


def _config_words(line: str) -> list[str]:
    line = line.strip()
    if not line or line.startswith("#"):
        return []
    # `Keyword=value` is as valid as `Keyword value`.
    line = re.sub(r"^(\w+)\s*=\s*", r"\1 ", line)
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.split()


def read_config_aliases(path: Path, *, _seen: set[Path] | None = None) -> list[str]:
    """Concrete ``Host`` names in an ssh config, following ``Include``.

    Relative ``Include`` paths are taken from the config's own directory,
    which is ``~/.ssh`` for the user config, as ssh does.
    """
    seen = _seen if _seen is not None else set()
    try:
        resolved = path.resolve()
        if resolved in seen:
            return []
        seen.add(resolved)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    aliases: list[str] = []
    for line in text.splitlines():
        words = _config_words(line)
        if not words:
            continue
        keyword, args = words[0].lower(), words[1:]
        if keyword == "host":
            aliases.extend(a for a in args if not _is_pattern(a) and not a.startswith("-"))
        elif keyword == "include":
            for pattern in args:
                expanded = os.path.expanduser(pattern)
                if not os.path.isabs(expanded):
                    expanded = str(path.parent / expanded)
                for match in sorted(glob.glob(expanded)):
                    aliases.extend(read_config_aliases(Path(match), _seen=seen))
    return list(dict.fromkeys(aliases))


def _host_and_port(name: str) -> tuple[str, int]:
    bracketed = re.fullmatch(r"\[(.+)\]:(\d+)", name)
    if bracketed:
        return bracketed.group(1), int(bracketed.group(2))
    return name, DEFAULT_SSH_PORT


def parse_known_hosts(text: str) -> list[KnownHost]:
    """Readable host names in a ``known_hosts`` file, one per name per line."""
    entries: list[KnownHost] = []
    for line in text.splitlines():
        fields = line.split()
        # Markers (@cert-authority, @revoked) aren't keys for a host.
        if len(fields) < 3 or fields[0].startswith(("#", "@", "|")):
            continue
        names, keytype, key = fields[0], fields[1], fields[2]
        for name in names.split(","):
            if not name or _is_pattern(name):
                continue
            host, port = _host_and_port(name.lower())
            entries.append(KnownHost(host, port, keytype, key))
    return entries


def _read_known_hosts(ssh_dir: Path) -> list[KnownHost]:
    entries: list[KnownHost] = []
    for name in ("known_hosts", "known_hosts2"):
        try:
            text = (ssh_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entries.extend(parse_known_hosts(text))
    return entries


def parse_ssh_g(text: str) -> Resolved:
    """The fields discovery uses from ``ssh -G`` output."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        values.setdefault(key.lower(), value.strip())
    alias = values.get("hostkeyalias")
    try:
        port = int(values.get("port", DEFAULT_SSH_PORT))
    except ValueError:
        port = DEFAULT_SSH_PORT
    return Resolved(
        hostname=values.get("hostname", ""),
        port=port,
        user=values.get("user") or None,
        hostkeyalias=alias if alias and alias != "none" else None,
        hashed=values.get("hashknownhosts") == "yes",
    )


def default_resolver(host: str) -> Resolved:
    """``ssh -G host``: applies config, ``Include`` and ``Match`` as ssh would.

    Falls back to the name itself when ssh isn't there or fails.
    """
    try:
        completed = subprocess.run(
            ["ssh", "-G", "--", host],
            capture_output=True,
            text=True,
            timeout=RESOLVE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("ssh -G %s failed: %s", host, exc)
        return Resolved(hostname=host)
    if completed.returncode != 0:
        logger.debug("ssh -G %s exited %d: %s", host, completed.returncode, completed.stderr)
        return Resolved(hostname=host)
    resolved = parse_ssh_g(completed.stdout)
    return resolved if resolved.hostname else Resolved(hostname=host)


def default_keyscan(hostname: str, port: int) -> list[tuple[str, str]]:
    """``ssh-keyscan``: the host's public keys, without logging in."""
    try:
        completed = subprocess.run(
            ["ssh-keyscan", "-T", str(KEYSCAN_TIMEOUT_S), "-p", str(port), "--", hostname],
            capture_output=True,
            text=True,
            timeout=KEYSCAN_TIMEOUT_S * 3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("ssh-keyscan %s failed: %s", hostname, exc)
        return []
    keys = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and not fields[0].startswith("#"):
            keys.append((fields[1], fields[2]))
    return keys


def default_self_keys(directory: Path = Path("/etc/ssh")) -> set[str]:
    """This machine's own SSH host keys, when it runs an SSH server."""
    keys = set()
    for public in directory.glob("ssh_host_*_key.pub"):
        try:
            fields = public.read_text(encoding="utf-8").split()
        except OSError:
            continue
        if len(fields) >= 2:
            keys.add(fields[1])
    return keys


def default_self_names() -> set[str]:
    """Names this machine goes by, including its Bonjour ``.local`` name."""
    names = set()
    hostname = socket.gethostname().lower()
    if hostname:
        short = hostname.split(".")[0]
        names |= {hostname, short, f"{short}.local"}
    if sys.platform == "darwin":
        try:
            local = subprocess.run(
                ["scutil", "--get", "LocalHostName"],
                capture_output=True,
                text=True,
                timeout=RESOLVE_TIMEOUT_S,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            local = ""
        if local:
            names.add(f"{local.lower()}.local")
    return names


# --- mDNS ------------------------------------------------------------------


def _run_for(
    args: Sequence[str], seconds: float, until: Callable[[str], bool] | None = None
) -> str:
    """Run ``args`` for at most ``seconds`` and return its stdout.

    For commands that never exit on their own (``dns-sd``): stopped at the
    deadline, or as soon as ``until`` is satisfied by what has been read.
    A missing program is just no output. POSIX only (pipes in a selector).
    """
    try:
        process = subprocess.Popen(
            list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        logger.debug("could not run %s: %s", args[0], exc)
        return ""
    assert process.stdout is not None
    chunks: list[bytes] = []
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while (remaining := deadline - time.monotonic()) > 0:
                if not selector.select(remaining):
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if until is not None and until(b"".join(chunks).decode("utf-8", "replace")):
                    break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # Whatever a buffered writer flushed on its way out.
        chunks.append(process.stdout.read())
        process.stdout.close()
    return b"".join(chunks).decode("utf-8", "replace")


_DNS_SD_EVENT = re.compile(r"^\S+\s+(Add|Rmv)\s+\d+\s+(\d+)\s+\S+\s+\S+\s+(.*\S)\s*$")
_DNS_SD_REACHED = re.compile(r"can be reached at (\S+):(\d+)")


def parse_dns_sd_browse(text: str) -> list[str]:
    """Display names still advertised at the end of ``dns-sd -B`` output.

    Each name is listed once per network interface; it counts as present
    while any interface still has it.
    """
    interfaces: dict[str, set[str]] = {}
    for line in text.splitlines():
        match = _DNS_SD_EVENT.match(line)
        if not match:
            continue
        event, interface, name = match.groups()
        if event == "Add":
            interfaces.setdefault(name, set()).add(interface)
        elif name in interfaces:
            interfaces[name].discard(interface)
    return [name for name, present in interfaces.items() if present]


def parse_dns_sd_lookup(text: str) -> tuple[str, int] | None:
    """The host and port from ``dns-sd -L`` output, or None before it answers."""
    match = _DNS_SD_REACHED.search(text)
    if not match:
        return None
    return match.group(1).rstrip("."), int(match.group(2))


def _avahi_unescape(text: str) -> str:
    """Undo avahi-browse's ``\\DDD`` (decimal byte) and ``\\x`` escapes."""
    out = bytearray()
    i = 0
    while i < len(text):
        digits = text[i + 1 : i + 4]
        if text[i] == "\\" and len(digits) == 3 and digits.isdigit() and int(digits) < 256:
            out.append(int(digits))
            i += 4
        elif text[i] == "\\" and i + 1 < len(text):
            out += text[i + 1].encode()
            i += 2
        else:
            out += text[i].encode()
            i += 1
    return out.decode("utf-8", "replace")


def parse_avahi_browse(text: str) -> list[MdnsService]:
    """Resolved (``=``) services from ``avahi-browse -rpt``, one per host and port."""
    services: dict[tuple[str, str, int], list[str]] = {}
    for line in text.splitlines():
        fields = line.split(";")
        if len(fields) < 9 or fields[0] != "=":
            continue
        try:
            port = int(fields[8])
        except ValueError:
            continue
        key = (_avahi_unescape(fields[3]), fields[6].rstrip("."), port)
        addresses = services.setdefault(key, [])
        if fields[7] and fields[7] not in addresses:
            addresses.append(fields[7])
    return [
        MdnsService(label, host, port, tuple(addresses))
        for (label, host, port), addresses in services.items()
    ]


def _unique(services: Iterable[MdnsService]) -> list[MdnsService]:
    return list(dict.fromkeys(services))


def _browse_dns_sd(window_s: float) -> list[MdnsService]:
    with ThreadPoolExecutor(max_workers=16) as pool:
        browses = list(
            pool.map(
                lambda kind: _run_for(["dns-sd", "-B", kind, "local."], window_s),
                MDNS_SERVICE_TYPES,
            )
        )
        names = list(
            dict.fromkeys(
                (name, kind)
                for kind, text in zip(MDNS_SERVICE_TYPES, browses, strict=True)
                for name in parse_dns_sd_browse(text)
            )
        )

        def lookup(item: tuple[str, str]) -> tuple[str, int] | None:
            name, kind = item
            return parse_dns_sd_lookup(
                _run_for(
                    ["dns-sd", "-L", name, kind, "local."],
                    MDNS_LOOKUP_S,
                    until=lambda out: parse_dns_sd_lookup(out) is not None,
                )
            )

        answers = list(pool.map(lookup, names))
    services = []
    for (name, _), answer in zip(names, answers, strict=True):
        if answer is None:
            logger.debug("mDNS: no host for %r", name)
            continue
        services.append(MdnsService(name, *answer))
    return _unique(services)


def _browse_avahi(window_s: float) -> list[MdnsService]:
    # -t ends the browse by itself once the cache is dumped; the window is
    # only a backstop.
    with ThreadPoolExecutor(max_workers=len(MDNS_SERVICE_TYPES)) as pool:
        texts = list(
            pool.map(
                lambda kind: _run_for(["avahi-browse", "-rpt", kind], window_s),
                MDNS_SERVICE_TYPES,
            )
        )
    return _unique(service for text in texts for service in parse_avahi_browse(text))


def default_browse(window_s: float) -> list[MdnsService]:
    """SSH services advertised on the local network, or none where unsupported."""
    if sys.platform == "darwin":
        if shutil.which("dns-sd"):
            return _browse_dns_sd(window_s)
        logger.debug("mDNS: dns-sd not found; skipping")
    elif sys.platform.startswith("linux"):
        if shutil.which("avahi-browse"):
            return _browse_avahi(window_s)
        logger.debug("mDNS: avahi-browse not found (install avahi-utils); skipping")
    else:
        # Windows' OpenSSH server doesn't advertise itself anyway.
        logger.debug("mDNS: not supported on %s; skipping", sys.platform)
    return []


def _browse_result(browsing: Future[list[MdnsService]] | None) -> list[MdnsService]:
    if browsing is None:
        return []
    try:
        return browsing.result()
    except Exception as exc:  # noqa: BLE001 -- an optional source must never end discovery
        logger.info("mDNS browse failed, continuing without it: %s", exc)
        return []


# --- addresses -------------------------------------------------------------


def _usable_address(address: str) -> str | None:
    """``address`` without an IPv6 ``%scope``, or None for loopback and junk.

    Loopback never identifies a machine: Debian maps its own name to
    127.0.1.1, so every such host would "match" every other.
    """
    address = address.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_unspecified:
        return None
    return str(ip)


def default_lookup(name: str) -> set[str]:
    """``name``'s addresses via getaddrinfo (``.local`` included where supported)."""
    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        logger.debug("no addresses for %s: %s", name, exc)
        return set()
    return {address for *_, sockaddr in infos if (address := _usable_address(str(sockaddr[0])))}


class _AddressLookups:
    """Lookups started early and collected later, under one deadline.

    The pool is never waited on: a lookup stuck in the resolver is left to
    finish on its own rather than holding discovery up.
    """

    def __init__(self, lookup: AddressLookup) -> None:
        self._lookup = lookup
        self._pool = ThreadPoolExecutor(max_workers=16)
        self._futures: dict[str, Future[Collection[str]]] = {}

    def start(self, names: Iterable[str]) -> None:
        for name in names:
            key = name.lower()
            if key and key not in self._futures and not _is_ip(key):
                self._futures[key] = self._pool.submit(self._lookup, name)

    def collect(self, timeout_s: float) -> dict[str, frozenset[str]]:
        done, _ = wait(self._futures.values(), timeout=timeout_s)
        self._pool.shutdown(wait=False, cancel_futures=True)
        found = {}
        for key, future in self._futures.items():
            if future not in done or future.exception() is not None:
                continue
            addresses = frozenset(
                usable for address in future.result() if (usable := _usable_address(address))
            )
            if addresses:
                found[key] = addresses
        return found


def resolve_addresses(
    names: Iterable[str], lookup: AddressLookup, *, timeout_s: float = ADDRESS_TIMEOUT_S
) -> dict[str, frozenset[str]]:
    """Each name's addresses (keyed lowercase), looked up in parallel."""
    lookups = _AddressLookups(lookup)
    lookups.start(names)
    return lookups.collect(timeout_s)


# --- candidates ------------------------------------------------------------


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


@dataclass
class _Entry:
    name: str
    source: str
    order: int
    resolved: Resolved
    keys: set[str]
    names: set[tuple[str, int]]
    label: str | None = None
    addresses: set[str] = field(default_factory=set)


def _key_index(known: Iterable[KnownHost]) -> dict[tuple[str, int], set[str]]:
    index: dict[tuple[str, int], set[str]] = {}
    for entry in known:
        index.setdefault((entry.host, entry.port), set()).add(entry.key)
    return index


def _entry(
    name: str,
    source: str,
    order: int,
    resolve: Resolver,
    index: Mapping[tuple[str, int], set[str]],
) -> _Entry:
    resolved = resolve(name)
    keys = set(index.get((resolved.key_name, resolved.port), ()))
    names = {(name.lower(), resolved.port), (resolved.hostname.lower(), resolved.port)}
    return _Entry(name, source, order, resolved, keys, names)


def _addresses_of(entry: _Entry, table: Mapping[str, Collection[str]]) -> set[str]:
    found = set(entry.addresses)
    for name, _ in entry.names:
        if _is_ip(name):
            if usable := _usable_address(name):
                found.add(usable)
        else:
            found |= {a for a in map(_usable_address, table.get(name, ())) if a}
    return found


def _link_probable(
    candidates: Sequence[SSHCandidate], inventory: Sequence[tuple[str, _Entry]]
) -> None:
    """Note address overlaps that host keys can't settle (one side has none)."""
    for i, one in enumerate(candidates):
        for other in candidates[i + 1 :]:
            if one.addresses & other.addresses and not (one.keys and other.keys):
                one.probable.append(Probable(other.name, other.keys))
                other.probable.append(Probable(one.name, one.keys))
        for name, entry in inventory:
            if one.addresses & entry.addresses and not (one.keys and entry.keys):
                one.probable.append(Probable(name, frozenset(entry.keys), in_inventory=True))


def _groups(entries: list[_Entry]) -> list[list[_Entry]]:
    """Entries that share a host key or a resolved name, merged transitively."""
    groups: list[list[_Entry]] = []
    for entry in entries:
        touching = [
            group
            for group in groups
            if any(entry.keys & other.keys or entry.names & other.names for other in group)
        ]
        merged = [member for group in touching for member in group] + [entry]
        groups = [group for group in groups if group not in touching] + [merged]
    return groups


def _representative(group: list[_Entry]) -> _Entry:
    # A config alias is a name the user chose; otherwise a name beats an IP.
    return min(group, key=lambda e: (e.source != SOURCE_CONFIG, _is_ip(e.name), e.order))


def gather_candidates(
    *,
    aliases: Sequence[str],
    known: Sequence[KnownHost],
    resolve: Resolver,
    existing: Mapping[str, Mapping[str, Any]] | None = None,
    self_keys: Collection[str] = (),
    self_names: Collection[str] = (),
    mdns: Sequence[MdnsService] = (),
    addresses: Mapping[str, Collection[str]] | None = None,
) -> list[SSHCandidate]:
    """One candidate per machine not already in ``existing`` and not this one.

    ``addresses`` (names lowercased, as :func:`resolve_addresses` gives them)
    turns on the probable-duplicate check; without it there is none.
    """
    index = _key_index(known)
    entries = [_entry(alias, SOURCE_CONFIG, i, resolve, index) for i, alias in enumerate(aliases)]
    hosts = dict.fromkeys(
        entry.host for entry in known if entry.port == DEFAULT_SSH_PORT and entry.host
    )
    entries += [
        _entry(host, SOURCE_KNOWN_HOSTS, len(entries) + i, resolve, index)
        for i, host in enumerate(hosts)
    ]
    for service in mdns:
        if service.port != DEFAULT_SSH_PORT:
            logger.debug("mDNS: skipping %s on port %d", service.host, service.port)
            continue
        entry = _entry(service.host, SOURCE_MDNS, len(entries), resolve, index)
        entry.label = service.label
        entry.addresses = {a for a in map(_usable_address, service.addresses) if a}
        entries.append(entry)

    taken_keys = set(self_keys)
    taken_names = {(name.lower(), DEFAULT_SSH_PORT) for name in (*self_names, *_ALWAYS_SELF)}
    inventory: list[tuple[str, _Entry]] = []
    for name, config in (existing or {}).items():
        if str(config.get("transport", "ssh")) != "ssh":
            continue
        known_entry = _entry(name, "inventory", -1, resolve, index)
        taken_keys |= known_entry.keys
        taken_names |= known_entry.names
        inventory.append((name, known_entry))

    table = addresses or {}
    for entry in [*entries, *(e for _, e in inventory)]:
        entry.addresses = _addresses_of(entry, table)

    candidates = []
    for group in _groups(entries):
        keys = set().union(*(e.keys for e in group))
        names = set().union(*(e.names for e in group))
        if keys & taken_keys or names & taken_names:
            logger.debug("ssh discovery: skipping known %s", [e.name for e in group])
            continue
        if any(name in GIT_HOSTS for name, _ in names):
            logger.debug("ssh discovery: skipping git hosting %s", [e.name for e in group])
            continue
        chosen = _representative(group)
        also = [
            e.name
            for e in sorted(group, key=lambda e: e.order)
            if e.name.lower() != chosen.name.lower()
        ]
        candidates.append(
            SSHCandidate(
                name=chosen.name,
                source=chosen.source,
                resolved=chosen.resolved,
                also=list(dict.fromkeys(also)),
                keys=frozenset(keys),
                label=next((e.label for e in group if e.label), None),
                addresses=frozenset(set().union(*(e.addresses for e in group))),
            )
        )
    # By source, then named hosts before bare IPs; stable within each.
    candidates.sort(key=lambda c: (_SOURCE_RANK[c.source], _is_ip(c.name)))
    _link_probable(candidates, inventory)
    return candidates


# --- the interactive bits --------------------------------------------------


def parse_selection(text: str, count: int) -> list[int]:
    """``1,4-6`` / ``all`` / ``none`` (or empty) into sorted 0-based indices."""
    text = text.strip().lower()
    if text in ("", "none"):
        return []
    if text == "all":
        return list(range(count))
    picked: set[int] = set()
    for token in re.split(r"[\s,]+", text):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise ValueError(f"not a number or range: {token!r}")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if not 1 <= first <= last <= count:
            raise ValueError(f"{token} is not within 1-{count}")
        picked.update(range(first - 1, last))
    return sorted(picked)


def describe(candidates: Sequence[SSHCandidate], ssh_dir: Path) -> list[str]:
    """The numbered list, grouped by where each host came from."""
    known_hosts = ssh_dir / "known_hosts"
    lines = ["SSH hosts found (none of them has been contacted):"]
    headers = {
        SOURCE_CONFIG: f"from {ssh_dir / 'config'}:",
        SOURCE_KNOWN_HOSTS: f"from {known_hosts}:",
        SOURCE_MDNS: "advertised on the local network (mDNS):",
    }
    source = None
    for number, candidate in enumerate(candidates, 1):
        if candidate.source != source:
            source = candidate.source
            lines.append(f"  {headers[source]}")
        label = f'  "{candidate.label}"' if candidate.label else ""
        also = f"  (also {', '.join(candidate.also)})" if candidate.also else ""
        maybe = "".join(
            f"  (= {p.name}{' in the inventory' if p.in_inventory else ''}?)"
            for p in candidate.probable
        )
        lines.append(f"  {number:>3}. {candidate.name}{label}{also}{maybe}")
        if candidate.needs_host_key:
            lines.append(
                "       no host key in known_hosts yet: picking it also accepts its key, "
                f"fetched with ssh-keyscan and added to {known_hosts}"
            )
    if any(candidate.probable for candidate in candidates):
        lines.append(
            "  (= X?): shares an address with X, so probably the same machine; "
            "if picked, host keys decide and a duplicate is skipped"
        )
    return lines


def _ask_selection(ask: Callable[[str], str], tell: Callable[[str], None], count: int) -> list[int]:
    while True:
        try:
            text = ask("Hosts to test and add (e.g. 1,3-4, all, none) [none]: ")
        except EOFError:
            return []
        try:
            return parse_selection(text, count)
        except ValueError as exc:
            tell(f"{exc}; try again")


def fingerprint(key: str) -> str:
    """OpenSSH's ``SHA256:...`` fingerprint of a base64 public key."""
    digest = hashlib.sha256(base64.b64decode(key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def known_hosts_line(
    name: str,
    port: int,
    keytype: str,
    key: str,
    *,
    hashed: bool = False,
    salt: bytes | None = None,
) -> str:
    """A ``known_hosts`` line, hashed the way ssh does for ``HashKnownHosts yes``."""
    host = name if port == DEFAULT_SSH_PORT else f"[{name}]:{port}"
    if hashed:
        salt = salt if salt is not None else os.urandom(20)
        digest = hmac.new(salt, host.encode(), hashlib.sha1).digest()
        host = f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"
    return f"{host} {keytype} {key}"


def _append_known_hosts(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix = ""
    if path.is_file():
        existing = path.read_bytes()
        if existing and not existing.endswith(b"\n"):
            prefix = "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(prefix + "".join(f"{line}\n" for line in lines))


def _fetch_host_keys(candidate: SSHCandidate, keyscan: KeyScanner) -> list[tuple[str, str, str]]:
    """``(keytype, key, fingerprint)`` for each key the host presents."""
    resolved = candidate.resolved
    valid = []
    for keytype, key in keyscan(resolved.hostname, resolved.port):
        try:
            valid.append((keytype, key, fingerprint(key)))
        except (binascii.Error, ValueError):
            logger.debug("ssh-keyscan %s: unreadable key %r", resolved.hostname, key)
    return valid


def _record_host_keys(
    candidate: SSHCandidate,
    valid: Sequence[tuple[str, str, str]],
    known_hosts: Path,
    tell: Callable[[str], None],
) -> None:
    resolved = candidate.resolved
    _append_known_hosts(
        known_hosts,
        [
            known_hosts_line(resolved.key_name, resolved.port, keytype, key, hashed=resolved.hashed)
            for keytype, key, _ in valid
        ],
    )
    for keytype, _, print_ in valid:
        tell(f"{candidate.name}: added {keytype} {print_} to {known_hosts}")


def _is_duplicate(
    candidate: SSHCandidate,
    keys: frozenset[str],
    kept: Mapping[str, frozenset[str]],
    pending: Collection[str],
    tell: Callable[[str], None],
) -> bool:
    """Settle ``candidate``'s probable duplicates by host key; True to skip it.

    ``kept`` is each pick already accepted, by the keys it presented;
    ``pending`` the picks still to come, which do their own comparing.
    """
    for partner in candidate.probable:
        theirs = kept.get(partner.name, partner.keys)
        if not theirs:
            if partner.name not in pending:
                tell(
                    f"{candidate.name}: could not confirm whether it's the same machine as "
                    f"{partner.name} (no host key to compare); treating it as a different one"
                )
            continue
        if keys & theirs:
            if partner.in_inventory:
                tell(
                    f"{candidate.name}: same machine as {partner.name}, already in the "
                    "inventory (host keys match); skipped"
                )
                return True
            if partner.name in kept:
                tell(
                    f"{candidate.name}: same machine as {partner.name}, picked above "
                    "(host keys match); skipped"
                )
                return True
            tell(f"{candidate.name}: same machine as {partner.name} (host keys match)")
            continue
        tell(
            f"warning: {candidate.name} presents a different host key than "
            f"{partner.name}, which shares its address -- a different machine, or "
            f"{partner.name}'s key has changed; keeping them separate"
        )
    return False


def _confirm_picks(
    picked: Sequence[SSHCandidate],
    keyscan: KeyScanner,
    known_hosts: Path,
    tell: Callable[[str], None],
) -> list[SSHCandidate]:
    """The picks to test: keys fetched where missing, duplicates dropped.

    Only picked hosts are ever contacted (by ssh-keyscan), and a host's key
    is written to known_hosts only once it's certain to be tested.
    """
    kept: dict[str, frozenset[str]] = {}
    pending = {candidate.name for candidate in picked}
    ready = []
    for candidate in picked:
        pending.discard(candidate.name)
        fetched: list[tuple[str, str, str]] = []
        keys = candidate.keys
        if candidate.needs_host_key:
            fetched = _fetch_host_keys(candidate, keyscan)
            if not fetched:
                tell(f"{candidate.name}: could not fetch its host key with ssh-keyscan; not tested")
                continue
            keys = frozenset(key for _, key, _ in fetched)
        if _is_duplicate(candidate, keys, kept, pending, tell):
            continue
        if fetched:
            _record_host_keys(candidate, fetched, known_hosts, tell)
        kept[candidate.name] = keys
        ready.append(candidate)
    return ready


async def _probe(
    name: str,
    defaults: Mapping[str, Any],
    path: Path,
    evaluate: Evaluator,
    tell: Callable[[str], None],
) -> dict[str, Any] | None:
    """The entry that works for ``name`` -- as-is, else with become -- or None."""
    reason = "no result"
    for entry in ({"transport": "ssh"}, {"transport": "ssh", "become": True}):
        target = _target_from_entry(name, dict(entry), dict(defaults), path)
        results = await evaluate(PROBE_RELEVANCE, [target], timeout_s=SSH_PROBE_TIMEOUT_S)
        if results and all(r.error_kind is None for r in results):
            tell(f"{name}: works{' with become' if entry.get('become') else ''}")
            return entry
        failed = next((r for r in results if r.error_kind is not None), None)
        if failed is None or failed.error_kind == ERROR_KIND_TRANSPORT:
            reason = failed.error if failed is not None and failed.error else reason
            break
        reason = failed.error or failed.error_kind or reason
    tell(f"{name}: did not work ({reason})")
    return None


def run_ssh_discovery(
    path: Path,
    *,
    ask: Callable[[str], str],
    tell: Callable[[str], None],
    ssh_dir: Path | None = None,
    resolve: Resolver | None = None,
    keyscan: KeyScanner | None = None,
    evaluate: Evaluator | None = None,
    self_keys: Collection[str] | None = None,
    self_names: Collection[str] | None = None,
    browse: Browser | None = None,
    mdns_window_s: float = MDNS_WINDOW_S,
    lookup: AddressLookup | None = None,
    address_timeout_s: float = ADDRESS_TIMEOUT_S,
) -> list[str]:
    """List SSH hosts, test the ones picked, add the working ones to ``path``.

    ``ask`` shows a prompt and returns the reply (raising EOFError for no
    reply); ``tell`` shows a line. ``mdns_window_s`` of 0 skips mDNS.
    Returns the inventory names added.
    """
    ensure_writable(path)
    defaults, existing = read_existing(path)
    ssh_dir = ssh_dir if ssh_dir is not None else Path.home() / ".ssh"
    # Cached: each name is resolved ahead of the mDNS wait, then reused.
    resolve = functools.cache(resolve or default_resolver)
    lookups = _AddressLookups(lookup or default_lookup)
    inventory_ssh = [
        name for name, config in existing.items() if str(config.get("transport", "ssh")) == "ssh"
    ]

    with ThreadPoolExecutor(max_workers=1) as pool:
        browsing = (
            pool.submit(browse or default_browse, mdns_window_s) if mdns_window_s > 0 else None
        )
        aliases = read_config_aliases(ssh_dir / "config")
        known = _read_known_hosts(ssh_dir)
        # The slow part of the files (one ssh -G each) runs while the browse
        # is still collecting, so its window adds little or nothing.
        file_names = list(dict.fromkeys([*aliases, *(entry.host for entry in known)]))
        for name in [*file_names, *inventory_ssh]:
            resolve(name)
        # Address lookups start now too, for the same reason.
        lookups.start(
            [*file_names, *inventory_ssh, *(resolve(n).hostname for n in aliases + inventory_ssh)]
        )
        mdns = _browse_result(browsing)
    lookups.start(service.host for service in mdns)
    addresses = lookups.collect(address_timeout_s)

    candidates = gather_candidates(
        aliases=aliases,
        known=known,
        resolve=resolve,
        existing=existing,
        self_keys=default_self_keys() if self_keys is None else self_keys,
        self_names=default_self_names() if self_names is None else self_names,
        mdns=mdns,
        addresses=addresses,
    )
    if not candidates:
        tell(f"no SSH hosts found that aren't already in {path}")
        return []

    for line in describe(candidates, ssh_dir):
        tell(line)
    picked = [candidates[i] for i in _ask_selection(ask, tell, len(candidates))]
    if not picked:
        return []

    ready = _confirm_picks(picked, keyscan or default_keyscan, ssh_dir / "known_hosts", tell)

    async def probe_all() -> dict[str, dict[str, Any]]:
        entries = await asyncio.gather(
            *(
                _probe(c.name, defaults, path, evaluate or evaluate_client_relevance, tell)
                for c in ready
            )
        )
        return {c.name: entry for c, entry in zip(ready, entries, strict=True) if entry is not None}

    found = asyncio.run(probe_all()) if ready else {}
    return write_discovered(path, found)


__all__ = [
    "GIT_HOSTS",
    "KnownHost",
    "MdnsService",
    "Probable",
    "Resolved",
    "SSHCandidate",
    "default_browse",
    "default_lookup",
    "describe",
    "fingerprint",
    "gather_candidates",
    "known_hosts_line",
    "parse_avahi_browse",
    "parse_dns_sd_browse",
    "parse_dns_sd_lookup",
    "parse_known_hosts",
    "parse_selection",
    "parse_ssh_g",
    "read_config_aliases",
    "resolve_addresses",
    "run_ssh_discovery",
]
