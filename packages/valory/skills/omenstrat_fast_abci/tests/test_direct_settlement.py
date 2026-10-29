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

"""Check that direct bets settle without attributing a Mech tool."""

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from packages.valory.skills.abstract_round_abci.base import AbciAppDB
from packages.valory.skills.decision_maker_abci.states.base import SynchronizedData
from packages.valory.skills.fresh_watch_abci.rounds import (
    Event,
    FreshWatchRound,
    SelectionPayload,
)


@pytest.mark.parametrize("previous_tool", [False, True])
@pytest.mark.parametrize("deferred", [None, ["deferred"], '["deferred"]'])
def test_direct_bet_settlement_has_no_tool_attribution(
    previous_tool: bool, deferred: Any
) -> None:
    """Settle a direct bet and reach trade counting without a tool credit."""
    from packages.valory.skills.decision_maker_abci.states.bet_placement import (
        BetPlacementRound,
    )
    from packages.valory.skills.decision_maker_abci.states.trade_count import (
        TradeCountRound,
    )
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        OmenstratFastAbciApp,
    )
    from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
        Event as SettlementEvent,
    )
    from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
        PostTxSettlementRound,
    )

    setup: Dict[str, List[Any]] = dict(
        all_participants=[("agent",)],
        participants=[("agent",)],
        consensus_threshold=[1],
        utilized_tools=[json.dumps({"previous_tx": "previous_tool"})],
        mech_only_queue=[deferred],
        is_checkpoint_reached=[previous_tool],
    )
    if previous_tool:
        setup.update(
            mech_tool=["previous_tool"],
            selected_tool_key=["previous_identity:previous_tool"],
            mech_identities=[json.dumps({"0xprevious": "previous_identity"})],
            mech_responses=[
                json.dumps([dict(nonce="old", result="old", mech_address="0xprevious")])
            ],
        )
    data = SynchronizedData(AbciAppDB(setup_data=setup))
    if not previous_tool:
        assert data.mech_tool_key is None
    round_ = FreshWatchRound(data, MagicMock())
    round_.process_payload(
        SelectionPayload(
            "agent",
            json.dumps(
                dict(
                    lane="lightning_decision",
                    sampled_bet_index=0,
                    lightning_probability=0.95,
                    lightning_rule="still_hold_phrase",
                )
            ),
        )
    )
    outcome = round_.end_block()
    assert outcome is not None
    result, event = outcome
    data = SynchronizedData(result.db)
    assert event == Event.LIGHTNING
    data = SynchronizedData(
        data.update(
            tx_submitter=BetPlacementRound.auto_round_id(),
            final_tx_hash="direct_tx",
        ).db
    )
    settled = PostTxSettlementRound(data, MagicMock()).end_block()
    assert settled is not None
    result, event = settled
    data = SynchronizedData(result.db)
    assert event == SettlementEvent.BET_PLACEMENT_DONE
    next_round = OmenstratFastAbciApp.transition_function[PostTxSettlementRound][event]
    assert next_round is TradeCountRound
    assert isinstance(next_round(data, MagicMock()), TradeCountRound)
    assert data.utilized_tools == {"previous_tx": "previous_tool"}
    assert data.db.get("mech_tool") is None
    assert data.db.get("selected_tool_key") is None
    assert data.mech_tool_key is None
    assert data.delivering_mech is None
    assert data.mech_only_queue == ([] if deferred is None else ["deferred"])
    assert data.is_checkpoint_reached is previous_tool
    copied_queue = data.mech_only_queue
    copied_queue.append("later")
    assert data.mech_only_queue == ([] if deferred is None else ["deferred"])
