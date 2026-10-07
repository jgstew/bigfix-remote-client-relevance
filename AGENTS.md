# AGENTS.md

Guidance for AI coding agents working in this repo. Human-facing docs are
[README.md](README.md) (usage) and [DESIGN.md](DESIGN.md) (the specification,
including § Testing).

## What this is

A Python (>=3.11) CLI and library that evaluates BigFix client relevance with
`qna` on remote endpoints (SSH), inside containers (Docker), and locally,
without a full BES client install. Entry point: `bigfix_remote_client_relevance.cli:main`.

- `src/bigfix_remote_client_relevance/` -- `cli.py`, `orchestrate.py`,
  `inventory.py`, `discovery.py` / `ssh_discovery.py`, `render.py`, `results.py`,
  `serialize.py` (the JSON payload and its schema)
- `bootstrap/` -- getting qna onto a target: `release_site.py` (version specs,
  artifact selection, stepping back a release), `cache.py`, `targets.py`,
  `provision.py`, `extract_local.py`, `compat_memory.py` (which builds are too
  new for which target, remembered across runs)
- `transports/` -- one module per target kind: `ssh.py`, `container.py`
  (+ `container_libs.py`, `container_setup.py`), `local.py`, `online_evaluator.py`
- `tests/unit/` -- offline; `tests/integration/` -- needs real prerequisites

## Commands

```bash
uv sync                                  # install, dev group included
uv run pytest                            # runs with -n auto (xdist)
uv run pytest -n0 path/to/test.py -k name  # serial, for a focused run
uv run pyright src tests
uv run ruff check src tests
```

Pre-commit runs on commit (ruff, ruff format, mypy, bandit, typos, codespell,
uv-lock, and more); keep it green rather than skipping hooks.

Unmarked tests run offline on a bare machine. Tests marked `live_qna`,
`docker`, `ssh_localhost`, `ssh_windows` need a real prerequisite and
auto-skip without it (see `tests/conftest.py`).

## How to work here

- **Test first.** Write the failing test, run it and confirm it fails for
  the right reason, then implement until green. A new test that passes
  before the fix isn't testing the fix.
- **Don't create or switch git branches** without asking; the maintainer
  manages git state.
- Match the surrounding code: comments explain *why* (often a real failure
  that forced the design), names are descriptive, and test fakes implement
  only the slice of a third-party API the code touches.
- Third-party calls that a fake doesn't implement will fail in tests --
  extend the fake (e.g. `FakeDockerClient` in
  `tests/unit/test_transport_container.py`) rather than special-casing code.

- **The result payload is a public contract.** A new `ClientRelevanceResult`
  field also needs `serialize.py` updated (`ResultPayload`, `_KEY_ORDER`, the
  schema property), `SCHEMA_VERSION` bumped (additive change = minor bump),
  and `test_result_has_every_designed_field` in `tests/unit/test_results.py`.
- **Log levels.** The CLI defaults to INFO on purpose, so slow first-time
  work (downloads, image pulls, image prep) shows progress. Anything that
  would repeat on every run -- emulation notices, a remembered fallback --
  belongs at DEBUG (`-v`), or it buries the answers.
- **Tests never touch real user state.** Anything persisted under
  `platformdirs` must be redirected in `tests/conftest.py`, as
  `_isolated_compat_memory` does.
- **The release-site fixtures lag the real site.**
  `tests/fixtures/release_site/release_index.html` predates 11.0.7, so code
  walking the index must cope with a version it does not list.
- **deb and rpm never cross.** A deb-family platform only ever gets a `.deb`
  and an rpm-family one only an `.rpm`, fallbacks included; tests in
  `tests/unit/test_release_site.py` enforce it. Don't relax a pattern to fix
  one image.
- **Check user-visible changes with a real run** when Docker is available,
  since unit tests run on fakes, e.g.
  `uv run bigfix-remote-client-relevance --container debian:11 --arch arm64 --qna-version 11.0.7.61 "name of operating system"`.

## Releasing

The version lives only in `pyproject.toml` (`[project] version`); after
changing it, run `uv lock` so `uv.lock` matches.

Pushing a new version to `main` triggers
`.github/workflows/tag_and_release.yaml`, which tags, builds, writes
`SHA256SUMS.txt`, and creates the GitHub Release (body: Full Changelog link
plus SHA-256 block). Release notes are added above that CI-generated body,
with a `## Fixed` / `## Added` style section; keep the CI part intact.
