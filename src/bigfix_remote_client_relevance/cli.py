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

from bigfix_remote_client_relevance.bootstrap.targets import KNOWN_TARGETS
from bigfix_remote_client_relevance.inventory import (
    InventoryError,
    load_inventory,
    update_inventory_platform,
)
from bigfix_remote_client_relevance.orchestrate import (
    DEFAULT_MAX_PARALLEL,
    DEFAULT_PULL_PARALLEL,
    EXIT_QNA,
    Target,
    count_work,
    evaluate_client_relevance,
    evaluate_client_relevance_stream,
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

# Implied when no target is given and this file exists in the current
# directory -- see the `not any(modes)` branch in `evaluate`.
DEFAULT_INVENTORY_PATH = Path("hosts.toml")


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


def _display_host(result: ClientRelevanceResult) -> str:
    """The host as shown in headers -- not necessarily ``result.host`` as-is.

    ``local`` and ``container:<image>@<arch>`` already say how they're
    reached; a bare ssh host like ``192.168.4.115`` doesn't, and an
    inventory can mix an ssh and a fastquery entry for the same address.
    This is display-only: ``result.host`` itself stays untouched, since
    ``_update_inventory_platforms`` matches it against the inventory's
    table names verbatim.
    """
    if result.transport in ("ssh", "fastquery") and not result.host.startswith(
        f"{result.transport}:"
    ):
        return f"{result.transport}:{result.host}"
    return result.host


def _label(result: ClientRelevanceResult) -> str:
    """Full header text: the display host, its platform when known, and the
    qna version -- e.g. ``ssh:192.168.4.115:windows (qna 11.0.6.137)``, echoing
    the ``container:<image>@<arch>`` shape's colon-separated qualifier.

    Platform is shown only for ssh/fastquery, the two transports whose host
    string alone doesn't say what's on the other end (an inventory full of
    bare IPs and SSH aliases gives no hint which is Windows, macOS, or
    Linux). ``local`` is always this machine and a container image already
    names its own OS, so both would just be repeating themselves.
    """
    label = _display_host(result)
    if result.transport in ("ssh", "fastquery") and result.platform:
        label = f"{label}:{result.platform}"
    if result.qna_version:
        label = f"{label} (qna {result.qna_version})"
    return label


def _render_one_plain(result: ClientRelevanceResult, *, labelled: bool) -> str:
    """One result's section. Shared by the batch and streaming renderers, so
    the two can never drift into printing the same result differently."""
    lines: list[str] = []
    if labelled:
        lines.append(f"== {_label(result)}")
    lines.extend(result.answers)
    if result.error:
        # Errors go to stdout only as part of a labelled section; the
        # summary on stderr is what a human reads.
        lines.append(f"!! {result.error_kind}: {result.error}")
    return "\n".join(lines)


def _render_plain(results: list[ClientRelevanceResult]) -> str:
    """Answers for a single result; host-labelled sections for a fan-out."""
    multiple = len(results) > 1
    return "\n".join(_render_one_plain(result, labelled=multiple) for result in results)


def _diff_key(result: ClientRelevanceResult) -> tuple[object, ...]:
    """What makes two results the same answer.

    The answer *type* counts: an inspector returning 1 as an integer on one
    platform and a string on another is exactly the difference this tool
    exists to find. ``raw_qna_output`` deliberately does not — it carries
    timings and paths that differ between identical answers and would defeat
    the collapse. Neither does the qna version, which identifies a member
    rather than an answer.
    """
    return (
        result.error_kind,
        result.error,
        tuple(result.answers),
        tuple(result.answer_types),
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _render_diff(results: list[ClientRelevanceResult]) -> str:
    """Group results by answer, so disagreement is what stands out."""
    groups: dict[tuple[object, ...], list[ClientRelevanceResult]] = {}
    for result in results:
        groups.setdefault(_diff_key(result), []).append(result)

    # Majority first, ties broken by first appearance so the output is stable.
    order = sorted(
        groups.items(),
        key=lambda item: (-len(item[1]), results.index(item[1][0])),
    )

    lines: list[str] = []
    for index, (_key, members) in enumerate(order, start=1):
        if lines:
            lines.append("")
        if len(order) == 1:
            lines.append(f"== all targets agree ({len(members)})")
        else:
            lines.append(f"== group {index} ({_plural(len(members), 'target')})")
        lines.extend(f"-- {_label(member)}" for member in members)
        first = members[0]
        lines.extend(first.answers)
        if first.error:
            lines.append(f"!! {first.error_kind}: {first.error}")
    return "\n".join(lines)


def _summarize_failures(results: list[ClientRelevanceResult]) -> None:
    for result in results:
        if result.error_kind is not None:
            logger.error("%s: %s (%s)", result.host, result.error, result.error_kind)


def _update_inventory_platforms(
    inventory: Path,
    inventory_targets: list[Target],
    results: list[ClientRelevanceResult],
) -> None:
    """Write a probed or corrected `platform` back for each inventory host.

    Compares each result's (possibly probed, possibly corrected) platform
    against what that host's *original* Target carried before the run, so an
    already-correct entry is never rewritten and a --qna-version fan-out over
    multiple versions never writes the same host twice. Best-effort: a write
    failure (e.g. the file became read-only mid-run) is logged, not raised --
    one host's config problem must not turn a successful evaluation into a
    failed command.
    """
    configured = {target.name: target.platform for target in inventory_targets}
    written: set[str] = set()
    for result in results:
        if result.host not in configured or result.host in written:
            continue
        if result.platform is None or result.platform == configured[result.host]:
            continue
        try:
            update_inventory_platform(inventory, result.host, result.platform)
        except InventoryError as exc:
            logger.warning(
                "could not update platform for %s in %s: %s", result.host, inventory, exc
            )
        else:
            written.add(result.host)


@app.command()
def evaluate(
    args: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[HOST] [CLIENT_RELEVANCE]",
            help=(
                "SSH host followed by the client relevance; with --local, "
                "--container or --inventory, just the client relevance."
            ),
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
        list[str] | None,
        typer.Option(
            "--container",
            metavar="IMAGE",
            help="Evaluate inside a container image. Repeatable, and composes with --inventory.",
        ),
    ] = None,
    inventory: Annotated[
        Path | None,
        typer.Option(
            "--inventory",
            help=(
                "Evaluate across the hosts in a hosts.toml file. Implied when "
                "no target is given and hosts.toml exists in the current directory."
            ),
        ),
    ] = None,
    update_inventory: Annotated[
        bool,
        typer.Option(
            "--update-inventory/--no-update-inventory",
            help=(
                "Write a probed or corrected `platform` back into --inventory's "
                "hosts.toml, so future runs skip the probe and a wrong entry stops "
                "failing silently forever."
            ),
        ),
    ] = True,
    rebuild_image: Annotated[
        bool,
        typer.Option(
            "--rebuild-image",
            help="Force a fresh prepared container image instead of reusing a cached one.",
        ),
    ] = False,
    auto_setup: Annotated[
        bool,
        typer.Option(
            "--auto-setup/--no-auto-setup",
            help=(
                "Install runtime libraries a container image is missing. "
                "Turn off for air-gapped hosts."
            ),
        ),
    ] = True,
    qna_version: Annotated[
        list[str] | None,
        typer.Option(
            "--qna-version",
            help="Version spec ('11.0' or '11.0.6.137'). Repeatable to compare versions.",
        ),
    ] = None,
    user: Annotated[str | None, typer.Option("--user", help="SSH username.")] = None,
    become: Annotated[
        bool | None,
        typer.Option(
            "--become/--no-become",
            help=(
                "Run qna under sudo (root-only inspectors). Applies to SSH and "
                "--local; needs passwordless sudo, and has no effect on Windows. "
                "Defaults on for --local when this machine is macOS, since qna "
                "requires root there; pass --no-become to get the plain refusal "
                "instead."
            ),
        ),
    ] = None,
    arch: Annotated[str, typer.Option("--arch", help="Target architecture.")] = "x86_64",
    platform: Annotated[
        str | None,
        typer.Option(
            "--platform",
            metavar="PLATFORM",
            help=(
                "Force the bootstrap platform instead of probing the target. "
                "Applies to targets named by flags, never to --inventory hosts."
            ),
        ),
    ] = None,
    insecure_skip_host_key_check: Annotated[
        bool,
        typer.Option(
            "--insecure-skip-host-key-check",
            help=(
                "Do not verify the SSH host key. Removes protection against "
                "interception; for throwaway lab endpoints only."
            ),
        ),
    ] = False,
    max_parallel: Annotated[
        int, typer.Option("--max-parallel", help="Cap on concurrent evaluations.")
    ] = DEFAULT_MAX_PARALLEL,
    pull_parallel: Annotated[
        int,
        typer.Option(
            "--pull-parallel",
            help=(
                "Cap on concurrent image pulls and builds. Lower than "
                "--max-parallel because pulls are far more expensive."
            ),
        ),
    ] = DEFAULT_PULL_PARALLEL,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-evaluation timeout in seconds.")
    ] = 30.0,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit one JSON document per (target x version).")
    ] = False,
    as_jsonl: Annotated[
        bool,
        typer.Option(
            "--jsonl",
            help=(
                "Like --json, but one compact JSON object per line, written as "
                "each target answers instead of all at once."
            ),
        ),
    ] = False,
    diff: Annotated[
        bool,
        typer.Option(
            "--diff",
            help="Collapse identical answers and show where targets disagree.",
        ),
    ] = False,
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="-v for info, -vv for debug.")
    ] = 0,
) -> None:
    """Evaluate a BigFix client-relevance expression and print the answers."""
    _configure_logging(verbose)
    args = args or []
    qna_version = qna_version or []
    container = container or []

    # --container and --inventory compose: a fleet plus an ad-hoc image is a
    # normal thing to want. --local is still on its own.
    if local and (container or inventory):
        _fail("--local cannot be combined with --container or --inventory")
    modes = [bool(local), bool(container), bool(inventory)]

    if platform is not None and platform not in KNOWN_TARGETS:
        _fail(f"unknown platform {platform!r}; known: {', '.join(sorted(KNOWN_TARGETS))}")

    if rebuild_image and not container:
        _fail("--rebuild-image only applies to --container targets")

    if diff and (as_json or as_jsonl):
        # --json is one schema, a flat array of results, and it is the future
        # MCP tool's contract; `jq 'group_by(.answers)'` covers this case.
        # --diff can never stream anyway: the grouping needs every answer in.
        _fail("--diff renders a text summary; use --json on its own for machine-readable results")

    if as_json and as_jsonl:
        # Same records, two framings. Emitting both would put an array and
        # bare objects on the one channel that has to stay parseable.
        _fail("--json and --jsonl are two framings of the same records; pick one")

    # With an explicit target mode every positional is client relevance;
    # otherwise the first positional is the SSH host.
    host: str | None = None
    if not any(modes):
        # A bare `HOST` with no relevance and a bare relevance with no target
        # look identical here, so say what a complete invocation needs --
        # unless a hosts.toml sits right here, in which case that's obviously
        # what was meant and there's no need to spell out --inventory.
        needed = 1 if client_relevance_file else 2
        if len(args) < needed:
            if DEFAULT_INVENTORY_PATH.exists():
                inventory = DEFAULT_INVENTORY_PATH
            else:
                _fail(
                    "expected a target and a client relevance, e.g. "
                    '`HOST "name of operating system"`; or choose a target with '
                    "--local, --container IMAGE, or --inventory hosts.toml"
                )
        else:
            host = args[0]
            args = args[1:]

    text = _read_client_relevance(args, client_relevance_file)

    # Inventory hosts carry their own platform; a global --platform would
    # silently override it, which is the guessing this tool just stopped doing.
    # Kept separate from `targets` (which also gathers --container images) so
    # the write-back below only ever touches hosts that actually came from
    # this file, by the original, pre-run platform each one had.
    inventory_targets: list[Target] = []
    targets: list[Target] = []
    if inventory is not None:
        try:
            inventory_targets = load_inventory(inventory)
        except InventoryError as exc:
            _fail(str(exc))
        targets.extend(inventory_targets)
    targets.extend(
        Target(
            kind="container",
            name=image,
            image=image,
            arch=arch,
            platform=platform,
            rebuild_image=rebuild_image,
            auto_setup=auto_setup,
        )
        for image in container
    )
    if local:
        # `become` stays whatever the caller passed (True/False/unspecified);
        # default_transport_factory is where an unspecified value resolves,
        # since inventory-loaded local targets need the same macOS-aware
        # default and shouldn't need that logic duplicated here.
        targets = [Target(kind="local", name="local", platform=platform, become=become)]
    elif host is not None:
        targets = [
            Target(
                kind="ssh",
                name=host,
                user=user,
                become=become,
                platform=platform,
                verify_host_key=not insecure_skip_host_key_check,
            )
        ]

    # --json and --diff are the whole-set views -- one array, and a grouping
    # that only exists once every answer is in -- so they alone wait for the
    # full fan-out. Plain text and --jsonl are both per-result framings and
    # can emit as each target answers, so a slow SSH endpoint stops holding
    # up the containers that already finished.
    stream = not as_json and not diff
    # Decided before the run rather than from len(results), so the streaming
    # path labels its first section the same way the batch path would.
    labelled = count_work(targets, qna_version or None) > 1

    def _render_streamed(result: ClientRelevanceResult) -> str:
        if as_jsonl:
            # Compact and newline-free: one record per line is the whole
            # contract a line-oriented reader depends on.
            return json.dumps(dataclasses.asdict(result), separators=(",", ":"))
        return _render_one_plain(result, labelled=labelled)

    async def _run() -> list[ClientRelevanceResult]:
        if not stream:
            return await evaluate_client_relevance(
                text,
                targets,
                qna_version=qna_version or None,
                max_parallel=max_parallel,
                pull_parallel=pull_parallel,
                timeout_s=timeout,
            )
        collected: list[ClientRelevanceResult] = []
        async for result in evaluate_client_relevance_stream(
            text,
            targets,
            qna_version=qna_version or None,
            max_parallel=max_parallel,
            pull_parallel=pull_parallel,
            timeout_s=timeout,
        ):
            collected.append(result)
            line = _render_streamed(result)
            if line:
                typer.echo(line)
        return collected

    results = asyncio.run(_run())

    _summarize_failures(results)

    if inventory is not None and update_inventory and inventory_targets:
        _update_inventory_platforms(inventory, inventory_targets, results)

    if not stream:
        payload = (
            json.dumps([dataclasses.asdict(r) for r in results], indent=2)
            if as_json
            else _render_diff(results)
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
