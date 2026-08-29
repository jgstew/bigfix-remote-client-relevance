"""Put a pinned qna version onto a target, whatever kind of target it is.

The sequence is identical over SSH, on the local machine, and inside a
container, so it lives here once and each transport supplies a
:class:`CommandRunner`::

    marker check -> prereq check -> push -> extract -> rename -> marker write

Two ordering choices are deliberate:

* The prereq check runs **before** the push. Transferring an artifact to a
  target that cannot unpack it wastes the slowest step in the sequence.
* Extraction lands in a staging directory that is renamed into place only on
  success, so an interrupted run never leaves a partial tree that the marker
  check would accept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
from collections.abc import Awaitable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol

import platformdirs

from bigfix_remote_client_relevance.bootstrap.targets import (
    MARKER_FILENAME,
    TargetSpec,
)
from bigfix_remote_client_relevance.exceptions import BigFixRelevanceError
from bigfix_remote_client_relevance.results import ResolvedQna

logger = logging.getLogger(__name__)

APP_NAME = "bigfix_remote_client_relevance"

# Emitted by the prereq probe so it is recognizable in a command transcript.
PREREQ_PROBE_MARKER = "prereq-probe"

RunResult = tuple[str, str, int]
"""``(stdout, stderr, exit_status)``."""


class BootstrapFailure(BigFixRelevanceError):
    """Provisioning failed; maps to ``error_kind="bootstrap"``."""


class CommandRunner(Protocol):
    """Runs commands and delivers files somewhere — a host, a container, here."""

    async def run(
        self, command: str, *, input: str | None = None, timeout: float | None = None
    ) -> RunResult: ...

    def put_file(self, local: Path, remote: str) -> Awaitable[None]: ...


def join(spec: TargetSpec, *parts: str) -> str:
    """Join path segments using the target's own separator."""
    if spec.family == "windows":
        return str(PureWindowsPath(*parts))
    return str(PurePosixPath(*parts))


def version_dir(spec: TargetSpec, version: str) -> str:
    return join(spec, spec.cache_root, version)


def qna_path_for(spec: TargetSpec, version: str) -> str:
    return join(spec, version_dir(spec, version), spec.qna_relative_path)


def mkdir_command(spec: TargetSpec, path: str) -> str:
    if spec.family == "windows":
        return f"New-Item -ItemType Directory -Force -Path '{path}' | Out-Null"
    return f"mkdir -p {shlex.quote(path)}"


def rename_command(spec: TargetSpec, source: str, destination: str) -> str:
    if spec.family == "windows":
        return (
            f"Remove-Item -Recurse -Force '{destination}' -ErrorAction SilentlyContinue; "
            f"Move-Item -Force '{source}' '{destination}'"
        )
    return (
        f"rm -rf {shlex.quote(destination)} && mv {shlex.quote(source)} {shlex.quote(destination)}"
    )


def marker_write_command(spec: TargetSpec, marker: str, version: str) -> str:
    if spec.family == "windows":
        return f"Set-Content -Path '{marker}' -Value '{version}'"
    return f"printf '%s' {shlex.quote(version)} > {shlex.quote(marker)}"


def marker_test_command(spec: TargetSpec, marker: str) -> str:
    if spec.family == "windows":
        return f"if (Test-Path '{marker}') {{ exit 0 }} else {{ exit 1 }}"
    return f"test -f {shlex.quote(marker)}"


def prereq_probe_command(spec: TargetSpec) -> str:
    """A command that echoes back which of the needed tools are present."""
    tools = [p.tool for p in spec.prereqs]
    if spec.family == "windows":
        checks = " ".join(
            f"if (Get-Command '{t}' -ErrorAction SilentlyContinue) {{ Write-Output '{t}' }};"
            for t in tools
        )
        return f"# {PREREQ_PROBE_MARKER}\n{checks}"
    checks = " ".join(
        f"command -v {shlex.quote(t)} >/dev/null 2>&1 && printf '%s ' {shlex.quote(t)};"
        for t in tools
    )
    return f"{checks} : {PREREQ_PROBE_MARKER}"


def _satisfied_by_alternatives(spec: TargetSpec, present: set[str]) -> bool:
    """deb targets extract with either dpkg-deb or the ar + tar fallback."""
    if spec.release_platform in {"ubuntu", "debian", "raspbian"}:
        return "dpkg-deb" in present or {"ar", "tar"} <= present
    return False


def _prereq_state_file(host_label: str, spec: TargetSpec, state_dir: Path | None) -> Path:
    root = state_dir or Path(platformdirs.user_state_dir(APP_NAME))
    # Host labels and image names contain characters awkward in filenames.
    digest = hashlib.sha256(f"{host_label}:{spec.name}".encode()).hexdigest()[:16]
    return root / "prereqs" / f"{digest}.json"


async def check_prereqs(
    runner: CommandRunner,
    spec: TargetSpec,
    *,
    host_label: str,
    state_dir: Path | None = None,
    recheck: bool = False,
    timeout_s: float = 30.0,
) -> None:
    """Fail unless the target has the tools to unpack its artifact.

    The answer is cached per (target, platform) in the platform *state*
    directory — distinct from the artifact cache, which is safe to wipe — so
    this costs one probe per target rather than one per evaluation.
    """
    state_file = _prereq_state_file(host_label, spec, state_dir)
    if not recheck and state_file.is_file():
        try:
            if json.loads(state_file.read_text(encoding="utf-8")).get("ok"):
                return
        except (OSError, ValueError):
            pass  # unreadable cache just means re-probing

    stdout, stderr, code = await runner.run(prereq_probe_command(spec), timeout=timeout_s)
    present = set(stdout.split())
    missing = [p for p in spec.prereqs if p.tool not in present]

    if missing and not _satisfied_by_alternatives(spec, present):
        if stderr.strip():
            # A probe that could not run at all reports every tool as missing,
            # which reads as "install these" when the real fault is the shell.
            logger.debug(
                "prereq probe on %s failed (exit %d): %s", host_label, code, stderr.strip()
            )
        names = ", ".join(p.tool for p in missing)
        hints = "; ".join(f"{p.tool}: {p.install_hint}" for p in missing)
        raise BootstrapFailure(
            f"{host_label} is missing extraction tools ({names}) needed to unpack the "
            f"{spec.name} qna artifact - {hints}"
        )

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"ok": True, "tools": sorted(present)}), encoding="utf-8")


async def _run_checked(
    runner: CommandRunner, command: str, timeout_s: float, what: str, host_label: str
) -> None:
    stdout, stderr, code = await runner.run(command, timeout=timeout_s)
    if code != 0:
        detail = (stderr or stdout).strip() or f"exit {code}"
        raise BootstrapFailure(f"could not {what} on {host_label}: {detail}")


async def provision_qna(
    runner: CommandRunner,
    spec: TargetSpec,
    qna: ResolvedQna,
    *,
    host_label: str,
    state_dir: Path | None = None,
    recheck_prereqs: bool = False,
    timeout_s: float = 300.0,
) -> str:
    """Ensure ``qna.version`` is extracted on the target; return its qna path.

    The extracted tree is deliberately left in place afterwards, so a given
    version crosses to a given target once ever rather than once per run. An OS
    temp cleanup may remove it, which is harmless: the marker check simply
    re-provisions from the controller cache.
    """
    target_dir = version_dir(spec, qna.version)
    marker = join(spec, target_dir, MARKER_FILENAME)
    installed_qna = qna_path_for(spec, qna.version)

    _stdout, _stderr, code = await runner.run(marker_test_command(spec, marker), timeout=timeout_s)
    if code == 0:
        logger.debug("%s already has qna %s", host_label, qna.version)
        return installed_qna

    await check_prereqs(
        runner,
        spec,
        host_label=host_label,
        state_dir=state_dir,
        recheck=recheck_prereqs,
        timeout_s=timeout_s,
    )

    if qna.artifact_path is None:
        raise BootstrapFailure(f"no cached artifact for qna {qna.version}")

    staging = f"{target_dir}.partial"
    remote_artifact = join(spec, staging, qna.artifact_path.name)

    await _run_checked(
        runner, mkdir_command(spec, staging), timeout_s, "prepare staging directory", host_label
    )

    logger.info("delivering qna %s to %s", qna.version, host_label)
    try:
        await runner.put_file(qna.artifact_path, remote_artifact)
    except OSError as exc:
        raise BootstrapFailure(f"could not deliver artifact to {host_label}: {exc}") from exc

    for command in spec.extract_commands(remote_artifact, staging):
        await _run_checked(runner, command, timeout_s, "extract qna", host_label)

    await _run_checked(
        runner, rename_command(spec, staging, target_dir), timeout_s, "install qna", host_label
    )
    await _run_checked(
        runner,
        marker_write_command(spec, marker, qna.version),
        timeout_s,
        "mark qna complete",
        host_label,
    )
    logger.info("qna %s ready at %s on %s", qna.version, installed_qna, host_label)
    return installed_qna


__all__ = [
    "APP_NAME",
    "PREREQ_PROBE_MARKER",
    "BootstrapFailure",
    "CommandRunner",
    "RunResult",
    "check_prereqs",
    "join",
    "marker_test_command",
    "prereq_probe_command",
    "provision_qna",
    "qna_path_for",
    "version_dir",
]
