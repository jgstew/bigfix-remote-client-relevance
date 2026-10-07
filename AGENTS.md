# AGENTS.md

Guidance for AI coding agents working in this repo. Human-facing docs are
[README.md](README.md) (usage) and [DESIGN.md](DESIGN.md) (the specification,
including § Testing).

## What this is

A Python (>=3.11) CLI and library that evaluates BigFix client relevance with
`qna` on remote endpoints (SSH), inside containers (Docker), and locally,
without a full BES client install. Entry point: `bigfix_remote_client_relevance.cli:main`.

- `src/bigfix_remote_client_relevance/` -- `cli.py`, `orchestrate.py`,
  `inventory.py`, `discovery.py` / `ssh_discovery.py`, `render.py`, `results.py`
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

## Releasing

The version lives only in `pyproject.toml` (`[project] version`); after
changing it, run `uv lock` so `uv.lock` matches.

Pushing a new version to `main` triggers
`.github/workflows/tag_and_release.yaml`, which tags, builds, writes
`SHA256SUMS.txt`, and creates the GitHub Release (body: Full Changelog link
plus SHA-256 block). Release notes are added above that CI-generated body,
with a `## Fixed` / `## Added` style section; keep the CI part intact.
