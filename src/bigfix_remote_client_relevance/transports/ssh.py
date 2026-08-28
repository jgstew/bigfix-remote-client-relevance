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
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
    parse_qna_output,
)
from bigfix_remote_client_relevance.transports.local import (
    QNA_EVAL_FLAGS,
    classify_qna_outcome,
    normalize_stdin_payload,
)

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "ssh"

class SSHConnectionError(Exception):
    """Connecting to or authenticating with the target failed."""


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


async def _connect(host: str, user: str | None, key: str | None, port: int) -> SSHRunner:
    import asyncssh

    try:
        connection = await asyncssh.connect(
            host,
            port=port,
            username=user,
            client_keys=[key] if key else None,
            known_hosts=None,
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
    ) -> None:
        self.host = host
        self._become = become
        # `target` and `platform` both name a bootstrap spec; `target` wins.
        self._target = target or platform
        self._state_dir = state_dir
        self._recheck_prereqs = recheck_prereqs
        self._connection_factory = connection_factory or (
            lambda: _connect(host, user, key, port)
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
                        error=f"no qna binary found on {self.host}; "
                        "pass qna_path or a qna version to provision one",
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

    async def _resolve_spec(self, runner: SSHRunner, timeout_s: float) -> TargetSpec:
        if self._spec is not None:
            return self._spec
        if self._target is not None:
            self._spec = spec_for(self._target)
            return self._spec

        stdout, _stderr, _code = await runner.run(
            "uname -s; . /etc/os-release 2>/dev/null && echo \"$ID $ID_LIKE\"",
            timeout=timeout_s,
        )
        target = classify_uname(stdout)
        logger.debug("%s classified as %s", self.host, target)
        self._spec = spec_for(target)
        return self._spec

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

        stdout, _stderr, _code = await runner.run(probe, timeout=timeout_s)
        found = stdout.strip().splitlines()
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
