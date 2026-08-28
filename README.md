# bigfix_remote_client_relevance

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

Evaluate on a real endpoint over SSH (a `~/.ssh/config` alias works):

```bash
bigfix-remote-client-relevance mac-test --become "name of operating system"
```

Fan out across an inventory and emit JSON:

```bash
bigfix-remote-client-relevance --inventory hosts.toml -f probe.rel --json
```

`--json` writes one document per (target × version) to stdout; logs go to
stderr, so piping into `jq` always works.

### Exit codes

Actionable for CI gating; the worst across the fan-out wins.

| Code | Meaning |
|---|---|
| 0 | every target evaluated without error |
| 1 | a client-relevance error (qna emitted an `E:` line) |
| 2 | qna failed, or provisioning it did |
| 3 | a transport failure — connect, auth, or timeout |
| 4 | a qna version spec could not be resolved |

### Inventory

```toml
# hosts.toml
[defaults]
qna_version = "11.0"        # version spec; overridable per host

[hosts.mac-test]            # table name is the ~/.ssh/config alias
transport = "ssh"
become = true               # sudo for root-only inspectors

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

One `ClientRelevanceResult` comes back per (target × version), carrying
`answers`, `answer_types`, `error` / `error_kind`, the resolved `qna_version`,
and the full `raw_qna_output` for debugging. Failures are reported inside
results rather than raised, so one unreachable host never derails a fan-out.

The library logs through `logging` and never writes to stdout — that channel
belongs to the CLI's payload, and later to a stdio MCP server's JSON-RPC.

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

## Requirements

- Python 3.11+
- Docker for `--container`; SSH access for remote hosts
- On macOS, `qna` needs root — use `sudo`, or `--become` over SSH

SSH host keys are verified against `~/.ssh/known_hosts` like the `ssh` CLI,
so a brand-new endpoint needs its key trusted first. For throwaway lab hosts,
`--insecure-skip-host-key-check` skips that at the cost of the connection's
protection against interception.

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

## License

MIT — see [LICENSE](LICENSE).
