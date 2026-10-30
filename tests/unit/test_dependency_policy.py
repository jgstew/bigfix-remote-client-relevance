"""The supply-chain cooldown and its one exemption, pinned.

Releases wait 7 days before uv or Dependabot takes them (``exclude-newer``,
``cooldown``), except the maintainer's own bigfix* libraries
(github.com/jgstew/bigfix*), which this repo adopts on release day. A fixed
date in ``exclude-newer-package`` goes stale and silently holds a package
back, so the exemption is ``"0 days"``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
DEPENDABOT = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")


def _own_libraries() -> list[str]:
    names = [
        re.split(r"[<>=!~;\[ ]", dep, maxsplit=1)[0] for dep in PYPROJECT["project"]["dependencies"]
    ]
    return [name for name in names if name.startswith("bigfix-")]


def _ecosystem_block(ecosystem: str) -> str:
    blocks = re.split(r"\n  - package-ecosystem: ", DEPENDABOT)
    [block] = [b for b in blocks if b.startswith(f'"{ecosystem}"')]
    return block


def test_there_is_an_own_library_to_exempt():
    assert "bigfix-remote-session-relevance" in _own_libraries()


def test_everything_else_keeps_the_seven_day_window():
    assert PYPROJECT["tool"]["uv"]["exclude-newer"] == "-P7D"
    assert "default-days: 7" in _ecosystem_block("uv")


def test_uv_takes_own_libraries_on_release_day():
    overrides = PYPROJECT["tool"]["uv"]["exclude-newer-package"]

    assert overrides == dict.fromkeys(_own_libraries(), "0 days")


def test_dependabot_exempts_own_libraries_from_its_cooldown():
    block = _ecosystem_block("uv")
    match = re.search(r"\n      exclude:\n((?:        - .+\n)+)", block)
    assert match, block
    excluded = re.findall(r'- "?([^"\n]+)"?', match.group(1))

    # Named one by one, not "bigfix-*": a wildcard would also exempt any
    # stranger's package that happens to start with bigfix-.
    assert sorted(excluded) == sorted(_own_libraries())
