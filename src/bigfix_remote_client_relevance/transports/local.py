"""Evaluate client relevance against a qna binary on this machine.

Ported from ``jgstew/EvaluateRelevance``'s ``evaluate_relevance_raw_stdin()``,
with the encoding pinned to UTF-8, a timeout added, and failures returned as
results rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from bigfix_remote_client_relevance.bootstrap.provision import (
    BootstrapFailure,
    RunResult,
    provision_qna,
)
from bigfix_remote_client_relevance.bootstrap.targets import spec_for
from bigfix_remote_client_relevance.qna_paths import find_qna_path
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ParsedQnaOutput,
    ResolvedQna,
    parse_qna_output,
)

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "local"

# -t emits T: timing, -showtypes emits I: result types.
QNA_EVAL_FLAGS: tuple[str, ...] = ("-t", "-showtypes")


def normalize_stdin_payload(client_relevance: str) -> str:
    """Prepare a client-relevance expression for qna's stdin.

    qna's file mode requires a ``Q: `` prefix and its stdin mode rejects one, so
    the prefix is stripped here. A trailing newline terminates the question.
    """
    # API boundary: qna's CLI vocabulary uses "relevance"; internal name stays
    # `client_relevance`.
    payload = client_relevance.removeprefix("Q: ")
    if not payload.endswith("\n"):
        payload += "\n"
    return payload


class LocalRunner:
    """Runs provisioning commands on this machine.

    Satisfies the same :class:`~...bootstrap.provision.CommandRunner` protocol
    the SSH and container transports use, so all three share one provisioning
    sequence. "Delivering" a file here is just a copy.
    """

    async def run(
        self, command: str, *, input: str | None = None, timeout: float | None = None
    ) -> RunResult:
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = input.encode("utf-8") if input else None
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            process.returncode or 0,
        )

    async def put_file(self, local: Path, remote: str) -> None:
        await asyncio.to_thread(shutil.copy, local, remote)


class TransportLocal:
    """Runs qna as a subprocess on the controller itself.

    Used for tests and for a fast syntax check before paying for an SSH or
    container round-trip.

    Once ``become`` proves unable to elevate (no passwordless sudo, or no
    ``sudo`` binary at all), that verdict is cached for the life of this
    instance rather than retried on every call -- see ``_sudo_broken``. There
    is no reset method: this instance has no connection or other state worth
    preserving, so after fixing sudo config the way to retry is to construct
    a new ``TransportLocal``. The CLI already does this on every invocation;
    a long-lived embedder (e.g. a future MCP server) wanting a retry should
    do the same.
    """

    def __init__(
        self,
        qna_path: str | None = None,
        *,
        candidates: Sequence[str] | None = None,
        become: bool = False,
        require_root_on_macos: bool = True,
        target: str | None = None,
        state_dir: Path | None = None,
        recheck_prereqs: bool = False,
    ) -> None:
        self._qna_path = qna_path
        self._candidates = candidates
        self._become = become
        self._require_root_on_macos = require_root_on_macos
        self._target = target
        self._state_dir = state_dir
        self._recheck_prereqs = recheck_prereqs
        # Set once a `become` run proves sudo cannot elevate; see class docstring.
        self._sudo_broken: str | None = None

    def _local_target(self) -> str:
        if self._target is not None:
            return self._target
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("win"):
            return "windows"
        return "ubuntu"

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
                "host": TRANSPORT_NAME,
                "transport": TRANSPORT_NAME,
                "client_relevance": client_relevance,
                "qna_version": qna.version if qna else None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
            base.update(overrides)
            return ClientRelevanceResult(**base)  # type: ignore[arg-type]

        root_problem = self._macos_root_problem()
        if root_problem is not None:
            return _result(error=root_problem, error_kind=ERROR_KIND_BOOTSTRAP)

        if self._sudo_broken is not None:
            return _result(error=self._sudo_broken, error_kind=ERROR_KIND_BOOTSTRAP)

        if qna is not None:
            try:
                qna_path = await provision_qna(
                    LocalRunner(),
                    spec_for(self._local_target()),
                    qna,
                    host_label="local",
                    state_dir=self._state_dir,
                    recheck_prereqs=self._recheck_prereqs,
                    timeout_s=max(timeout_s, 300.0),
                )
            except BootstrapFailure as exc:
                return _result(error=str(exc), error_kind=ERROR_KIND_BOOTSTRAP)

        resolved_path = qna_path or self._qna_path or find_qna_path(self._candidates)
        if resolved_path is None:
            return _result(
                error="no qna binary found; pass qna_path or install the BES client",
                error_kind=ERROR_KIND_BOOTSTRAP,
            )

        argv = self._eval_argv(resolved_path)
        logger.debug("running %s", " ".join(argv))

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # Which binary failed to exec matters: blaming the qna path for a
            # missing sudo would send the user looking in the wrong place.
            detail = (
                f"could not run sudo for become: {exc}"
                if argv[0] == "sudo"
                else f"could not start qna at {resolved_path}: {exc}"
            )
            if argv[0] == "sudo":
                self._sudo_broken = detail
            return _result(
                qna_path=resolved_path,
                error=detail,
                error_kind=ERROR_KIND_BOOTSTRAP,
            )

        payload = normalize_stdin_payload(client_relevance).encode("utf-8")
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(payload), timeout=timeout_s
            )
        except TimeoutError:
            await _terminate(process)
            logger.warning("qna timed out after %.1fs; process killed", timeout_s)
            return _result(
                qna_path=resolved_path,
                error=f"qna timed out after {timeout_s}s",
                error_kind=ERROR_KIND_TRANSPORT,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = process.returncode or 0
        parsed = parse_qna_output(stdout)
        error, error_kind = classify_qna_outcome(parsed, exit_code, stderr)

        # Both gates are structural. ERROR_KIND_QNA means a relevance `E:` line
        # -- proof that elevation worked -- can never be overridden, and a clean
        # run is never touched; `self._become` means qna's own stderr can never
        # be mistaken for sudo's.
        if self._become and error_kind == ERROR_KIND_QNA:
            privilege = sudo_privilege_problem(stderr)
            if privilege is not None:
                error, error_kind = privilege, ERROR_KIND_BOOTSTRAP
                self._sudo_broken = privilege

        return _result(
            answers=parsed.answers,
            answer_types=parsed.answer_types,
            error=error,
            error_kind=error_kind,
            raw_qna_output=stdout,
            qna_path=resolved_path,
            qna_time=parsed.qna_time,
            exit_code=exit_code,
        )

    def _eval_argv(self, qna_path: str) -> list[str]:
        """Build the qna command line, elevating it when ``become`` is set.

        The mirror of :meth:`~...transports.ssh.TransportSSH._eval_command`, and
        the one place the sudo prefix lives. No quoting is needed: this is an
        argv list for ``create_subprocess_exec``, not a shell string.
        """
        # API boundary: qna's CLI vocabulary uses "relevance"; internal name
        # stays `client_relevance`.
        argv = [qna_path, *QNA_EVAL_FLAGS]
        if not self._become:
            return argv

        if sys.platform.startswith("win"):
            logger.warning(
                "become has no effect on Windows: there is no sudo, and elevation "
                "is a per-process UAC decision rather than a command prefix"
            )
            return argv

        if os.geteuid() == 0:
            # Already root: sudo would be a redundant extra exec, and this
            # sidesteps needing to reason about how sudo's own PAM stack
            # treats a root-owned parent, which varies by platform.
            return argv

        # -n never prompts: sudo either has a cached or NOPASSWD credential or
        # it fails immediately. It reads passwords from /dev/tty, never stdin,
        # so the relevance expression still reaches qna untouched.
        return ["sudo", "-n", *argv]

    def _macos_root_problem(self) -> str | None:
        """Refuse to run qna as a non-root user on macOS.

        Observed against BESAgent 11.x on macOS 15: a non-root qna aborts with
        an uncaught ``FileIOError`` from libc++ before answering anything — even
        ``TRUE`` — so there is no partial-answer mode worth preserving. Failing
        here turns an opaque crash dump into an actionable message. Waivable via
        ``require_root_on_macos=False`` for setups where it does work.
        """
        if self._become:
            # sudo makes qna root whatever this process's euid is, so the
            # pre-flight refusal would be a false negative — and so would the
            # waiver's "running as non-root" warning below. A sudo that cannot
            # actually elevate surfaces after the run, not as a guess before it.
            return None

        if sys.platform != "darwin" or os.geteuid() == 0:
            return None

        if not self._require_root_on_macos:
            logger.warning(
                "running qna as non-root on macOS; it may abort instead of answering "
                "(require_root_on_macos=False was set)"
            )
            return None

        return (
            "qna requires root on macOS and aborts without it; rerun with sudo, "
            "or pass require_root_on_macos=False to attempt it anyway"
        )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill a process and reap it so no orphan outlives the timeout.

    Under ``become`` this kills sudo, and the root-owned qna child survives —
    an unprivileged parent cannot signal it. The same hazard already exists
    over SSH; fixing it needs a ``sudo -n kill`` or a pty, which is out of
    proportion to a qna run that has already hit its timeout.
    """
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - raced with natural exit
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:  # pragma: no cover - unkillable process
        logger.error("qna process %s did not die after kill", process.pid)


def classify_qna_outcome(
    parsed: ParsedQnaOutput, exit_code: int, stderr: str
) -> tuple[str | None, str | None]:
    """Map a qna run onto ``(error, error_kind)``.

    Shared by every transport so the classification is identical whether qna ran
    locally, over SSH, or in a container.
    """
    if parsed.errors:
        return parsed.errors[0], ERROR_KIND_RELEVANCE

    if exit_code != 0:
        detail = stderr.strip() or f"qna exited {exit_code} with no output"
        return detail, ERROR_KIND_QNA

    if not parsed.has_recognizable_output:
        detail = stderr.strip() or "qna produced no A:, E:, I: or T: lines"
        return detail, ERROR_KIND_QNA

    return None, None


def sudo_privilege_problem(stderr: str) -> str | None:
    """Recognize sudo's own refusal so it is not blamed on qna.

    ``sudo -n`` exits 1 without running anything when it has no usable
    credential, when the user is not a sudoer, or when the command is not
    permitted. Left alone those all classify as :data:`ERROR_KIND_QNA`, which
    reads as "the relevance engine failed".

    Detection keys off the ``sudo: `` prefix sudo puts on its own diagnostics
    in every locale — qna never emits one — rather than the English wording of
    any particular message.
    """
    line = next(
        (stripped for raw in stderr.splitlines() if (stripped := raw.strip()).startswith("sudo:")),
        None,
    )
    if line is None:
        return None
    return (
        f"sudo could not elevate qna: {line}; grant passwordless sudo for the qna "
        "binary (a NOPASSWD sudoers rule), rerun the whole command under sudo, "
        "or drop --become"
    )


__all__ = [
    "TransportLocal",
    "classify_qna_outcome",
    "normalize_stdin_payload",
    "sudo_privilege_problem",
]
