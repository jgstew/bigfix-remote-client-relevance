"""Evaluate client relevance on a remote host over SSH.

The primary remote transport: SSH is built into macOS and Windows 10/11 and
Server 2019+, so real endpoints need no extra agent and no BigFix action
round-trip.

asyncssh is reached through the small :class:`SSHRunner` seam rather than
directly, which keeps every phase below testable without a live host.

Provisioning a pinned version runs as::

    marker check -> prereq check -> push -> extract -> marker write -> run

The prereq check comes before the push deliberately: transferring an artifact
to a host that cannot unpack it wastes the slowest step in the sequence.
"""

from __future__ import annotations

import base64
import logging
import shlex
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from bigfix_remote_client_relevance.bootstrap.provision import (
    PREREQ_PROBE_MARKER,
    BootstrapFailure,
    RunResult,
    provision_qna,
)
from bigfix_remote_client_relevance.bootstrap.targets import (
    TargetSpec,
    classify_uname,
    spec_for,
)
from bigfix_remote_client_relevance.qna_paths import default_candidates
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
    parse_qna_output,
)
from bigfix_remote_client_relevance.transports.local import (
    QNA_EVAL_FLAGS,
    classify_qna_outcome,
    normalize_stdin_payload,
    sudo_privilege_problem,
)

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "ssh"


class SSHConnectionError(Exception):
    """Connecting to or authenticating with the target failed."""


def powershell_command(source: str) -> str:
    """Wrap PowerShell so it survives whatever login shell the host uses.

    Windows OpenSSH hands commands to the shell named by the ``DefaultShell``
    registry value, which is ``cmd.exe`` unless someone changed it — and every
    Windows command this package builds is PowerShell. Rather than requiring
    that registry edit, invoke ``powershell.exe`` explicitly.

    ``-EncodedCommand`` rather than ``-Command`` because the commands embed
    single quotes and paths with spaces *and* parentheses (``C:/Program Files
    (x86)/...``); ``(`` and ``&`` are cmd metacharacters, so an unencoded
    command fails with ``'C:/Program was unexpected at this time.`` Base64
    leaves cmd nothing to misparse.

    ``$ProgressPreference`` is silenced because PowerShell otherwise writes
    CLIXML progress records to stderr, which the qna outcome classifier reads.

    The encoding inflates by roughly 2.7x against cmd's ~8191 character limit,
    so sources beyond ~3000 characters would truncate. Everything built here is
    short, and the client relevance itself travels on stdin, not the command line.
    """
    payload = f"$ProgressPreference='SilentlyContinue'; {source}"
    encoded = base64.b64encode(payload.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"


class _PowerShellRunner:
    """Runs every command through powershell.exe, leaving the login shell alone.

    Wrapping at the runner rather than at each call site is what makes this
    reach provisioning too: :func:`provision_qna` is handed a runner and calls
    ``run`` itself, so a wrapper applied inside the transport would miss it.
    """

    def __init__(self, inner: SSHRunner) -> None:
        self._inner = inner

    async def run(
        self, command: str, *, input: str | None = None, timeout: float | None = None
    ) -> RunResult:
        logger.debug("windows command: %s", command)
        return await self._inner.run(powershell_command(command), input=input, timeout=timeout)

    async def put_file(self, local: Path, remote: str) -> None:
        # SFTP is shell-independent, so this needs no wrapping.
        await self._inner.put_file(local, remote)

    async def close(self) -> None:
        await self._inner.close()


class SSHRunner(Protocol):
    """The slice of an SSH connection this transport actually uses."""

    async def run(
        self, command: str, *, input: str | None = None, timeout: float | None = None
    ) -> RunResult: ...

    async def put_file(self, local: Path, remote: str) -> None: ...

    async def close(self) -> None: ...


class _AsyncsshRunner:
    """Adapter from asyncssh onto :class:`SSHRunner`."""

    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def run(
        self, command: str, *, input: str | None = None, timeout: float | None = None
    ) -> RunResult:
        result = await self._connection.run(  # type: ignore[attr-defined]
            command, input=input, timeout=timeout, check=False
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        return stdout, stderr, int(result.exit_status or 0)

    async def put_file(self, local: Path, remote: str) -> None:
        async with self._connection.start_sftp_client() as sftp:  # type: ignore[attr-defined]
            await sftp.put(str(local), remote)

    async def close(self) -> None:
        self._connection.close()  # type: ignore[attr-defined]
        await self._connection.wait_closed()  # type: ignore[attr-defined]


def connect_kwargs(
    user: str | None,
    key: str | None,
    port: int,
    *,
    verify_host_key: bool = True,
) -> dict[str, object]:
    """Build asyncssh.connect options, omitting anything not set.

    Two asyncssh quirks shape this:

    * ``username=None`` is not the same as omitting it — option construction
      raises a bare ``TypeError`` before it ever tries to connect, turning a
      mistyped hostname into an opaque internal error instead of a DNS failure.
    * ``known_hosts=None`` means *accept any host key*, disabling verification
      entirely. It is only passed when the caller explicitly opts out;
      otherwise it is omitted so asyncssh verifies against ``~/.ssh/known_hosts``
      the way the ssh CLI does.

    Everything unset is left out so asyncssh applies its own defaults
    (``~/.ssh/config``, the agent, the usual key names).
    """
    kwargs: dict[str, object] = {"port": port}
    if user:
        kwargs["username"] = user
    if key:
        kwargs["client_keys"] = [key]
    if not verify_host_key:
        kwargs["known_hosts"] = None
    return kwargs


async def _connect(
    host: str,
    user: str | None,
    key: str | None,
    port: int,
    *,
    verify_host_key: bool = True,
) -> SSHRunner:
    import asyncssh

    if not verify_host_key:
        logger.warning(
            "host key verification is disabled for %s; the connection is not "
            "protected against interception",
            host,
        )
    try:
        connection = await asyncssh.connect(
            host, **connect_kwargs(user, key, port, verify_host_key=verify_host_key)
        )
    except (OSError, asyncssh.Error) as exc:
        raise SSHConnectionError(f"could not connect to {host}: {exc}") from exc
    return _AsyncsshRunner(connection)


class TransportSSH:
    """Runs qna on a remote host, provisioning a pinned version when asked."""

    def __init__(
        self,
        host: str,
        *,
        user: str | None = None,
        key: str | None = None,
        port: int = 22,
        become: bool = False,
        platform: str | None = None,
        target: str | None = None,
        connection_factory: Callable[[], Awaitable[SSHRunner]] | None = None,
        state_dir: Path | None = None,
        recheck_prereqs: bool = False,
        verify_host_key: bool = True,
    ) -> None:
        """
        Args:
            verify_host_key: Check the target against ``~/.ssh/known_hosts``,
                as the ssh CLI does. Turn it off only for throwaway lab
                endpoints whose keys change; doing so removes the connection's
                protection against interception.
        """
        self.host = host
        self._become = become
        # `target` and `platform` both name a bootstrap spec; `target` wins.
        self._target = target or platform
        self._state_dir = state_dir
        self._recheck_prereqs = recheck_prereqs
        self._connection_factory = connection_factory or (
            lambda: _connect(host, user, key, port, verify_host_key=verify_host_key)
        )
        self._runner: SSHRunner | None = None
        self._spec: TargetSpec | None = None
        # -showtypes support is per remote qna binary; probe at most once.
        self._showtypes_supported: dict[str, bool] = {}

    # -- connection ---------------------------------------------------------

    async def _connection(self) -> SSHRunner:
        """One connection per host, multiplexed across evaluations."""
        if self._runner is None:
            self._runner = await self._connection_factory()
        return self._runner

    async def aclose(self) -> None:
        if self._runner is not None:
            await self._runner.close()
            self._runner = None

    # -- public API ---------------------------------------------------------

    async def evaluate_client_relevance(
        self,
        client_relevance: str,
        *,
        qna_path: str | None = None,
        qna: ResolvedQna | None = None,
        timeout_s: float = 30.0,
    ) -> ClientRelevanceResult:
        started = time.monotonic()

        def _result(**overrides: object) -> ClientRelevanceResult:
            base: dict[str, object] = {
                "host": self.host,
                "transport": TRANSPORT_NAME,
                "client_relevance": client_relevance,
                "qna_version": qna.version if qna else None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
            base.update(overrides)
            return ClientRelevanceResult(**base)  # type: ignore[arg-type]

        try:
            runner = await self._connection()
            spec = await self._resolve_spec(runner, timeout_s)

            if spec.family == "windows":
                # The probe above is POSIX on purpose; everything after it is
                # PowerShell, so it goes through powershell.exe explicitly.
                runner = _PowerShellRunner(runner)
                if self._become:
                    logger.warning(
                        "--become has no effect on %s: elevation over SSH is a "
                        "Windows-side configuration, not a sudo call",
                        self.host,
                    )

            if qna is not None:
                qna_path = await provision_qna(
                    runner,
                    spec,
                    qna,
                    host_label=self.host,
                    state_dir=self._state_dir,
                    recheck_prereqs=self._recheck_prereqs,
                    timeout_s=timeout_s,
                )

            if qna_path is None:
                qna_path = await self._discover_qna(runner, spec, timeout_s)
                if qna_path is None:
                    return _result(
                        error=(
                            f"no qna binary found on {self.host}; "
                            "pass qna_path or a qna version to provision one"
                        ),
                        error_kind=ERROR_KIND_BOOTSTRAP,
                    )

            stdout, stderr, exit_code, used_types = await self._run_qna(
                runner, spec, qna_path, client_relevance, timeout_s
            )
        except BootstrapFailure as exc:
            return _result(error=str(exc), error_kind=ERROR_KIND_BOOTSTRAP, qna_path=qna_path or "")
        except (SSHConnectionError, OSError, TimeoutError) as exc:
            return _result(error=f"{self.host}: {exc}", error_kind=ERROR_KIND_TRANSPORT)
        except Exception as exc:
            logger.exception("unexpected failure evaluating on %s", self.host)
            return _result(error=f"{self.host}: {exc}", error_kind=ERROR_KIND_TRANSPORT)

        parsed = parse_qna_output(stdout)
        error, error_kind = classify_qna_outcome(parsed, exit_code, stderr)

        # Both gates are structural, matching TransportLocal: ERROR_KIND_QNA
        # means a relevance `E:` line -- proof elevation worked -- is never
        # overridden, and `self._become` means qna's own stderr can never be
        # mistaken for sudo's.
        if self._become and error_kind == ERROR_KIND_QNA:
            privilege = sudo_privilege_problem(stderr)
            if privilege is not None:
                error, error_kind = privilege, ERROR_KIND_BOOTSTRAP

        return _result(
            answers=parsed.answers,
            answer_types=parsed.answer_types if used_types else [],
            error=error,
            error_kind=error_kind,
            raw_qna_output=stdout,
            qna_path=qna_path,
            qna_time=parsed.qna_time,
            exit_code=exit_code,
        )

    # -- platform -----------------------------------------------------------

    async def resolve_platform(self, *, timeout_s: float = 30.0) -> str:
        """The :data:`KNOWN_TARGETS` key for this host.

        An explicit ``target``/``platform`` wins; otherwise the host is
        probed once (the same ``uname``/``os-release`` probe
        :meth:`_resolve_spec` already runs before provisioning) and the
        answer cached on this instance. Exposing it here lets
        ``orchestrate.py`` pick the right qna artifact *before* resolving
        one -- the same probe-before-resolve step it already does for
        :class:`~...transports.container.TransportContainer` -- instead of
        guessing a platform and finding out the truth only once connected.
        """
        runner = await self._connection()
        spec = await self._resolve_spec(runner, timeout_s)
        return spec.name

    async def _classify_platform(self, runner: SSHRunner, timeout_s: float) -> str:
        """Run the ``uname``/``os-release`` probe and classify the result.

        Shared by :meth:`_resolve_spec` (which trusts an explicit target) and
        :meth:`reprobe_platform` (which deliberately does not).
        """
        stdout, _stderr, _code = await runner.run(
            'uname -s; . /etc/os-release 2>/dev/null && echo "$ID $ID_LIKE"',
            timeout=timeout_s,
        )
        target = classify_uname(stdout)
        logger.debug("%s classified as %s", self.host, target)
        return target

    async def _resolve_spec(self, runner: SSHRunner, timeout_s: float) -> TargetSpec:
        if self._spec is not None:
            return self._spec
        if self._target is not None:
            self._spec = spec_for(self._target)
            return self._spec

        self._spec = spec_for(await self._classify_platform(runner, timeout_s))
        return self._spec

    async def reprobe_platform(self, *, timeout_s: float = 30.0) -> str:
        """Classify what this host actually is, ignoring any explicit
        ``target``/``platform``.

        For detecting and correcting a wrong ``platform`` in an inventory
        file: an explicit value is normally trusted outright (see
        :meth:`_resolve_spec`), which is exactly the problem when it's wrong
        -- the artifact resolved from it never matches the real host, and
        nothing else ever re-checks. This bypasses that trust deliberately,
        reusing the live connection rather than opening a second one.
        """
        runner = await self._connection()
        return await self._classify_platform(runner, timeout_s)

    # -- discovery ----------------------------------------------------------

    async def _discover_qna(
        self, runner: SSHRunner, spec: TargetSpec, timeout_s: float
    ) -> str | None:
        if spec.family == "windows":
            candidates = default_candidates("win32")
            probe = "; ".join(
                f"if (Test-Path '{c}') {{ Write-Output '{c}'; exit 0 }}" for c in candidates
            )
        else:
            platform_key = "darwin" if spec.name == "macos" else "linux"
            candidates = default_candidates(platform_key)
            tests = " ".join(
                f'if [ -x {shlex.quote(c)} ]; then printf "%s\\n" {shlex.quote(c)}; exit 0; fi;'
                for c in candidates
            )
            probe = f"{tests} command -v qna 2>/dev/null || true"

        stdout, stderr, code = await runner.run(probe, timeout=timeout_s)
        found = stdout.strip().splitlines()
        if not found and stderr.strip():
            # Without this a shell-level failure (the wrong interpreter, a
            # missing binary) surfaces only as "no qna binary found".
            logger.debug(
                "qna discovery on %s produced no candidates (exit %d): %s",
                self.host,
                code,
                stderr.strip(),
            )
        return found[0].strip() if found and found[0].strip() else None

    # -- evaluation ---------------------------------------------------------

    async def _run_qna(
        self,
        runner: SSHRunner,
        spec: TargetSpec,
        qna_path: str,
        client_relevance: str,
        timeout_s: float,
    ) -> tuple[str, str, int, bool]:
        payload = normalize_stdin_payload(client_relevance)
        supports_types = self._showtypes_supported.get(qna_path, True)

        stdout, stderr, code = await runner.run(
            self._eval_command(spec, qna_path, with_types=supports_types),
            input=payload,
            timeout=timeout_s,
        )

        # 9.2/9.5-era qna predates -showtypes. Degrade rather than fail, and
        # remember the answer so the retry is paid once per binary.
        if supports_types and code != 0 and "showtypes" in stderr.lower():
            logger.warning(
                "qna at %s does not support -showtypes; falling back to -t "
                "(answer types will be unavailable)",
                qna_path,
            )
            self._showtypes_supported[qna_path] = False
            stdout, stderr, code = await runner.run(
                self._eval_command(spec, qna_path, with_types=False),
                input=payload,
                timeout=timeout_s,
            )
            return stdout, stderr, code, False

        return stdout, stderr, code, supports_types

    def _eval_command(self, spec: TargetSpec, qna_path: str, *, with_types: bool) -> str:
        # API boundary: qna's CLI vocabulary uses "relevance"; internal name
        # stays `client_relevance`.
        flags: Sequence[str] = QNA_EVAL_FLAGS if with_types else ("-t",)
        if spec.family == "windows":
            command = f"& '{qna_path}' {' '.join(flags)}"
        else:
            command = " ".join([shlex.quote(qna_path), *flags])
            if self._become:
                command = f"sudo -n {command}"
        return command


__all__ = ["PREREQ_PROBE_MARKER", "SSHConnectionError", "SSHRunner", "TransportSSH"]
