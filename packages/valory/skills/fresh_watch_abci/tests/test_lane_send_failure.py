# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2026 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Keep failed sends from consuming the lane budget."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from aea.helpers.transaction.base import SignedTransaction

from packages.open_aea.protocols.signing import SigningMessage
from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import run
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MARKET,
    MODULE,
    SAFE,
    TOKEN,
    lane,
    prepared,
    reply_on_send,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lane_mempool import pending


@pytest.mark.parametrize(
    "error_type", [TimeoutException, ValueError, KeyError, TypeError]
)
@pytest.mark.parametrize(
    "phase,mempool",
    [
        ("signing", False),
        ("signing", True),
        ("broadcast", False),
        ("broadcast", True),
        ("cancelled", True),
    ],
)
@pytest.mark.parametrize("already_marked", [False, True])
def test_send_failure_restores_budget_and_lock(
    tmp_path: Any,
    error_type: Any,
    phase: str,
    mempool: bool,
    already_marked: bool,
) -> None:
    """Restore prior state and log only the error class after a send fails."""
    b = lane(tmp_path, lightning_mempool_delay_ms=0)
    prepared(b)
    b._shots = 2
    sent_markets = {TOKEN, MARKET} if already_marked else {TOKEN}
    b.context.shared_state["lightning_mempool_sent_markets"] = set(sent_markets)
    candidate = pending()
    error = error_type("https://rpc.test/private-key sensitive details")
    response = SimpleNamespace(
        performative=SigningMessage.Performative.SIGNED_TRANSACTION,
        signed_transaction=SignedTransaction(
            "ethereum", {"raw_transaction": "0xabcdef"}
        ),
    )
    reply_on_send(b, response, signing=True)
    b._broadcast = MagicMock()
    if phase == "signing":
        b.context.decision_maker_message_queue.put_nowait.side_effect = error
    elif phase == "broadcast":
        b._broadcast.side_effect = error
    else:
        b._pending_cancelled = MagicMock(return_value=True)
        persist = b._persist
        writes = 0

        def persist_or_fail() -> None:
            nonlocal writes
            writes += 1
            if writes == 3:
                raise error
            persist()

        b._persist = persist_or_fail
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", return_value=1002),
    ):
        sd.return_value.safe_contract_address = SAFE
        assert (
            run(
                b._send(
                    candidate.market,
                    SimpleNamespace(stake=4.0, price=0.5, p_hat=0.95),
                    (0.9, 4444444444444444445),
                    {"baseFeePerGas": "0x1", "number": "0x2", "gasLimit": "0x1000000"},
                    1002,
                    pending=candidate if mempool else None,
                )
            )
            is False
        )
    assert b._shots == 2
    assert b.context.shared_state["lightning_mempool_sent_markets"] == sent_markets
    assert "lightning_lane_inflight" not in b.context.shared_state
    saved = json.loads((tmp_path / "lightning_lane_state.json").read_text())
    assert saved["shots"] == 2
    assert saved["mempool_sent"] == sorted(sent_markets)
    assert saved["inflight"] is None
    failure_lines = [
        call.args[0] % call.args[1:]
        for call in b.context.logger.info.call_args_list
        if "reason=send_failed" in call.args[0]
    ]
    assert failure_lines == [
        f"[LIGHTNING_LANE] market={MARKET} reason=send_failed error={error_type.__name__}"
    ]
    assert b._broadcast.call_count == (phase == "broadcast")
