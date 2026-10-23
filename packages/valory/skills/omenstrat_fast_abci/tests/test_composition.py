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

"""Check fast and housekeeping paths in the composed FSM."""

import pytest

from packages.valory.skills.decision_maker_abci.states.base import Event as D
from packages.valory.skills.decision_maker_abci.states.bet_placement import (
    BetPlacementRound,
)
from packages.valory.skills.decision_maker_abci.states.decision_request import (
    DecisionRequestRound,
)
from packages.valory.skills.decision_maker_abci.states.post_bet_update import (
    PostBetUpdateRound,
)
from packages.valory.skills.decision_maker_abci.states.redeem import RedeemRound
from packages.valory.skills.fresh_watch_abci.rounds import (
    Event,
    FastDecisionRound,
    FreshWatchRound,
)
from packages.valory.skills.mech_interact_abci.states.base import Event as M
from packages.valory.skills.mech_interact_abci.states.request import MechRequestRound
from packages.valory.skills.mech_interact_abci.states.response import MechResponseRound
from packages.valory.skills.omenstrat_fast_abci.composition import (
    FastPreTxSettlementRound,
    OmenstratFastAbciApp,
)
from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import Event as T
from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
    PostTxSettlementRound,
)


def test_fast_route() -> None:
    """A request and bet return to the watch without housekeeping."""
    transitions = OmenstratFastAbciApp.transition_function
    assert transitions[FreshWatchRound][Event.MARKET] == DecisionRequestRound
    assert transitions[FreshWatchRound][Event.HOUSEKEEPING] == RedeemRound
    assert transitions[DecisionRequestRound][D.DONE] == MechRequestRound
    assert transitions[MechRequestRound][M.OFFCHAIN_DONE] == MechResponseRound
    assert transitions[MechRequestRound][M.OFFCHAIN_ALL_FAILED] == FastDecisionRound
    assert transitions[MechResponseRound][M.DONE] == FastDecisionRound
    assert transitions[FastDecisionRound][Event.BET] == BetPlacementRound
    assert transitions[FastDecisionRound][Event.SKIP] == FreshWatchRound
    assert transitions[BetPlacementRound][D.DONE] == FastPreTxSettlementRound
    assert (
        transitions[PostTxSettlementRound][T.BET_PLACEMENT_DONE] == PostBetUpdateRound
    )
    assert transitions[PostBetUpdateRound][D.DONE] == FreshWatchRound
    names = {round_.__name__ for round_ in transitions}
    assert not names & {
        "ResetAndPauseRound",
        "TradeCapRound",
        "CheckStopTradingRound",
        "MechVersionDetectionRound",
        "UpdateBetsRound",
        "CheckBenchmarkingModeRound",
    }


@pytest.mark.parametrize("vote", [True, False])
def test_settlement_drops_previous_transaction_state(vote: bool) -> None:
    """A new transaction must not inherit the previous transaction's retries."""
    from unittest.mock import MagicMock

    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        FastPreTxSettlementRound,
    )
    from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
        SynchronizedData,
        VotingPayload,
    )

    data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                tx_hashes_history=["old"],
                blacklisted_keepers=["agent"],
                final_tx_hash=["old"],
                tx_submitter=["bet_placement_round"],
                most_voted_tx_hash=["new"],
            )
        )
    )
    round_ = FastPreTxSettlementRound(data, MagicMock())
    assert round_.end_block() is None
    round_.process_payload(VotingPayload("agent", vote))
    outcome = round_.end_block()
    assert outcome is not None
    result, event = outcome
    if not vote:
        assert event == T.REFILL_REQUIRED
        assert result.db.get("tx_hashes_history") == "old"
        return
    assert event == T.CHECKS_PASSED
    assert result.db.get("tx_hashes_history") == ""
    assert result.db.get("blacklisted_keepers") == ""
    assert result.db.get("final_tx_hash") is None
    assert result.db.get("most_voted_tx_hash") == "new"
    assert result.db.get("tx_submitter") == "bet_placement_round"
