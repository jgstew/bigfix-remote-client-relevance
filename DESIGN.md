# bigfix-remote-client-relevance — remote qna client-relevance eval over SSH + Docker

## Status
**Milestones M1–M5 are implemented** (parser + local eval, version
resolution + artifact cache, SSH, containers, orchestration + CLI). This
document remains the specification; where implementation proved a detail
wrong, the document has been corrected and the correction noted inline.

Verified end to end: `--container ubuntu:22.04 --qna-version 11.0
--qna-version 10.0 "version of client"` resolves both streams against the
live release site, downloads and checksum-verifies each agent package,
extracts them inside a container, and answers `11.0.6.137` and
`10.0.16.61` from one command.

Corrections implementation forced, all applied below:
1. `answer_types` comes from qna's `I:` lines, not `T:` — `T:` is time
   taken. A `qna_time` field was added rather than discarding it.
2. Exit code `0` means *no error*, not *at least one answer*. A plural
   inspector that legitimately matches nothing must not fail a CI gate.
3. `TransportLocal` refuses to run as non-root on macOS by default:
   BESAgent 11.x aborts there with an uncaught `FileIOError` even for
   `TRUE`, so a clear pre-flight message beats an opaque crash dump.
   `require_root_on_macos=False` waives it, and `become=True` makes the
   check moot by running qna under `sudo -n` instead.
4. Class names in the prose now follow the document's own `Transport<Kind>`
   rule.

Remaining: rpm-family and SUSE bootstraps are specified but only the
Debian/Ubuntu and macOS extract paths are exercised; Fast Query is a stub;
the MCP server is a separate task.

## Testing
`uv run pytest` runs the offline unit suite on a bare machine. Tests
needing a real prerequisite carry a marker and auto-skip without it:

| Marker | Needs | Run it with |
|---|---|---|
| `live_qna` | a local qna binary; **root on macOS** | `sudo -E uv run pytest -m live_qna` |
| `docker` | a reachable Docker daemon | `uv run pytest -m docker` |
| `ssh_localhost` | sshd on localhost with key auth | `uv run pytest -m ssh_localhost` |
| `network` | support.bigfix.com | `BFRCR_NETWORK_TESTS=1 uv run pytest -m network` |

Release-site fixtures under `tests/fixtures/release_site/` are captured
from the live site and trimmed to `#main-content`, so the scraper is
tested against the real markup — including the meaningless `section-N`
heading ids, interleaved Utilities tables, and the Patch 5 row where the
Agent column (`11.0.5.204`) differs from every other column
(`11.0.5.203`).

## Development methodology: Test Driven Development
Use `pytest`. Write tests *before* implementation, observe them fail,
then implement the code and observe them pass. A test is only valid if
it has been observed changing state (fail → pass) due to code changes at
least once — a test that has never been seen failing proves nothing.

This pairs with the design's testability choices: the qna output parser,
the release-site scraper, and every `Transport` are specified to work
against captured fixtures (qna output samples, saved HTML) so the
red → green loop needs no live endpoint, network, or Docker daemon for
unit-level work.

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
- Python package (import name): `bigfix_remote_client_relevance` (underscored;
  Python identifiers can't contain hyphens). Repo name, PyPI distribution
  name, and CLI entry point: `bigfix-remote-client-relevance` (hyphenated per
  PyPA convention).
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
# A leading "Q: " is stripped: qna's file mode requires that prefix and its
# stdin mode rejects it. A trailing newline terminates the question.
qna_process.stdin.write(client_relevance + "\n")
```

Prior-art code we port (`jgstew/EvaluateRelevance/evaluate_relevance.py`,
`bigfix_run_qna_*.sh`) is *renamed* on the way in, not preserved verbatim,
so nothing internal reintroduces bare "relevance".

## Output convention (project-wide rule): `logging`, never `print`

**Rule:** no `print()` anywhere in the library — not in transports, not in
`bootstrap/`, not in `orchestrate.py`, not even temporarily during
debugging. Every diagnostic, progress, and debug message goes through the
stdlib `logging` module.

**Why:** stdout is a reserved channel. A future **stdio MCP server**
speaks JSON-RPC over stdout, and a single stray `print` — or a library
that helpfully echoes qna output — corrupts the protocol stream and
produces a confusing, hard-to-trace client-side parse failure. Keeping
stdout clean from the first commit costs nothing; retrofitting it later
means auditing every module for writes it shouldn't be making. The same
discipline is what makes the CLI safe to pipe into `jq` today.

**Library convention:**
- Module-level `logger = logging.getLogger(__name__)` in each module.
- `__init__.py` attaches a `logging.NullHandler()` to the package's root
  logger. The library never calls `basicConfig()`, never adds a real
  handler, never sets a level, and never touches the root logger —
  configuration belongs to the embedding application (CLI, MCP server,
  or a consumer importing us as a dependency).
- Level guidance: `DEBUG` for the qna command line, resolved paths, cache
  hits/misses; `INFO` for phase transitions (resolve → fetch → push →
  extract → run); `WARNING` for degraded-but-continuing behavior (e.g.
  `-showtypes` unsupported on an old qna, falling back to plain `-t`);
  `ERROR` only where the failure isn't already carried back to the caller
  in `ClientRelevanceResult.error` / `error_kind`. Errors that *are*
  carried in the result are the caller's to report — the library
  shouldn't log and return the same failure twice.

**`cli.py` is the one stdout writer.** It configures logging on startup —
a `StreamHandler(sys.stderr)`, with `-v`/`-vv` mapping to `INFO`/`DEBUG`
— and writes **only the final result payload** (the `--json` documents,
or the plain-text answers) to stdout. Nothing else in the process writes
to stdout. This is exactly the split the stdio MCP server needs later:
swap the CLI's stdout writer for the JSON-RPC transport and every
existing log call is already pointed somewhere safe.

**`raw_qna_output` is a result field, not output.** The library captures
qna's stdout/stderr into `ClientRelevanceResult.raw_qna_output` and
returns it. It never relays it to the process's own stdout; a caller that
wants to see it prints it themselves.

**Ported code:** any `print` in code lifted from
`jgstew/EvaluateRelevance` or the `bigfix_run_qna_*` scripts is converted
to `logger.*` on the way in — the same treatment the `relevance` →
`client_relevance` rename gets. Nothing is preserved verbatim.

**Testability:** this rule is testable, so test it (see § Development
methodology). Assert on log records with pytest's `caplog` where a
message is part of the contract, and assert `capsys.readouterr().out ==
""` around library-level calls so a reintroduced `print` fails a test
rather than surfacing as a broken MCP session months later. A ruff rule
banning `print` in `src/` (flake8-print, `T20`) is the cheap static
backstop, with `cli.py` as the single documented exemption.

## Problem
Iterate on BigFix **client relevance** against real endpoints (Mac +
Windows primarily, plus the Linux family the `tools/bash/` scripts already
cover) without the slow action-deployment loop and without requiring the
BES client to be installed. `qna` evaluates client relevance locally — the
missing pieces are (a) a remote-execution transport and (b) a way to run
against *different qna versions* on the same host, not just an installed
one.

## Project home
Repo: **`jgstew/bigfix-remote-client-relevance`**.
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

- **Build backend:** `hatchling`. Pure-Python, PEP 517/518/621 native.
  `version` is a static field in `pyproject.toml` `[project]`, bumped by
  hand as part of cutting a release.
- **Primary dev tool:** [`uv`](https://docs.astral.sh/uv/). One tool for
  venvs, Python versions, install, lock, build, publish, and `uvx`
  no-install runs. Consumers can still `pip install
  bigfix-remote-client-relevance` from PyPI — `uv` writes standard
  artifacts.
- **Interpreter support floor:** Python `>=3.11` (declared in
  `pyproject.toml` `[project] requires-python`). Buys three things the
  design leans on: stdlib `tomllib` (so `inventory.py` reads
  `remote_clients.toml` with no `tomli` shim and zero conditional dependency),
  `asyncio.TaskGroup` + `ExceptionGroup` / `except*` (the natural shape
  for `orchestrate.py`'s targets × versions fan-out — structured
  cancellation and *all* per-target failures surfaced together rather
  than the first one winning), and `typing.Self`. PEP 604 `str | None`
  and `match` come along from 3.10.
- **Primary dev/test target:** Python `3.12` (declared in
  `.python-version`, single line, no patch pin). `uv sync` / `uv run`
  auto-fetch it via `[tool.uv] python-preference = "managed"` in
  `pyproject.toml` — no external Python setup required.
- **CI matrix:** 3.11–3.13 on Ubuntu, plus 3.12 on macOS and Windows.
  Uses `astral-sh/setup-uv@v3` + `uv sync --frozen --python
  ${{ matrix.python-version }}` + `uv run pytest`. No
  `actions/setup-python` needed.
- **Runtime dependencies:** `asyncssh` (SSH), `docker` (containers),
  `typer` (CLI), plus `requests`, `beautifulsoup4` and `platformdirs` added
  when the release-site scraper and controller cache landed. `tomllib` is
  stdlib at the 3.11 floor, so the inventory loader needs no TOML package.
- **Dependency quarantine — 7 days** (`[tool.uv] exclude-newer = "-P7D"`).
  Resolution ignores any distribution uploaded in the last week. The
  window is a supply-chain measure: the common failure modes of a fresh
  PyPI release — a hijacked maintainer account, a malicious post-install
  payload, a release yanked hours later as broken — are usually caught
  and pulled within days, and waiting a week means this project's builds
  never pick those up in the first place. The cost is that legitimate new
  releases land here a week late, which for an alpha with four runtime
  dependencies is nothing.

  Mechanics and limits worth knowing:
  - The value is **relative and rolling**, evaluated at resolution time,
    not a pinned timestamp that rots. uv normalizes it into `uv.lock` as
    `exclude-newer-span = "-P7D"` (alongside an inert
    `exclude-newer = "0001-01-01T00:00:00Z"` that exists only for
    backwards compatibility — it is not a real bound, do not "fix" it).
  - It binds **this repo's own resolution only** — dev environments, CI,
    and the lockfile. It is *not* inherited by anyone who
    `pip install bigfix-remote-client-relevance`; their resolver never
    sees it. This is hygiene for our builds, not a guarantee shipped to
    consumers, and the README should not imply otherwise.
  - **The delay must not block a security patch.** When an advisory lands
    against a dependency, take the fix immediately rather than waiting
    out the window: `--exclude-newer-package <name>=<date>` lifts the
    quarantine for one package, and `--exclude-newer` overrides it
    wholesale for a single run. Reach for the per-package form — it keeps
    the window in force for everything else.
  - Because resolution is time-dependent, CI must run `uv sync --frozen`
    (as the matrix above does) so a build resolves from the committed
    lockfile rather than re-resolving against a window that has moved
    since the lock was written.

Quick start (README-worthy):
```bash
# ephemeral, no install
uvx bigfix-remote-client-relevance --container ubuntu:22.04 "name of operating system"

# or install it
uv tool install bigfix-remote-client-relevance
# or, pip world
pip install bigfix-remote-client-relevance
```

## Release flow (planned, not wired yet)
Cut a release in three steps:
1. **Bump the version** — edit `version` in `pyproject.toml` `[project]` by hand (no `hatch-vcs`/tag-derived versioning). [Release Drafter](https://github.com/release-drafter/release-drafter) keeps a draft GitHub Release open, auto-populated from merged PR titles/labels with a proposed next version, to guide the bump and tidy the notes.
2. **Publish** the draft. GitHub creates the `v*` tag, matching the version just committed.
3. **CI does the rest** — a `push: tags: ['v*']` workflow runs `uv build` + `uv publish` (via PyPI Trusted Publishing / OIDC, no long-lived token) and attaches the wheel to the release.

Not implemented yet — added when the project is closer to a first release.

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
    `evaluate_client_relevance_local()`, kept as `TransportLocal`'s
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
    release_site.py          # resolve version specs ("11.0" -> "11.0.6.137")
                             # against support.bigfix.com/bes/release/
    cache.py                 # controller-side artifact cache + checksum
                             # verify + push-to-target (see § qna binary
                             # cache & distribution)
    macos.py                 # port of bash/bigfix_run_qna_macos.sh
    windows.py               # port of CMD/bigfix_run_qna_win.bat
    linux.py                 # port of debian/ubuntu variant to start
    container_images.py      # thin catalog of images used by TransportContainer
  orchestrate.py             # evaluate_client_relevance() entry point:
                             # version-spec resolution, (targets ×
                             # versions) fan-out, max_parallel,
                             # exit-code aggregation. The CLI and the
                             # future MCP tool both sit on this.
  cli.py                     # `bigfix-remote-client-relevance` entry
  inventory.py               # optional remote_clients.toml loader
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
    answer_types: list[str]      # parsed I: result-type lines (per answer)
    qna_time: str | None         # parsed T: line (qna's own timing)
    error: str | None            # human-readable: the first E: line for
                                 # error_kind "relevance", else an
                                 # exception / stderr summary
    error_kind: str | None       # None on success; else "relevance"
                                 # (E: line) | "qna" (nonzero exit /
                                 # unparsable output) | "bootstrap"
                                 # (push/extract/prereq failure) |
                                 # "transport" (connect/auth/timeout) |
                                 # "resolve" (version spec resolution)
    raw_qna_output: str          # full qna stdout, for debugging / agents
    qna_path: str                # binary used on the remote / in container
    qna_version: str | None      # parsed from `qna -version` when available
    elapsed_ms: int              # measured caller-side
    exit_code: int
```

```python
@dataclass
class ResolvedQna:
    version: str                 # full version, e.g. "11.0.6.137" —
                                 # never a spec like "11.0"
    artifact_path: Path          # controller-cache artifact matching the
                                 # target's platform/arch
```

`ResolvedQna` is produced only by the orchestration layer (via
`bootstrap/release_site.py` + `cache.py`) and consumed by transports.
Transports never resolve version specs or touch the network for
artifacts — that keeps every `Transport` offline-testable and gives spec
resolution exactly one owner.

### Transport interface
```python
class Transport(Protocol):
    async def evaluate_client_relevance(
        self,
        client_relevance: str,
        *,
        qna_path: str | None = None,     # None => discover on target
        qna: ResolvedQna | None = None,  # None => use whatever qna is
                                         # present on the target; else a
                                         # fully-resolved version + cached
                                         # artifact, produced upstream by
                                         # the orchestration layer (specs
                                         # like "11.0" never reach a
                                         # transport)
        timeout_s: float = 30.0,
    ) -> ClientRelevanceResult: ...
```

Five concrete transports, all class-named `Transport<Kind>`:

- `TransportLocal(become=False, require_root_on_macos=True)` — subprocess
  against a local `qna` binary. Same code path as `EvaluateRelevance` today,
  renamed. Used for tests and for AI agents doing a fast syntax check on the
  developer's own box before an SSH/container round-trip. As on SSH, `become`
  wraps only the evaluation in `sudo -n`, never provisioning.
- `TransportSSH(host, user=..., key=..., become=False)` — asyncssh-based.
  Pipes the client-relevance string to `qna -t -showtypes` on the remote.
  When a `ResolvedQna` is passed and that version isn't cached on the
  remote, pushes the artifact from the controller cache and extracts it
  into `/tmp/bigfix_qna/<version>/` or `\Windows\Temp\bigfix_qna\<version>\`
  (non-admin Windows SSH users fall back to `%TEMP%\bigfix_qna\<version>\`,
  since `\Windows\Temp` needs elevation; see § qna binary cache &
  distribution — the target never downloads).
  Primary transport for real Mac / Windows / Linux endpoints.
  `resolve_platform()` exposes the same `uname`/`os-release` probe
  `_resolve_spec` runs internally, so — same as `TransportContainer` — an
  unset `platform` is probed *before* the resolver picks an artifact, not
  guessed. Before this existed, an unspecified SSH `platform` resolved to a
  hard-coded `"ubuntu"` fallback, so a real Windows or RHEL-family box got a
  `.deb` pushed to it and failed at extraction — after the correct
  classification had already happened, just too late to matter. The
  `"ubuntu"` fallback in `default_resolver` is now unreachable in the
  normal fan-out (`_one()` always probes first when a spec is set);
  it remains only as a defensive default for a resolver called directly.
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
  API-vocabulary exception to the naming rule. **Version pinning does not
  apply here:** Fast Query evaluates with whatever BES agent is installed
  on each endpoint — there is no way to choose a qna version, so passing
  a `ResolvedQna` to this transport is an error (`error_kind:
  "bootstrap"`), version specs targeting it fail at resolution time in
  `orchestrate.py` with a clear message, and the result's `qna_version`
  reports the endpoint's installed agent version instead. This
  inflexibility is inherent to the transport, not a first-cut
  limitation.
- `TransportOnlineEvaluator(base_url, host=None, session=None,
  max_retries=1)` — speaks the small JSON contract behind BigFix's own
  "Online Evaluator" (<https://developer.bigfix.com/relevance/evaluate/>),
  reverse-engineered from its JS bundle and confirmed live: `POST
  {base_url}/api/relevance/evaluate` with `{"relevance": "<expr>"}`, getting
  back `{"answers": [...], "errors": [...], "time": <int ms>, "type":
  "<string>"}`. The docs text says evaluation runs "on a Linux RHEL system" —
  this is a hosted service that runs (or embeds) `qna` on one fixed box and
  reports the outcome as JSON instead of qna's own `A:`/`E:`/`I:`/`T:` lines,
  so `classify_qna_outcome` (shared with `TransportLocal`) still applies once
  the JSON is reshaped into a `ParsedQnaOutput`. **`base_url` has no
  default** — sending a client-relevance expression to a third-party service
  is an opt-in choice, not an accident, and the endpoint this was
  reverse-engineered from is undocumented, unauthenticated, and unsupported
  (can change or disappear without notice). **Version pinning does not apply
  here either**, for the same structural reason as Fast Query: the
  environment is fixed and remotely managed, so a `ResolvedQna` is refused
  (`error_kind: "bootstrap"`) and a version spec targeting it fails at
  resolution time in `orchestrate.py`, mirroring the Fast Query check byte
  for byte. One difference from Fast Query's per-answer types: this service
  reports a single aggregate `type` for the whole result rather than one per
  answer, so `answer_types` is that one value broadcast across every answer.
  A 502 ("online evaluator is not available") gets one retry, since the
  page's own error handling singles that status out as a known transient
  failure of the backend; every other status, timeout, or connection error is
  reported immediately.

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
- **Engine abstraction:** talk to the engine via the `docker` Python SDK
  behind a small `ContainerEngine` interface. `DockerEngine` and
  `PodmanEngine` (podman's socket is docker-API-compatible, so it subclasses
  `DockerEngine` and only overrides socket discovery and engine-starter
  detection) both implement it. `--engine {auto,docker,podman}` picks between
  them; `auto` (the default) prefers docker and falls back to podman only
  when docker is unreachable, so it is a no-op for anyone not using podman.
- **Lifecycle:**
  1. Ensure the image exists locally (pull if missing).
  2. If `qna_version` is set and the image doesn't already have that
     version baked in, push the artifact from the controller cache
     (mount or `docker cp`) and extract it into
     `/tmp/bigfix_qna/<version>/` (same convention as SSH, so caching
     semantics match; see § qna binary cache & distribution).
  3. `docker run --rm -i <image> qna -t -showtypes` with the
     client-relevance string on stdin. Capture stdout/stderr + exit code.
  4. `keep_alive=True` reuses a long-lived container (via `docker exec`)
     for hot repeat evals against the same image; default is one-shot for
     hermetic answers.
  5. **Every** container carries its own deadline (`DEFAULT_IDLE_TTL_S`,
     120s, always widened to cover the evaluation about to run): PID 1 is a
     POSIX-sh loop watching a deadline file rather than `sleep infinity`, and
     every exec pushes the deadline out. The deadline
     has to live *inside* the container, because the cases it exists for are
     exactly the ones where no host-side timer survives — a `SIGKILL`, a dead
     daemon connection, a lost removal. The `finally` blocks that stop
     transient and build containers stay as the fast path; the deadline is the
     backstop. A prepared-image build gets a longer window than the default,
     since a build that fetches packages legitimately outlives two minutes.
  6. `keep_alive` is scoped to **one process, one batch**: the container is
     reused across the expressions of a batch and stopped when that batch
     finishes. Containers are never shared between processes. Cross-process
     adoption was built and measured (labels, a key hashed over image/platform/
     mounts, a liveness check on adopt) and then removed: on an already-local
     prepared image `docker run` is cheap enough that reuse measured as
     roughly neutral against batching alone, and a shared container cannot
     safely be stopped by whichever process finishes with it first — another
     may be mid-evaluation inside it — so the small win came with a
     reclamation problem that only a short deadline could paper over. Keeping
     the container private to one process makes "whoever kept it puts it down"
     correct again, and leaves the deadline as a pure backstop.
- **Arch coverage:** `--arch` defaults to `x86_64` — the common case for
  BigFix clients, regardless of the controller's own architecture — and is
  repeatable, so `--container ubuntu:24.04 --arch amd64 --arch arm64`
  evaluates both in one run — the concrete case is Docker Desktop on Apple
  Silicon, which runs arm64 natively and amd64 via Rosetta/QEMU emulation
  simultaneously. Matches the `run_qna_arm64.yaml` and `run_qna_qemu.yaml`
  workflows already in `jgstew/tools`.
- **Result `host` field:** `"container:<image>@<arch>"` so an agent /
  human can tell which image produced an answer.
- **Not-goals for the first cut:** Windows containers (host-OS coupled),
  image publication. Note them as follow-ups.

### qna binary cache & distribution (controller-side)
The machine running `bigfix-remote-client-relevance` (the **controller**)
owns downloading qna/BESAgent artifacts and distributing them to targets.
Targets never fetch from the internet themselves.

**Why:** targeting 10 Windows devices with the same version and query must
cost **one** download on the controller, not 10 independent downloads on
the targets (slow, redundant, and assumes every target has outbound
internet — often false for lab/isolated endpoints). The existing
`bigfix_run_qna_*.sh` scripts download *on the target*; the port inverts
that into fetch-on-controller + push.

**Source of truth:** `https://support.bigfix.com/bes/release/`.
- The index lists version streams (11, 10, 9.5, 9.2, …) with patches in
  descending order; each patch links to `{major.minor}/patch{N}/` (e.g.
  `11.0/patch6/`).
- A patch page lists per-OS/arch agent installers named
  `BESAgent-<full-version>-<platform>.<rpm|deb|pkg|exe>` (e.g.
  `BESAgent-11.0.6.137-the7.x86_64.rpm`), plus `QNA<full-version>.zip`
  (standalone QnA, Windows) and published checksums.
- `bootstrap/release_site.py` scrapes this: resolve a version spec to a
  full version, then map (full version, target platform/arch) to a
  download URL. The index shows per-component versions (Server / Console
  / Relay / Agent, some `N/A` on older patches) — the scraper reads the
  **Agent** column specifically, since that's the version qna ships
  with. Layout changes on the site are a known external dependency — the
  scraper is fixture-tested against captured HTML and fails loudly with
  the URL it tried.

**Artifact selection per target platform:**
| Target | Artifact | Extracted with |
|---|---|---|
| Windows | `QNA<full-version>.zip` (standalone QnA) — **never** the `BESAgent-*.exe` installer, which is InstallShield and not practically extractable | `Expand-Archive` |
| macOS | `BESAgent-<full-version>-BigFix_MacOS*.pkg` | `pkgutil` + `tar` |
| Linux deb-family | `BESAgent-<full-version>-*.deb` | `dpkg-deb` (or `ar` + `tar`) |
| Linux rpm-family | `BESAgent-<full-version>-*.rpm` | `rpm2cpio` + `cpio` |

Only Windows has a standalone QnA artifact; every other platform
extracts qna out of the agent installer package without installing it —
exactly what the existing `bigfix_run_qna_*.sh` scripts do today.

**Version specs** (accepted anywhere a `qna_version` appears — API, CLI,
`remote_clients.toml`):
| Spec | Meaning |
|---|---|
| *(unset)* | Newest patch of the newest stream (top of the release index). |
| `11.0`, `10.0`, `9.5`, `9.2` | Newest patch of that stream (e.g. `11.0` → `11.0.6.137` today). |
| `11.0.6.137` | Exactly that version. |

Resolution happens once per run on the controller and the resolved **full
version** is what flows everywhere downstream: cache paths, target paths,
`ClientRelevanceResult.qna_version`. Two runs with spec `11.0` on
different days may resolve differently — that's the point — so results
always record the resolved version, never the spec. Stream→newest lookups
are cached on disk with a short TTL (default ~1 day, `--refresh-versions`
to force) so repeated runs don't re-scrape; exact-version specs skip
resolution entirely and work offline once the artifact is cached.

**Controller cache** (`bootstrap/cache.py`):
- Location: platform user-cache dir (via `platformdirs`), e.g.
  `~/Library/Caches/bigfix_remote_client_relevance/qna/` on macOS. Layout:
  `qna/<full-version>/<platform-arch>/<artifact>` + a `.sha256` sidecar.
- Download once, verify against the release page's published checksums,
  then reuse forever (artifacts are immutable per full version).
- Concurrent evals dedupe in-process via an asyncio lock keyed by
  artifact, so a 10-host fan-out triggers at most one download per
  (version, platform-arch) pair.

**Push to targets:**
- **SSH:** before eval, check the target for
  `/tmp/bigfix_qna/<full-version>/` (or
  `\Windows\Temp\bigfix_qna\<full-version>\`) with a working `qna`
  binary. If present, skip. If absent, SFTP the cached artifact and run
  the ported *extract* step remotely (unzip / rpm2cpio / tar / pkgutil —
  extraction stays on the target because it needs the target's tooling
  and filesystem). The existing bootstrap modules are therefore split
  into `resolve → fetch (controller) → push → extract (target) → run`
  phases instead of one download-and-run script.
- **Target-side cache persists across runs (SSH):** the extracted
  `bigfix_qna/<full-version>/` tree is deliberately left in place, so a
  given version crosses the wire to a given target **once ever**, not
  once per run — subsequent evals against the same version skip straight
  to `run`. The temp-dir location means an OS cleanup or reboot may
  purge it (Windows temp cleanup, Linux `/tmp` on tmpfs); that's
  acceptable because the presence check just re-pushes from the
  controller cache, which self-heals. No automatic remote cleanup in the
  first cut; a `--clean-target-cache` maintenance flag is a noted
  follow-up.
- **Container:** the wire-cost concern mostly evaporates here — the
  engine is local, so "push" is a local copy. Preferred mechanism: bind
  mount the controller artifact cache **read-only** into the container
  (`-v <cache>/qna:/bigfix_qna_artifacts:ro`) so nothing is copied at
  all and the container can't corrupt the cache; extraction still
  happens inside the container into `/tmp/bigfix_qna/<full-version>/`
  (same paths as SSH, so caching semantics match). `docker cp` is the
  fallback for engines/contexts where bind mounts are awkward (remote
  Docker contexts). One-shot containers re-extract each run (cheap,
  local); `keep_alive=True` containers retain the extracted tree like an
  SSH target does. Images with qna baked in (e.g. `bigfix_centos`) skip
  all of this when the baked version satisfies the spec.
- **Extraction-prereq check (one-time per target):** before the first
  push to a target, verify the tools its extraction path needs are
  actually present, and fail *before* transferring the artifact with a
  message naming the missing tool and the install command
  (`dnf install cpio`, `apt install binutils`, …). Per-platform needs:
  - Windows: PowerShell `Expand-Archive` (built in since PS5 — part of
    why the default shell is switched to PowerShell in setup step 2).
  - macOS: `pkgutil` + `tar` (always present).
  - Linux rpm-family: `rpm2cpio` + `cpio` — **frequently missing** on
    minimal/container images.
  - Linux deb-family: `dpkg-deb` (or `ar` + `tar` as fallback).

  The prereq table covers all families for completeness; the rpm-family
  bootstrap itself is a fast follow after the Debian/Ubuntu first cut
  (see § Implementation milestones). The check result is cached per
  (host, platform) in a small controller-side state file (platformdirs
  *state* dir — distinct from the artifact cache dir, which is safe to
  wipe) so it runs once per target, not once per eval;
  `--recheck-prereqs` forces it (e.g. after installing the missing
  tool). This project only *reports* missing prereqs — it does not
  install packages on targets. Exception: `TransportContainer` may
  install them in a `keep_alive` container on request, since containers
  are disposable; the seed images should just bake them in.
- The presence check is `<dir exists>` + version marker file written
  after successful extraction (not just the binary — a half-extracted
  tree must not count as cached). Pushes are idempotent and safe to race
  from concurrent runs: extract into a temp dir, rename into place.
- Escape hatch: `--fetch-on-target` restores the old
  download-on-the-target behavior for cases where the target has better
  internet than its link to the controller.

**Multi-expression eval on the same target:** the fan-out grid is
`targets x versions x expressions`. `evaluate_many` / `evaluate_many_stream`
take a sequence of expressions; the unit of *work* is a group — one (target,
version) pair — and the unit of *result* is a cell, one expression within a
group. A group builds its transport, probes it and prepares its image **once**,
then runs its expressions through that one transport in sequence. That is the
whole saving: measured on `ubuntu:22.04` at qna 11.0, ten expressions cost 74s
run separately and 18s batched (4.1x). A group that dies before evaluating
anything still emits one result per expression, so the count always matches
`count_work(targets, qna_version, expressions)` — the denominator a streaming
progress indicator commits to before the first result arrives. Container
targets carrying more than one expression are kept alive for the batch, since
otherwise the second expression starts a second container and the batching
saves nothing; the container is stopped when the batch is. `evaluate_client_relevance` is the same machinery with a
one-element expression list, so the two can never drift.

**Multi-version eval on the same target:** `qna_version` fans out —
`orchestrate.py` takes `str | Sequence[str]` and the CLI flag is
repeatable (`--qna-version 11.0 --qna-version 10.0`). Each resolved
version lives in its own `bigfix_qna/<full-version>/` directory on the
target, so versions coexist without conflict, and the run produces one
`ClientRelevanceResult` **per (target × version)** — the `qna_version`
field disambiguates. Individual `Transport` implementations stay
single-version and receive only `ResolvedQna` values; the fan-out
(targets × versions, shared cache, concurrency) is `orchestrate.py`'s
job. This makes "does this client relevance answer the same on 11.0 and
9.5 on this exact box?" a one-liner.

- **Concurrency:** the fan-out is bounded by `max_parallel` (API kwarg +
  `--max-parallel`, default 8) via an asyncio semaphore — a 10-host ×
  2-version run is 20 units of work, not 20 simultaneous SSH sessions.
  `TransportSSH` opens one asyncssh connection per host and multiplexes
  that host's evals over it as sessions, rather than reconnecting per
  (version × eval).
- **Old-version flag compatibility:** the eval command is
  `qna -t -showtypes`, but `-showtypes` support on 9.2/9.5-era qna is
  **unverified** — verify during M2/M3. The design assumption: probe
  once per resolved version (cache the answer next to the
  stream-resolution cache), degrade to plain `qna -t` where unsupported,
  and leave `answer_types` empty for those results rather than failing.

### CLI surface
The entry point is `bigfix-remote-client-relevance` (hyphenated —
matches `[project.scripts]` in `pyproject.toml`; the Python package it
resolves to is `bigfix_remote_client_relevance.cli:main`).

```
bigfix-remote-client-relevance HOST "name of operating system"
bigfix-remote-client-relevance HOST --client-relevance-file probe.rel
bigfix-remote-client-relevance --inventory remote_clients.toml \
    --client-relevance-file probe.rel --json
bigfix-remote-client-relevance HOST --qna-version 11.0.4.60 "..."
bigfix-remote-client-relevance HOST --qna-version 11.0 "..."   # newest 11.0.x patch
bigfix-remote-client-relevance HOST --qna-version 11.0 --qna-version 9.5 "..."
                                       # same query, both versions, same host
bigfix-remote-client-relevance --local "..."
bigfix-remote-client-relevance --container ubuntu:22.04 "..."
bigfix-remote-client-relevance --container bigfix_centos --qna-version 11.0.4.60 -f probe.rel
bigfix-remote-client-relevance --online-evaluator https://developer.bigfix.com "..."
```

- Short alias `-f` is fine for `--client-relevance-file`.
- `--container IMAGE` picks `TransportContainer`; `HOST` selects
  `TransportSSH`; `--local` picks `TransportLocal`; `--online-evaluator URL`
  picks `TransportOnlineEvaluator` (no default URL — see its design note
  above). Exactly one of the four is required per invocation (or
  `--inventory remote_clients.toml` for a mixed grid); `--container` and
  `--online-evaluator` both compose with `--inventory` the same way, adding
  one ad hoc target to the fleet.
- `--qna-version` accepts a version spec (`11.0` or `11.0.6.137`) and is
  repeatable for multi-version fan-out; `--refresh-versions` and
  `--fetch-on-target` are described in § qna binary cache & distribution.
- `--json` emits one `ClientRelevanceResult` per (target × version) —
  same shape the future MCP tool will return — as a single array, once the
  whole fan-out is in.
- `--jsonl` emits those same records one per line, written as each target
  answers rather than at the end. Same schema, different framing; the two
  are mutually exclusive. This is the framing the stdio MCP server wants:
  a long fan-out can relay partial results instead of going silent until
  the slowest endpoint returns. `--diff` cannot stream in either framing,
  since the grouping is only defined once every answer exists.
- Exit codes are actionable for CI gating, mirroring
  `ClientRelevanceResult.error_kind`: `0` — every (target × version)
  completed with no error (an empty answer set from a plural inspector is
  a valid result and does **not** fail the run); `1` — a relevance error (`E:`
  line); `2` — qna or bootstrap failure on a target; `3` —
  transport/connection failure; `4` — version-resolution failure. The
  worst code across the fan-out wins; per-result detail is in the
  `--json` output.

**Auto-discovery search path** (`inventory_paths.py`, used only for the
zero-argument case — no `--local`/`--container`/`--online-evaluator`/
`--inventory`/`HOST` — never for an explicit `--inventory PATH`), current
directory first:

1. `./remote_clients.toml`
2. `~/.bigfix/remote_clients.toml` — per-user, a literal dotfolder in the
   home directory on every OS (`Path.home()`, so it resolves correctly on
   Windows too), matching the `~/.ssh` / `~/.aws` / `~/.docker` convention.
3. The platform's all-users config directory, via
   `platformdirs.site_config_dir("bigfix")` — `/etc/xdg/bigfix` on Linux,
   `/Library/Application Support/bigfix` on macOS, `C:\ProgramData\bigfix`
   on Windows.

`.bigfix` and the all-users `"bigfix"` directory are both named for the
shared, cross-project convention rather than this package's own name
(`bigfix_remote_client_relevance`, used elsewhere for this package's own qna
artifact cache and prereq-check state dirs) — other BigFix tools can store
their own config under the same `.bigfix` folder later. Mirrors the
`qna_paths.py` precedent (`default_candidates()` + `find_*_path()`, first
match wins, `None` if nothing found) rather than inventing a new shape.

Inventory format (`inventory.py`, loaded via `--inventory`):
```toml
# remote_clients.toml
[defaults]
qna_version = "11.0"        # version spec; overridable per host

[hosts.mac-test]            # table name = ~/.ssh/config alias by default
transport = "ssh"
become = true               # sudo for root-only inspectors

[hosts.this-controller]
transport = "local"          # no `become` line needed: implied on a macOS controller

[hosts.win11-lab]
transport = "ssh"
user = "labadmin"

[hosts.ubuntu-22]
transport = "container"
image = "ubuntu:22.04"

[hosts.web-eval]
transport = "online_evaluator"
base_url = "https://developer.bigfix.com"
```

### MCP-ready contract (server itself out of scope)
The server is still out of scope; the affordances a server needs are not. The
project is expected to be consumed by *several* MCP servers, so anything each
of them would otherwise reimplement lives here, and none of it costs a
dependency — it is all stdlib or a rearrangement of code the CLI already had.

- The whole surface a tool body needs is one pure async function —
  the same one the CLI sits on:
  `orchestrate.evaluate_client_relevance(client_relevance, targets?,
  qna_version?) -> list[ClientRelevanceResult]` (one result per
  target × version). `evaluate_client_relevance_stream` is the
  completion-order form, and `count_work` gives the denominator, which is
  the pair a progress notification needs.
- MCP tool name will be `eval_client_relevance` (never
  `eval_relevance`), matching the naming rule.
- `raw_qna_output` + `error` + `qna_version` in the result let an agent
  self-correct on syntax errors and know which OS/version the answer came
  from.
- **Clean stdout is a hard prerequisite** for the stdio flavor of that
  server, which owns stdout for JSON-RPC — see § Output convention. The
  library logs to `logging` and returns data; only `cli.py` writes to
  stdout, so the MCP server can claim the channel without auditing
  anything.

What is *provided*, rather than merely possible:

- **`py.typed`.** Without it every downstream `mypy`/`pyright` saw `Any`, which
  wasted the strict typing this project already maintains internally.
- **`serialize.py` owns the wire shape.** `result_to_dict` /`results_to_dicts`
  replace the `dataclasses.asdict` that used to be inlined in `cli.py`, so the
  CLI's `--json` and a server's `structuredContent` are the same document from
  the same code. It adds a stable key order, the derived `ok` field, and
  `max_raw_output` — a cap on the one unbounded field, which matters when the
  payload is charged in tokens. `ResultPayload` is a `TypedDict`, so indexing a
  payload yields a real type rather than `object`.
- **`RESULT_JSON_SCHEMA`** is a hand-written JSON Schema 2020-12 document
  serving as a tool's `outputSchema`. A unit test asserts its properties are
  exactly the keys `result_to_dict` emits, which are exactly the dataclass
  fields plus `ok` — without that guard the schema rots silently the first time
  a field is added. Evolution is additive-only within a major version, tracked
  by `SCHEMA_VERSION`; the schema deliberately omits
  `additionalProperties: false` so a consumer pinning an older copy keeps
  validating against a newer emitter.
- **`render.py`** holds the plain-text rendering the CLI used to keep private.
  A server wants that text for a tool result's `content` block and should not
  have to import `cli.py` — and therefore `typer` — to get it. A test parses the
  module's imports to keep it that way.
- **`BigFixRelevanceError`** is a single base under all eight exception
  classes. The fan-out never raises for a target failure, but the setup path
  (inventory loading, version resolution, artifact caching) does; one `except`
  now covers it.
- **The CLI is a wire format**, for servers written in something other than
  Python. `--schema` prints `RESULT_JSON_SCHEMA` and exits without needing a
  target; `--jsonl` is one payload object per line in completion order.
  `USAGE_EXIT_CODE` moved from `2` to sysexits `EX_USAGE` (64) because `2` is
  also `EXIT_QNA` — a subprocess consumer could not distinguish bad flags from
  a failed evaluation. Breaking, and taken deliberately at 0.1.x.

Two operational notes for a multi-server deployment:

- **The artifact cache lock is cross-process.** `_locks` in `bootstrap/cache.py`
  dedupes concurrent downloads within one process (an `asyncio.Lock` per cache
  key); `_cross_process_lock` extends that across processes with a
  `filelock.FileLock` sibling to the artifact, so several server processes
  sharing the platformdirs cache pay for one download between them, not one
  each. Neither lock was ever load-bearing for correctness — each download
  stages to a `.part` file, is verified against the published sha256, and
  lands by atomic rename, so no reader ever observes a partial or unverified
  artifact regardless of locking. Recovery from a crashed holder is
  structural rather than a stale-lock heuristic: `filelock` uses the OS's own
  primitive (`flock` / `LockFile`), which the kernel releases the instant a
  holding process exits, so there is no orphaned lock file to detect or break.
  `ensure_artifact`'s `lock_timeout_s` (10 minutes by default) only bounds how
  long a waiter blocks on a *live* holder — a genuinely wedged download, or a
  filesystem where OS-level locking is unreliable.
- **Cancellation propagates.** MCP servers cancel in-flight requests routinely.
  `_one` wraps each stage in a broad `except Exception`, which does not catch
  `CancelledError` (a `BaseException`), so cancelling a fan-out cancels the
  in-flight transport calls and re-raises rather than fabricating results with
  `error_kind="transport"`. Pinned by tests in `tests/unit/test_orchestrate.py`.

## Setup guide (packaged as README)

### 1. `qna` on the target — three provisioning options
1. Already-installed BES client (SSH target):
   - macOS: `/Library/BESAgent/BESAgent.app/Contents/MacOS/QnA`
   - Windows: `C:\Program Files (x86)\BigFix Enterprise\BES Client\QnA.exe`
   - Linux: `/opt/BESClient/bin/qna`
2. Bootstrapped standalone via the ported `bootstrap/` modules — any
   version spec, no BES install, artifact downloaded once on the
   controller and pushed (§ qna binary cache & distribution). Works over
   SSH **and** inside containers.
3. Pre-staged — drop a `qna` binary anywhere and pass `--qna-path`.
   Includes container images that bake qna in at build time (see
   `tools/docker/Dockerfiles/bigfix_centos` and `bigfix_ubuntu`).

Discovery order matches the ported `find_qna_path()` plus `$PATH`.

### 2. Enable SSH server
- **macOS:** System Settings → General → Sharing → *Remote Login*, or
  `sudo systemsetup -setremotelogin on`.
- **Windows:**
  `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`,
  `Start-Service sshd; Set-Service sshd -StartupType Automatic`, and open
  TCP/22. **No shell change is needed** — Windows commands are invoked
  through `powershell.exe` explicitly (see *Windows shell* below), so the
  stock `cmd.exe` default shell works. Switching the default shell to
  PowerShell is still supported and harmless:
  `New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force`.

### 3. Host key verification
`TransportSSH` verifies the target against `~/.ssh/known_hosts`, exactly as
the `ssh` CLI does. A first connection to a new endpoint therefore fails
until its key is trusted — `ssh-keyscan -H <host> >> ~/.ssh/known_hosts`, or
just connect once with `ssh` and accept the key.

`verify_host_key=False` (CLI: `--insecure-skip-host-key-check`) turns this
off for throwaway lab endpoints whose keys are regenerated often. It logs a
warning every time, because it removes the connection's protection against
interception — a real exposure when a client-relevance expression and its
answers cross an untrusted network.

### 4. Key-based auth
- One ed25519 key on the workstation.
- Push with `ssh-copy-id` on Mac; on Windows append to
  `C:\Users\<user>\.ssh\authorized_keys`, or
  `C:\ProgramData\ssh\administrators_authorized_keys` for admin accounts
  (Windows OpenSSH treats admins specially — miss this and admin logins
  silently ignore per-user keys).
- Named entries in `~/.ssh/config` so the CLI can just say
  `bigfix-remote-client-relevance mac-test ...`.

### 5. Permissions & gotchas
- On macOS, `qna` needs root — `TransportSSH(become=True)` uses sudo, and
  `TransportLocal` refuses to run without it by default. Observed against
  BESAgent 11.x on macOS 15: a non-root qna aborts with an uncaught
  `FileIOError` before answering anything, even `TRUE`, so there is no
  partial-answer mode worth preserving. Pass
  `TransportLocal(require_root_on_macos=False)` to attempt it anyway.
- `Target.become` is `bool | None`, not a plain bool: `None` means
  unspecified, and `default_transport_factory` is the single place that
  resolves it — `True` for a `local` target when the controller itself is
  macOS (`sys.platform == "darwin"`, since qna needs root there
  unconditionally), `False` otherwise. An explicit `True`/`False` always
  wins. Both the CLI's `--local`/`--no-become` and an inventory host with
  `transport = "local"` and no `become` line resolve through this one path,
  so a macOS box picks up the default whether reached by a flag or by
  `remote_clients.toml` — the CLI itself does no platform-aware defaulting, it just
  forwards whatever `--become`/`--no-become`/neither produced. SSH always
  coerces `None` to `False` (`bool(target.become)`) with no platform
  awareness, since the remote OS isn't known without a round trip.
  `TransportLocal`'s and `TransportSSH`'s own constructor defaults are
  untouched (`become=False`) — this is resolved one layer up, in
  orchestration, not in the transports themselves.
- `TransportLocal(become=True)` runs qna under
  `sudo -n` and skips the refusal above — the qna process will be root
  whatever this process's euid is, so the pre-flight check would be a false
  negative. `-n` never prompts, so this needs passwordless sudo or a cached
  credential; when it has neither, the `sudo:` line on stderr is reported as
  a `bootstrap` error rather than blamed on qna. Elevation applies on every
  POSIX platform, not just macOS; on Windows it warns and runs unelevated,
  as it does over SSH. One caveat: a timeout kills sudo, and the root-owned
  qna child outlives it, because an unprivileged parent cannot signal it.
  When this process is already root, `sudo` is skipped entirely rather than
  invoked as a no-op — simpler than depending on how `sudo`'s own PAM stack
  treats a root-owned caller, which differs by platform. Once a `become` run
  proves sudo cannot elevate (missing binary, no usable credential), that
  verdict is cached for the life of the `TransportLocal` instance rather than
  retried on every call; there is deliberately no reset method; a fresh
  instance is the way to retry after fixing sudoers, which the CLI already
  does on every invocation.
- **Windows shell.** Every Windows command this package builds is PowerShell
  (`Test-Path`, `New-Item`, `Expand-Archive`, the `&` call operator), but
  Windows OpenSSH hands commands to `cmd.exe` unless the `DefaultShell`
  registry value says otherwise. Rather than requiring that edit, the SSH
  runner wraps Windows commands as
  `powershell -NoProfile -NonInteractive -EncodedCommand <base64 UTF-16LE>`.
  `-EncodedCommand` rather than `-Command` because the commands embed single
  quotes and paths with spaces *and* parentheses (`C:/Program Files (x86)/…`),
  and both `(` and `&` are cmd metacharacters — unencoded, the discovery probe
  fails with `'C:/Program was unexpected at this time.` The wrapped source
  also silences `$ProgressPreference`, without which PowerShell writes CLIXML
  progress records to stderr, where the qna outcome classifier reads. SFTP
  (the artifact push) is shell-independent and is not wrapped. Verified
  against a real Server 2022 host whose login shell is `cmd.exe`.
- Save client-relevance files as UTF-8, no BOM.
- **Client relevance ≠ session relevance.** This tool is only for the
  client dialect; session relevance stays with the BigFix REST/`besapi`
  path.
- `qna -showtypes` emits `I:` result-type lines and `-t` emits a `T:`
  timing line. The parser keeps types in `answer_types` and the timing in
  `qna_time`, so agents can distinguish string / version / plural answers.

## Out of scope (for this task)
- The MCP server itself (design keeps the surface compatible).
- Full `TransportFastQuery` implementation (stub only).
- Windows containers, and publishing our own qna-preloaded images to a
  registry (noted for `TransportContainer` follow-ups). podman is supported
  via `PodmanEngine`/`--engine podman`; niche rootless-podman configurations
  beyond the default per-user socket are still unverified.
- AIX / Solaris / ppc64le / s390x bootstraps (start with macOS + Windows +
  Debian/Ubuntu; the rest are mechanical follow-ups against the existing
  shell scripts).
- Shared-team inventory: jump hosts, cert-based SSH, secrets management.

## Implementation milestones
Each milestone is independently testable and shippable; land them in
order.

1. **M1 — parser + local eval.** `results.py` (`parse_qna_output`,
   `ClientRelevanceResult`), `qna_paths.py`, `TransportLocal`. Pure port
   of `EvaluateRelevance`, fixture-tested, no network required.
2. **M2 — resolve + cache.** `bootstrap/release_site.py` (version-spec
   resolution, Agent-column scrape, fixture-tested against captured
   HTML) and `bootstrap/cache.py` (download, checksum verify,
   concurrent-download dedupe).
3. **M3 — SSH.** `TransportSSH` + push/extract phases + extraction-prereq
   check for macOS, Windows, Debian/Ubuntu.
4. **M4 — Container.** `ContainerEngine` abstraction +
   `TransportContainer` against the seed image catalog.
5. **M5 — orchestration + CLI.** `orchestrate.py` fan-out
   (targets × versions, `max_parallel`, exit-code aggregation),
   `inventory.py`, `cli.py`, `--json` output.

Follow-ups after M5: rpm-family + SUSE bootstraps, remaining arches,
Fast Query, MCP server (separate task). Fine-grained work items live in
GitHub issues, not in this doc.

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
