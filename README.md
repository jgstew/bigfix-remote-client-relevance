# bigfix-remote-client-relevance

Evaluate BigFix **client relevance** on remote endpoints and inside containers
via `qna`, without a full BES install — over SSH, in Docker, or locally.

The point is a fast edit → evaluate loop while authoring content, instead of
the minutes-long action-deployment round trip. A future MCP server can import
this package so AI agents can write and test client relevance the same way.

> **Client relevance, not session relevance.** This deals only with the dialect
> `qna` and the BES client evaluate on an endpoint. Session relevance — the
> `bes-*` object model queried through the root server's REST `/api/query` —
> is a different dialect and out of scope.

See [DESIGN.md](DESIGN.md) for the full design and rationale.

## Install

```bash
uv tool install bigfix-remote-client-relevance
```

Or run it without installing:

```bash
uvx bigfix-remote-client-relevance --container ubuntu:22.04 --qna-version 11.0 "name of operating system"
```

## Use

Evaluate on a container, provisioning a pinned qna version on the fly — no
BigFix install, no SSH credentials, nothing to clean up:

```bash
bigfix-remote-client-relevance --container ubuntu:22.04 --qna-version 11.0 "name of operating system"
```

Compare the same expression across two qna versions on one target:

```bash
bigfix-remote-client-relevance --container ubuntu:22.04 --qna-version 11.0 --qna-version 10.0 "version of client"
```

Evaluate against the BigFix client on this machine:

```bash
bigfix-remote-client-relevance --local "name of operating system"
```

On macOS `qna` needs root, so `--local` implies `--become` there automatically —
just the qna invocation runs under `sudo -n`, not the whole CLI. Pass
`--no-become` to get the plain "needs root" refusal instead.

Evaluate on a real endpoint over SSH (a `~/.ssh/config` alias works):

```bash
bigfix-remote-client-relevance mac-test --become "name of operating system"
```

Fan out across an inventory and emit JSON:

```bash
bigfix-remote-client-relevance --inventory remote_clients.toml -f probe.rel --json
```

`--json` writes one document per (target × version) to stdout; logs go to
stderr, so piping into `jq` always works.

If no target is given at all — no `--local`, `--container`, `--inventory`,
or `HOST` — a `remote_clients.toml` is searched for automatically, current
directory first, then `~/.bigfix/`, then the platform's all-users config
directory (see § Inventory below), so the above also works as:

```bash
bigfix-remote-client-relevance -f probe.rel --json
```

### Streaming

Results are emitted as each target answers, in completion order rather than
inventory order — a slow SSH endpoint no longer holds up the containers that
already finished. This applies to plain text and to `--jsonl`, which writes
one compact JSON object per line:

```bash
bigfix-remote-client-relevance --inventory remote_clients.toml --jsonl "version of client" | jq -c '{host, elapsed_ms}'
```

```
{"host":"local","elapsed_ms":928}
{"host":"container:debian:12@x86_64","elapsed_ms":653}
{"host":"ssh:192.168.4.115","elapsed_ms":2603}
```

`--jsonl` carries exactly the same fields as `--json`; the only difference is
the framing, so pick `--jsonl` for a line-oriented reader and `--json` when
you want one document to parse in full. They are mutually exclusive.

`--json` and `--diff` are whole-set views — one array, and a grouping that
only exists once every answer is in — so those two still print once at the
end. Exit codes are always decided after the full fan-out, streaming or not.

### Exit codes

Actionable for CI gating; the worst across the fan-out wins.

| Code | Meaning |
|---|---|
| 0 | every target evaluated without error |
| 1 | a client-relevance error (qna emitted an `E:` line) |
| 2 | qna failed, or provisioning it did |
| 3 | a transport failure — connect, auth, or timeout |
| 4 | a qna version spec could not be resolved |
| 64 | usage error — bad flags or a missing argument |

64 is sysexits `EX_USAGE`, deliberately outside the fan-out's range so a caller
that shells out can tell a bad invocation from a failed evaluation. (It was 2
through 0.1.2, which collided with "qna failed".)

### Inventory

With no target given at all, `remote_clients.toml` is searched for in three
places, current directory first, and the first one found wins:

1. `./remote_clients.toml`
2. `~/.bigfix/remote_clients.toml` — per-user, a literal dotfolder in the
   home directory on every OS (same idea as `~/.ssh`, `~/.aws`, `~/.docker`).
   `.bigfix` is a shared, cross-project folder name, not specific to this
   tool.
3. The platform's all-users config directory — `/etc/xdg/bigfix` on Linux,
   `/Library/Application Support/bigfix` on macOS, `C:\ProgramData\bigfix`
   on Windows.

```toml
# remote_clients.toml
[defaults]
qna_version = "11.0"        # version spec; overridable per host

[hosts.mac-test]            # table name is the ~/.ssh/config alias
transport = "ssh"
become = true               # sudo for root-only inspectors

[hosts.this-controller]
transport = "local"          # no `become` line needed: implied on a macOS controller

[hosts.ubuntu-22]
transport = "container"
image = "ubuntu:22.04"
```

## As a library

```python
from bigfix_remote_client_relevance import Target, evaluate_client_relevance

results = await evaluate_client_relevance(
    "name of operating system",
    [Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04")],
    qna_version="11.0",
)
```

For results as they arrive rather than all at once — live progress, or an MCP
tool streaming partial output — iterate the streaming variant instead:

```python
from bigfix_remote_client_relevance import evaluate_client_relevance_stream

async for result in evaluate_client_relevance_stream("name of operating system", targets):
    print(result.host, result.answers)
```

It yields in completion order; `evaluate_client_relevance` waits for the whole
fan-out and returns target-then-version order.

### Several expressions at once

`evaluate_many` takes an array of relevance statements and an inventory of
hosts, and evaluates the whole grid — `targets × versions × expressions`:

```python
from bigfix_remote_client_relevance import Target, evaluate_many, load_inventory

results = await evaluate_many(
    [
        "name of operating system",
        "version of client",
        "number of processes",
    ],
    load_inventory("remote_clients.toml"),
    qna_version="11.0",
)

for result in results:
    print(result.host, result.client_relevance, result.answers)
```

Each (target, version) pair builds **one** transport and runs every expression
through it: for a container that is one image pull, one prepared-image lookup
and one `docker run` instead of N, and for SSH one connection instead of N.
Container targets carrying more than one expression are kept alive for the
batch automatically. Every result carries the expression that produced it in
`client_relevance`, so nothing has to be matched up by position.

Measured on `ubuntu:22.04` at `qna 11.0`, x86_64 under emulation on Apple
Silicon (medians of three runs, `scripts/bench_batch.py`):

| expressions | one call each | `evaluate_many` | saving |
| ---: | ---: | ---: | ---: |
| 1 | 5.6s | 6.1s | 0.9× |
| 3 | 18.9s | 7.9s | 2.4× |
| 10 | 74.4s | 18.2s | 4.1× |

One expression is a wash — there is nothing to amortize, and the batch pays a
little extra to keep a container it only uses once. From three up, the saving
is the shared setup: one transport, one version resolution, one image
preparation instead of N. Reusing the container on top of that measured as
roughly neutral, since `docker run` on an already-prepared image is cheap; it is
kept for the batch because starting one per expression would undo the batching,
not because the reuse itself is where the time goes.

`evaluate_many_stream` is the completion-order form, the same way
`evaluate_client_relevance_stream` is for the single-expression call.

### Container lifetime

A container target evaluating more than one expression is kept alive for the
length of that batch — one `docker run` for the whole grid rather than one per
expression — and stopped as soon as the batch is done. A single expression is
left one-shot, which is the more hermetic default and has nothing to amortize
anyway. Containers are never shared between processes: two runs of this tool
each get their own.

Every container also carries a deadline of its own and removes itself once it
has been idle for two minutes (`DEFAULT_IDLE_TTL_S`). That is a backstop, not
the normal path — a run that finishes stops its own containers. It exists
because a `finally` covers an exception but not a `SIGKILL`, a dead daemon
connection, or a removal call that is simply lost, and any of those used to
strand a `sleep infinity` container for the life of the machine. The window
always covers the evaluation about to run, so a slow `timeout_s` can never
expire a container under itself.

```python
from bigfix_remote_client_relevance import reclaim_stray_containers

await reclaim_stray_containers()  # clear what a killed run left behind
```

One `ClientRelevanceResult` comes back per (target × version × expression), carrying
`answers`, `answer_types`, `error` / `error_kind`, the resolved `qna_version`,
and the full `raw_qna_output` for debugging. Failures are reported inside
results rather than raised, so one unreachable host never breaks a fan-out.

The library logs through `logging` and never writes to stdout — that channel
belongs to the CLI's payload, and to a stdio MCP server's JSON-RPC.

The package ships `py.typed`, so `mypy` and `pyright` see real types across the
import boundary.

## Building an MCP server on this

The whole surface an MCP tool needs is here, in stdlib-only helpers — nothing
below adds a dependency to your server.

### From Python

```python
from bigfix_remote_client_relevance import (
    BigFixRelevanceError,
    Target,
    count_work,
    evaluate_client_relevance_stream,
    format_results,
    result_to_dict,
)

targets = [Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04")]

total = count_work(targets, "11.0")          # the progress denominator
results = []
async for result in evaluate_client_relevance_stream(expr, targets, qna_version="11.0"):
    results.append(result)
    await report_progress(len(results), total)

return {
    "content": [{"type": "text", "text": format_results(results)}],
    # max_raw_output caps qna's transcript, which is unbounded by default.
    "structuredContent": {"results": [result_to_dict(r, max_raw_output=4000) for r in results]},
    "isError": any(not r.ok for r in results),
}
```

| Need | Use |
|---|---|
| the tool body | `evaluate_client_relevance` (batched) or `evaluate_client_relevance_stream` (completion order) |
| progress notifications | `count_work(targets, qna_version)` for the total |
| `structuredContent` | `result_to_dict` / `results_to_dicts`, typed as `ResultPayload` |
| `outputSchema` | `RESULT_JSON_SCHEMA` (JSON Schema 2020-12), versioned by `SCHEMA_VERSION` |
| the text `content` block | `format_result` / `format_results` — the CLI's own rendering, without typer |
| classifying a failure | `result.error_kind`, one of `ERROR_KINDS`; `error_kind == "relevance"` plus `raw_qna_output` is what lets an agent fix its own expression and retry |
| errors from the setup path | one `except BigFixRelevanceError` around `load_inventory`, version resolution, and artifact caching |

The fan-out functions never raise for a target failure — an unreachable host
comes back as a result with `error_kind` set — so only the setup path needs the
`try`. Cancelling the fan-out (an MCP request cancellation) propagates
`CancelledError` normally rather than being turned into a result.

Result payloads only ever gain fields within a major version; `SCHEMA_VERSION`
bumps when they do, and `RESULT_JSON_SCHEMA` does not set
`additionalProperties: false` so an older validator keeps working.

### From a non-Python server

Shell out to the CLI, which is the same code paths:

```bash
bigfix-remote-client-relevance --schema
```

prints the JSON Schema for a single result and exits 0 — no target needed. Then
`--jsonl` emits exactly one of those objects per line, flushed as each target
answers, so a Node or Go server can stream progress off the pipe. `--json`
emits the whole array once. stdout carries only the payload; logs and error
summaries go to stderr, at a level set by `-v`/`-vv`.

If several server processes share a machine, they also share the qna artifact
cache — safely and efficiently. Downloads stage to a temp name, verify their
published sha256, and land by atomic rename, so a race can never serve a
partial or corrupt artifact; a `filelock`-backed cross-process lock means two
processes starting at once share one download rather than each fetching it.
A stuck download only ever blocks a *live* wait — the lock is released by the
OS the instant a holder crashes, no stale-lock cleanup needed — bounded by
`ensure_artifact`'s `lock_timeout_s` (10 minutes by default).

## How qna gets to the target

The machine running the CLI (the *controller*) owns downloading agent
artifacts from `support.bigfix.com` and pushes them out; targets never fetch
from the internet. Ten hosts on the same version cost **one** download, which
also means it works against lab endpoints with no outbound access.

A version spec resolves at run time — `11.0` picks the newest patch in that
stream, `11.0.6.137` pins exactly — and the resolved full version is what
gets recorded in every result. Artifacts are checksum-verified against the
release site's published `SHA256SUMS` and cached forever, since they are
immutable per version. On a target, an extracted version is left in place, so
it crosses the wire once ever rather than once per run.

Only Windows has a standalone QnA download; every other platform extracts qna
out of the agent package without installing it.

### Comparing across targets

Across several targets the useful answer is usually not one block per target,
it is where they disagree. `--diff` collapses identical answers:

```bash
bigfix-remote-client-relevance \
  --container ubuntu:22.04 --container debian:12 \
  --container almalinux:9 --container rockylinux:9 \
  --qna-version 11.0 --diff "number of properties"
```

```
== group 1 (2 targets)
-- container:ubuntu:22.04@x86_64 (qna 11.0.6.137)
-- container:debian:12@x86_64 (qna 11.0.6.137)
2134

== group 2 (2 targets)
-- container:almalinux:9@x86_64 (qna 11.0.6.137)
-- container:rockylinux:9@x86_64 (qna 11.0.6.137)
2151
```

When everything agrees it says so once, which is the quickest way to check
that a set of platforms answers identically. Answer *types* count as part of
the answer; the qna version does not, so `--qna-version 11.0 --qna-version
10.0 --diff` tells you whether the two versions agree. `--diff` is a text
summary — for machine consumption use `--json` on its own.

### Containers

With `--container`, an unspecified platform is **probed**, not assumed — the
image is asked what it is (the same check SSH runs, including over
`--inventory`, and for the same reason: a wrong guess silently hands the box
a `.deb` or an `.rpm` it can't extract), and an unrecognized answer fails
loudly rather than silently running the wrong agent. Pass `--platform` to
skip the probe or override it for a fleet.

The qna artifact is extracted once on the controller and bind-mounted in, so
an image needs no package manager of its own — `rpm2cpio`/`cpio` or
`dpkg-deb`/`ar`/`tar` are never required inside a container. The first run
against a given (image, qna version, arch) also builds a small derived image
with the tree baked in and reuses it on every later run against the same
combination — no mount, no extraction, sub-second start. Pass
`--rebuild-image` to force a fresh one.

While building that image, qna is checked to see that it actually starts.
Minimal images often lack a shared library it needs — `rockylinux:9` and
`amazonlinux:2023` have no `libdbus-1.so.3` — so the missing package is
installed and baked in, paid once rather than per run. `--no-auto-setup`
turns that off for air-gapped hosts; the run then fails naming the library
rather than installing anything. A failed or skipped install is never
committed, so nothing broken is cached.

These derived images are tagged `bfrcr/prepared:*` and are safe to remove at
any time:

```bash
docker rmi $(docker images 'bfrcr/prepared:*' -q)
```

`--arch` defaults to `x86_64` — the common case for BigFix clients,
regardless of this host's own architecture, so a bare
`--container ubuntu:24.04` targets `x86_64` even on Apple Silicon (emulated
via Rosetta/QEMU). It is also repeatable, to evaluate more than one
architecture in a single run:

```bash
bigfix-remote-client-relevance \
  --container ubuntu:24.04 --arch amd64 --arch arm64 "name of operating system"
```

Ubuntu and Debian have no native arm64 BigFix client, so the `arm64` run above
uses the raspbian armhf (32-bit ARM) build under the host's arm64 kernel's
32-bit compat — it works generically on Debian, and mostly on Ubuntu with
some rough edges, which still beats having no arm64 option at all. RHEL's
arm64 client ships under an Amazon Linux-named filename (officially
supported only there, but it's a plain rpm that runs on any rhel-family
arm64 host), which this tool resolves transparently for `--container
amazonlinux:2023 --arch arm64` and any other rhel-family image.

Running that armhf build at all — even on a native arm64 host, since armhf
(32-bit ARM) is a different architecture from arm64 (64-bit) — needs
QEMU/binfmt_misc support for it registered with the container engine.
`--auto-setup` installs the missing 32-bit C library *inside* the image, but
the engine itself still needs to know how to execute a 32-bit ARM binary at
all. Check what's registered, and install the rest, with:

```bash
docker run --privileged --rm tonistiigi/binfmt          # lists registered platforms
docker run --privileged --rm tonistiigi/binfmt --install all
```

Without this, exec calls into the container can fail with a raw Docker API
error (e.g. a 500) rather than a qna-level message this tool can parse.

Hardware inspectors that read SMBIOS/DMI — `number of processors` among them —
cannot answer in a container whose *host* kernel exposes no DMI tables. A
container shares the host's `/sys`, so this has nothing to do with running as
root and nothing to do with the emulated architecture: an Apple Silicon
container VM boots from a device tree and has no SMBIOS at all, and neither
`--privileged` nor an x86_64 image conjures one. Evaluate that relevance on an
x86_64 Linux host, or over SSH against real hardware.

`--engine` picks the container engine: `auto` (default, prefers Docker and
falls back to podman only if Docker is unreachable), `docker`, or `podman`.

## Requirements

- Python 3.11+
- Docker or podman for `--container`; SSH access for remote hosts
- On macOS, `qna` needs root — `--local` implies `--become` there automatically
  (pass `--no-become` to opt out); over SSH it stays opt-in, since the remote
  platform isn't known up front. `--become` uses `sudo -n`, so it needs
  passwordless sudo or a cached credential; it never prompts.

SSH host keys are verified against `~/.ssh/known_hosts` like the `ssh` CLI,
so a brand-new endpoint needs its key trusted first. For throwaway lab hosts,
`--insecure-skip-host-key-check` skips that at the cost of the connection's
protection against interception.

Windows endpoints need OpenSSH server and nothing else — commands are invoked
through `powershell.exe` explicitly, so the stock `cmd.exe` default shell works
and no registry change is required.

## Development

```bash
uv sync
uv run pytest
```

The unit suite runs offline on a bare machine. Tests needing a real qna
binary, Docker, sshd, or the network are marked and auto-skip — see
[DESIGN.md § Testing](DESIGN.md#testing).

## Prior art

- [`jgstew/EvaluateRelevance`](https://github.com/jgstew/EvaluateRelevance) —
  the local qna wrapper and output parser this ports.
- [`jgstew/tools`](https://github.com/jgstew/tools) — the canonical
  `bash/bigfix_run_qna_*.sh` and `CMD/bigfix_run_qna_win.bat` bootstrap
  scripts, ported here into `bootstrap/` with the pinned version turned into a
  parameter and the download moved to the controller.
- [`jgstew/remote_relevance`](https://github.com/jgstew/remote_relevance) —
  an earlier action-deployment approach, superseded by the SSH and container
  transports.

## Demo

![Demo](docs/demos/demo_1.svg)

## License

MIT — see [LICENSE](LICENSE).
