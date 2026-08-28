"""Provisioning qna on a target without installing a BES client.

The controller owns the network side of this: it resolves a version spec to a
full version, downloads the matching artifact once, and pushes it to targets.
Targets never fetch from the internet themselves, so a fan-out across ten hosts
costs one download and works against isolated lab endpoints.

Phases, split out of the single download-and-run shell scripts this ports:

    resolve -> fetch (controller) -> push -> extract (target) -> run

Extraction stays on the target because it needs that target's tooling.
"""

from __future__ import annotations

__all__: list[str] = []
