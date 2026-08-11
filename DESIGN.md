# bigfix_remote_client_relevance — remote qna client-relevance eval over SSH + Docker

## Status
Design phase. This document is the seed for the package's implementation:
naming rules, package layout, transport contracts, CLI surface, packaging
choices, and MCP-readiness. Implementation lands incrementally against the
todos in the design (see § Todos).

## Naming convention (project-wide rule)
BigFix has two distinct dialects that share a family name:
- **Client relevance** — evaluated on an endpoint by `qna` / the BES client.
  What this project deals with.
- **Session relevance** — evaluated on the BigFix root server against its
  bes-* object model, via the REST `/api/query` endpoint. **Out of scope
  here.**

Because the two are easy to confuse for both humans and AI agents, this
project uses the phrase **"client relevance"** — never bare "relevance" —
everywhere it can control the wording:
- Repo name and Python package: `bigfix_remote_client_relevance`.
  Distribution name on PyPI and CLI entry point: `bigfix-remote-client-relevance`
  (hyphenated per PyPA convention).
- Public functions, dataclasses, fields, CLI flags, docs, log messages,
  error strings, MCP tool name (future), README headings.
- Examples: `evaluate_client_relevance()`, `ClientRelevanceResult`,
  `client_relevance: str`, `--client-relevance-file`, `client_relevance.rel`
  file extension (kept because `.rel` is the community convention, but
  variables that hold the *content* are `client_relevance`, not
  `relevance`).

**Exception:** when interoperating with an external API, CLI, or file
format whose contract already says "relevance" (e.g. `qna` accepts what it
calls a relevance statement; BigFix REST calls it `Relevance`; the
`.rel`/`.qna` file extensions), we use the wire term at the boundary and
translate on both sides. Every such boundary is called out with a comment
like:

```python
# API boundary: qna's CLI vocabulary uses "relevance"; internal name stays
# `client_relevance`.
qna_process.stdin.write(client_relevance + "\n")
```

Prior-art code we port (`jgstew/EvaluateRelevance/evaluate_relevance.py`,
`bigfix_run_qna_*.sh`) is *renamed* on the way in, not preserved verbatim,
so nothing internal reintroduces bare "relevance".

## Problem
Iterate on BigFix **client relevance** against real endpoints (Mac +
Windows primarily, plus the Linux family the `tools/bash/` scripts already
cover) without the slow action-deployment loop and without requiring the
BES client to be installed. `qna` evaluates client relevance locally — the
missing pieces are (a) a remote-execution transport and (b) a way to run
against *different qna versions* on the same host, not just an installed
one.

## Project home
Repo: **`jgstew/bigfix_remote_client_relevance`**.
- Clean dependency surface (`asyncssh`, `docker` SDK, packaging) not forced
  on `jgstew/tools` consumers.
- Publishable as a standalone pip/uv package on PyPI; future MCP server
  imports it.
- Bootstrap scripts stay canonical in `jgstew/tools/bash` and
  `jgstew/tools/CMD` — this repo *ports* them into Python (renaming
  variables to `client_relevance` on the way in) and references the
  originals from the README.

## Packaging & Python versions
Files in the repo are the source of truth; this section explains *why*.

- **Build backend:** `hatchling` + `hatch-vcs`. Pure-Python, PEP 517/518/621
  native, git-tag-driven versioning. See `pyproject.toml`.
- **Primary dev tool:** [`uv`](https://docs.astral.sh/uv/). One tool for
  venvs, Python versions, install, lock, build, publish, and `uvx`
  no-install runs. Consumers can still `pip install
  bigfix-remote-client-relevance` from PyPI — `uv` writes standard
  artifacts.
- **Interpreter support floor:** Python `>=3.10` (declared in
  `pyproject.toml` `[project] requires-python`). Enough for `str | None`,
  `match`, PEP 604; `tomli` covers 3.10's lack of `tomllib`.
- **Primary dev/test target:** Python `3.12` (declared in
  `.python-version`, single line, no patch pin). `uv sync` / `uv run`
  auto-fetch it via `[tool.uv] python-preference = "managed"` in
  `pyproject.toml` — no external Python setup required.
- **CI matrix:** 3.10–3.13 on Ubuntu, plus 3.12 on macOS and Windows.
  Uses `astral-sh/setup-uv@v3` + `uv sync --frozen --python
  ${{ matrix.python-version }}` + `uv run pytest`. No
  `actions/setup-python` needed.

Quick start (README-worthy):
```bash
# ephemeral, no install
uvx bigfix-remote-client-relevance --container ubuntu:22.04 "name of operating system"

# or install it
uv tool install bigfix-remote-client-relevance
# or, pip world
pip install bigfix-remote-client-relevance
```

## Existing pieces in `jgstew/tools` we build on
- `bash/bigfix_run_qna_*.sh` and `CMD/bigfix_run_qna_win.bat` — per-OS
  bootstrap scripts that download a pinned qna/BESAgent version to
  `/tmp/bigfix_qna` or `\Windows\Temp\bigfix_qna`, extract it, and run it.
  Give us "run qna without installing BigFix" and "pin a qna version" for
  free.
- `AppleScript/openQnA.scpt`, `bash/openQnA.sh`, `CMD/EvalRelevance.bat` —
  local invocation shims; reference for arg/stdin handling.

## Existing pieces from other repos to port / reuse
- `jgstew/EvaluateRelevance/evaluate_relevance.py` — pieces to lift:
  - `get_path_qna()` search list (Mac/Linux/Windows) → renamed to
    `find_qna_path()` in `qna_paths.py`.
  - `parse_raw_result_array()` → renamed `parse_qna_output()`, moved to
    `results.py`. Returned strings represent client-relevance answers.
  - `evaluate_relevance_raw_stdin()` → renamed
    `evaluate_client_relevance_local()`, kept as `LocalTransport`'s
    implementation.
  - `E:` / `T:` line handling preserved and extended.
- `jgstew/remote_relevance` (old action-based prototype) — **not** the
  model we follow. Slow feedback loop, wrong tool for dev-time work. Cited
  in the README as prior art, superseded.

## Recommendation: Python library + CLI, SSH-first, MCP-ready

### Why Python
- Ecosystem match with `jgstew/tools` and `EvaluateRelevance`.
- `asyncio` + `asyncssh` gives real concurrent multi-host eval.
- The qna output parser and transport interface are unit-testable against
  captured fixtures without a live endpoint.
- A future MCP tool is a thin wrapper around a typed Python function.

### Why SSH + Docker as the two primary transports (Fast Query later)
- **SSH** is built into macOS (Remote Login) and Windows 10/11 + Server
  2019+ (OpenSSH Server capability). No third-party agent, no BigFix action
  round-trip. Sub-second edit → evaluate loop against real Mac / Windows /
  Linux endpoints you actually own.
- **Docker (TransportContainer)** covers the on-demand Linux long tail
  without SSH creds or a persistent remote host — pull image, run qna,
  parse output. Complements SSH on the platforms where containers are
  cheap; SSH stays the answer for macOS and Windows, which don't run
  meaningfully in containers.
- **BigFix Client Fast Query** is a legitimate third transport (server
  pushes a client-relevance query out to endpoints via relays) worth
  designing for, but it needs a live BigFix deployment, only runs against
  the *installed* client's qna version, and gates on operator permissions.
  SSH and Docker don't. The `Transport` interface is designed so Fast
  Query slots in later without changing callers.
- Alternatives rejected: action deploy (too slow), WinRM (Windows-only),
  Ansible (declarative, wrong shape), REST `/api/query` (session relevance,
  wrong dialect), Fixlet Debugger (GUI-only), custom sidecar agent
  (reinvents sshd).

## Proposed shape

### Library layout (`src/bigfix_remote_client_relevance/`)
Uses the src-layout (matches `pyproject.toml` `[tool.hatch.build.targets.wheel]`).

```
src/bigfix_remote_client_relevance/
  __init__.py                # re-exports evaluate_client_relevance,
                             # ClientRelevanceResult, TransportLocal,
                             # TransportSSH, TransportContainer,
                             # TransportFastQuery
  results.py                 # ClientRelevanceResult dataclass +
                             # parse_qna_output()
  qna_paths.py               # find_qna_path() + per-OS candidate lists
  transports/
    __init__.py              # Transport ABC / Protocol
    local.py                 # TransportLocal    (subprocess)
    ssh.py                   # TransportSSH      (asyncssh) — primary remote
    container.py             # TransportContainer(Docker) — primary on-demand
    fastquery.py             # TransportFastQuery(BigFix Fast Query) — stub
  bootstrap/
    __init__.py              # "provision qna without installing BigFix"
    macos.py                 # port of bash/bigfix_run_qna_macos.sh
    windows.py               # port of CMD/bigfix_run_qna_win.bat
    linux.py                 # port of debian/ubuntu variant to start
    container_images.py      # thin catalog of images used by TransportContainer
  cli.py                     # `bigfix-remote-client-relevance` entry
  inventory.py               # optional hosts.toml loader
tests/                       # sibling of src/, not inside the package
```

### Core types
```python
@dataclass
class ClientRelevanceResult:
    host: str                    # "mac-test", "local", or "container:<image>"
    transport: str               # "local" | "ssh" | "container" | "fastquery"
    client_relevance: str        # input expression (internal name)
    answers: list[str]           # parsed A: lines from qna output
    answer_types: list[str]      # parsed T: type lines (per answer)
    error: str | None            # first E: line, if any
    raw_qna_output: str          # full qna stdout, for debugging / agents
    qna_path: str                # binary used on the remote / in container
    qna_version: str | None      # parsed from `qna -version` when available
    elapsed_ms: int              # measured caller-side
    exit_code: int
```

### Transport interface
```python
class Transport(Protocol):
    async def evaluate_client_relevance(
        self,
        client_relevance: str,
        *,
        qna_path: str | None = None,     # None => discover on target
        qna_version: str | None = None,  # None => use whatever is present;
                                         # else bootstrap that version
        timeout_s: float = 30.0,
    ) -> ClientRelevanceResult: ...
```

Four concrete transports, all class-named `Transport<Kind>`:

- `TransportLocal()` — subprocess against a local `qna` binary. Same code
  path as `EvaluateRelevance` today, renamed. Used for tests and for AI
  agents doing a fast syntax check on the developer's own box before an
  SSH/container round-trip.
- `TransportSSH(host, user=..., key=..., become=False)` — asyncssh-based.
  Pipes the client-relevance string to `qna -t -showtypes` on the remote.
  When `qna_version` is set and that version isn't cached on the remote,
  runs the matching bootstrap module to fetch/extract it into
  `/tmp/bigfix_qna/<version>/` or `\Windows\Temp\bigfix_qna\<version>\`.
  Primary transport for real Mac / Windows / Linux endpoints.
- `TransportContainer(image, engine="docker", qna_version=None,
  keep_alive=False)` — **on-demand eval via Docker (or another OCI engine)
  without SSH creds or a persistent remote host.** Runs `qna` inside a
  short-lived container of `image`; pipes client-relevance to the
  container's stdin. Primary transport for "answer this on
  Ubuntu/Debian/RHEL/Amazon-Linux/etc. right now, no reservation required."
  Design notes below.
- `TransportFastQuery(besapi_client, computer_query=...)` — stub only. Nails
  down the constructor signature so future work does not break callers.
  Documents that the BigFix REST payload uses the key `Relevance` as an
  API-vocabulary exception to the naming rule.

### TransportContainer design
- **Why it matters:** SSH covers Mac + Windows + whatever real Linux boxes
  you own, but for the long tail of "does this client relevance answer
  correctly on Ubuntu 22.04 / RHEL 9 / Amazon Linux 2023" you don't want
  to keep a live VM per distro. Docker gives on-demand answers with zero
  SSH setup and works from the same laptop that drives the CLI.
- **Complements SSH, not replaces it.** Docker can't run macOS or Windows
  client-relevance targets in any practical way; that's why SSH stays the
  primary remote transport for those platforms. TransportContainer is the
  primary transport for on-demand Linux (and, later, arch-emulated)
  targets.
- **Reuses existing `jgstew/tools/docker/Dockerfiles`** (`bigfix_centos`,
  `bigfix_ubuntu`) as the seed image catalog; extend as needed. Also
  aligns with the `.github/workflows/run_qna*.yaml` CI, which already
  proves qna-in-a-container works for the family.
- **Engine abstraction:** default `engine="docker"`, but talk to it via
  the `docker` Python SDK (or plain `docker` CLI subprocess as a fallback)
  behind a small `ContainerEngine` interface so `podman` slots in later.
- **Lifecycle:**
  1. Ensure the image exists locally (pull if missing).
  2. If `qna_version` is set and the image doesn't already have that
     version baked in, run the ported bootstrap steps inside the container
     to fetch/extract that qna version into `/tmp/bigfix_qna/<version>/`
     (same convention as SSH, so caching semantics match).
  3. `docker run --rm -i <image> qna -t -showtypes` with the
     client-relevance string on stdin. Capture stdout/stderr + exit code.
  4. `keep_alive=True` reuses a long-lived container (via `docker exec`)
     for hot repeat evals against the same image; default is one-shot for
     hermetic answers.
- **Arch coverage:** on Docker Desktop / Colima with QEMU, `--platform
  linux/arm64` (or `linux/amd64`) lets one host answer for both arches,
  which matches the `run_qna_arm64.yaml` and `run_qna_qemu.yaml`
  workflows already in `jgstew/tools`.
- **Result `host` field:** `"container:<image>@<arch>"` so an agent /
  human can tell which image produced an answer.
- **Not-goals for the first cut:** Windows containers (host-OS coupled),
  rootless-podman quirks, image publication. Note them as follow-ups.

### CLI surface
The entry point is `bigfix-remote-client-relevance` (hyphenated —
matches `[project.scripts]` in `pyproject.toml`; the Python package it
resolves to is `bigfix_remote_client_relevance.cli:main`).

```
bigfix-remote-client-relevance HOST "name of operating system"
bigfix-remote-client-relevance HOST --client-relevance-file probe.rel
bigfix-remote-client-relevance --all hosts.toml \
    --client-relevance-file probe.rel --json
bigfix-remote-client-relevance HOST --qna-version 11.0.4.60 "..."
bigfix-remote-client-relevance --local "..."
bigfix-remote-client-relevance --container ubuntu:22.04 "..."
bigfix-remote-client-relevance --container bigfix_centos --qna-version 11.0.4.60 -f probe.rel
```

- Short alias `-f` is fine for `--client-relevance-file`.
- `--container IMAGE` picks `TransportContainer`; `HOST` selects
  `TransportSSH`; `--local` picks `TransportLocal`. Exactly one of the
  three is required per invocation (or `--all` for a mixed grid from
  `hosts.toml`).
- `--json` emits one `ClientRelevanceResult` per target — same shape the
  future MCP tool will return.
- Exit code: 0 iff every target produced ≥1 answer and no `E:` line.
  Useful as a CI syntax gate for content authoring.

### MCP-ready contract (server itself out of scope)
- The whole surface a future MCP tool needs is one pure async function:
  `evaluate_client_relevance(client_relevance, host?, os_family?,
  qna_version?) -> ClientRelevanceResult`.
- MCP tool name will be `eval_client_relevance` (never
  `eval_relevance`), matching the naming rule.
- `raw_qna_output` + `error` + `qna_version` in the result let an agent
  self-correct on syntax errors and know which OS/version the answer came
  from.

## Setup guide (packaged as README)

### 1. `qna` on the target — three provisioning options
1. Already-installed BES client (SSH target):
   - macOS: `/Library/BESAgent/BESAgent.app/Contents/MacOS/QnA`
   - Windows: `C:\Program Files (x86)\BigFix Enterprise\BES Client\QnA.exe`
   - Linux: `/opt/BESClient/bin/qna`
2. Bootstrapped standalone via the ported `bootstrap/` modules — pinned
   qna version, no BES install. Works over SSH **and** inside containers.
3. Pre-staged — drop a `qna` binary anywhere and pass `--qna-path`.
   Includes container images that bake qna in at build time (see
   `tools/docker/Dockerfiles/bigfix_centos` and `bigfix_ubuntu`).

Discovery order matches the ported `find_qna_path()` plus `$PATH`.

### 2. Enable SSH server
- **macOS:** System Settings → General → Sharing → *Remote Login*, or
  `sudo systemsetup -setremotelogin on`.
- **Windows:**
  `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`,
  `Start-Service sshd; Set-Service sshd -StartupType Automatic`, open
  TCP/22, and set the default shell to PowerShell for predictable
  stdin/UTF-8:
  `New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force`.

### 3. Key-based auth
- One ed25519 key on the workstation.
- Push with `ssh-copy-id` on Mac; on Windows append to
  `C:\Users\<user>\.ssh\authorized_keys`, or
  `C:\ProgramData\ssh\administrators_authorized_keys` for admin accounts
  (Windows OpenSSH treats admins specially — miss this and admin logins
  silently ignore per-user keys).
- Named entries in `~/.ssh/config` so the CLI can just say
  `bigfix-remote-client-relevance mac-test ...`.

### 4. Permissions & gotchas
- On macOS, `qna` needs root for some inspectors —
  `SSHTransport(become=True)` uses sudo.
- Windows OpenSSH defaults to `cmd.exe`; switch the default shell to
  PowerShell (step 2) for reliable UTF-8 stdin.
- Save client-relevance files as UTF-8, no BOM.
- **Client relevance ≠ session relevance.** This tool is only for the
  client dialect; session relevance stays with the BigFix REST/`besapi`
  path.
- `qna -showtypes` emits `T:` type lines — the parser retains them on the
  result so agents can distinguish string / time / plural answers.

## Out of scope (for this task)
- The MCP server itself (design keeps the surface compatible).
- Full `TransportFastQuery` implementation (stub only).
- Windows containers, rootless-podman quirks, and publishing our own
  qna-preloaded images to a registry (noted for `TransportContainer`
  follow-ups).
- AIX / Solaris / ppc64le / s390x bootstraps (start with macOS + Windows +
  Debian/Ubuntu; the rest are mechanical follow-ups against the existing
  shell scripts).
- Shared-team inventory: jump hosts, cert-based SSH, secrets management.

## Todos
Tracked in the SQL `todos` table.

---

## Related projects

Short summaries + usefulness assessment for everything this design pulls from
or intentionally departs from.

### `jgstew/EvaluateRelevance`
<https://github.com/jgstew/EvaluateRelevance> — specifically
[`evaluate_relevance.py`](https://github.com/jgstew/EvaluateRelevance/blob/main/evaluate_relevance.py).

Local-only Python that shells out to a discovered `qna` binary and parses
its output. Ships `get_path_qna()`, `parse_raw_result_array()`, and
`evaluate_relevance_raw_stdin()`, plus the macOS "must be root" check.

**Usefulness: very high.** Direct source of `TransportLocal`, the qna
output parser (`parse_qna_output`), and the qna path candidate list
(`find_qna_path`). Renamed on the way in per the naming rule; no functional
rewrite needed.

### `jgstew/tools` — bootstrap scripts
<https://github.com/jgstew/tools>. Relevant files:

- [`bash/bigfix_run_qna_macos.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_macos.sh)
- [`bash/bigfix_run_qna_debian.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_debian.sh)
- [`bash/bigfix_run_qna_ubuntu.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_ubuntu.sh)
- [`bash/bigfix_run_qna_rhel_family.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_rhel_family.sh)
- [`bash/bigfix_run_qna_suse_family.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_suse_family.sh)
- [`bash/bigfix_run_qna_amazonlinux_aarch64.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_amazonlinux_aarch64.sh)
- [`bash/bigfix_run_qna_raspbian.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_raspbian.sh)
- [`bash/bigfix_run_qna_aix.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_aix.sh)
- [`bash/bigfix_run_qna_solaris.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_solaris.sh)
- [`bash/bigfix_run_qna_rhel_ppc64le.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_rhel_ppc64le.sh)
- [`bash/bigfix_run_qna_rhel_s390x.sh`](https://github.com/jgstew/tools/blob/master/bash/bigfix_run_qna_rhel_s390x.sh)
- [`CMD/bigfix_run_qna_win.bat`](https://github.com/jgstew/tools/blob/master/CMD/bigfix_run_qna_win.bat)

Per-OS scripts that download a pinned qna/BESAgent version, extract it
into `/tmp/bigfix_qna` (or `\Windows\Temp\bigfix_qna`), and run it. Solve
"eval qna against a specific version without installing BigFix."

**Usefulness: high — canonical source.** Get ported into `bootstrap/` on
the way in (macOS + Windows + Debian/Ubuntu first cut), with the pinned
version turned into a parameter. The originals stay canonical in
`jgstew/tools`; this repo does not fork them.

### `jgstew/tools/.github/workflows/run_qna.yaml`
<https://github.com/jgstew/tools/blob/master/.github/workflows/run_qna.yaml>

GitHub Actions workflow that runs the bootstrap scripts across a matrix
(`macos-latest`, `windows-latest`, `windows-2025`, `ubuntu-24.04`,
`ubuntu-22.04`) on push/PR to the qna bootstrap files or on
`workflow_dispatch` with an arbitrary client-relevance input. Parses
`A: / E: / I: / T:` lines out of qna output and renders them into
`$GITHUB_STEP_SUMMARY` as headed sections. Companion workflows extend the
matrix to arm64, QEMU-emulated s390x/ppc64le, Solaris, and Raspbian.

**Usefulness: very high, in three ways.**
1. **Validates the whole approach.** It's the same pattern this project
   proposes (bootstrap qna + pipe expression on stdin + parse `A:/E:/T:`
   lines) already working in CI across five OS/arch runners.
2. **Direct blueprint for `TransportContainer`.** Ubuntu / RHEL-family
   runners map straight onto Docker images; the ported bootstrap modules
   plus `parse_qna_output` reproduce what the workflow does today, on
   demand, from a developer laptop instead of a hosted runner.
3. **Reference for the `--json` result shape.** The workflow already
   splits qna output into Errors / Result Type / Time Taken / Answers —
   `ClientRelevanceResult`'s `error`, `answer_types`, `elapsed_ms`, and
   `answers` fields mirror that shape 1:1, so results stay familiar to
   anyone who reads the workflow's step summaries.

Companion workflows worth citing in the README next to this one:
[`run_qna_arm64.yaml`](https://github.com/jgstew/tools/blob/master/.github/workflows/run_qna_arm64.yaml),
[`run_qna_qemu.yaml`](https://github.com/jgstew/tools/blob/master/.github/workflows/run_qna_qemu.yaml),
[`run_qna_raspbian.yaml`](https://github.com/jgstew/tools/blob/master/.github/workflows/run_qna_raspbian.yaml),
[`run_qna_solaris.yaml`](https://github.com/jgstew/tools/blob/master/.github/workflows/run_qna_solaris.yaml).

### `jgstew/tools/docker/Dockerfiles`
<https://github.com/jgstew/tools/tree/master/docker/Dockerfiles> — includes
`bigfix_centos` and `bigfix_ubuntu`.

Container images that install (or can install) a BigFix client / qna into
CentOS and Ubuntu bases.

**Usefulness: high.** Seed catalog for `TransportContainer`; referenced
from `bootstrap/container_images.py`. Removes the need for this project to
ship its own Dockerfiles in the first cut.

### `jgstew/remote_relevance`
<https://github.com/jgstew/remote_relevance/tree/master/python>

Prototype that deploys BigFix custom actions to run qna on target
endpoints and reports results back. Requires editing `BES_CONFIG.py` and
returns results only on the server console; the roadmap called for
WebSockets to a web app.

**Usefulness: low, as a code source; high, as a "do not repeat this"
marker.** Confirms the problem is real and long-standing, but the
action-deploy loop is the wrong shape for iterative content authoring
(minutes per iteration, no clean stdout, gated on operator perms). Called
out in the README as prior art, superseded by SSH + Container transports.

### BigFix qna itself (external tool, API-vocabulary exception)
Ships with the BES client (paths above) and is downloadable standalone
from BigFix's own site (e.g. `QNA11.0.4.60.zip` for Windows, `BESAgent-*.pkg`
for macOS) — the URLs the bootstrap scripts already use.

**Usefulness: essential; it is the evaluator.** Its CLI vocabulary uses
"relevance"; the naming rule accepts that at this boundary and translates
to `client_relevance` on the caller side.

### BigFix REST API (external tool, API-vocabulary exception)
Referenced only for `TransportFastQuery` (stub) and to disambiguate from
`/api/query`. REST payload key is `Relevance`. **Usefulness: low for this
task; kept in the interface so it doesn't reshape callers when Fast Query
lands.**
