"""Command line interface.

This module is the single sanctioned stdout writer in the package (see
DESIGN.md § Output convention). The split it maintains is deliberate and
load-bearing:

* **stdout** carries the result payload and nothing else — the ``--json``
  documents, or the plain-text answers. It stays machine-readable even when the
  run fails, so ``... --json | jq`` works unconditionally.
* **stderr** carries logs, diagnostics, and human-facing error summaries, at a
  level chosen by ``-v`` / ``-vv``.

That is exactly the discipline a stdio MCP server needs, which is why it is
enforced here rather than left to convention.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from bigfix_remote_client_relevance.inventory import InventoryError, load_inventory
from bigfix_remote_client_relevance.orchestrate import (
    DEFAULT_MAX_PARALLEL,
    EXIT_QNA,
    Target,
    evaluate_client_relevance,
    worst_exit_code,
)
from bigfix_remote_client_relevance.results import ClientRelevanceResult

logger = logging.getLogger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Evaluate BigFix client relevance on remote endpoints and in containers.",
)

USAGE_EXIT_CODE = 2


def _configure_logging(verbosity: int) -> None:
    """Send logs to stderr, leaving stdout free for the payload.

    Adds a handler rather than replacing the package's ``NullHandler``: the
    library's own configuration is left intact, and only the level this
    application wants is applied.
    """
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    package_logger = logging.getLogger("bigfix_remote_client_relevance")
    package_logger.addHandler(handler)
    package_logger.setLevel(level)


def _fail(message: str) -> None:
    """Report a usage problem on stderr and exit."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(USAGE_EXIT_CODE)


def _read_client_relevance(inline: list[str], from_file: Path | None) -> str:
    if from_file is not None:
        if inline:
            _fail("give the client relevance either inline or with --client-relevance-file")
        try:
            # Files are read as UTF-8 without a BOM, matching what qna expects.
            return from_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            _fail(f"could not read {from_file}: {exc}")
    if not inline:
        _fail("no client relevance given (pass it inline or with --client-relevance-file)")
    return " ".join(inline)


def _render_plain(results: list[ClientRelevanceResult]) -> str:
    """Answers for a single result; host-labelled sections for a fan-out."""
    lines: list[str] = []
    multiple = len(results) > 1
    for result in results:
        if multiple:
            label = result.host
            if result.qna_version:
                label = f"{label} (qna {result.qna_version})"
            lines.append(f"== {label}")
        lines.extend(result.answers)
        if result.error:
            # Errors go to stdout only as part of a labelled section; the
            # summary on stderr is what a human reads.
            lines.append(f"!! {result.error_kind}: {result.error}")
    return "\n".join(lines)


def _summarize_failures(results: list[ClientRelevanceResult]) -> None:
    for result in results:
        if result.error_kind is not None:
            logger.error("%s: %s (%s)", result.host, result.error, result.error_kind)


@app.command()
def evaluate(
    args: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[HOST] [CLIENT_RELEVANCE]",
            help="SSH host followed by the client relevance; with --local, "
            "--container or --inventory, just the client relevance.",
        ),
    ] = None,
    client_relevance_file: Annotated[
        Path | None,
        typer.Option(
            "--client-relevance-file",
            "-f",
            help="Read the client relevance from a UTF-8 file.",
        ),
    ] = None,
    local: Annotated[
        bool, typer.Option("--local", help="Evaluate against a local qna binary.")
    ] = False,
    container: Annotated[
        str | None,
        typer.Option("--container", metavar="IMAGE", help="Evaluate inside a container image."),
    ] = None,
    inventory: Annotated[
        Path | None,
        typer.Option("--inventory", help="Evaluate across the hosts in a hosts.toml file."),
    ] = None,
    qna_version: Annotated[
        list[str] | None,
        typer.Option(
            "--qna-version",
            help="Version spec ('11.0' or '11.0.6.137'). Repeatable to compare versions.",
        ),
    ] = None,
    user: Annotated[str | None, typer.Option("--user", help="SSH username.")] = None,
    become: Annotated[
        bool, typer.Option("--become", help="Use sudo on the target (root-only inspectors).")
    ] = False,
    arch: Annotated[str, typer.Option("--arch", help="Target architecture.")] = "x86_64",
    insecure_skip_host_key_check: Annotated[
        bool,
        typer.Option(
            "--insecure-skip-host-key-check",
            help="Do not verify the SSH host key. Removes protection against "
            "interception; for throwaway lab endpoints only.",
        ),
    ] = False,
    max_parallel: Annotated[
        int, typer.Option("--max-parallel", help="Cap on concurrent evaluations.")
    ] = DEFAULT_MAX_PARALLEL,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-evaluation timeout in seconds.")
    ] = 30.0,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit one JSON document per (target x version).")
    ] = False,
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="-v for info, -vv for debug.")
    ] = 0,
) -> None:
    """Evaluate a BigFix client-relevance expression and print the answers."""
    _configure_logging(verbose)
    args = args or []
    qna_version = qna_version or []

    modes = [bool(local), bool(container), bool(inventory)]
    if sum(modes) > 1:
        _fail("choose only one of --local, --container, or --inventory")

    # With an explicit target mode every positional is client relevance;
    # otherwise the first positional is the SSH host.
    host: str | None = None
    if not any(modes):
        # A bare `HOST` with no relevance and a bare relevance with no target
        # look identical here, so say what a complete invocation needs.
        needed = 1 if client_relevance_file else 2
        if len(args) < needed:
            _fail(
                "expected a target and a client relevance, e.g. "
                "`HOST \"name of operating system\"`; or choose a target with "
                "--local, --container IMAGE, or --inventory hosts.toml"
            )
        host = args[0]
        args = args[1:]

    text = _read_client_relevance(args, client_relevance_file)

    targets: list[Target]
    if inventory is not None:
        try:
            targets = load_inventory(inventory)
        except InventoryError as exc:
            _fail(str(exc))
    elif local:
        targets = [Target(kind="local", name="local")]
    elif container:
        targets = [Target(kind="container", name=container, image=container, arch=arch)]
    else:
        assert host is not None
        targets = [
            Target(
                kind="ssh",
                name=host,
                user=user,
                become=become,
                verify_host_key=not insecure_skip_host_key_check,
            )
        ]

    results = asyncio.run(
        evaluate_client_relevance(
            text,
            targets,
            qna_version=qna_version or None,
            max_parallel=max_parallel,
            timeout_s=timeout,
        )
    )

    _summarize_failures(results)

    payload = (
        json.dumps([dataclasses.asdict(r) for r in results], indent=2)
        if as_json
        else _render_plain(results)
    )
    if payload:
        typer.echo(payload)

    raise typer.Exit(worst_exit_code(results) if results else EXIT_QNA)


def main() -> None:
    """Console-script entry point.

    This must go through the Typer app rather than calling the command function
    directly: pointing ``[project.scripts]`` at the decorated command skips
    argument parsing entirely, so even ``--help`` would raise.
    """
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
