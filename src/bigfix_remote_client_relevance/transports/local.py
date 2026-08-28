"""Evaluate client relevance against a qna binary on this machine.

Ported from ``jgstew/EvaluateRelevance``'s ``evaluate_relevance_raw_stdin()``,
with the encoding pinned to UTF-8, a timeout added, and failures returned as
results rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Sequence

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


class TransportLocal:
    """Runs qna as a subprocess on the controller itself.

    Used for tests and for a fast syntax check before paying for an SSH or
    container round-trip.
    """

    def __init__(
        self,
        qna_path: str | None = None,
        *,
        candidates: Sequence[str] | None = None,
        require_root_on_macos: bool = True,
    ) -> None:
        self._qna_path = qna_path
        self._candidates = candidates
        self._require_root_on_macos = require_root_on_macos

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
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
            base.update(overrides)
            return ClientRelevanceResult(**base)  # type: ignore[arg-type]

        if qna is not None:
            # Provisioning a pinned version locally arrives with the bootstrap
            # extract phase in M3; until then, say so plainly.
            return _result(
                error="provisioning a pinned qna version is not implemented for the "
                "local transport yet",
                error_kind=ERROR_KIND_BOOTSTRAP,
            )

        root_problem = self._macos_root_problem()
        if root_problem is not None:
            return _result(error=root_problem, error_kind=ERROR_KIND_BOOTSTRAP)

        resolved_path = qna_path or self._qna_path or find_qna_path(self._candidates)
        if resolved_path is None:
            return _result(
                error="no qna binary found; pass qna_path or install the BES client",
                error_kind=ERROR_KIND_BOOTSTRAP,
            )

        argv = [resolved_path, *QNA_EVAL_FLAGS]
        logger.debug("running %s", " ".join(argv))

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return _result(
                qna_path=resolved_path,
                error=f"could not start qna at {resolved_path}: {exc}",
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

    def _macos_root_problem(self) -> str | None:
        """Refuse to run qna as a non-root user on macOS.

        Observed against BESAgent 11.x on macOS 15: a non-root qna aborts with
        an uncaught ``FileIOError`` from libc++ before answering anything — even
        ``TRUE`` — so there is no partial-answer mode worth preserving. Failing
        here turns an opaque crash dump into an actionable message. Waivable via
        ``require_root_on_macos=False`` for setups where it does work.
        """
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
    """Kill a process and reap it so no orphan outlives the timeout."""
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


__all__ = ["TransportLocal", "classify_qna_outcome", "normalize_stdin_payload"]
