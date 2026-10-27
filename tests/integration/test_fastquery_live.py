"""Live Fast Query round trip against a real BigFix server.

Opt-in: set ``BFRCR_FASTQUERY_CONNECTION`` to a REST or SaaS connection name in the
bigfix-remote-session-relevance config and ``BFRCR_FASTQUERY_COMPUTER_ID`` to
a computer that operator can see. The endpoint must be online and reachable
by UDP notification (or a persistent connection), on 9.5 Patch 2 or later.
"""

from __future__ import annotations

import os

import pytest

from bigfix_remote_client_relevance.orchestrate import Target, evaluate_client_relevance

pytestmark = pytest.mark.fastquery_live


async def test_computer_name_comes_back_as_the_computers_name():
    target = Target(
        kind="fastquery",
        name=os.environ["BFRCR_FASTQUERY_CONNECTION"],
        connection=os.environ["BFRCR_FASTQUERY_CONNECTION"],
        computer_ids=(int(os.environ["BFRCR_FASTQUERY_COMPUTER_ID"]),),
    )

    [result] = await evaluate_client_relevance("computer name", [target], timeout_s=60)

    assert result.error_kind is None, result.error
    assert result.transport == "fastquery"
    assert result.answers == [result.host]
